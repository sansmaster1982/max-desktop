"""Contact cards/actions bound to an actual contact, never to an invented chat.

WEB34 ADD/UPDATE/REMOVE/BLOCK/UNBLOCK, with one write and typed CONTACT_INFO32
readback. See research/CONTACT_PROFILE_CORE_20261104.md for source/limits.
"""
from __future__ import annotations

import copy
import json
import math
from dataclasses import dataclass, field

from ..protocol.client import AuthenticatedSession, MaxError, MaxWriteNotStarted, MaxWriteOutcomeUnknown
from ..protocol.contact_create_contracts import contact_name, created_contact
from ..protocol.folder_contracts import int64
from ..protocol.direct_dialog_contracts import direct_dialog_id, direct_dialog_record, direct_dialog_reply
from .models import Chat


UNKNOWN = ('MAX пока не подтвердил изменение контакта. Нажмите «Проверить состояние». '
           'Повторное изменение до подтверждения недоступно.')


@dataclass(frozen=True, eq=False)
class ContactContext:
    owner_id: int
    source_client: object = field(repr=False)
    session_context: AuthenticatedSession = field(repr=False)
    contact_id: int


@dataclass(frozen=True, eq=False)
class _ContactObservation:
    session: object = field(repr=False)
    canonical: dict = field(repr=False)


def _contact_action_fields(raw):
    """Conservative action state; names/phone and the model clock are not rights."""
    from .service import _has_contact_flag
    record = created_contact(raw)
    options = record.get('options', [])
    account_status = record.get('accountStatus', 0)
    if (not isinstance(options, list) or any(not isinstance(value, str) for value in options)
            or type(account_status) is not int or account_status not in (0, 1, 2)):
        raise ValueError('Не подтверждены признаки контакта')
    return {'flags': record.get('flags', 0), 'status': record.get('status'),
            'accountStatus': account_status, 'options': frozenset(options),
            'service': _has_contact_flag(record, 'SERVICE_ACCOUNT')}


def _names(raw):
    names = raw.get('names', [])
    names = [row for row in names if isinstance(row, dict)] if isinstance(names, list) else []
    custom = [row for row in names if row.get('type') == 'CUSTOM']
    if type(raw.get('flags')) is int and raw['flags'] & 512 and len(custom) == 1:
        names = custom
    row = names[0] if names else {}
    first, last = row.get('firstName', row.get('name', '')), row.get('lastName', '')
    return first if isinstance(first, str) else '', last if isinstance(last, str) else ''


def _readback(data, identity):
    rows = data.get('contacts') if isinstance(data, dict) else None
    if not isinstance(rows, list):
        raise ValueError('MAX не вернул сведения контакта')
    rows = [row for row in rows if isinstance(row, dict) and type(row.get('id')) is int
            and row['id'] == identity]
    if len(rows) != 1:
        raise ValueError('MAX не подтвердил выбранный контакт')
    return created_contact(rows[0])


def _dialog_phase(stage, operation):
    """Local diagnostic label only: no IDs, payloads or exception text."""
    try:
        return operation()
    except Exception as exc:
        exc.contact_dialog_stage = stage
        raise


