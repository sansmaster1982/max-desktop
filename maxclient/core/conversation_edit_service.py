"""Session-bound explicit group/channel edits with durable no-replay recovery."""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
import hashlib
import json
import time
import uuid
import weakref

from ..protocol.client import MaxError, MaxWriteNotStarted, MaxWriteOutcomeUnknown
from ..protocol.conversation_edit_contracts import (
    conversation_edit_limits, conversation_edit_values, build_conversation_edit,
    parse_conversation_edit_ack, edit_matching_fields,
)
from ..protocol.conversation_lifecycle_contracts import chat_sync_time, lifecycle_readback


def _revision(raw, owner_id):
    # Message arrivals and other participants' activity do not invalidate an
    # open editor. Bind only editable fields and the viewer's actual authority.
    admins = raw.get('adminParticipants', {})
    admin = admins.get(str(owner_id), admins.get(owner_id))
    participants = raw.get('participants', {})
    options = raw.get('options', {})
    join = raw.get('joinTime', 0)
    relevant = {key: raw.get(key) for key in ('id', 'type', 'status', 'owner', 'title', 'videoConversation', 'postId')}
    relevant.update(description=raw.get('description', ''), restrictions=raw.get('restrictions', 0),
        viewer_admin=None if admin is None else {'id': admin.get('id'), 'permissions': admin.get('permissions', 0)},
        viewer_participant=str(owner_id) in participants or owner_id in participants,
        joined=type(join) in (int, float) and join > 0,
        service=options.get('SERVICE_CHAT', False),
        only_owner=options.get('ONLY_OWNER_CAN_CHANGE_ICON_TITLE', True))
    encoded = json.dumps(relevant, sort_keys=True, ensure_ascii=True, allow_nan=False, separators=(',', ':'))
    return hashlib.sha256(encoded.encode('ascii')).hexdigest()


def _ui_fields(fields):
    return ['title' if item == 'theme' else item for item in fields]


@dataclass(frozen=True, eq=False)
class ConversationEditTicket:
    context: object = field(repr=False)
    revision: str = field(repr=False)
    chat_type: str
    title: str
    description: str
    limits: tuple
    nonce: str = field(repr=False)


