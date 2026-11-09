"""Explicit channel/bot complaints, separate from conversation lifecycle.

No readback/undo exists in the pinned producer. A lost result stays reserved;
neither reconnect, a new modal nor another chat alias authorizes a resend.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import copy
import time
import uuid

from ..protocol.client import MaxError, MaxWriteNotStarted, MaxWriteOutcomeUnknown
from ..protocol.conversation_report_contracts import (
    build_conversation_report, parse_report_reasons, parse_report_ack,
)


@dataclass(frozen=True, eq=False)
class ConversationReportTicket:
    context: object = field(repr=False)
    type_id: int
    target_id: int
    reasons: tuple
    sync: int | float
    nonce: str = field(repr=False)


class ConversationReportServiceMixin:
    @staticmethod
    def _report_target(cached):
        binding, _, raw, peer, contact, _, config = cached
        from .conversation_action_capabilities import conversation_action_capabilities
        cap = conversation_action_capabilities(raw, contact, binding[0],
                                                config.get('server', {}), peer_id=peer)
        if cap['kind'] == 'channel':
            return 2, raw['id'], cap
        if cap['kind'] == 'bot' and type(peer) is int and contact.get('id') == peer:
            return 7, peer, cap
        return None, None, cap

    def conversation_report_status(self, context):
        cached = self._require_conversation(context)
        type_id, target_id, cap = self._report_target(cached)
        record = (self.store.conversation_report_journal(context.owner_id, type_id, target_id)
                  if type_id is not None else None)
        reported = bool(record and record['status'] == 'confirmed')
        pending = bool(record and not reported)
        # ACTIVE-only is a local policy; the raw capability getter is broader.
        allowed = (cap['supported'] and 'report' in cap['more']
                   and cached[2].get('status') == 'ACTIVE' and type_id is not None)
        if type_id == 7 and self.store.bot_delete_for_peer(context.owner_id, target_id):
            allowed = False
        lifecycle = self.store.conversation_action_journal(context.owner_id, context.chat_id)
        if lifecycle and lifecycle.get('action') in ('owner_transfer', 'conversation_edit') and lifecycle.get('stage') != 'complete':
            allowed = False
        self._require_conversation(context)
        return {'context': context, 'chat_id': context.chat_id, 'type_id': type_id,
                'target_id': target_id, 'pending': pending, 'already_reported': reported,
                'available': bool(allowed and not record),
                'status': 'reported' if reported else 'pending' if pending else
                          'ready' if allowed else 'unavailable'}

    def _require_report_target(self, context, type_id=None, target_id=None):
        state = self.conversation_report_status(context)
        if not state['available']:
            if state['pending']:
                raise ValueError('Результат отправки не подтверждён. Повторная отправка остановлена.')
            if state['already_reported']:
                raise ValueError('Жалоба на этот канал или бота уже отправлена.')
            raise ValueError('Жалоба сейчас недоступна для выбранного чата.')
        if type_id is not None and (state['type_id'], state['target_id']) != (type_id, target_id):
            raise ValueError('Получатель жалобы изменился. Откройте окно заново.')
        return state

    def _read_report_catalog(self, context):
        self._require_conversation(context)
        with self._metadata_lock:
            previous = getattr(self, '_conversation_report_catalog', None)
            if not previous or previous[0] is not context.session_context or previous[1] is not context.source_client:
                previous = None
            sync = previous[2]['complainSync'] if previous else 0
        result = parse_report_reasons(context.source_client.conversation_report_reasons(
            sync, expected_session=context.session_context))
        self._require_conversation(context)
        if result['complainSync'] < sync:
            raise ValueError('MAX вернул устаревший список причин. Откройте окно заново.')
        with self._metadata_lock:
            self._require_conversation(context)
            current = getattr(self, '_conversation_report_catalog', None)
            if current and current[0] is context.session_context and current[1] is context.source_client:
                if current[2]['complainSync'] > result['complainSync']:
                    raise ValueError('Список причин изменился. Откройте окно заново.')
                previous = current
            if not result['complains']:
                # Original delta cache retains BOTH reasons and sync on empty.
                result = copy.deepcopy(previous[2]) if previous else {'complainSync': 0, 'complains': []}
            self._conversation_report_catalog = (context.session_context, context.source_client, copy.deepcopy(result))
            return result

    @staticmethod
    def _report_choices(catalog, type_id):
        group = next((g for g in catalog['complains'] if g['typeId'] == type_id), None)
        # Original submit requires truthy reasonId. A blank title is schema-valid
        # but is not an informed choice; do not invent a substitute label.
        return tuple((r['reasonId'], r['reasonTitle']) for r in group['reasons']
                     if r['reasonId'] and r['reasonTitle'].strip()) if group else ()

    def get_conversation_report_reasons(self, context):
        initial = self.conversation_report_status(context)
        if initial['pending'] or initial['already_reported']:
            return {**initial, 'reasons': [], 'ticket': None}
        self._require_report_target(context)
        self.refresh_conversation_menu_metadata(context)
        current = self.conversation_report_status(context)
        if current['pending'] or current['already_reported']:
            return {**current, 'reasons': [], 'ticket': None}
        state = self._require_report_target(context)
        catalog = self._read_report_catalog(context)
        current = self.conversation_report_status(context)
        if current['pending'] or current['already_reported']:
            return {**current, 'reasons': [], 'ticket': None}
        self._require_report_target(context, state['type_id'], state['target_id'])
        choices = self._report_choices(catalog, state['type_id'])
        ticket = ConversationReportTicket(context, state['type_id'], state['target_id'], choices,
                                          catalog['complainSync'], uuid.uuid4().hex)
        with self._metadata_lock:
            self._require_conversation(context)
            tickets = getattr(self, '_conversation_report_tickets', None)
            if tickets is None:
                self._conversation_report_tickets = tickets = {}
            tickets[(context.owner_id, state['type_id'], state['target_id'])] = ticket
        return {**state, 'reasons': [{'reason_id': identity, 'title': title} for identity, title in choices],
                'ticket': ticket}

    def _validate_report_ticket(self, context, reason_id, ticket):
        if not isinstance(ticket, ConversationReportTicket) or ticket.context is not context:
            raise ValueError('Откройте выбор причины заново.')
        build_conversation_report(ticket.type_id, ticket.target_id, reason_id)
        key = (context.owner_id, ticket.type_id, ticket.target_id)
        if getattr(self, '_conversation_report_tickets', {}).get(key) is not ticket:
            raise ValueError('Выбор причины устарел. Откройте окно заново.')
        choice = next((entry for entry in ticket.reasons if entry[0] == reason_id), None)
        if choice is None:
            raise ValueError('Выберите причину из подтверждённого списка MAX.')
        return choice

    def _require_current_report_reason(self, context, ticket, choice):
        current = getattr(self, '_conversation_report_catalog', None)
        if (not current or current[0] is not context.session_context or current[1] is not context.source_client
                or current[2]['complainSync'] < ticket.sync
                or choice not in self._report_choices(current[2], ticket.type_id)):
            raise ValueError('Список причин изменился. Откройте окно заново.')

    def submit_conversation_report(self, context, reason_id, *, ticket):
        self._require_conversation(context)
        choice = self._validate_report_ticket(context, reason_id, ticket)
        self._require_report_target(context, ticket.type_id, ticket.target_id)
        self.refresh_conversation_menu_metadata(context)
        self._require_report_target(context, ticket.type_id, ticket.target_id)
        catalog = self._read_report_catalog(context)
        if choice not in self._report_choices(catalog, ticket.type_id):
            raise ValueError('Причина жалобы изменилась. Откройте окно заново.')
        record = {'status': 'pending', 'operation_id': uuid.uuid4().hex,
                  'chat_id': context.chat_id, 'reason_id': reason_id,
                  'selection_id': ticket.nonce, 'created': time.time_ns() // 1_000_000}
        with self._metadata_lock, self.store.account_scope(context.owner_id):
            self._validate_report_ticket(context, reason_id, ticket)
            self._require_current_report_reason(context, ticket, choice)
            self._require_report_target(context, ticket.type_id, ticket.target_id)
            self.store.reserve_conversation_report(context.owner_id, ticket.type_id, ticket.target_id, record)
        started = False
        try:
            self._validate_report_ticket(context, reason_id, ticket)
            self._require_current_report_reason(context, ticket, choice)
            cached = self._require_conversation(context)
            actual_type, actual_target, cap = self._report_target(cached)
            if ((actual_type, actual_target) != (ticket.type_id, ticket.target_id)
                    or not cap['supported'] or 'report' not in cap['more']
                    or cached[2].get('status') != 'ACTIVE'):
                raise ValueError('Возможность пожаловаться изменилась. Откройте меню заново.')
            if self.store.conversation_report_journal(context.owner_id, ticket.type_id, ticket.target_id) != record:
                raise ValueError('Ожидающая жалоба изменилась.')
            started = True
            ack = parse_report_ack(context.source_client.report_conversation(
                ticket.type_id, ticket.target_id, reason_id, expected_session=context.session_context))
            if ack['success'] is not True:
                raise MaxError('MAX отклонил жалобу.')
            with self._metadata_lock, self.store.account_scope(context.owner_id):
                self._require_conversation(context)
                if self.store.conversation_report_journal(context.owner_id, ticket.type_id, ticket.target_id) != record:
                    raise ValueError('Ожидающая жалоба изменилась.')
                if not self.store.settle_conversation_report(context.owner_id, ticket.type_id,
                        ticket.target_id, record['operation_id'], confirmed=True):
                    raise ValueError('Ожидающая жалоба изменилась.')
        except MaxWriteOutcomeUnknown:
            raise
        except (MaxError, MaxWriteNotStarted):
            try:
                self._release_report(context, ticket, record)
            except Exception:
                raise MaxWriteOutcomeUnknown('Результат отправки не подтверждён. Повторная отправка остановлена.') from None
            raise
        except Exception:
            if not started:
                self._release_report(context, ticket, record)
                raise
            raise MaxWriteOutcomeUnknown('Результат отправки не подтверждён. Повторная отправка остановлена.') from None
        return {'context': context, 'chat_id': context.chat_id, 'type_id': ticket.type_id,
                'target_id': ticket.target_id, 'action': 'report', 'accepted': True,
                'confirmed': True, 'pending': False, 'already_reported': True}

    def _release_report(self, context, ticket, record):
        # A late refusal must never release a replacement/another actor's intent.
        with self._metadata_lock, self.store.account_scope(context.owner_id):
            self._require_conversation(context)
            self.store.settle_conversation_report(context.owner_id, ticket.type_id,
                ticket.target_id, record['operation_id'], confirmed=False)
