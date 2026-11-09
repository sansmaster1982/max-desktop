"""Explicit row-menu read/unread actions over a captured server frontier.

No message fetch or account action occurs when a menu is opened. Unknown writes
stay reserved until an explicit authoritative readback confirms the intent.
"""
from __future__ import annotations

from dataclasses import dataclass
from contextlib import contextmanager
import math

from .conversation_info_service import ConversationContext
from .folder_view import _properties
from ..protocol.folder_contracts import int64
from ..protocol.client import MaxError, MaxWriteNotStarted, MaxWriteOutcomeUnknown, MaxTimeout, MaxNotConnected
from ..protocol.conversation_actions_contracts import parse_action_chat_readback


def _time(value):
    try:
        return (int64(value) if type(value) is int
                else type(value) is float and math.isfinite(value) and -2**63 <= value < 2**63)
    except (ValueError, OverflowError):
        return False


@dataclass(frozen=True, eq=False)
class ConversationReadContext:
    conversation: ConversationContext
    message_id: int
    message_time: int | float
    receipt_version: int


class ConversationReadServiceMixin:
    def _read_records(self):
        # Call only with the existing metadata lock held (also for __new__ test fixtures).
        if not hasattr(self, '_chat_read_inflight'):
            self._chat_read_inflight = {}
            self._chat_read_pending = {}
        return self._chat_read_inflight, self._chat_read_pending

    def _receipt_version(self, owner, chat_id):
        return getattr(self, '_read_receipt_versions', {}).get((owner, chat_id), 0)

    @contextmanager
    def _reserve_chat_read(self, binding, chat_id, *, pending_record=None):
        """Serialize explicit and viewport receipts, without locking across RPC."""
        key, ticket = (binding[0], chat_id), object()
        with self._metadata_lock:
            self._require_history_context(binding)
            active, pending = self._read_records()
            if key in active or key in pending and pending[key] is not pending_record:
                raise ValueError('Предыдущая отметка прочтения ещё не подтверждена.')
            if pending_record is not None and pending.get(key) is not pending_record:
                raise ValueError('Состояние отметки прочтения изменилось.')
            active[key] = ticket
        try:
            yield
        finally:
            with self._metadata_lock:
                active, _ = self._read_records()
                if active.get(key) is ticket:
                    del active[key]

    def capture_conversation_read(self, context):
        with self._metadata_lock:
            cached = self._require_conversation(context)
            last = cached[2].get('lastMessage')
            if (not isinstance(last, dict) or not int64(last.get('id'))
                    or not _time(last.get('time'))):
                raise ValueError('Нет подтверждённого последнего сообщения.')
            return ConversationReadContext(context, last['id'], last['time'],
                self._receipt_version(context.owner_id, context.chat_id))

    def _read_context_cache(self, context):
        if not isinstance(context, ConversationReadContext):
            raise ValueError('Заново откройте меню чата.')
        if not int64(context.message_id) or not _time(context.message_time):
            raise ValueError('Нет подтверждённого сообщения.')
        cached = self._require_conversation(context.conversation)
        if cached[1] is None:
            raise ValueError('Чат больше недоступен. Заново откройте меню.')
        return cached

    def _read_state(self, context, cached):
        binding, chat, raw = cached[:3]
        flags, _read, _muted = _properties(chat, raw, {}, binding[0], None, None, {})
        active, pending = self._read_records()
        key = (binding[0], context.conversation.chat_id)
        last = raw.get('lastMessage')
        anchored = (isinstance(last, dict) and type(last.get('id')) is int
                    and last['id'] == context.message_id and _time(last.get('time'))
                    and last['time'] == context.message_time)
        supported = (raw.get('status') in ('ACTIVE', 'LEFT', 'REMOVED', 'CLOSED', 'HIDDEN')
                     and raw.get('type') in ('DIALOG', 'CHAT', 'CHANNEL'))
        return {'context': context, 'unread': flags[0], 'pending': key in pending,
                'inflight': key in active,
                'available': supported and anchored and flags[0] is not None
                    and key not in active and key not in pending
                    and context.receipt_version == self._receipt_version(*key)
                    and callable(getattr(binding[1], 'mark_unread', None))
                    and callable(getattr(binding[1], 'mark_read', None))}

    def get_conversation_read_state(self, context):
        with self._metadata_lock:
            return self._read_state(context, self._read_context_cache(context))

    def _read_baseline(self, cached):
        binding, chat, raw = cached[:3]
        participants = raw.get('participants', {})
        mark = participants.get(str(binding[0]), participants.get(binding[0])) if isinstance(participants, dict) else None
        last = raw.get('lastMessage', {})
        frontier = (last.get('id'), last.get('time')) if isinstance(last, dict) else None
        return ((mark, raw.get('markedAsUnread') is True, self._receipt_version(binding[0], raw['id'])),
                (chat.unread, chat.last_time_ms, chat.last_preview, frontier))

    def set_conversation_read(self, context, read):
        if type(read) is not bool:
            raise ValueError('Нужна явная отметка прочитано или непрочитано.')
        with self._metadata_lock:
            cached = self._read_context_cache(context)
            state = self._read_state(context, cached)
            if not state['available']:
                raise ValueError('Состояние чата изменилось. Заново откройте меню.')
            if state['unread'] is (not read):
                return {**state, 'accepted': False, 'confirmed': True, 'unchanged': True}
            binding = cached[0]
        conversation = context.conversation
        key = (binding[0], conversation.chat_id)
        with self._reserve_chat_read(binding, conversation.chat_id):
            with self._metadata_lock:
                cached = self._read_context_cache(context)
                # Recheck after reservation. Only our own inflight bit is ignored.
                state = self._read_state(context, cached)
                last = cached[2].get('lastMessage', {})
                if (state['pending'] or last.get('id') != context.message_id
                        or last.get('time') != context.message_time
                        or context.receipt_version != self._receipt_version(*key)
                        or cached[2].get('status') not in ('ACTIVE', 'LEFT', 'REMOVED', 'CLOSED', 'HIDDEN')):
                    raise ValueError('Состояние чата изменилось. Заново откройте меню.')
                baseline = self._read_baseline(cached)
                record = {'read': read, 'context': context, 'inflight': True}
                self._chat_read_pending[key] = record
            started = False
            try:
                self._require_conversation(conversation)
                started = True
                if read:
                    ack = binding[1].mark_read(conversation.chat_id, message_id=context.message_id,
                        mark=context.message_time, expected_session=binding[2], explicit=True)
                else:
                    ack = binding[1].mark_unread(conversation.chat_id, mark=context.message_time - 1,
                        expected_session=binding[2])
                if isinstance(ack, dict) and ack.get('success') is False:
                    raise MaxError('MAX отклонил отметку прочтения.')
                if (not isinstance(ack, dict) or not _time(ack.get('mark'))
                        or type(ack.get('unread')) is not int or not 0 <= ack['unread'] < 2**31
                        or 'success' in ack and type(ack['success']) is not bool):
                    raise MaxWriteOutcomeUnknown('MAX не подтвердил отметку прочтения.')
                if read and ack['mark'] < context.message_time:
                    raise MaxWriteOutcomeUnknown('MAX не подтвердил прочтение выбранного сообщения.')
                with self._metadata_lock, self.store.account_scope(binding[0]):
                    cached = self._read_context_cache(context)
                    current = self._read_baseline(cached)
                    changed = False
                    if current[0] == baseline[0]:
                        changed = self._apply_read_receipt(conversation.chat_id, binding[0], ack['mark'],
                            ack['unread'], set_as_unread=not read, apply_count=current[1] == baseline[1],
                            allow_equal=True)
                    if self._chat_read_pending.get(key) is record:
                        del self._chat_read_pending[key]
                    state = self._read_state(context, self._read_context_cache(context))
                if changed and self.on_chat_changed:
                    self._require_conversation(conversation)
                    self.on_chat_changed(conversation.chat_id)
                return {**state, 'accepted': True, 'confirmed': True, 'unchanged': False}
            except (MaxWriteOutcomeUnknown, MaxTimeout, MaxNotConnected):
                with self._metadata_lock:
                    record['inflight'] = False
                raise MaxWriteOutcomeUnknown('MAX не подтвердил отметку прочтения.') from None
            except (MaxError, MaxWriteNotStarted):
                with self._metadata_lock:
                    if self._chat_read_pending.get(key) is record:
                        del self._chat_read_pending[key]
                raise
            except Exception:
                with self._metadata_lock:
                    if not started and self._chat_read_pending.get(key) is record:
                        del self._chat_read_pending[key]
                    else:
                        record['inflight'] = False
                if not started:
                    raise
                raise MaxWriteOutcomeUnknown('Отметка прочтения не подтверждена. Проверьте состояние.') from None

    def refresh_conversation_read_state(self, context):
        """Resolve an unknown operation by CHAT_INFO only; never resend CHAT_MARK."""
        with self._metadata_lock:
            binding = self._read_context_cache(context)[0]
            _, pending = self._read_records()
            record = pending.get((binding[0], context.conversation.chat_id))
            if record is None:
                raise ValueError('Нет неподтверждённой отметки прочтения.')
        with self._reserve_chat_read(binding, context.conversation.chat_id, pending_record=record):
            return self._refresh_conversation_read_reserved(context, record)

    def _refresh_conversation_read_reserved(self, context, record):
        with self._metadata_lock:
            cached = self._read_context_cache(context)
            binding = cached[0]
            key = (binding[0], context.conversation.chat_id)
            active, pending = self._read_records()
            if pending.get(key) is not record or record['inflight']:
                raise ValueError('Дождитесь завершения отметки прочтения.')
            baseline = self._read_baseline(cached)
        raw = parse_action_chat_readback(binding[1].chat_info([key[1]], expected_session=binding[2]), key[1])
        participants = raw.get('participants', {})
        mark = participants.get(str(binding[0]), participants.get(binding[0]))
        marked = raw.get('markedAsUnread', False)
        count = raw.get('newMessages', int(marked) if type(marked) is bool else None)
        if not _time(mark) or type(marked) is not bool or type(count) is not int or not 0 <= count < 2**31:
            raise ValueError('MAX не вернул подтверждённую отметку прочтения.')
        with self._metadata_lock, self.store.account_scope(binding[0]):
            cached = self._read_context_cache(context)
            current = self._read_baseline(cached)
            last = raw.get('lastMessage', {})
            observed_frontier = ((last.get('id'), last.get('time'))
                                 if isinstance(last, dict) else None)
            changed = False
            if current[0] == baseline[0]:
                changed = self._apply_read_receipt(key[1], binding[0], mark, count,
                    set_as_unread=marked, allow_equal=True,
                    apply_count=current[1] == baseline[1] and observed_frontier == current[1][3])
            # A later incoming message may keep the total unread count nonzero;
            # the read intent covers only its captured message frontier.
            matches = (not marked and mark >= record['context'].message_time
                       if record['read'] else marked)
            if (pending.get(key) is record and matches
                    and current[0] == baseline[0] and current[1] == baseline[1]):
                del pending[key]
            state = self._read_state(context, self._read_context_cache(context))
        if changed and self.on_chat_changed:
            self._require_history_context(binding)
            self.on_chat_changed(key[1])
        return {**state, 'confirmed': not state['pending'], 'accepted': False}