class ConversationEditServiceMixin:
    def _conversation_edit_allowed(self, context, cached):
        raw, config = cached[2], cached[6]
        cap = self._lifecycle_capabilities(cached)
        if (context.peer_id is not None or context.chat_id == 0 or not cap['supported']
                or cap['kind'] not in ('group', 'channel') or cap['service']
                or raw.get('status') not in ('ACTIVE', 'HIDDEN') or raw.get('restrictions', 0) & 4
                or not isinstance(raw.get('title'), str) or not raw['title']
                or not isinstance(raw.get('description', ''), str)):
            raise ValueError('Изменение сведений этого чата сейчас недоступно')
        admin = raw.get('adminParticipants', {}).get(str(context.owner_id), {})
        # Normalized DTO maps use string IDs; absent permissions mean zero.
        permission = admin.get('permissions', 0) & 8
        if raw['type'] == 'CHANNEL':
            allowed = cap['owner'] or permission
        else:
            only_owner = raw.get('options', {}).get('ONLY_OWNER_CAN_CHANGE_ICON_TITLE', True)
            if type(only_owner) is not bool:
                raise ValueError('Не подтверждены права изменения сведений')
            allowed = permission or (cap['owner'] if only_owner else cap['participant'])
        if not allowed:
            raise ValueError('MAX не подтвердил право изменения сведений')
        return conversation_edit_limits(config.get('server', {}), config.get('experiments', {}))

    def _conversation_edit_record(self, context):
        self._require_conversation(context)
        record = self.store.conversation_action_journal(context.owner_id, context.chat_id)
        if not record or record.get('action') != 'conversation_edit':
            return None
        if (record.get('owner_id') != context.owner_id or record.get('chat_id') != context.chat_id
                or record.get('peer_id') is not None or context.peer_id is not None
                or record.get('chat_type') not in ('CHAT', 'CHANNEL')
                or type(record.get('acknowledged')) is not bool
                or not all(isinstance(record.get(k), str) and record[k] for k in ('operation_id', 'session_nonce', 'revision'))
                or not isinstance(record.get('limits'), list) or len(record['limits']) != 2
                or any(type(value) is not int or not 1 <= value <= 100000 for value in record['limits'])
                or record['limits'][1] > 400
                or not isinstance(record.get('observed_fields'), list)
                or not isinstance(record.get('patch'), dict)):
            raise ValueError('Журнал изменения сведений повреждён. Повтор не выполнен')
        build_conversation_edit(context.chat_id, record['patch'])
        if (any(not isinstance(item, str) for item in record['observed_fields'])
                or len(record['observed_fields']) != len(set(record['observed_fields']))
                or set(record['observed_fields']) - set(record['patch'])):
            raise ValueError('Журнал изменения сведений повреждён')
        chat_sync_time({'created': record.get('cutoff'), 'lastEventTime': record.get('cutoff')})
        return record

    def _conversation_edit_status(self, context, record, *, accepted=False):
        self._require_conversation(context)
        if self.store.conversation_action_journal(context.owner_id, context.chat_id) != record:
            raise ValueError('Ожидающее изменение сведений изменилось')
        observed = record['observed_fields']
        return {'context': context, 'chat_id': context.chat_id, 'peer_id': None,
            'action': 'edit', 'lifecycle_kind': 'conversation_edit', 'accepted': accepted,
            'acknowledged': record['acknowledged'], 'confirmed': False, 'pending': True, 'unchanged': False,
            'confirmed_fields': _ui_fields(observed), 'pending_fields': _ui_fields(k for k in record['patch'] if k not in observed),
            'action_status': ('MAX подтвердил часть сведений. Остальные изменения пока не подтверждены. Повтор не отправлен.'
                              if observed else 'Изменение сведений пока не подтверждено. Проверьте состояние; повтор не отправлен.')}

    def conversation_edit_available(self, context):
        try:
            with self._metadata_lock, self.store.account_scope(context.owner_id):
                cached = self._require_conversation(context)
                record = self._conversation_edit_record(context)
                if record:
                    return True  # Reopen the read-only pending editor, even after permission loss.
                self._conversation_edit_allowed(context, cached)
                lifecycle = self.conversation_lifecycle_status(context)
                return not lifecycle['pending'] and not self._owner_transfer_busy(context)
        except (ValueError, TypeError, AttributeError):
            return False

    def _conversation_edit_read(self, context, record=None):
        previous = copy.deepcopy(self._require_conversation(context)[2])
        raw = lifecycle_readback(context.source_client.chat_info(
            [context.chat_id], expected_session=context.session_context), context.chat_id)
        with self._metadata_lock, self.store.account_scope(context.owner_id):
            current = self._require_conversation(context)[2]
            if (raw is None or raw['type'] not in ('CHAT', 'CHANNEL') or raw['type'] != previous['type']
                    or raw['type'] != current['type'] or not isinstance(raw.get('title'), str)
                    or chat_sync_time(raw) < max(chat_sync_time(current), record['cutoff'] if record else 0)
                    or current != previous and _revision(current, context.owner_id) != _revision(raw, context.owner_id)):
                raise ValueError('Сведения чата изменились во время проверки. Откройте редактор заново')
            if record and self.store.conversation_action_journal(context.owner_id, context.chat_id) != record:
                raise ValueError('Ожидающее изменение сведений изменилось')
            self._remember_chat_info({'chats': [raw]}, my_id=context.owner_id)
            self._require_conversation(context)
            return copy.deepcopy(raw)

    def prepare_conversation_edit(self, context):
        record = self._conversation_edit_record(context)
        if not self.conversation_edit_available(context):
            raise ValueError('Редактирование сейчас недоступно')
        raw = self._conversation_edit_read(context, record)
        with self._metadata_lock, self.store.account_scope(context.owner_id):
            cached = self._require_conversation(context)
            if cached[2] != raw:
                raise ValueError('Сведения изменились во время открытия редактора')
            pending = self._conversation_edit_record(context)
            limits = (tuple(pending['limits']) if pending else
                      conversation_edit_limits(cached[6].get('server', {}), cached[6].get('experiments', {})))
            result = {'context': context, 'chat_id': context.chat_id,
                'kind': 'group' if raw['type'] == 'CHAT' else 'channel', 'title': raw['title'],
                'description': raw.get('description', ''), 'title_limit': limits[0], 'description_limit': limits[1],
                'pending': bool(pending), 'can_submit': not pending, 'ticket': None}
            if pending:
                result['conversation_edit_state'] = self._conversation_edit_status(context, pending)
                return result
            self._conversation_edit_allowed(context, cached)
            if not self.conversation_edit_available(context):
                raise ValueError('Редактирование сейчас недоступно')
            ticket = ConversationEditTicket(context, _revision(raw, context.owner_id), raw['type'], raw['title'],
                raw.get('description', ''), limits, uuid.uuid4().hex)
            tickets = getattr(self, '_conversation_edit_tickets', None)
            if tickets is None:
                self._conversation_edit_tickets = tickets = weakref.WeakSet()
            tickets.add(ticket)
            result['ticket'] = ticket
            return result

    def _require_edit_ticket(self, context, ticket):
        self._require_conversation(context)
        if (not isinstance(ticket, ConversationEditTicket) or ticket.context is not context
                or ticket not in getattr(self, '_conversation_edit_tickets', ())):
            raise ValueError('Откройте редактор заново')

    def _conversation_edit_cas(self, context, record, **changes):
        with self._metadata_lock, self.store.account_scope(context.owner_id):
            self._require_conversation(context)
            updated = {**record, **changes}
            if not self.store.compare_conversation_action(context.owner_id, context.chat_id, record, updated):
                raise ValueError('Ожидающее изменение сведений изменилось')
            return updated

    def _refresh_conversation_edit(self, context, record):
        raw = self._conversation_edit_read(context, record)
        if raw['type'] != record['chat_type']:
            raise ValueError('Тип ожидающего изменения изменился')
        observed = edit_matching_fields(raw, record['patch'])
        with self._metadata_lock, self.store.account_scope(context.owner_id):
            if self._require_conversation(context)[2] != raw:
                raise ValueError('Сведения изменились после проверки')
            if set(observed) != set(record['patch']):
                record = self._conversation_edit_cas(context, record, observed_fields=observed)
                return self._conversation_edit_status(context, record)
            # Supersede a legacy local alias only for an explicit confirmed title edit.
            if not self.store.compare_conversation_action(context.owner_id, context.chat_id, record, None,
                    edit_title=raw['title'] if 'theme' in record['patch'] else None):
                raise ValueError('Ожидающее изменение сведений изменилось')
        if self.on_chat_changed:
            self.on_chat_changed(context.chat_id)
        return {'context': context, 'chat_id': context.chat_id, 'peer_id': None,
            'action': 'edit', 'lifecycle_kind': 'conversation_edit', 'accepted': False,
            'acknowledged': record['acknowledged'], 'confirmed': True, 'pending': False, 'unchanged': False,
            'confirmed_fields': _ui_fields(observed), 'pending_fields': [], 'action_status': 'Сведения чата изменены.'}

    def submit_conversation_edit(self, context, *, ticket, title, description):
        self._require_edit_ticket(context, ticket)
        title, description = conversation_edit_values(title, description, ticket.limits)
        if self.conversation_lifecycle_status(context)['pending'] or not self.conversation_edit_available(context):
            raise ValueError('Предыдущее действие ещё не подтверждено')
        raw = self._conversation_edit_read(context)
        with self._metadata_lock, self.store.account_scope(context.owner_id):
            self._require_edit_ticket(context, ticket)
            cached = self._require_conversation(context)
            limits = self._conversation_edit_allowed(context, cached)
            if (_revision(raw, context.owner_id) != ticket.revision
                    or _revision(cached[2], context.owner_id) != ticket.revision or limits != ticket.limits
                    or not self.conversation_edit_available(context)):
                raise ValueError('Сведения или права изменились. Откройте редактор заново')
            patch = {}
            if title != ticket.title:
                patch['theme'] = title
            if description != ticket.description:
                patch['description'] = description
            if not patch:
                return {'context': context, 'chat_id': context.chat_id, 'peer_id': None,
                    'action': 'edit', 'lifecycle_kind': 'conversation_edit', 'accepted': False, 'acknowledged': False,
                    'confirmed': True, 'pending': False, 'unchanged': True,
                    'confirmed_fields': [], 'pending_fields': [], 'action_status': 'Изменений нет.'}
            record = {'action': 'conversation_edit', 'owner_id': context.owner_id, 'chat_id': context.chat_id,
                'peer_id': None, 'chat_type': raw['type'], 'revision': ticket.revision, 'cutoff': chat_sync_time(raw),
                'patch': patch, 'session_nonce': self._owner_transfer_nonce(context), 'acknowledged': False,
                'limits': list(limits), 'observed_fields': [], 'created': time.time_ns() // 1_000_000}
            self._reserve_lifecycle(context, record)
        try:
            try:
                with self._metadata_lock, self.store.account_scope(context.owner_id):
                    cached = self._require_conversation(context)
                    limits = self._conversation_edit_allowed(context, cached)
                    if (_revision(cached[2], context.owner_id) != ticket.revision or limits != ticket.limits
                            or self._owner_transfer_busy(context)
                            or record['session_nonce'] != self._owner_transfer_nonce(context)):
                        raise ValueError('Сведения или права изменились перед сохранением')
                    record = self._conversation_edit_cas(context, record, dispatched=True)
                ack = context.source_client.edit_conversation_fields(context.chat_id, raw['type'], patch,
                    expected_session=context.session_context)
                parse_conversation_edit_ack({'chat': ack}, context.chat_id, raw['type'], patch)
                record = self._conversation_edit_cas(context, record, acknowledged=True)
            except MaxWriteOutcomeUnknown:
                raise
            except (MaxError, MaxWriteNotStarted):
                with self._metadata_lock, self.store.account_scope(context.owner_id):
                    self._require_conversation(context)
                    self.store.compare_conversation_action(context.owner_id, context.chat_id, record, None)
                raise
            except Exception:
                if not record.get('dispatched'):
                    with self._metadata_lock, self.store.account_scope(context.owner_id):
                        self._require_conversation(context)
                        self.store.compare_conversation_action(context.owner_id, context.chat_id, record, None)
                    raise
                raise MaxWriteOutcomeUnknown('Изменение сведений не подтверждено. Проверьте состояние; повтор не отправлен.') from None
        finally:
            self._lifecycle_finished_sending(context)
        try:
            return {**self._refresh_conversation_edit(context, record), 'accepted': True}
        except Exception:
            self._require_conversation(context)
            return self._conversation_edit_status(context, record, accepted=True)