def _contact_readback_shape(data, identity):
    """Fixed field/type vocabulary only; never retain an input value or name."""
    def kind(value):
        return {dict: 'object', list: 'array', str: 'string', int: 'integer',
                float: 'number', bool: 'boolean', type(None): 'null'}.get(type(value), 'other')
    result = {'envelope_type': kind(data)}
    rows = data.get('contacts') if isinstance(data, dict) else None
    result['contacts_type'] = kind(rows) if isinstance(data, dict) and 'contacts' in data else 'absent'
    matches = ([row for row in rows if isinstance(row, dict) and type(row.get('id')) is int
                and row['id'] == identity] if isinstance(rows, list) else [])
    result['target_matches'] = 'none' if not matches else 'one' if len(matches) == 1 else 'multiple'
    if len(matches) != 1:
        return result
    row = matches[0]
    validators = {
        'id': int64, 'updateTime': int64,
        'names': lambda value: isinstance(value, list),
        'status': lambda value: isinstance(value, str) and value in ('REMOVED', 'BLOCKED', 'HIDDEN'),
        'flags': lambda value: type(value) is int and -(2**31) <= value < 2**31,
        'phone': lambda value: type(value) in (int, float) and 0 < value < 10**15
                             and math.isfinite(value) and value == int(value),
    }
    result['fields'] = {key: {'type': kind(row[key]), 'valid': bool(check(row[key]))}
                       if key in row else {'type': 'absent'} for key, check in validators.items()}
    names = row.get('names')
    if isinstance(names, list):
        result['name_fields_valid'] = all(isinstance(name, dict)
            and all(key not in name or isinstance(name[key], str) for key in ('name', 'firstName', 'lastName'))
            and ('type' not in name or name['type'] in ('ONEME', 'CUSTOM')) for name in names)
    return result


def _matches(raw, record):
    action = record['action']
    if action == 'ADD':
        return bool(raw.get('flags', 0) & 512) and raw.get('status') not in ('REMOVED', 'HIDDEN')
    if action == 'UPDATE':
        custom = [row for row in raw['names'] if row.get('type') == 'CUSTOM']
        return (bool(raw.get('flags', 0) & 512) and len(custom) == 1
                and custom[0].get('firstName') == record['payload']['firstName']
                and custom[0].get('lastName', '') == record['payload'].get('lastName', ''))
    if action == 'REMOVE':
        # Absent from a bounded list is not proof. Require explicit removed state
        # and an explicitly supplied cleared membership bit on this actual ID.
        return raw.get('status') == 'REMOVED' and 'flags' in raw and not raw['flags'] & 512
    return (raw.get('status') == 'BLOCKED') == (action == 'BLOCK')


def _rename_fields(raw):
    record = created_contact(raw)
    return {**_contact_action_fields(record), 'phone': ('phone' in record, record.get('phone')),
            'names': record['names']}


def _confirmed_older_rename(raw, prior, pending, binding):
    """A typed UPDATE ACK and matching32 can confirm names without lowering clocks.

    The retained prewrite record distinguishes our old alias from a concurrent
    rename. Only names may be merged into a newer unchanged canonical record.
    No ACK, another session, or another contact action gets this exception.
    """
    if not (pending and pending.get('action') == 'UPDATE' and 'rename_ack' in pending):
        return None
    ack = created_contact(pending['rename_ack'])
    before = created_contact(pending.get('rename_before'))
    current = created_contact(prior)
    if any(row['id'] != raw['id'] for row in (ack, before, current)):
        raise ValueError('Изменился выбранный контакт')
    if raw['updateTime'] >= max(ack['updateTime'], current['updateTime']):
        return None  # Ordinary fresh32 confirmation remains unchanged.
    captured = pending.get('rename_binding')
    if (pending.get('owner') != binding[0] or pending.get('id') != raw['id']
            or pending.get('inflight') is not False or not isinstance(captured, tuple)
            or len(captured) != 3 or captured[0] != binding[0]
            or captured[1] is not binding[1] or captured[2] is not binding[2]):
        raise ValueError('Сессия подтверждения имени изменилась')
    expected, actual, previous, retained = map(_rename_fields, (ack, raw, before, current))
    if (expected != actual or not _matches(ack, pending) or not _matches(raw, pending)
            or ack.get('status') in ('REMOVED', 'HIDDEN')
            or any(expected[key] != previous[key] or expected[key] != retained[key]
                   for key in expected if key != 'names')
            or current['updateTime'] < before['updateTime']
            or retained['names'] not in (previous['names'], expected['names'])):
        raise ValueError('MAX пока не подтвердил то же изменение имени')
    # Keep the highest canonical revision and all its unrelated metadata. The
    # sole names replacement is supported by both the actual ACK and read32.
    result = copy.deepcopy(max((current, ack), key=lambda row: row['updateTime']))
    result['names'] = copy.deepcopy(ack['names'])
    return result


