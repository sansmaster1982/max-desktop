"""Explicit ownership transfer55 followed by leave58, with durable recovery.

Membership reads and refreshes never dispatch writes. Unknown55/58 are never
replayed, and ownership is never rolled back. See OWNER_TRANSFER_CORE_20261106.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
import time
import uuid
import weakref

from ..protocol.client import MaxError, MaxWriteNotStarted, MaxWriteOutcomeUnknown
from ..protocol.owner_transfer_contracts import parse_owner_transfer_ack
from ..protocol.conversation_lifecycle_contracts import chat_sync_time, lifecycle_readback, parse_leave_ack
from ..protocol.search_contracts import _integer, _number, _contact
from .conversation_action_capabilities import _admin_ids
from .conversation_info_service import _contact_name


@dataclass(frozen=True, eq=False)
class OwnerTransferTicket:
    context: object = field(repr=False)
    successor_id: int
    successor_name: str
    chat_type: str
    query: str = field(repr=False)
    marker: int | float | None = field(repr=False)
    cutoff: int | float
    nonce: str = field(repr=False)


class OwnerTransferServiceMixin:
    def _owner_transfer_nonce(self, context):
        sessions = getattr(self, '_owner_transfer_sessions', None)
        if sessions is None:
            self._owner_transfer_sessions = sessions = weakref.WeakKeyDictionary()
        return sessions.setdefault(context.session_context, uuid.uuid4().hex)

    def _owner_transfer_busy(self, context):
        adapter = getattr(self, '_conversation_actions_state', None)
        if adapter is not None:
            with adapter._lock:
                if any(owner == context.owner_id and chat == context.chat_id
                       for owner, chat, _family in adapter._pending):
                    return True
        for name in ('_pending_folder_write', '_pending_contact_write', '_pending_contact_profile_write'):
            row = getattr(self, name, None)
            if row and row.get('owner') == context.owner_id:
                return True
        report = self.store.conversation_report_journal(context.owner_id, 2, context.chat_id)
        return bool(report and report.get('status') != 'confirmed')

    def _owner_transfer_allowed(self, context, cached, *, successor_id=None, leaving=False):
        raw = cached[2]
        cap = self._lifecycle_capabilities(cached)
        if (not _integer(context.owner_id, 64) or context.owner_id <= 0 or context.peer_id is not None
                or not cap['supported'] or cap['kind'] not in ('group', 'channel') or cap['service']
                or raw.get('type') not in ('CHAT', 'CHANNEL') or raw.get('status') not in ('ACTIVE', 'BLOCKED')
                or not cap['participant'] or 'leave' not in cap['more']):
            raise ValueError('Передача прав и выход сейчас недоступны')
        if leaving:
            if cap['owner'] or raw.get('owner') != successor_id:
                raise ValueError('Новый владелец или ваша роль изменились')
        elif (not cap['owner'] or type(raw.get('participantsCount')) is not int
              or raw['participantsCount'] <= 1):
            raise ValueError('MAX не подтвердил владельца и состав чата')
        if successor_id is not None:
            if not _integer(successor_id, 64) or successor_id <= 0 or successor_id == context.owner_id:
                raise ValueError('Выберите другого подтверждённого участника')
            if not leaving and raw['type'] == 'CHAT' and successor_id not in (_admin_ids(raw.get('adminParticipants', {})) or ()):
                raise ValueError('Преемник больше не является администратором группы')

    def owner_transfer_available(self, context):
        try:
            with self._metadata_lock, self.store.account_scope(context.owner_id):
                self._owner_transfer_allowed(context, self._require_conversation(context))
                record = self.store.conversation_action_journal(context.owner_id, context.chat_id)
                if record and record.get('action') == 'owner_transfer':
                    record = self._owner_transfer_record(context)
                settled = bool(record and record.get('action') == 'owner_transfer' and record.get('stage') == 'complete')
                return ((not record or settled) and not self._owner_transfer_busy(context)
                        and callable(getattr(context.source_client, 'transfer_conversation_owner', None))
                        and callable(getattr(context.source_client, 'conversation_transfer_members', None)))
        except (ValueError, TypeError, AttributeError):
            return False

    def _owner_transfer_read(self, context, record=None):
        previous = self._require_conversation(context)[2]
        raw = lifecycle_readback(context.source_client.chat_info(
            [context.chat_id], expected_session=context.session_context), context.chat_id)
        with self._metadata_lock, self.store.account_scope(context.owner_id):
            current = self._require_conversation(context)[2]
            if (raw is None or raw['type'] != previous['type'] or raw['type'] not in ('CHAT', 'CHANNEL')
                    or raw['type'] != current['type']
                    or chat_sync_time(raw) < max(chat_sync_time(current), record['cutoff'] if record else 0)):
                raise ValueError('MAX не подтвердил свежие сведения выбранного чата')
            if record is not None and self.store.conversation_action_journal(context.owner_id, context.chat_id) != record:
                raise ValueError('Ожидающее действие изменилось')
            self._remember_chat_info({'chats': [raw]}, my_id=context.owner_id)
            self._require_conversation(context)
            return copy.deepcopy(raw)

    def _owner_transfer_members(self, context, raw, query, marker):
        member_type = 'ADMIN' if raw['type'] == 'CHAT' else 'MEMBER'
        if member_type == 'ADMIN' and marker is not None:
            raise ValueError('Постраничный поиск администраторов не подтверждён')
        result = context.source_client.conversation_transfer_members(context.chat_id, member_type,
            query=query, marker=marker, expected_session=context.session_context)
        self._require_conversation(context)
        if (not isinstance(result, dict) or not isinstance(result.get('contacts'), list)
                or len(result['contacts']) > 1000
                or type(result.get('complete')) is not bool
                or result.get('marker') is not None and (not _number(result['marker']) or result['marker'] < 0)
                or result['complete'] != (result.get('marker') is None)):
            raise ValueError('MAX не подтвердил список участников')
        contacts, seen = [], set()
        admins = _admin_ids(raw.get('adminParticipants', {}))
        if admins is None:
            raise ValueError('MAX не подтвердил администраторов')
        for contact in result['contacts']:
            parsed = _contact(contact)
            if parsed is None or parsed['id'] <= 0 or parsed['id'] in seen:
                raise ValueError('MAX вернул неоднозначного участника')
            seen.add(parsed['id'])
            name = _contact_name(parsed).strip()
            if (parsed['id'] != context.owner_id and name
                    and (raw['type'] == 'CHANNEL' or parsed['id'] in admins)):
                contacts.append({'id': parsed['id'], 'name': name,
                    'role': 'ADMIN' if parsed['id'] in admins else 'MEMBER'})
        labels = [(item['name'].casefold(), item['role']) for item in contacts]
        for item, label in zip(contacts, labels):
            item['ambiguous'] = labels.count(label) > 1
        return {**result, 'contacts': contacts}

    def get_owner_transfer_candidates(self, context, *, query='', marker=None):
        if (not isinstance(query, str) or len(query) > 1024 or
                marker is not None and (not _number(marker) or marker < 0) or query and marker is not None):
            raise ValueError('Некорректная страница участников')
        if not self.owner_transfer_available(context):
            raise ValueError('Выбор нового владельца сейчас недоступен')
        raw = self._owner_transfer_read(context)
        self._owner_transfer_allowed(context, self._require_conversation(context))
        page = self._owner_transfer_members(context, raw, query, marker)
        with self._metadata_lock, self.store.account_scope(context.owner_id):
            current = self._require_conversation(context)
            self._owner_transfer_allowed(context, current)
            if current[2] != raw or not self.owner_transfer_available(context):
                raise ValueError('Состав или состояние чата изменились')
            tickets = getattr(self, '_owner_transfer_tickets', None)
            if tickets is None:
                self._owner_transfer_tickets = tickets = weakref.WeakSet()
            candidates = []
            for item in page['contacts']:
                ticket = OwnerTransferTicket(context, item['id'], item['name'], raw['type'],
                    query, marker, chat_sync_time(raw), uuid.uuid4().hex)
                tickets.add(ticket)
                candidates.append({**item, 'ticket': ticket})
            return {'context': context, 'chat_id': context.chat_id,
                'kind': 'group' if raw['type'] == 'CHAT' else 'channel', 'candidates': candidates,
                'marker': page['marker'] if not query and raw['type'] == 'CHANNEL' else None,
                'complete': bool(not query and page['complete'])}

    def _owner_transfer_ticket(self, context, ticket):
        self._require_conversation(context)
        if (not isinstance(ticket, OwnerTransferTicket) or ticket.context is not context
                or ticket not in getattr(self, '_owner_transfer_tickets', ())):
            raise ValueError('Выберите участника заново из подтверждённого списка')

    def _owner_transfer_record(self, context):
        self._require_conversation(context)
        row = self.store.conversation_action_journal(context.owner_id, context.chat_id)
        if not row or row.get('action') != 'owner_transfer':
            return None
        if (row.get('owner_id') != context.owner_id or row.get('chat_id') != context.chat_id
                or row.get('peer_id') is not None or context.peer_id is not None
                or row.get('chat_type') not in ('CHAT', 'CHANNEL')
                or not _integer(row.get('successor_id'), 64) or row['successor_id'] <= 0
                or row['successor_id'] == context.owner_id or not isinstance(row.get('successor_name'), str)
                or not row['successor_name'].strip()
                or not all(isinstance(row.get(k), str) and row[k] for k in ('operation_id', 'session_nonce'))
                or row.get('stage') not in ('transfer_pending', 'transferred', 'leave_pending', 'complete')
                or any(type(row.get(k)) is not bool for k in
                       ('transfer_confirmed', 'leave_confirmed', 'transfer_acknowledged', 'leave_acknowledged'))
                or row.get('leave_outcome') not in ('not_sent', 'unknown', 'rejected', 'not_started', 'acknowledged')
                or row['stage'] != 'transfer_pending' and not row['transfer_confirmed']
                or row['stage'] == 'transfer_pending' and row['transfer_confirmed']
                or row['stage'] != 'complete' and row['leave_confirmed']
                or row['stage'] == 'complete' and not row['leave_confirmed']
                or row['leave_acknowledged'] != (row['leave_outcome'] == 'acknowledged')
                or row['stage'] in ('transfer_pending', 'transferred') and row['leave_outcome'] in ('unknown', 'acknowledged')
                or row['stage'] in ('leave_pending', 'complete') and row['leave_outcome'] not in ('unknown', 'acknowledged')
                or not _number(row.get('cutoff')) or row['cutoff'] < 0):
            raise ValueError('Журнал передачи прав повреждён. Повтор не выполнен')
        return row

    def _owner_transfer_cas(self, context, record, *, departure_cutoff=None, **changes):
        with self._metadata_lock, self.store.account_scope(context.owner_id):
            self._require_conversation(context)
            updated = {**record, **changes}
            if not self.store.compare_conversation_action(context.owner_id, context.chat_id,
                    record, updated, departure_cutoff=departure_cutoff):
                raise ValueError('Ожидающая передача прав изменилась')
            return updated

    def _owner_transfer_status(self, context, record, *, accepted=False):
        cached = self._require_conversation(context)
        if self.store.conversation_action_journal(context.owner_id, context.chat_id) != record:
            raise ValueError('Ожидающая передача прав изменилась')
        settled = record['stage'] == 'complete' and record['transfer_confirmed'] and record['leave_confirmed']
        complete = bool(settled and cached[2].get('owner') == record['successor_id']
                        and self._leave_readback_confirmed(cached[2], record, context.owner_id)
                        and self.store.conversation_departure(context.chat_id) is not None)
        continuation = False
        if (record['stage'] == 'transferred' and record['leave_outcome'] in ('not_sent', 'rejected', 'not_started')
                and (context.owner_id, context.chat_id) not in getattr(self, '_lifecycle_inflight', set())):
            try:
                self._owner_transfer_allowed(context, cached, successor_id=record['successor_id'], leaving=True)
                continuation = not self._owner_transfer_busy(context)
            except ValueError:
                pass
        status = ('Права переданы, выход из чата подтверждён.' if complete else
                  'Ранее права были переданы и выход подтверждён. Текущее состояние чата изменилось.' if settled else
                  'Выход из чата пока не подтверждён. Проверьте состояние; повтор не отправлен.'
                  if record['stage'] == 'leave_pending' else
                  'Права переданы. Выход не завершён; его можно продолжить отдельным действием.'
                  if continuation else
                  'Передача прав была подтверждена. Состояние чата изменилось; продолжение выхода пока недоступно. Проверьте состояние.'
                  if record['transfer_confirmed'] else
                  'Передача прав пока не подтверждена. Выход не отправлен. Проверьте состояние.')
        return {'context': context, 'chat_id': context.chat_id, 'peer_id': None,
            'action': 'leave', 'lifecycle_kind': 'owner_transfer', 'stage': record['stage'],
            'successor_id': record['successor_id'], 'successor_name': record['successor_name'],
            'transfer_confirmed': record['transfer_confirmed'], 'leave_confirmed': record['leave_confirmed'],
            'transfer_acknowledged': record['transfer_acknowledged'], 'leave_acknowledged': record['leave_acknowledged'],
            'accepted': accepted, 'confirmed': complete, 'pending': not settled, 'historical': settled and not complete,
            'partial': record['transfer_confirmed'] and not settled, 'can_continue_leave': continuation,
            'action_status': status}

    def _observe_owner_transfer(self, context, record):
        raw = self._owner_transfer_read(context, record)
        if raw['type'] != record['chat_type'] or raw['owner'] != record['successor_id']:
            return record
        if record['stage'] == 'transfer_pending':
            return self._owner_transfer_cas(context, record, stage='transferred', transfer_confirmed=True,
                                            cutoff=chat_sync_time(raw))
        if record['stage'] == 'leave_pending' and self._leave_readback_confirmed(raw, record, context.owner_id):
            with self._metadata_lock, self.store.account_scope(context.owner_id):
                if self._require_conversation(context)[2] != raw:
                    raise ValueError('Состояние чата изменилось после проверки')
                record = self._owner_transfer_cas(context, record, stage='complete', leave_confirmed=True,
                                                 departure_cutoff=chat_sync_time(raw))
                self._chat_fetch_ts.pop(context.chat_id, None)
                self._chat_fetch_anchor.pop(context.chat_id, None)
            if self.on_chat_changed:
                self.on_chat_changed(context.chat_id)
        return record

    def _refresh_owner_transfer(self, context, record):
        record = self._observe_owner_transfer(context, record)
        return self._owner_transfer_status(context, record)

    def _dispatch_owner_leave(self, context, record):
        try:
            raw = self._owner_transfer_read(context, record)
            with self._metadata_lock, self.store.account_scope(context.owner_id):
                cached = self._require_conversation(context)
                self._owner_transfer_allowed(context, cached, successor_id=record['successor_id'], leaving=True)
                if (cached[2] != raw or raw['type'] != record['chat_type'] or self._owner_transfer_busy(context)
                        or record['session_nonce'] != self._owner_transfer_nonce(context)):
                    raise ValueError('Состояние чата или сессия изменились')
                record = self._owner_transfer_cas(context, record, stage='leave_pending',
                    leave_outcome='unknown', cutoff=chat_sync_time(raw))
        except Exception:
            self._require_conversation(context)
            return self._owner_transfer_status(context, record)
        try:
            self._require_conversation(context)
            ack = context.source_client.leave_conversation(context.chat_id, expected_session=context.session_context)
            parse_leave_ack(ack)
            record = self._owner_transfer_cas(context, record, leave_acknowledged=True, leave_outcome='acknowledged')
        except MaxWriteOutcomeUnknown:
            raise
        except (MaxError, MaxWriteNotStarted) as error:
            record = self._owner_transfer_cas(context, record, stage='transferred',
                leave_outcome='not_started' if isinstance(error, MaxWriteNotStarted) else 'rejected')
            return self._owner_transfer_status(context, record)
        except Exception:
            raise MaxWriteOutcomeUnknown('Выход из чата не подтверждён. Повтор не выполнен.') from None
        try:
            record = self._observe_owner_transfer(context, record)
        except Exception:
            self._require_conversation(context)
        return self._owner_transfer_status(context, record, accepted=True)

    def transfer_conversation_owner_and_leave(self, context, ticket):
        self._owner_transfer_ticket(context, ticket)
        if not self.owner_transfer_available(context):
            raise ValueError('Передача прав сейчас недоступна')
        raw = self._owner_transfer_read(context)
        self._owner_transfer_allowed(context, self._require_conversation(context), successor_id=ticket.successor_id)
        page = self._owner_transfer_members(context, raw, ticket.query, ticket.marker)
        if not any(item['id'] == ticket.successor_id and item['name'] == ticket.successor_name
                   and not item['ambiguous'] for item in page['contacts']):
            raise ValueError('Выбранный участник изменился. Выберите его заново')
        raw = self._owner_transfer_read(context)
        with self._metadata_lock, self.store.account_scope(context.owner_id):
            self._owner_transfer_ticket(context, ticket)
            self._owner_transfer_allowed(context, self._require_conversation(context), successor_id=ticket.successor_id)
            if (raw['type'] != ticket.chat_type or chat_sync_time(raw) < ticket.cutoff
                    or not self.owner_transfer_available(context)):
                raise ValueError('Состояние чата изменилось')
            record = {'action': 'owner_transfer', 'owner_id': context.owner_id, 'chat_id': context.chat_id,
                'peer_id': None, 'chat_type': raw['type'], 'successor_id': ticket.successor_id,
                'successor_name': ticket.successor_name, 'session_nonce': self._owner_transfer_nonce(context),
                'stage': 'transfer_pending', 'cutoff': chat_sync_time(raw), 'created': time.time_ns() // 1_000_000,
                'transfer_confirmed': False, 'leave_confirmed': False,
                'transfer_acknowledged': False, 'leave_acknowledged': False, 'leave_outcome': 'not_sent'}
            self._reserve_lifecycle(context, record)
        try:
            try:
                with self._metadata_lock, self.store.account_scope(context.owner_id):
                    cached = self._require_conversation(context)
                    self._owner_transfer_allowed(context, cached, successor_id=ticket.successor_id)
                    if (cached[2] != raw or self._owner_transfer_busy(context)
                            or record['session_nonce'] != self._owner_transfer_nonce(context)):
                        raise ValueError('Сведения изменились после выбора преемника')
                    record = self._owner_transfer_cas(context, record, transfer_dispatched=True)
                ack = context.source_client.transfer_conversation_owner(context.chat_id, ticket.successor_id,
                    expected_session=context.session_context)
                parsed = parse_owner_transfer_ack({'chat': ack}, context.chat_id, ticket.successor_id)
                if parsed['type'] != record['chat_type']:
                    raise ValueError('MAX вернул другой тип чата')
                record = self._owner_transfer_cas(context, record, transfer_acknowledged=True)
            except MaxWriteOutcomeUnknown:
                raise
            except (MaxError, MaxWriteNotStarted):
                with self._metadata_lock, self.store.account_scope(context.owner_id):
                    self._require_conversation(context)
                    self.store.compare_conversation_action(context.owner_id, context.chat_id, record, None)
                raise
            except Exception:
                if not record.get('transfer_dispatched'):
                    with self._metadata_lock, self.store.account_scope(context.owner_id):
                        self._require_conversation(context)
                        self.store.compare_conversation_action(context.owner_id, context.chat_id, record, None)
                    raise
                raise MaxWriteOutcomeUnknown('Передача прав не подтверждена. Выход не отправлен; повтор не выполнен.') from None
            try:
                record = self._observe_owner_transfer(context, record)
            except Exception:
                self._require_conversation(context)
            if record['stage'] == 'transferred':
                self._dispatch_owner_leave(context, record)
        finally:
            self._lifecycle_finished_sending(context)
        return self._owner_transfer_status(context, self._owner_transfer_record(context), accepted=True)

    def continue_conversation_owner_leave(self, context):
        with self._metadata_lock, self.store.account_scope(context.owner_id):
            record = self._owner_transfer_record(context)
            if not record or not self._owner_transfer_status(context, record)['can_continue_leave']:
                raise ValueError('Продолжение выхода недоступно. Проверьте состояние')
            record = self._owner_transfer_cas(context, record, session_nonce=self._owner_transfer_nonce(context))
            self._lifecycle_inflight = getattr(self, '_lifecycle_inflight', set())
            self._lifecycle_inflight.add((context.owner_id, context.chat_id))
        try:
            result = self._dispatch_owner_leave(context, record)
        finally:
            self._lifecycle_finished_sending(context)
        return self._owner_transfer_status(context, self._owner_transfer_record(context), accepted=result['accepted'])
