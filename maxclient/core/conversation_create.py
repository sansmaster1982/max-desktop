"""Explicit group/channel creation, with durable owner-bound no-retry journal."""
from __future__ import annotations

import copy
from dataclasses import dataclass
import threading
import time

from ..protocol.client import AuthenticatedSession, MaxError, MaxWriteNotStarted, MaxWriteOutcomeUnknown
from ..protocol.folder_contracts import int64
from ..protocol.conversation_create_contracts import (
    CONVERSATION_CREATE, CONVERSATION_UPDATE, conversation_create_values,
    conversation_creation_limits, conversation_create_payload, conversation_create_reply,
    conversation_create_readback, conversation_description_reply,
)
from .models import Chat

UNKNOWN = ('Результат создания не подтверждён. Повторное создание заблокировано. '
           'Проверьте результат без повторной отправки.')
DESCRIPTION_UNKNOWN = 'Чат создан. Сохранение описания не подтверждено; повторного создания не будет.'


@dataclass(frozen=True)
class ConversationCreateResult:
    chat: Chat
    description_confirmed: bool = True
    description_error: str = ''


class ConversationCreateServiceMixin:
    def _conversation_creation_lock(self):
        with self._metadata_lock:
            if not hasattr(self, '_conversation_create_lock'):
                self._conversation_create_lock = threading.Lock()
            return self._conversation_create_lock

    def _conversation_creation_binding(self, context):
        if not isinstance(context, AuthenticatedSession):
            raise ValueError('Откройте создание в подтверждённой сессии')
        return self._contact_lookup_binding(context)

    def conversation_creation_limits(self, *, context):
        binding = self._conversation_creation_binding(context)
        return self._history_cache_write(binding, lambda: conversation_creation_limits(
            self.store.load_config(binding[0]).get('server', {})))

    def _conversation_creation_load(self, binding):
        pending = self._history_cache_write(binding, lambda: self.store.load_conversation_creation(binding[0]))
        if pending is not None:
            if (type(pending.get('version')) is not int or pending['version'] != 1
                    or pending.get('kind') not in ('group', 'channel')
                    or pending.get('stage') not in ('create', 'description')
                    or not int64(pending.get('cid')) or pending['cid'] <= 0
                    or not isinstance(pending.get('values'), dict)
                    or pending['values'].get('kind') != pending['kind']
                    or ('chat_id' in pending and (not int64(pending['chat_id']) or pending['chat_id'] == 0))):
                raise ValueError('Журнал создания повреждён; повторная отправка заблокирована')
        return pending

    def _conversation_creation_save(self, binding, pending):
        self._history_cache_write(binding, lambda: self.store.save_conversation_creation(binding[0], pending))

    def _conversation_creation_clear(self, binding):
        self._history_cache_write(binding, lambda: self.store.clear_conversation_creation(binding[0]))

    def pending_conversation_creation(self, *, context):
        binding = self._conversation_creation_binding(context)
        pending = self._conversation_creation_load(binding)
        if pending is None:
            return None
        return {'kind': pending['kind'], 'stage': pending['stage'],
                'has_chat_id': 'chat_id' in pending, 'chat_id': pending.get('chat_id')}

    def _conversation_creation_observe(self, binding, pending, *, description=False):
        if 'chat_id' not in pending:
            raise MaxWriteOutcomeUnknown(UNKNOWN)
        client = self._require_history_context(binding)
        data = client.chat_info([pending['chat_id']], expected_session=binding[2])
        self._require_history_context(binding)
        raw = conversation_create_readback(data, pending['values'], binding[0], pending['chat_id'],
                                           description=description)
        def remember():
            self._remember_chat_info({'chats': [raw]}, my_id=binding[0])
            chat = self.store.get_chat(raw['id'])
            if chat is None:
                raise ValueError('MAX не подтвердил созданный чат')
            return copy.deepcopy(chat)
        return self._history_cache_write(binding, remember)

    @staticmethod
    def _conversation_creation_frame(frame, opcode):
        if frame.opcode != opcode or frame.cmd not in (1, 3):
            raise ValueError('Неверный ответ MAX')

    def create_conversation(self, kind, title, member_ids=(), description='', *, context):
        """One explicit64, actual-ID48, then optional channel55 and48.

        context must be captured by the UI before queueing. The durable reservation
        precedes every write; an unknown result never causes an automatic resend.
        """
        binding = self._conversation_creation_binding(context)
        values = conversation_create_values(kind, title, member_ids, description,
                                            limits=self.conversation_creation_limits(context=context))
        lock = self._conversation_creation_lock()
        if not lock.acquire(blocking=False):
            raise ValueError('Создание уже выполняется')
        try:
            if self._conversation_creation_load(binding) is not None:
                raise MaxWriteOutcomeUnknown(UNKNOWN)
            if values['member_ids']:
                known = self._history_cache_write(binding, lambda: {row.id for row in self.store.list_contacts()})
                if binding[0] in values['member_ids'] or not set(values['member_ids']).issubset(known):
                    raise ValueError('Выберите участников из контактов этого аккаунта')
            client = self._require_history_context(binding)
            with client._message_cid_lock:
                cid = max(time.time_ns() // 1_000_000, client._last_message_cid + 1)
                client._last_message_cid = cid
            pending = {'version': 1, 'kind': kind, 'stage': 'create', 'values': values, 'cid': cid}
            self._history_cache_write(binding, lambda: self.store.reserve_conversation_creation(binding[0], pending))
            try:
                frame = client._write_frame(CONVERSATION_CREATE, conversation_create_payload(values, cid),
                                            expected_session=context)
            except MaxWriteNotStarted:
                self._conversation_creation_clear(binding)
                raise
            except Exception:
                raise MaxWriteOutcomeUnknown(UNKNOWN) from None
            try:
                self._require_history_context(binding)
                self._conversation_creation_frame(frame, CONVERSATION_CREATE)
            except Exception:
                raise MaxWriteOutcomeUnknown(UNKNOWN) from None
            if not frame.ok:
                self._conversation_creation_clear(binding)
                raise MaxError('MAX отклонил создание. Проверьте название и выбранных участников.')
            try:
                record = conversation_create_reply(frame.decoded, values, binding[0], cid)
                pending['chat_id'] = record['id']
                self._conversation_creation_save(binding, pending)
                chat = self._conversation_creation_observe(binding, pending)
            except Exception:
                raise MaxWriteOutcomeUnknown(UNKNOWN) from None
            if not values['description']:
                self._conversation_creation_clear(binding)
                return ConversationCreateResult(chat)
            pending['stage'] = 'description'
            self._conversation_creation_save(binding, pending)
            # The actual creation is complete. A55 failure cannot turn this into
            # another64 request, even after restarting or switching accounts.
            try:
                client = self._require_history_context(binding)
                frame = client._write_frame(CONVERSATION_UPDATE,
                    {'chatId': pending['chat_id'], 'description': values['description']}, expected_session=context)
                self._require_history_context(binding)
                self._conversation_creation_frame(frame, CONVERSATION_UPDATE)
                if not frame.ok:
                    self._conversation_creation_clear(binding)
                    return ConversationCreateResult(chat, False, 'Канал создан, но MAX отклонил описание.')
                conversation_description_reply(frame.decoded, values, binding[0], pending['chat_id'])
                chat = self._conversation_creation_observe(binding, pending, description=True)
                self._conversation_creation_clear(binding)
                return ConversationCreateResult(chat)
            except MaxWriteNotStarted:
                self._conversation_creation_clear(binding)
                return ConversationCreateResult(chat, False, 'Канал создан; описание не было отправлено.')
            except Exception:
                self._require_history_context(binding)
                return ConversationCreateResult(chat, False, DESCRIPTION_UNKNOWN)
        finally:
            lock.release()

    def verify_conversation_creation(self, *, context):
        """Read48 by the recorded ACK ID. Never search titles, send64, or retry55."""
        binding = self._conversation_creation_binding(context)
        lock = self._conversation_creation_lock()
        if not lock.acquire(blocking=False):
            raise ValueError('Создание или проверка уже выполняется')
        try:
            pending = self._conversation_creation_load(binding)
            if pending is None:
                raise ValueError('Нет неподтверждённого создания')
            try:
                chat = self._conversation_creation_observe(binding, pending)
            except Exception:
                raise MaxWriteOutcomeUnknown(UNKNOWN) from None
            if pending['values']['description']:
                # A crash before55 or an unknown55 stays pending without resend.
                pending['stage'] = 'description'
                self._conversation_creation_save(binding, pending)
                raw = self._history_cache_write(binding, lambda: self._server_chats[chat.id])
                if raw.get('description', '') != pending['values']['description']:
                    return ConversationCreateResult(chat, False, DESCRIPTION_UNKNOWN)
            self._conversation_creation_clear(binding)
            return ConversationCreateResult(chat)
        finally:
            lock.release()