class ContactProfileServiceMixin:
    def _log_contact_profile_readback(self, reason, context, data=None, *, clock='unavailable',
                                      semantic='unavailable', differences=()):
        if reason not in ('read_failed', 'context_changed', 'parse_failed', 'stale_contact',
                          'cache_failed', 'snapshot_failed', 'equivalent_contact', 'confirmed_rename'):
            return
        try:
            details = {'reason': reason, 'clock': clock if clock in ('older', 'equal', 'newer') else 'unavailable',
                       **_contact_readback_shape(data, context.contact_id)}
            if semantic in ('match', 'conflict'):
                details['semantic'] = semantic
                details['differences'] = [key for key in ('flags', 'status', 'accountStatus', 'options', 'service')
                                          if key in differences]
            self.log('contact-profile-readback ' + json.dumps(details, sort_keys=True, separators=(',', ':')))
        except Exception:
            pass  # A diagnostic failure must not change the original outcome.

    def _contact_profile_cache(self, context=None, *, contact_id=None):
        binding = self._history_read_context()
        if context is not None:
            if (not isinstance(context, ContactContext) or not int64(context.contact_id)
                    or type(context.owner_id) is not int or context.owner_id != binding[0]
                    or context.source_client is not binding[1] or context.session_context is not binding[2]):
                raise ValueError('Сессия карточки контакта изменилась')
            contact_id = context.contact_id
        if not int64(contact_id):
            raise ValueError('Не подтверждён выбранный контакт')

        def read():
            if getattr(self, '_server_metadata_owner', binding[0]) != binding[0]:
                raise ValueError('Кэш контактов относится к другой сессии')
            raw = copy.deepcopy(self._server_contacts.get(contact_id, {}))
            if (not isinstance(raw, dict) or raw and
                    (type(raw.get('id')) is not int or raw['id'] != contact_id)):
                raise ValueError('Не подтверждён выбранный контакт')
            cached = next((row for row in self.store.list_contacts() if row.id == contact_id), None)
            if not raw and cached is None:
                raise ValueError('Контакта нет в текущей сессии')
            return copy.deepcopy(cached), raw
        cached, raw = self._history_cache_write(binding, read)
        return binding, cached, raw

    def contact_context(self, contact_id):
        binding, _, _ = self._contact_profile_cache(contact_id=contact_id)
        context = ContactContext(*binding, contact_id)
        self._contact_profile_cache(context)
        return context

    def is_current_contact(self, context):
        try:
            self._contact_profile_cache(context)
            return True
        except (ValueError, TypeError, AttributeError):
            return False

    def contact_snapshot(self, context):
        binding, cached, raw = self._contact_profile_cache(context)
        from .service import _person_name, _server_avatar_url, _has_contact_flag
        with self._metadata_lock:
            pending = getattr(self, '_pending_contact_profile_write', None)
            other = getattr(self, '_pending_contact_write', None)
            pending = bool(pending and pending['owner'] == binding[0]
                           or other and other['owner'] == binding[0])
            observed = getattr(self, '_contact_profile_observations', {}).get((binding[0], context.contact_id))
        try:
            typed = created_contact(raw)
        except ValueError:
            typed = None
        fresh = (isinstance(observed, _ContactObservation) and observed.session is binding[2]
                 and observed.canonical == raw and typed is not None)
        added = bool(typed and typed.get('flags', 0) & 512 and typed.get('status') not in ('REMOVED', 'HIDDEN'))
        blocked = typed.get('status') == 'BLOCKED' if typed else None
        hidden = self._history_cache_write(
            binding, lambda: context.contact_id in self.store.hidden_contact_ids(binding[0]))
        own = context.contact_id == binding[0]
        service = _has_contact_flag(raw, 'SERVICE_ACCOUNT')
        first, last = _names(raw)
        title = _person_name(raw) or (cached.display if cached else f'Контакт {context.contact_id}')
        dialog = self._history_cache_write(binding, lambda: self.dialog_for_contact(context.contact_id))
        # CONTACT_INFO's optional phone omission does not erase a number the
        # user already supplied/read for this exact owner/contact. Keep its
        # origin explicit; never derive a phone from the ID or another record.
        phone = raw.get('phone', cached.phone if cached else None)
        phone_source = 'server' if 'phone' in raw else 'local' if phone else 'missing'
        if (type(phone) in (int, float) and 0 < phone < 10**15
                and math.isfinite(phone) and phone == int(phone)):
            phone = '+' + str(int(phone))
        elif not isinstance(phone, str):
            phone = ''
        if not phone:
            phone_source = 'missing'
        state = {'context': context, 'contact_id': context.contact_id, 'title': title,
                 'first_name': first, 'last_name': last, 'phone': phone, 'phone_source': phone_source,
                 'avatar_url': _server_avatar_url(raw) or (cached.avatar_url if cached else None),
                 'is_verified': _has_contact_flag(raw, 'OFFICIAL'),
                 'description': raw.get('description') if isinstance(raw.get('description'), str) else '',
                 'is_self': own, 'is_added': added, 'blocked': blocked, 'fresh': fresh,
                 'is_local_hidden': hidden,
                 'pending': pending, 'action_status': UNKNOWN if pending else '',
                 'chat_id': dialog.id if dialog else None,
                 'availability': {'chat': dialog is not None or binding[0] > 0 and context.contact_id > 0,
                    'call': False, 'video': False,
                    'add': fresh and not added and not own and not service and not pending,
                    'edit': fresh and added and not own and not pending,
                    'delete': fresh and added and not own and not pending,
                    'hide_local': fresh and not added and not own and not hidden and not pending,
                    'block': fresh and not own and not service and not blocked and not pending,
                    'unblock': fresh and not own and not service and blocked is True and not pending}}
        _, _, current_raw = self._contact_profile_cache(context)
        same_observation = self._history_cache_write(binding, lambda:
            getattr(self, '_contact_profile_observations', {}).get((binding[0], context.contact_id)) is observed
            and self._server_contacts.get(context.contact_id, {}) == raw)
        if current_raw != raw or not same_observation:
            state['fresh'] = False
            for capability in ('add', 'edit', 'delete', 'hide_local', 'block', 'unblock'):
                state['availability'][capability] = False
        return state

    def _contact_dialog_draft(self, chat_id, binding):
        """Return a current local navigation draft, not fabricated server state."""
        self._require_history_context(binding)
        with self._metadata_lock:
            context = getattr(self, '_contact_dialog_drafts', {}).get((binding[0], chat_id))
            raw = self._server_chats.get(chat_id, {})
            if context is None:
                return None
            if raw.get('type') == 'DIALOG':
                return None  # A subsequent server DTO has promoted this route.
            if (context.source_client is not binding[1] or context.session_context is not binding[2]):
                raise ValueError('Откройте контакт заново после смены соединения')
            chat = self.store.get_chat(chat_id)
            if (chat is None or chat.is_group or chat.chat_type != 'DIALOG'
                    or chat.peer_user_id != context.contact_id
                    or direct_dialog_id(binding[0], context.contact_id) != chat_id
                    or raw.get('type') not in (None, 'DIALOG')):
                raise ValueError('Данные личного диалога изменились')
            return context

    def open_contact_dialog(self, context):
        """Explicit 'Write': read 32/48, then navigate; never ADD/create/send."""
        binding, _, _ = _dialog_phase('capture', lambda: self._contact_profile_cache(context))
        existing = _dialog_phase('capture', lambda: self._history_cache_write(
            binding, lambda: self.dialog_for_contact(context.contact_id)))
        if existing is not None:
            return {'context': context, 'chat': existing, 'local_draft': False}
        route = _dialog_phase('capture', lambda: direct_dialog_id(binding[0], context.contact_id))
        profile = _dialog_phase('read32', lambda: self._read_contact_profile(context, allow_stale_for_navigation=True))

        def check_collision():
            chat = self.store.get_chat(route)
            raw = self._server_chats.get(route, {})
            if (chat is not None and (chat.is_group or chat.chat_type != 'DIALOG'
                    or chat.peer_user_id != context.contact_id)
                    or raw and (raw.get('type') not in (None, 'DIALOG')
                                or raw.get('id', route) != route)):
                raise ValueError('Этот маршрут уже связан с другим чатом')
            return copy.deepcopy(raw)
        before_read = _dialog_phase('capture', lambda: self._history_cache_write(binding, check_collision))
        data = _dialog_phase('read48', lambda: binding[1].chat_info([route], expected_session=binding[2]))
        _dialog_phase('capture', lambda: self._require_history_context(binding))
        raw = _dialog_phase('parse48', lambda: direct_dialog_reply(data, binding[0], context.contact_id, route))

        def canonical_chat():
            # Explicit navigation is independent of the sidebar's deletion
            # boundary. The current typed DTO, not a visible row, proves routing.
            direct_dialog_record(self._server_chats.get(route), binding[0], context.contact_id, route)
            chat = self.store.get_chat(route)
            if (chat is None or chat.id != route or chat.is_group or chat.chat_type != 'DIALOG'
                    or chat.peer_user_id != context.contact_id):
                raise ValueError('Не удалось подтвердить личный диалог')
            return self.present_chat(route, context=binding)

        def remember():
            check_collision()
            # A concurrent push can supply a real dialog while GET48 is pending.
            current = self.dialog_for_contact(context.contact_id)
            if current is not None:
                return {'context': context, 'chat': current, 'local_draft': False}
            if raw is not None:
                self._remember_chat_info({'chats': [raw]}, my_id=binding[0])
                return {'context': context, 'chat': canonical_chat(), 'local_draft': False}
            current_raw = self._server_chats.get(route, {})
            if current_raw and current_raw != before_read:
                # A fresh hidden/removed DTO may also be absent from the sidebar.
                # Do not discard that concurrent observation as an empty draft.
                return {'context': context, 'chat': canonical_chat(), 'local_draft': False}
            # An explicit empty48 supersedes unchanged old server metadata.
            # Retaining its DIALOG type would incorrectly promote the new draft
            # and force open_chat to query history again. Keep the SQL cutoff.
            self._server_chats.pop(route, None)
            # Official DM constructs a local REMOVED DIALOG on explicit empty48.
            # Keep that provenance outside _server_chats: no server write, no
            # lastMessage/time, no member/settings capabilities are invented.
            chat = Chat(id=route, title='Избранное' if route == 0 else profile['title'],
                        peer_user_id=context.contact_id, chat_type='DIALOG',
                        avatar_url=profile['avatar_url'], is_verified=profile['is_verified'],
                        server_fields=frozenset({'title', 'peer_user_id', 'chat_type',
                                               'avatar_url', 'is_verified'}))
            self.store.upsert_chat(chat)
            drafts = getattr(self, '_contact_dialog_drafts', None)
            if drafts is None:
                self._contact_dialog_drafts = drafts = {}
            drafts[(binding[0], route)] = context
            binding[1].set_peer(route, context.contact_id)
            return {'context': context, 'chat': self.present_chat(route, context=binding), 'local_draft': True}
        return _dialog_phase('remember', lambda: self._history_cache_write(binding, remember))

    def _read_contact_profile(self, context, *, allow_stale_for_navigation=False,
                              allow_equivalent_cached=False):
        binding, _, _ = self._contact_profile_cache(context)
        pending_at_read = self._history_cache_write(
            binding, lambda: getattr(self, '_pending_contact_profile_write', None))
        try:
            data = binding[1].contact_info([context.contact_id], expected_session=binding[2])
        except Exception:
            self._log_contact_profile_readback('read_failed', context)
            raise
        try:
            self._require_history_context(binding)
        except Exception:
            self._log_contact_profile_readback('context_changed', context, data)
            raise
        try:
            raw = _readback(data, context.contact_id)
        except Exception:
            self._log_contact_profile_readback('parse_failed', context, data)
            raise
        failure_reason = 'cache_failed'
        clock = 'unavailable'
        semantic, differences = 'unavailable', ()
        confirmed_rename = False

        def remember():
            nonlocal failure_reason, clock, semantic, differences, confirmed_rename
            prior = self._server_contacts.get(context.contact_id, {})
            actual = raw
            pending = getattr(self, '_pending_contact_profile_write', None)
            if allow_stale_for_navigation is not True and pending_at_read and 'rename_ack' in pending_at_read:
                try:
                    if (pending is not pending_at_read
                            or getattr(self, '_pending_contact_write', None) is not None):
                        raise ValueError('Подтверждение контакта изменилось')
                    merged = _confirmed_older_rename(raw, prior, pending, binding)
                except ValueError:
                    failure_reason = 'stale_contact'
                    observations = getattr(self, '_contact_profile_observations', {})
                    observations.pop((binding[0], context.contact_id), None)
                    raise
                if merged is not None:
                    actual, confirmed_rename = merged, True
                    semantic = 'match'
            if isinstance(prior, dict) and int64(prior.get('updateTime')):
                clock = ('older' if raw['updateTime'] < prior['updateTime'] else
                         'equal' if raw['updateTime'] == prior['updateTime'] else 'newer')
                if clock == 'older' and not confirmed_rename:
                    if allow_stale_for_navigation is True:
                        # This typed32 still proves the selected contact exists.
                        # Navigation needs identity, not permission to replace a
                        # newer profile or settle any pending contact mutation.
                        # Keep cache, observation and pending records untouched.
                        return
                    observations = getattr(self, '_contact_profile_observations', None)
                    if observations is not None:
                        observations.pop((binding[0], context.contact_id), None)
                    if allow_equivalent_cached is True:
                        try:
                            old_fields, new_fields = _contact_action_fields(raw), _contact_action_fields(prior)
                            differences = tuple(key for key in old_fields if old_fields[key] != new_fields[key])
                            semantic = 'conflict' if differences else 'match'
                        except ValueError:
                            pass
                        if (semantic == 'match'
                                and getattr(self, '_pending_contact_profile_write', None) is None
                                and getattr(self, '_pending_contact_write', None) is None):
                            if observations is None:
                                self._contact_profile_observations = observations = {}
                            observations[(binding[0], context.contact_id)] = _ContactObservation(
                                binding[2], copy.deepcopy(prior))
                            # This observes the retained canonical state; it is
                            # never a mutation readback or permission to downgrade.
                            return
                    failure_reason = 'stale_contact'
                    raise ValueError('MAX вернул устаревшие сведения контакта')
            self._remember_contact_info({'contacts': [actual]})
            observations = getattr(self, '_contact_profile_observations', None)
            if observations is None:
                self._contact_profile_observations = observations = {}
            observations[(binding[0], context.contact_id)] = _ContactObservation(binding[2], copy.deepcopy(actual))
            pending = getattr(self, '_pending_contact_profile_write', None)
            if (pending and pending['owner'] == binding[0] and pending['id'] == context.contact_id
                    and not pending['inflight'] and _matches(actual, pending)):
                if pending['action'] == 'ADD':
                    # A confirmed explicit ADD may restore a locally hidden row.
                    # Unsolicited membership/metadata refreshes cannot do so.
                    self.store.set_contact_hidden(binding[0], context.contact_id, False)
                if pending['action'] == 'UPDATE':
                    # Only the actual confirmed dialog may carry a saved alias.
                    dialog = self.dialog_for_contact(context.contact_id)
                    if dialog is not None:
                        from .service import _person_name
                        self.store.set_confirmed_contact_chat_title(dialog.id, _person_name(actual))
                self._pending_contact_profile_write = None
            if raw.get('status') == 'REMOVED' and 'flags' in raw and not raw['flags'] & 512:
                self.store.delete_contact(context.contact_id)
                self._invalidate_contacts()
            # Also permit GET-only recovery of existing Info block pending state.
            other = getattr(self, '_pending_contact_write', None)
            if (other and other == {'owner': binding[0], 'id': context.contact_id,
                                   'blocked': raw.get('status') == 'BLOCKED'}):
                self._pending_contact_write = None
        try:
            self._history_cache_write(binding, remember)
        except Exception:
            self._log_contact_profile_readback(failure_reason, context, data, clock=clock,
                                               semantic=semantic, differences=differences)
            raise
        if confirmed_rename:
            self._log_contact_profile_readback('confirmed_rename', context, data, clock=clock, semantic='match')
        elif clock == 'older' and semantic == 'match':
            self._log_contact_profile_readback('equivalent_contact', context, data, clock=clock,
                                               semantic=semantic)
        try:
            return self.contact_snapshot(context)
        except Exception:
            self._log_contact_profile_readback('snapshot_failed', context, data, clock=clock)
            raise

    def refresh_contact_profile(self, context):
        self._contact_profile_cache(context)
        lock = self._feature_action_lock('blocked')
        if not lock.acquire(blocking=False):
            raise ValueError('Дождитесь текущего изменения контакта')
        try:
            return self._read_contact_profile(context, allow_equivalent_cached=True)
        finally:
            lock.release()

    def rename_contact_profile(self, context, first_name, last_name=''):
        first = contact_name(first_name, required=True)
        last = contact_name(last_name, required=False)
        return self._change_contact_profile(context, 'UPDATE',
                {'firstName': first, **({'lastName': last} if last else {})})

    def add_contact_profile(self, context):
        """Explicit native ADD for a known actual ID; opening a card never adds."""
        return self._change_contact_profile(context, 'ADD', {})

    def delete_contact_profile(self, context):
        return self._change_contact_profile(context, 'REMOVE', {})

    def hide_contact_profile(self, context):
        """Hide a fresh non-member locally; no read/write RPC or fake REMOVE."""
        binding, _, _ = self._contact_profile_cache(context)

        def hide():
            if not self.contact_snapshot(context)['availability']['hide_local']:
                raise ValueError('Убрать этот контакт из локального списка сейчас нельзя')
            self.store.set_contact_hidden(binding[0], context.contact_id, True)
            self._invalidate_contacts()
            return self.contact_snapshot(context)

        return self._history_cache_write(binding, hide)

    def set_contact_profile_blocked(self, context, blocked):
        if type(blocked) is not bool:
            raise ValueError('Нужно указать состояние блокировки')
        return self._change_contact_profile(context, 'BLOCK' if blocked else 'UNBLOCK', {})

    def _change_contact_profile(self, context, action, changes):
        binding, _, _ = self._contact_profile_cache(context)
        lock = self._feature_action_lock('blocked')
        if not lock.acquire(blocking=False):
            raise ValueError('Дождитесь текущего изменения контакта')
        try:
            self._contact_profile_cache(context)
            # Keep a previous owner's unknown record intact through account
            # switches. A new action may not overwrite its recovery evidence.
            if getattr(self, '_pending_contact_profile_write', None) is not None:
                raise MaxWriteOutcomeUnknown(UNKNOWN)
            before = self.contact_snapshot(context)
            if before['pending']:
                raise MaxWriteOutcomeUnknown(UNKNOWN)
            before = self._read_contact_profile(context, allow_equivalent_cached=True)
            capability = {'ADD': 'add', 'UPDATE': 'edit', 'REMOVE': 'delete',
                          'BLOCK': 'block', 'UNBLOCK': 'unblock'}[action]
            if not before['availability'][capability]:
                raise ValueError('Это действие для контакта сейчас недоступно')
            payload = {'contactId': context.contact_id, 'action': action, **changes}
            pending = {'owner': binding[0], 'id': context.contact_id, 'action': action,
                       'payload': payload, 'inflight': True}
            def reserve():
                if (getattr(self, '_pending_contact_profile_write', None) is not None
                        or getattr(self, '_pending_contact_write', None) is not None):
                    raise MaxWriteOutcomeUnknown(UNKNOWN)
                current = self.contact_snapshot(context)
                if not current['availability'][capability]:
                    raise ValueError('Сведения контакта изменились. Обновите карточку')
                observation = self._contact_profile_observations[(binding[0], context.contact_id)]
                if action == 'UPDATE':
                    pending['rename_before'] = copy.deepcopy(observation.canonical)
                    pending['rename_binding'] = binding
                self._pending_contact_profile_write = pending
                return observation
            observation = self._history_cache_write(binding, reserve)
            started = False
            try:
                self._contact_profile_cache(context)
                def dispatch_guard():
                    current = getattr(self, '_contact_profile_observations', {}).get((binding[0], context.contact_id))
                    if (current is not observation or observation.session is not binding[2]
                            or observation.canonical != self._server_contacts.get(context.contact_id)
                            or self._pending_contact_profile_write is not pending
                            or getattr(self, '_pending_contact_write', None) is not None):
                        raise ValueError('Сведения контакта изменились. Обновите карточку')
                self._history_cache_write(binding, dispatch_guard)
                started = True
                frame = binding[1]._write_frame(34, payload, expected_session=binding[2])
                self._require_history_context(binding)
                if frame.opcode != 34 or frame.cmd not in (1, 3):
                    raise ValueError('Неверный ответ MAX')
                if not frame.ok:
                    raise MaxError('MAX отклонил изменение контакта')
                if not isinstance(frame.decoded, dict):
                    raise ValueError('MAX не подтвердил изменение контакта')
                if 'contact' in frame.decoded:
                    acknowledged = created_contact(frame.decoded['contact'])
                    if acknowledged['id'] != context.contact_id:
                        raise ValueError('MAX вернул другой контакт')
                    if action == 'UPDATE':
                        def retain_ack():
                            if (self._pending_contact_profile_write is not pending
                                    or getattr(self, '_pending_contact_write', None) is not None):
                                raise ValueError('Подтверждение контакта изменилось')
                            pending['rename_ack'] = copy.deepcopy(acknowledged)
                        self._history_cache_write(binding, retain_ack)
            except (MaxWriteNotStarted, MaxError) as exc:
                # OutcomeUnknown inherits MaxError: it must never unlock retry.
                if isinstance(exc, MaxWriteOutcomeUnknown):
                    pending['inflight'] = False
                    raise
                with self._metadata_lock:
                    if self._pending_contact_profile_write is pending:
                        self._pending_contact_profile_write = None
                raise
            except Exception:
                pending['inflight'] = False
                if not started:
                    with self._metadata_lock:
                        if self._pending_contact_profile_write is pending:
                            self._pending_contact_profile_write = None
                    raise
                raise MaxWriteOutcomeUnknown(UNKNOWN) from None
            pending['inflight'] = False
            try:
                state = self._read_contact_profile(context)
            except Exception:
                raise MaxWriteOutcomeUnknown(UNKNOWN) from None
            if state['pending']:
                raise MaxWriteOutcomeUnknown(UNKNOWN)
            return {**state, 'accepted': True, 'confirmed': True}
        finally:
            lock.release()
