"""Sequential bot stop/delete, durable partial outcomes and read-only recovery.

119 and 52 are separate VOID writes. No retry, parallel dispatch or startBot
rollback is permitted. See research/BOT_DELETE_CORE_20261106.md.
"""
from __future__ import annotations

import time
import uuid

from ..protocol.client import MaxError, MaxWriteNotStarted, MaxWriteOutcomeUnknown
from ..protocol.conversation_actions_contracts import parse_action_contact_readback, parse_stop_bot_ack
from ..protocol.conversation_lifecycle_contracts import chat_sync_time, lifecycle_readback, parse_lifecycle_ack
from .conversation_info_service import _peer


UNKNOWN = 'Результат действия с ботом неизвестен. Проверьте состояние; повтор не отправлен.'


class BotDeleteServiceMixin:
    def bot_delete_available(self, context):
        """Cached UI eligibility; dispatch still rereads and validates the target."""
        try:
            with self._metadata_lock, self.store.account_scope(context.owner_id):
                cached = self._require_conversation(context)
                self._bot_delete_allowed(context, cached)
                current = self.store.conversation_action_journal(context.owner_id, context.chat_id)
                if current and not (current.get('action') == 'bot_delete' and current.get('stage') == 'complete'):
                    return False
                return (not self._bot_delete_busy(context)
                        and callable(getattr(context.source_client, 'stop_bot', None))
                        and callable(getattr(context.source_client, 'change_conversation_lifecycle', None)))
        except (ValueError, AttributeError, TypeError):
            return False

    def _bot_delete_session_nonce(self, context):
        # An opaque local marker, not credentials/device IDs. New transport or
        # process needs a new explicit continuation, never an automatic resume.
        sessions = getattr(self, '_bot_delete_sessions', None)
        if sessions is None:
            self._bot_delete_sessions = sessions = {}
        return sessions.setdefault(context.session_context, uuid.uuid4().hex)

    def _bot_delete_busy(self, context, *, operation_id=None):
        peer_record = self.store.bot_delete_for_peer(context.owner_id, context.peer_id)
        if peer_record and peer_record.get('operation_id') != operation_id:
            return True
        adapter = getattr(self, '_conversation_actions_state', None)
        if adapter is not None:
            with adapter._lock:
                for (owner, chat_id, family), pending in adapter._pending.items():
                    if owner == context.owner_id and (chat_id == context.chat_id or
                            family == 'stop_bot' and getattr(pending, 'peer_id', None) == context.peer_id):
                        return True
        for name in ('_pending_folder_write', '_pending_contact_write', '_pending_contact_profile_write'):
            pending = getattr(self, name, None)
            if pending and pending.get('owner') == context.owner_id:
                return True
        report = self.store.conversation_report_journal(context.owner_id, 7, context.peer_id)
        return bool(report and report.get('status') != 'confirmed')

    def _bot_delete_allowed(self, context, cached, *, suspended=False):
        raw, peer, contact = cached[2], cached[3], cached[4]
        cap = self._lifecycle_capabilities(cached)
        if (type(context.owner_id) is not int or context.owner_id <= 0
                or type(peer) is not int or peer <= 0 or peer == context.owner_id
                or context.chat_id == 0 or contact.get('id') != peer
                or raw.get('type') != 'DIALOG' or raw.get('hasBots') is not True
                or raw.get('status') not in ('ACTIVE', 'HIDDEN', 'BLOCKED')
                or cap.get('kind') != 'bot' or not cap.get('supported') or cap.get('service')
                or 'delete' not in cap.get('more', ())):
            raise ValueError('Удаление этого бота сейчас недоступно')
        if suspended and raw.get('isSuspended') is not True:
            raise ValueError('MAX не подтвердил остановку бота')

    def _bot_delete_record(self, context):
        self._require_conversation(context)
        record = self.store.conversation_action_journal(context.owner_id, context.chat_id)
        if record and record.get('action') == 'bot_delete':
            if (record.get('owner_id') != context.owner_id or record.get('chat_id') != context.chat_id
                    or record.get('peer_id') != context.peer_id or not isinstance(record.get('operation_id'), str)
                    or not record['operation_id'] or not isinstance(record.get('session_nonce'), str)
                    or not record['session_nonce'] or record.get('stage') not in
                        ('stop_pending', 'stopped', 'delete_pending', 'complete')
                    or any(type(record.get(key)) is not bool for key in
                           ('stop_confirmed', 'stop_acknowledged', 'delete_confirmed', 'delete_acknowledged'))
                    or record.get('delete_outcome') not in ('not_sent', 'rejected', 'not_started', 'unknown', 'acknowledged')
                    or record.get('stage') == 'complete' and not
                        (record['stop_confirmed'] and record['delete_confirmed'])):
                raise ValueError('Собеседник ожидающего удаления изменился')
            chat_sync_time({'created': record.get('cutoff'), 'lastEventTime': record.get('cutoff')})
            return record
        return None

    def _bot_delete_status(self, context, record, *, accepted=False):
        cached = self._require_conversation(context)
        if not record or self.store.conversation_action_journal(context.owner_id, context.chat_id) != record:
            raise ValueError('Ожидающее удаление бота изменилось')
        stage = record['stage']
        complete = stage == 'complete' and record.get('stop_confirmed') is True and record.get('delete_confirmed') is True
        stopped = record.get('stop_confirmed') is True
        continuation = False
        if stage == 'stopped' and stopped and record.get('delete_outcome') in ('not_sent', 'rejected', 'not_started'):
            try:
                self._bot_delete_allowed(context, cached, suspended=True)
                continuation = not self._bot_delete_busy(context, operation_id=record['operation_id'])
            except ValueError:
                pass
        if (context.owner_id, context.chat_id) in getattr(self, '_lifecycle_inflight', set()):
            continuation = False
        if complete:
            status = 'Бот остановлен, чат удалён.'
        elif stopped and cached[2].get('isSuspended') is not True:
            status = 'Остановка была подтверждена, но состояние бота изменилось. Удаление чата не завершено.'
        elif stage == 'delete_pending':
            status = 'Бот остановлен. Удаление чата пока не подтверждено. Проверьте состояние; повтор не отправлен.'
        elif stopped:
            status = 'Бот остановлен; чат не удалён. Удаление можно продолжить отдельным действием.'
            if not continuation:
                status = 'Бот остановлен; удаление чата не завершено. Обновите сведения перед продолжением.'
        else:
            status = 'Остановка бота пока не подтверждена. Чат не удалён. Проверьте состояние; повтор не отправлен.'
        return {'context': context, 'chat_id': context.chat_id, 'peer_id': context.peer_id,
                'action': 'delete', 'lifecycle_kind': 'bot_delete', 'stage': stage,
                'accepted': accepted, 'confirmed': complete, 'pending': not complete,
                'stop_confirmed': stopped, 'delete_confirmed': record.get('delete_confirmed') is True,
                'stop_acknowledged': record.get('stop_acknowledged') is True,
                'delete_acknowledged': record.get('delete_acknowledged') is True,
                'partial': stopped and not complete, 'can_continue_delete': continuation,
                'action_status': status}

    def _bot_delete_cas(self, context, record, *, delete_cutoff=None, **changes):
        with self._metadata_lock, self.store.account_scope(context.owner_id):
            self._require_conversation(context)
            updated = {**record, **changes}
            if not self.store.compare_conversation_action(context.owner_id, context.chat_id, record, updated,
                                                         delete_cutoff=delete_cutoff):
                raise ValueError('Ожидающее удаление бота изменилось')
            return updated

    def _bot_delete_read(self, context, record, *, with_contact=False):
        cached = self._require_conversation(context)
        raw = lifecycle_readback(context.source_client.chat_info(
            [context.chat_id], expected_session=context.session_context), context.chat_id)
        self._require_conversation(context)
        if raw is None:
            return None  # Bounded absence cannot prove either bot effect.
        if (raw.get('type') != 'DIALOG' or raw.get('hasBots') is not True
                or _peer(cached[1], raw, context.owner_id) != context.peer_id):
            raise ValueError('MAX вернул другой чат или тип собеседника')
        contact = None
        if with_contact:
            contact = parse_action_contact_readback(context.source_client.contact_info(
                [context.peer_id], expected_session=context.session_context), context.peer_id)
        with self._metadata_lock, self.store.account_scope(context.owner_id):
            current = self._require_conversation(context)
            if self.store.conversation_action_journal(context.owner_id, context.chat_id) != record:
                raise ValueError('Ожидающее удаление бота изменилось')
            if chat_sync_time(raw) < max(record['cutoff'], chat_sync_time(current[2])):
                raise ValueError('MAX вернул устаревшее состояние бота')
            if contact is not None and contact['updateTime'] < current[4].get('updateTime', -1):
                raise ValueError('MAX вернул устаревший профиль бота')
            self._remember_chat_info({'chats': [raw]}, my_id=context.owner_id)
            if contact is not None:
                self._remember_contact_info({'contacts': [contact]})
            self._require_conversation(context)
        return raw

    def _observe_bot_delete(self, context, record):
        """Read state only. Never turn a stop read into dispatch of delete52."""
        raw = self._bot_delete_read(context, record)
        if raw is None:
            return record
        if record['stage'] == 'stop_pending':
            # Explicit isSuspended is required; lastEventTime==0 is only a WEB
            # display default and cannot acknowledge a sent 119.
            if raw.get('isSuspended') is True:
                return self._bot_delete_cas(context, record, stage='stopped', stop_confirmed=True,
                                            stop_observed_sync=chat_sync_time(raw))
        elif record['stage'] == 'delete_pending':
            # A REMOVED DTO/52 ACK can never retroactively prove stop119.
            if (record.get('stop_confirmed') is True and raw.get('isSuspended') is True
                    and raw.get('status') in ('REMOVED', 'CLOSED')):
                with self._metadata_lock, self.store.account_scope(context.owner_id):
                    current = self._require_conversation(context)[2]
                    if chat_sync_time(current) > chat_sync_time(raw):
                        return record
                    complete = self._bot_delete_cas(context, record, stage='complete', delete_confirmed=True,
                                                     delete_cutoff=record['cutoff'])
                    self._chat_fetch_ts.pop(context.chat_id, None)
                    self._chat_fetch_anchor.pop(context.chat_id, None)
                if self.on_chat_changed:
                    self.on_chat_changed(context.chat_id)
                return complete
        return record

    def _refresh_bot_delete(self, context, record):
        if record['stage'] != 'complete':
            record = self._observe_bot_delete(context, record)
        return self._bot_delete_status(context, record)

    def _dispatch_bot_delete52(self, context, record):
        # A distinct fresh 48/32 after the stop observation is mandatory.
        try:
            raw = self._bot_delete_read(context, record, with_contact=True)
            if raw is None:
                raise ValueError('MAX не подтвердил текущий чат бота')
            with self._metadata_lock, self.store.account_scope(context.owner_id):
                current = self._require_conversation(context)
                self._bot_delete_allowed(context, current, suspended=True)
                if current[2] != raw:
                    raise ValueError('Сведения бота изменились после проверки')
                if record['session_nonce'] != self._bot_delete_session_nonce(context):
                    raise ValueError('Для новой сессии нужно явно продолжить удаление')
                if self._bot_delete_busy(context, operation_id=record['operation_id']):
                    raise ValueError('Другое действие с ботом ещё не завершено')
                record = self._bot_delete_cas(context, record, stage='delete_pending',
                    delete_outcome='unknown', cutoff=chat_sync_time(current[2]),
                    session_nonce=self._bot_delete_session_nonce(context))
                self._require_conversation(context)
        except Exception:
            self._require_conversation(context)
            current_record = self._bot_delete_record(context)
            if current_record != record:
                raise ValueError('Ожидающее удаление бота изменилось') from None
            return self._bot_delete_status(context, current_record)
        try:
            self._require_conversation(context)
            ack = context.source_client.change_conversation_lifecycle(
                'delete', context.chat_id, record['cutoff'], False, expected_session=context.session_context)
            parse_lifecycle_ack(ack)
            record = self._bot_delete_cas(context, record, delete_acknowledged=True, delete_outcome='acknowledged')
        except MaxWriteOutcomeUnknown:
            raise
        except (MaxError, MaxWriteNotStarted) as error:
            record = self._bot_delete_cas(context, record, stage='stopped',
                delete_outcome='not_started' if isinstance(error, MaxWriteNotStarted) else 'rejected')
            return self._bot_delete_status(context, record)
        except Exception:
            raise MaxWriteOutcomeUnknown(UNKNOWN) from None
        try:
            record = self._observe_bot_delete(context, record)
        except Exception:
            self._require_conversation(context)
        return self._bot_delete_status(context, record, accepted=True)

    def _delete_bot_conversation(self, context, *, for_all=False):
        result = self._run_bot_conversation_delete(context, for_all=for_all)
        record = self._bot_delete_record(context)
        return self._bot_delete_status(context, record,
            accepted=result.get('accepted') is True or record.get('stop_acknowledged') is True)

    def _run_bot_conversation_delete(self, context, *, for_all=False):
        if for_all is not False:
            raise ValueError('Чат с ботом удаляется только у вас')
        with self._metadata_lock, self.store.account_scope(context.owner_id):
            cached = self._require_conversation(context)
            self._bot_delete_allowed(context, cached)
            prior = self.store.conversation_action_journal(context.owner_id, context.chat_id)
            if prior and not (prior.get('action') == 'bot_delete' and prior.get('stage') == 'complete'):
                raise ValueError('Проверьте состояние предыдущего действия; повтор не отправлен')
            if self._bot_delete_busy(context):
                raise ValueError('Другое действие с ботом ещё не завершено')
        cached = self.refresh_conversation_menu_metadata(context)
        with self._metadata_lock, self.store.account_scope(context.owner_id):
            cached = self._require_conversation(context)
            self._bot_delete_allowed(context, cached)
            if self._bot_delete_busy(context):
                raise ValueError('Другое действие с ботом ещё не завершено')
            record = {'action': 'bot_delete', 'owner_id': context.owner_id, 'chat_id': context.chat_id,
                'peer_id': context.peer_id, 'session_nonce': self._bot_delete_session_nonce(context),
                'stage': 'stop_pending', 'cutoff': chat_sync_time(cached[2]), 'for_all': False,
                'created': time.time_ns() // 1_000_000, 'stop_confirmed': False,
                'stop_acknowledged': False, 'delete_confirmed': False, 'delete_acknowledged': False,
                'delete_outcome': 'not_sent'}
            self._reserve_lifecycle(context, record)
        try:
            # Already suspended is a state-confirmed no-op, never a claimed119.
            if cached[2].get('isSuspended') is True:
                record = self._bot_delete_cas(context, record, stage='stopped', stop_confirmed=True,
                                            stop_preexisting=True, stop_observed_sync=chat_sync_time(cached[2]))
            else:
                try:
                    with self._metadata_lock, self.store.account_scope(context.owner_id):
                        current = self._require_conversation(context)
                        self._bot_delete_allowed(context, current)
                        if current[2] != cached[2] or chat_sync_time(current[2]) != record['cutoff']:
                            raise ValueError('Сведения бота изменились после проверки')
                        if record['session_nonce'] != self._bot_delete_session_nonce(context):
                            raise ValueError('Сессия удаления бота изменилась')
                        if self._bot_delete_busy(context, operation_id=record['operation_id']):
                            raise ValueError('Другое действие с ботом ещё не завершено')
                        record = self._bot_delete_cas(context, record, stop_dispatched=True)
                    ack = context.source_client.stop_bot(context.peer_id, expected_session=context.session_context)
                    parse_stop_bot_ack(ack)
                    record = self._bot_delete_cas(context, record, stop_acknowledged=True)
                except MaxWriteOutcomeUnknown:
                    raise
                except (MaxError, MaxWriteNotStarted):
                    with self._metadata_lock, self.store.account_scope(context.owner_id):
                        self._require_conversation(context)
                        self.store.compare_conversation_action(context.owner_id, context.chat_id, record, None)
                    raise
                except Exception:
                    if not record.get('stop_dispatched'):
                        with self._metadata_lock, self.store.account_scope(context.owner_id):
                            self._require_conversation(context)
                            self.store.compare_conversation_action(context.owner_id, context.chat_id, record, None)
                        raise
                    raise MaxWriteOutcomeUnknown(UNKNOWN) from None
                try:
                    record = self._observe_bot_delete(context, record)
                except Exception:
                    self._require_conversation(context)
                if record['stage'] != 'stopped':
                    return self._bot_delete_status(context, record, accepted=True)
            return self._dispatch_bot_delete52(context, record)
        finally:
            self._lifecycle_finished_sending(context)

    def continue_bot_conversation_delete(self, context):
        """Explicit continuation sends only52; unknown52 can never be retried."""
        with self._metadata_lock, self.store.account_scope(context.owner_id):
            record = self._bot_delete_record(context)
            if not record or not self._bot_delete_status(context, record)['can_continue_delete']:
                raise ValueError('Продолжение удаления недоступно. Проверьте состояние')
            record = self._bot_delete_cas(context, record, session_nonce=self._bot_delete_session_nonce(context))
            self._lifecycle_inflight = getattr(self, '_lifecycle_inflight', set())
            self._lifecycle_inflight.add((context.owner_id, context.chat_id))
        try:
            result = self._dispatch_bot_delete52(context, record)
        finally:
            self._lifecycle_finished_sending(context)
        return self._bot_delete_status(context, self._bot_delete_record(context), accepted=result.get('accepted') is True)
