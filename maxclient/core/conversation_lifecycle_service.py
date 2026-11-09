"""Explicit More commands with durable single-write reservations and readback."""
from __future__ import annotations

import copy
import json
import time
import uuid
from ..protocol.client import MaxError, MaxWriteNotStarted, MaxWriteOutcomeUnknown
from ..protocol.conversation_lifecycle_contracts import (
    chat_sync_time, lifecycle_readback, parse_contact_share_ack, parse_leave_ack,
)
from ..protocol.conversation_actions_contracts import parse_action_contact_readback
from ..protocol.search_contracts import _number, _integer
from .conversation_action_capabilities import conversation_action_capabilities
from .conversation_write_capabilities import conversation_can_write
from .conversation_info_service import _peer
from .bot_delete_service import BotDeleteServiceMixin
from .owner_transfer_service import OwnerTransferServiceMixin
from .conversation_edit_service import ConversationEditServiceMixin


_UNREAD_LIFECYCLE_REPLY = object()


def _diagnostic_type(value):
    # Fixed vocabulary only: never stringify values, keys or custom type names.
    return {dict: 'object', list: 'array', str: 'string', int: 'integer',
            float: 'number', bool: 'boolean', type(None): 'null'}.get(type(value), 'other')


class _LifecycleReadbackUnconfirmed(ValueError):
    def __init__(self, reason):
        super().__init__('Состояние чата не подтверждено')
        self.reason = reason


class ConversationLifecycleServiceMixin(BotDeleteServiceMixin, OwnerTransferServiceMixin, ConversationEditServiceMixin):
    def _log_lifecycle_readback(self, context, record, reason, data=_UNREAD_LIFECYCLE_REPLY,
                                *, peer_match='unchecked'):
        """Emit categories through the existing debug sink, never account data."""
        try:
            action = record.get('action')
            reasons = ('readback_failed', 'readback_invalid', 'target_unconfirmed', 'target_mismatch',
                       'stale_readback', 'delete_status_unconfirmed', 'clear_history_unconfirmed',
                       'operation_changed', 'confirmed', 'ack_confirmed', 'ack_record_invalid')
            details = {'action': action if action in ('clear', 'delete', 'leave') else 'other',
                       'acknowledged': record.get('acknowledged') is True,
                       'reason': reason if reason in reasons else 'other',
                       'peer_match': peer_match if peer_match in (
                           'unchecked', 'match', 'mismatch', 'absent_chat', 'unavailable') else 'unavailable',
                       'reply_type': 'unread' if data is _UNREAD_LIFECYCLE_REPLY else _diagnostic_type(data)}
            if isinstance(data, dict):
                details['ambiguous_envelope'] = any(key in data for key in ('chat', 'chatId', 'message'))
                chats = data.get('chats')
                details['chats_type'] = _diagnostic_type(chats) if 'chats' in data else 'absent'
                if isinstance(chats, list):
                    details['chats_shape'] = 'empty' if not chats else 'single' if len(chats) == 1 else 'multiple'
                    if len(chats) == 1 and isinstance(chats[0], dict):
                        raw = chats[0]
                        details['fields'] = {key: _diagnostic_type(raw[key]) if key in raw else 'absent'
                            for key in ('id', 'type', 'status', 'owner', 'participants', 'created',
                                        'modified', 'lastEventTime', 'lastMessage', 'hasBots', 'isSuspended')}
                        status = raw.get('status')
                        details['status'] = status if type(status) is str and status in (
                            'ACTIVE', 'LEFT', 'REMOVED', 'CLOSED', 'HIDDEN', 'BLOCKED') else 'unavailable'
                        kind = raw.get('type')
                        details['chat_type'] = kind if type(kind) is str and kind in (
                            'DIALOG', 'CHAT', 'CHANNEL') else 'unavailable'
                        details['id_match'] = type(raw.get('id')) is int and raw['id'] == context.chat_id
                        try:
                            clock = chat_sync_time(raw)
                            cutoff = record['cutoff']
                            details['freshness'] = 'older' if clock < cutoff else 'equal' if clock == cutoff else 'newer'
                        except (KeyError, TypeError, ValueError):
                            details['freshness'] = 'unavailable'
                        last = raw.get('lastMessage')
                        if isinstance(last, dict):
                            moment = last.get('time')
                            cutoff = record.get('cutoff')
                            details['last_message_cutoff'] = ('newer' if moment > cutoff else 'not_newer') if (
                                _number(moment) and _number(cutoff)) else 'unavailable'
            self.log('conversation_lifecycle_readback ' + json.dumps(details, sort_keys=True, separators=(',', ':')))
        except Exception:
            # A diagnostic sink or unexpected object must never change an action.
            pass

    def _ordinary_delete_ack_record(self, context, record, cached):
        """A persisted matching success ACK proves 52, not removal of the entity."""
        operation = record.get('operation_id')
        return (record.get('action') == 'delete' and record.get('acknowledged') is True
                and type(operation) is str and len(operation) == 32
                and all(char in '0123456789abcdef' for char in operation)
                and _number(record.get('cutoff')) and record['cutoff'] >= 0
                and type(record.get('for_all')) is bool
                and _integer(record.get('created'), 64) and record['created'] > 0
                and record.get('peer_id') == context.peer_id
                and (record.get('peer_id') is None or _integer(record['peer_id'], 64))
                and self._lifecycle_capabilities(cached)['kind'] in ('dialog', 'group', 'channel', 'video')
                and (cached[2].get('type') == 'DIALOG' or record['for_all'] is True))

    def _confirm_acknowledged_delete(self, context, record, base):
        with self._metadata_lock, self.store.account_scope(context.owner_id):
            cached = self._require_conversation(context)
            current_record = self.store.conversation_action_journal(context.owner_id, context.chat_id)
            if current_record != record:
                self._log_lifecycle_readback(context, record, 'operation_changed')
                return {**base, 'confirmed': False, 'pending': current_record is not None,
                        'confirmation_reason': 'operation_changed'}
            if not self._ordinary_delete_ack_record(context, record, cached):
                self._log_lifecycle_readback(context, record, 'ack_record_invalid')
                return {**base, 'confirmed': False, 'pending': True,
                        'confirmation_reason': 'ack_record_invalid'}
            current = copy.deepcopy(cached[2])
            cutoff = record['cutoff']
            last = current.get('lastMessage')
            newer = isinstance(last, dict) and _number(last.get('time')) and last['time'] > cutoff
            if not self.store.compare_conversation_action(context.owner_id, context.chat_id,
                    record, None, delete_cutoff=cutoff):
                self._log_lifecycle_readback(context, record, 'operation_changed')
                return {**base, 'confirmed': False, 'pending': True,
                        'confirmation_reason': 'operation_changed'}
            if not newer:
                current.pop('lastMessage', None)
                current['status'] = 'REMOVED'
                self._server_chats[context.chat_id] = current
            self._chat_fetch_ts.pop(context.chat_id, None)
            self._chat_fetch_anchor.pop(context.chat_id, None)
        self._log_lifecycle_readback(context, record, 'ack_confirmed')
        if self.on_chat_changed:
            self.on_chat_changed(context.chat_id)
        return {**base, 'accepted': True, 'confirmed': True, 'pending': False,
                'confirmation_reason': 'ack_confirmed'}

    def conversation_lifecycle_status(self, context):
        self._require_conversation(context)
        row = self.store.conversation_action_journal(context.owner_id, context.chat_id)
        if row and row.get('action') == 'bot_delete':
            return self._bot_delete_status(context, self._bot_delete_record(context))
        if row and row.get('action') == 'owner_transfer':
            return self._owner_transfer_status(context, self._owner_transfer_record(context))
        if row and row.get('action') == 'conversation_edit':
            return self._conversation_edit_status(context, self._conversation_edit_record(context))
        return {'context': context, 'pending': row is not None,
                'action': row.get('action') if row else None}

    def refresh_conversation_menu_metadata(self, context):
        """Ordinary 48/32 for the captured target, never a write or reconnect."""
        cached = self._require_conversation(context)
        raw = lifecycle_readback(context.source_client.chat_info(
            [context.chat_id], expected_session=context.session_context), context.chat_id)
        self._require_conversation(context)
        if raw is None or _peer(cached[1], raw, context.owner_id) != context.peer_id:
            raise ValueError('MAX не подтвердил выбранный чат')
        contact = None
        if context.peer_id is not None:
            contact = parse_action_contact_readback(context.source_client.contact_info(
                [context.peer_id], expected_session=context.session_context), context.peer_id)
        with self._metadata_lock, self.store.account_scope(context.owner_id):
            self._require_conversation(context)
            # Preserve a newer push which arrived while the read was in flight.
            current = self._server_chats.get(context.chat_id, {})
            try:
                newer = chat_sync_time(current) > chat_sync_time(raw)
            except ValueError:
                newer = False
            if not newer:
                self._remember_chat_info({'chats': [raw]}, my_id=context.owner_id)
            if contact is not None:
                old = self._server_contacts.get(context.peer_id, {})
                if old.get('updateTime', -1) <= contact['updateTime']:
                    self._remember_contact_info({'contacts': [contact]})
            return self._require_conversation(context)

    @staticmethod
    def _lifecycle_capabilities(cached):
        binding, _, raw, peer, contact, _, config = cached
        return conversation_action_capabilities(raw, contact, binding[0],
                                                config.get('server', {}), peer_id=peer)

    def _reserve_lifecycle(self, context, record):
        with self._metadata_lock, self.store.account_scope(context.owner_id):
            self._require_conversation(context)
            if context.peer_id is not None and self.store.bot_delete_for_peer(context.owner_id, context.peer_id):
                raise ValueError('Удаление этого бота ещё не завершено')
            record['operation_id'] = uuid.uuid4().hex
            self.store.reserve_conversation_action(context.owner_id, context.chat_id, record)
            active = getattr(self, '_lifecycle_inflight', None)
            if active is None:
                self._lifecycle_inflight = active = set()
            active.add((context.owner_id, context.chat_id))

    def _lifecycle_finished_sending(self, context):
        with self._metadata_lock:
            getattr(self, '_lifecycle_inflight', set()).discard((context.owner_id, context.chat_id))

    def _release_lifecycle(self, context, *, expected_record=None):
        # A late result must not clear another owner's reservation.
        try:
            with self._metadata_lock, self.store.account_scope(context.owner_id):
                self._require_conversation(context)
                current = self.store.conversation_action_journal(context.owner_id, context.chat_id)
                if expected_record is None or current == expected_record:
                    self.store.finish_conversation_action(context.owner_id, context.chat_id)
        finally:
            self._lifecycle_finished_sending(context)

    def _change_conversation_lifecycle(self, context, action, for_all):
        if type(for_all) is not bool:
            raise ValueError('Подтвердите область удаления')
        self._require_conversation(context)
        if self.conversation_lifecycle_status(context)['pending']:
            raise ValueError('Результат предыдущего действия ещё не подтверждён')
        cached = self.refresh_conversation_menu_metadata(context)
        cap = self._lifecycle_capabilities(cached)
        raw = cached[2]
        if action not in cap['more']:
            raise ValueError('Это действие недоступно для выбранного чата')
        if action == 'delete' and cap['kind'] in ('saved', 'bot'):
            raise ValueError('Удаление этого вида чата пока не поддерживается')
        required_all = raw['type'] == 'CHANNEL' if action == 'clear' else raw['type'] != 'DIALOG'
        optional_all = cap['clear_for_all_optional'] if action == 'clear' else (
            cap['kind'] == 'dialog' and cached[6].get('server', {}).get('delete-for-all') is True)
        if for_all != required_all and not (for_all and optional_all):
            raise ValueError('Область удаления изменилась. Откройте подтверждение заново.')
        cutoff = chat_sync_time(raw)
        record = {'action': action, 'cutoff': cutoff, 'for_all': for_all,
                  'peer_id': context.peer_id, 'created': time.time_ns() // 1_000_000}
        self._reserve_lifecycle(context, record)
        started = False
        try:
            with self._metadata_lock, self.store.account_scope(context.owner_id):
                self._require_conversation(context)
                if self.store.conversation_action_journal(context.owner_id, context.chat_id) != record:
                    raise ValueError('Ожидающее действие изменилось')
            started = True
            context.source_client.change_conversation_lifecycle(action, context.chat_id, cutoff, for_all,
                                                                expected_session=context.session_context)
            with self._metadata_lock, self.store.account_scope(context.owner_id):
                self._require_conversation(context)
                if not self.store.compare_conversation_action(context.owner_id, context.chat_id,
                        record, {**record, 'acknowledged': True}):
                    raise ValueError('Ожидающее действие изменилось')
        except MaxWriteOutcomeUnknown:
            self._lifecycle_finished_sending(context)
            raise
        except (MaxError, MaxWriteNotStarted):
            self._release_lifecycle(context, expected_record=record)
            raise
        except Exception:
            self._lifecycle_finished_sending(context)
            if not started:
                self._release_lifecycle(context, expected_record=record)
                raise
            raise MaxWriteOutcomeUnknown('Результат действия неизвестен. Нажмите «Проверить состояние»; повтор не отправлен.') from None
        self._lifecycle_finished_sending(context)
        try:
            result = self.refresh_conversation_lifecycle(context)
            return {**result, 'accepted': True}
        except Exception as error:
            self._require_conversation(context)
            return {'context': context, 'chat_id': context.chat_id, 'action': action,
                    'accepted': True, 'confirmed': False, 'pending': True,
                    'confirmation_reason': (error.reason if isinstance(error, _LifecycleReadbackUnconfirmed)
                                            else 'readback_failed')}

    def clear_conversation_history(self, context, *, for_all=False):
        return self._change_conversation_lifecycle(context, 'clear', for_all)

    def delete_conversation(self, context, *, for_all=False):
        cached = self._require_conversation(context)
        if cached[2].get('hasBots') is True or self._bot_delete_record(context):
            return self._delete_bot_conversation(context, for_all=for_all)
        return self._change_conversation_lifecycle(context, 'delete', for_all)

    @staticmethod
    def _ordinary_leave_allowed(cached):
        cap = ConversationLifecycleServiceMixin._lifecycle_capabilities(cached)
        return (cap['supported'] and cap['kind'] in ('group', 'channel')
                and cached[2].get('status') in ('ACTIVE', 'BLOCKED')
                and cap['participant'] and not cap['owner'] and 'leave' in cap['more'])

    def leave_conversation(self, context):
        """One confirmed ordinary non-owner leave; never delete history or retry."""
        self._require_conversation(context)
        if self.conversation_lifecycle_status(context)['pending']:
            raise ValueError('Результат предыдущего действия ещё не подтверждён')
        cached = self.refresh_conversation_menu_metadata(context)
        if (self.store.conversation_departure(context.chat_id) is not None
                or not self._ordinary_leave_allowed(cached)):
            raise ValueError('Выход из этого чата сейчас недоступен')
        raw = cached[2]
        record = {'action': 'leave', 'cutoff': chat_sync_time(raw),
                  'chat_type': raw['type'], 'peer_id': context.peer_id,
                  'created': time.time_ns() // 1_000_000}
        self._reserve_lifecycle(context, record)
        started = False
        try:
            current = self._require_conversation(context)
            if (current[2].get('type') != record['chat_type']
                    or not self._ordinary_leave_allowed(current)):
                raise ValueError('Возможность выйти из чата изменилась. Откройте меню заново.')
            started = True
            ack = context.source_client.leave_conversation(context.chat_id,
                                                            expected_session=context.session_context)
            parse_leave_ack(ack)
            with self._metadata_lock, self.store.account_scope(context.owner_id):
                self._require_conversation(context)
                current_record = self.store.conversation_action_journal(context.owner_id, context.chat_id)
                if current_record != record:
                    raise ValueError('Ожидающее действие изменилось')
                self.store.acknowledge_conversation_action(context.owner_id, context.chat_id)
        except MaxWriteOutcomeUnknown:
            self._lifecycle_finished_sending(context)
            raise
        except (MaxError, MaxWriteNotStarted):
            self._release_lifecycle(context, expected_record=record)
            raise
        except Exception:
            self._lifecycle_finished_sending(context)
            if not started:
                self._release_lifecycle(context, expected_record=record)
                raise
            raise MaxWriteOutcomeUnknown('Выход не подтверждён. Нажмите «Проверить состояние»; повтор не отправлен.') from None
        self._lifecycle_finished_sending(context)
        try:
            return {**self.refresh_conversation_lifecycle(context), 'accepted': True}
        except Exception:
            self._require_conversation(context)
            return {'context': context, 'chat_id': context.chat_id, 'action': 'leave',
                    'accepted': True, 'confirmed': False, 'pending': True}

    @staticmethod
    def _leave_readback_confirmed(raw, record, owner_id):
        # A missing chat can be a private/inaccessible/partial read. The original
        # WEB itself fabricates a removed model for this case; it is not proof.
        if (raw is None or raw.get('type') != record.get('chat_type')
                or raw.get('status') not in ('LEFT', 'REMOVED', 'CLOSED')
                or chat_sync_time(raw) < record['cutoff']):
            return False
        # joinTime is optional timestamp, not nullable, in the pinned schema.
        # Missing/zero removes the viewer from the raw participant map, but
        # owner/admin membership still takes precedence in the derived model.
        if 'joinTime' in raw and (not _number(raw['joinTime']) or raw['joinTime'] != 0):
            return False
        cap = conversation_action_capabilities(raw, {}, owner_id, {})
        return (cap['supported'] and cap['kind'] in ('group', 'channel')
                and not cap['participant'] and not cap['owner'])

    def _refresh_conversation_leave(self, context, record, raw, base):
        if not self._leave_readback_confirmed(raw, record, context.owner_id):
            return {**base, 'confirmed': False, 'pending': True}
        with self._metadata_lock, self.store.account_scope(context.owner_id):
            current = self._require_conversation(context)[2]
            current_record = self.store.conversation_action_journal(context.owner_id, context.chat_id)
            if current_record != record:
                return {**base, 'confirmed': False, 'pending': current_record is not None}
            # A newer push/rejoin must not be overwritten by the older GET48.
            if (current.get('type') != record['chat_type']
                    or chat_sync_time(current) > chat_sync_time(raw)):
                return {**base, 'confirmed': False, 'pending': True}
            self._remember_chat_info({'chats': [raw]}, my_id=context.owner_id)
            self.store.confirm_conversation_departure(context.owner_id, context.chat_id,
                                                      chat_sync_time(raw), record['chat_type'])
            self.store.finish_conversation_action(context.owner_id, context.chat_id)
            self._chat_fetch_ts.pop(context.chat_id, None)
            self._chat_fetch_anchor.pop(context.chat_id, None)
        if self.on_chat_changed:
            self.on_chat_changed(context.chat_id)
        return {**base, 'confirmed': True, 'pending': False}

    def _reconcile_conversation_departure(self, raw, owner_id):
        """Only a newer complete confirmed membership restores a departed row."""
        record = self.store.conversation_departure(raw.get('id'))
        if record is None:
            return
        try:
            full = lifecycle_readback({'chats': [raw]}, raw['id'])
            cap = conversation_action_capabilities(full, {}, owner_id, {})
            rejoined = (full['type'] == record['chat_type']
                        and full['status'] in ('ACTIVE', 'BLOCKED')
                        and cap['supported'] and cap['kind'] in ('group', 'channel')
                        and cap['participant'] and chat_sync_time(full) > record['cutoff'])
        except (ValueError, TypeError, KeyError):
            return
        if rejoined:
            self.store.clear_conversation_departure(owner_id, raw['id'])

    def refresh_conversation_lifecycle(self, context):
        cached = self._require_conversation(context)
        record = self.store.conversation_action_journal(context.owner_id, context.chat_id)
        base = {'context': context, 'chat_id': context.chat_id,
                'accepted': bool(record and record.get('action') in ('clear', 'delete')
                                 and record.get('acknowledged') is True),
                'action': record.get('action') if record else None}
        if not record:
            return {**base, 'confirmed': True, 'pending': False}
        with self._metadata_lock:
            if (context.owner_id, context.chat_id) in getattr(self, '_lifecycle_inflight', set()):
                if record.get('action') == 'bot_delete':
                    return self._bot_delete_status(context, self._bot_delete_record(context))
                if record.get('action') == 'owner_transfer':
                    return self._owner_transfer_status(context, self._owner_transfer_record(context))
                if record.get('action') == 'conversation_edit':
                    return self._conversation_edit_status(context, self._conversation_edit_record(context))
                return {**base, 'confirmed': False, 'pending': True}
        if record.get('peer_id') != context.peer_id:
            raise ValueError('Собеседник ожидающего действия изменился')
        if record.get('action') == 'bot_delete':
            return self._refresh_bot_delete(context, self._bot_delete_record(context))
        if record.get('action') == 'owner_transfer':
            return self._refresh_owner_transfer(context, self._owner_transfer_record(context))
        if record.get('action') == 'conversation_edit':
            return self._refresh_conversation_edit(context, self._conversation_edit_record(context))
        if record['action'] == 'share_contact':
            return {**base, 'confirmed': False, 'pending': True}
        if record['action'] == 'delete' and record.get('acknowledged') is True:
            return self._confirm_acknowledged_delete(context, record, base)
        try:
            data = context.source_client.chat_info(
                [context.chat_id], expected_session=context.session_context)
        except Exception:
            self._log_lifecycle_readback(context, record, 'readback_failed')
            raise
        try:
            raw = lifecycle_readback(data, context.chat_id)
        except ValueError:
            self._log_lifecycle_readback(context, record, 'readback_invalid', data)
            raise _LifecycleReadbackUnconfirmed('readback_invalid') from None
        self._require_conversation(context)
        peer_match = 'absent_chat' if raw is None else 'match'
        try:
            if raw is not None and _peer(cached[1], raw, context.owner_id) != context.peer_id:
                peer_match = 'mismatch'
        except ValueError:
            self._log_lifecycle_readback(context, record, 'target_unconfirmed', data, peer_match='unavailable')
            raise _LifecycleReadbackUnconfirmed('target_unconfirmed') from None
        if peer_match == 'mismatch':
            self._log_lifecycle_readback(context, record, 'target_mismatch', data, peer_match=peer_match)
            raise _LifecycleReadbackUnconfirmed('target_mismatch')
        if record['action'] == 'leave':
            return self._refresh_conversation_leave(context, record, raw, base)
        cutoff = record['cutoff']
        try:
            stale = raw is not None and chat_sync_time(raw) < cutoff
        except ValueError:
            self._log_lifecycle_readback(context, record, 'readback_invalid', data, peer_match=peer_match)
            raise _LifecycleReadbackUnconfirmed('readback_invalid') from None
        if stale:
            self._log_lifecycle_readback(context, record, 'stale_readback', data, peer_match=peer_match)
            return {**base, 'confirmed': False, 'pending': True, 'confirmation_reason': 'stale_readback'}
        if record['action'] == 'delete':
            confirmed = raw is None or raw.get('status') in ('REMOVED', 'CLOSED')
        else:
            last = raw.get('lastMessage') if raw is not None else None
            confirmed = raw is None or last is None or (
                record.get('acknowledged') is True and last.get('time', -1) > cutoff)
        if not confirmed:
            reason = 'delete_status_unconfirmed' if record['action'] == 'delete' else 'clear_history_unconfirmed'
            self._log_lifecycle_readback(context, record, reason, data, peer_match=peer_match)
            return {**base, 'confirmed': False, 'pending': True, 'confirmation_reason': reason}
        with self._metadata_lock, self.store.account_scope(context.owner_id):
            self._require_conversation(context)
            # Another readback can settle this operation while GET48 is in flight.
            # A later user action owns its own reservation and must remain intact.
            current_record = self.store.conversation_action_journal(context.owner_id, context.chat_id)
            if current_record != record:
                self._log_lifecycle_readback(context, record, 'operation_changed', data, peer_match=peer_match)
                return {**base, 'confirmed': False, 'pending': current_record is not None,
                        'confirmation_reason': 'operation_changed'}
            self.store.clear_confirmed_history(context.owner_id, context.chat_id, cutoff,
                                               deleted=record['action'] == 'delete')
            current = copy.deepcopy(self._server_chats.get(context.chat_id, {}))
            last = current.get('lastMessage', {})
            newer = isinstance(last, dict) and type(last.get('time')) in (int, float) and last['time'] > cutoff
            if raw is not None and not newer:
                self._remember_chat_info({'chats': [raw]}, my_id=context.owner_id)
            elif not newer:
                current.pop('lastMessage', None)
                if record['action'] == 'delete':
                    current['status'] = 'REMOVED'
                self._server_chats[context.chat_id] = current
            self.store.finish_conversation_action(context.owner_id, context.chat_id)
            self._chat_fetch_ts.pop(context.chat_id, None)
            self._chat_fetch_anchor.pop(context.chat_id, None)
        if self.on_chat_changed:
            self.on_chat_changed(context.chat_id)
        self._log_lifecycle_readback(context, record, 'confirmed', data, peer_match=peer_match)
        return {**base, 'confirmed': True, 'pending': False, 'confirmation_reason': 'confirmed'}

    def share_conversation_destinations(self, context):
        """Project cached, confirmed writable destinations; no network or writes."""
        self._require_conversation(context)
        destinations = []
        with self._metadata_lock, self.store.account_scope(context.owner_id):
            self._require_conversation(context)
            for chat in self.list_chats():
                try:
                    target = self.conversation_context(chat.id, activate_files=False)
                    cached = self._require_conversation(target)
                    if (target.owner_id == context.owner_id and
                            target.session_context is context.session_context and
                            conversation_can_write(cached[2], cached[4], context.owner_id)):
                        destinations.append(chat)
                except (ValueError, RuntimeError):
                    continue
        return destinations

    def share_conversation_contact(self, context, target_context):
        source = self.refresh_conversation_menu_metadata(context)
        self._require_conversation(target_context)
        if (target_context.owner_id != context.owner_id or
                target_context.session_context is not context.session_context):
            raise ValueError('Сессия получателя изменилась')
        if 'share_contact' not in self._lifecycle_capabilities(source)['more']:
            raise ValueError('Контакт этого чата нельзя передать')
        target = self.refresh_conversation_menu_metadata(target_context)
        if not conversation_can_write(target[2], target[4], context.owner_id):
            raise ValueError('В выбранный чат сейчас нельзя отправить контакт')
        # CID identifies one explicit user request; it is never retried.
        with context.source_client._message_cid_lock:
            cid = max(time.time_ns() // 1_000_000, context.source_client._last_message_cid + 1)
            context.source_client._last_message_cid = cid
        record = {'action': 'share_contact', 'target_chat_id': target_context.chat_id,
                  'peer_id': context.peer_id, 'cid': cid}
        self._reserve_lifecycle(context, record)
        started = False
        try:
            source = self._require_conversation(context)
            if 'share_contact' not in self._lifecycle_capabilities(source)['more']:
                raise ValueError('Возможность передать этот контакт изменилась')
            target = self._require_conversation(target_context)
            if not conversation_can_write(target[2], target[4], context.owner_id):
                raise ValueError('Разрешение на отправку в выбранный чат изменилось')
            binding = target[0]
            started = True
            result = context.source_client.share_contact_message(target_context.chat_id, context.peer_id, cid,
                                                                 expected_session=context.session_context)
            result = parse_contact_share_ack(result, target_context.chat_id, cid)
            with self._metadata_lock, self.store.account_scope(context.owner_id):
                self._require_conversation(context)
                self._require_conversation(target_context)
                message = self._accept_message_ack(target_context.chat_id, result, outgoing=True,
                                                   binding=binding, notify=False)
                self.store.finish_conversation_action(context.owner_id, context.chat_id)
            self._lifecycle_finished_sending(context)
            if self.on_chat_changed:
                self.on_chat_changed(target_context.chat_id)
            return {'context': context, 'target_context': target_context, 'action': 'share_contact',
                    'accepted': True, 'confirmed': True, 'pending': False, 'message': message}
        except MaxWriteOutcomeUnknown:
            self._lifecycle_finished_sending(context)
            raise
        except (MaxError, MaxWriteNotStarted):
            self._release_lifecycle(context)
            raise
        except Exception:
            self._lifecycle_finished_sending(context)
            if not started:
                self._release_lifecycle(context)
                raise
            raise MaxWriteOutcomeUnknown('Передача контакта не подтверждена. Проверьте выбранный чат; повтор не отправлен.') from None
