"""Explicit server contact creation; one write, owner-bound readback, no retry."""
from __future__ import annotations

import copy
from dataclasses import dataclass
import json
import threading

from ..protocol.client import AuthenticatedSession, MaxError, MaxWriteNotStarted, MaxWriteOutcomeUnknown
from ..protocol.contact_create_contracts import (
    CONTACT_CREATE, contact_phone, contact_create_payload, contact_create_reply, contact_create_readback,
    contact_create_name_matches,
)
from ..protocol.folder_contracts import int64
from .models import Contact


UNKNOWN = ('Результат сохранения не подтверждён. Нажмите «Проверить сохранение». '
           'Повторная отправка для этого номера заблокирована до подтверждения.')


@dataclass(frozen=True)
class ContactCreateResult:
    contact: Contact
    is_new: bool | None
    name_matches: bool = True


class ContactCreateServiceMixin:
    def _log_contact_creation_readback(self, reason, pending, data, *, differences=()):
        if reason not in ('validation_failed', 'stale_conflict', 'stale_ack_conflict', 'confirmed'):
            return
        try:
            from .contact_profile_service import _contact_readback_shape
            identity = pending.get('contact_id')
            details = {'reason': reason, 'is_new': pending.get('is_new')
                       if type(pending.get('is_new')) is bool else None,
                       **_contact_readback_shape(data, identity)}
            if reason in ('stale_conflict', 'stale_ack_conflict'):
                # Fixed field names only: which observation part blocked32.
                details['differences'] = [key for key in ('updateTime', 'flags', 'status', 'phone', 'names',
                                                          'accountStatus', 'options', 'service')
                                          if key in differences]
            rows = data.get('contacts') if isinstance(data, dict) else None
            matches = [row for row in rows if isinstance(row, dict) and type(row.get('id')) is int
                       and row['id'] == identity] if isinstance(rows, list) else []
            if len(matches) == 1:
                row = matches[0]
                details['name_matches'] = contact_create_name_matches(row, pending['payload'])
                details['added'] = bool(row['flags'] & 512) if type(row.get('flags')) is int else None
            self.log('contact-create-readback ' + json.dumps(details, sort_keys=True, separators=(',', ':')))
        except Exception:
            pass  # Diagnostics cannot change a write's outcome or expose raw errors.

    def _contact_creation_state(self):
        with self._metadata_lock:
            if not hasattr(self, '_contact_create_lock'):
                self._contact_create_lock = threading.Lock()
                self._contact_create_pending = {}
            return self._contact_create_lock

    def _contact_creation_binding(self, context):
        if not isinstance(context, AuthenticatedSession):
            raise ValueError('Откройте форму добавления в подтверждённой сессии')
        return self._contact_lookup_binding(context)

    def _finish_contact_creation(self, binding, key, pending):
        client = self._require_history_context(binding)
        identity = pending.get('contact_id')
        if not int64(identity) or identity == binding[0]:
            raise MaxWriteOutcomeUnknown(UNKNOWN)
        data = client.contact_info([identity], expected_session=binding[2])
        self._require_history_context(binding)
        try:
            # Explicit41 new=False means an existing contact was returned. The
            # official ADD flow displays that name; changing it uses UPDATE34.
            record = contact_create_readback(data, identity, pending['payload'],
                                            require_name_match=pending.get('is_new') is not False)
        except Exception:
            self._log_contact_creation_readback('validation_failed', pending, data)
            raise

        def remember():
            actual = record
            #41 already supplied an authoritative contact even when the local
            #cache was empty. An older32 cannot discard that observation.
            observations = ((pending.get('ack_contact'), 'stale_ack_conflict'),
                            (self._server_contacts.get(identity), 'stale_conflict'))
            for current, reason in observations:
                if current is None:
                    continue
                if (reason == 'stale_conflict' and isinstance(current, dict)
                        and 'updateTime' not in current and type(current.get('id')) is int
                        and current['id'] == identity):
                    # Lookup46 caches a clockless projection, not a versioned
                    # ContactDTO. It cannot be newer than this exact post-write
                    # 32, so it neither blocks nor confirms; 32 replaces it.
                    continue
                if (not isinstance(current, dict) or not int64(current.get('updateTime'))
                        or not int64(current.get('id')) or current['id'] != identity):
                    self._log_contact_creation_readback(reason, pending, data, differences=('updateTime',))
                    raise MaxWriteOutcomeUnknown(UNKNOWN)
                if current['updateTime'] > actual['updateTime']:
                    relevant = ('flags', 'status', 'phone', 'names', 'accountStatus', 'options', 'service')
                    differences = [key for key in relevant
                                   if (key in current, current.get(key)) != (key in actual, actual.get(key))]
                    if differences:
                        self._log_contact_creation_readback(reason, pending, data, differences=differences)
                        raise MaxWriteOutcomeUnknown(UNKNOWN)
                    actual = contact_create_readback({'contacts': [current]}, identity, pending['payload'],
                                                    require_name_match=pending.get('is_new') is not False)
            self._remember_contact_info({'contacts': [actual]})
            contact = next(row for row in self.store.list_contacts() if row.id == identity)
            if 'phone' not in actual:
                #41 (or recovery46) bound this exact ID to the submitted phone;
                #32 confirmed the saved contact. Keep that known input in the
                #local book, without inventing an optional field in raw DTOs.
                contact.phone = pending['payload']['phone']
                self.store.upsert_contact(contact)
                self._invalidate_contacts()
            # Only confirmed explicit creation/recovery restores visibility.
            self.store.set_contact_hidden(binding[0], identity, False)
            return ContactCreateResult(copy.deepcopy(contact), pending.get('is_new'),
                                       contact_create_name_matches(actual, pending['payload']))
        result = self._history_cache_write(binding, remember)
        self._require_history_context(binding)
        self._contact_create_pending.pop(key, None)
        self._log_contact_creation_readback('confirmed', pending, data)
        return result

    def create_contact_by_phone(self, phone: str, first_name: str, last_name: str = '', *, context) -> ContactCreateResult:
        """Submit official41, then confirm actual-ID32 membership and CUSTOM name.

        Pass the session captured before queueing worker execution. Lookup46 and
        local aliases keep their existing separate APIs. No chat is created here.
        """
        payload = contact_create_payload(phone, first_name, last_name)
        binding = self._contact_creation_binding(context)
        lock = self._contact_creation_state()
        if not lock.acquire(blocking=False):
            raise ValueError('Сохранение контакта уже выполняется')
        key = (binding[0], payload['phone'])
        try:
            self._require_history_context(binding)
            if key in self._contact_create_pending:
                raise MaxWriteOutcomeUnknown(UNKNOWN)
            profile = self._history_cache_write(binding, self.store.load_profile)
            if profile is not None and profile.id == binding[0] and profile.phone:
                try:
                    own_phone = contact_phone(profile.phone)
                except ValueError:
                    own_phone = None
                if own_phone == payload['phone']:
                    raise ValueError('Это ваш номер телефона. Выберите другой контакт.')
            client = self._require_history_context(binding)
            pending = {'payload': payload}
            self._contact_create_pending[key] = pending
            try:
                frame = client._write_frame(CONTACT_CREATE, payload, expected_session=context)
            except MaxWriteNotStarted:
                self._contact_create_pending.pop(key, None)
                raise
            except Exception:
                raise MaxWriteOutcomeUnknown(UNKNOWN) from None
            # Losing the owner after any sent write leaves an unresolved record;
            # never read or install its response through the replacement account.
            try:
                self._require_history_context(binding)
                if frame.opcode != CONTACT_CREATE or frame.cmd not in (1, 3):
                    raise ValueError('Неверный ответ MAX')
            except Exception:
                raise MaxWriteOutcomeUnknown(UNKNOWN) from None
            if not frame.ok:
                self._contact_create_pending.pop(key, None)
                code = frame.error_text()
                text = {'user.not.found': 'Пользователь MAX с таким номером не найден.',
                        'error.add.self': 'Это ваш номер телефона. Выберите другой контакт.',
                        'error.invalid-phone': 'MAX не принял номер телефона.'}.get(code, 'MAX отклонил сохранение контакта.')
                raise MaxError(text)
            try:
                record, is_new = contact_create_reply(frame.decoded)
                if 'phone' in record and '+' + str(int(record['phone'])) != payload['phone']:
                    raise ValueError('MAX вернул контакт с другим номером')
                pending.update(contact_id=record['id'], is_new=is_new, ack_contact=copy.deepcopy(record))
                return self._finish_contact_creation(binding, key, pending)
            except Exception:
                raise MaxWriteOutcomeUnknown(UNKNOWN) from None
        finally:
            lock.release()

    def verify_contact_creation(self, phone: str, *, context) -> ContactCreateResult:
        """Resolve an uncertain save with only ordinary46/32 reads; never resend41."""
        phone = contact_phone(phone)
        binding = self._contact_creation_binding(context)
        lock = self._contact_creation_state()
        if not lock.acquire(blocking=False):
            raise ValueError('Проверка контакта уже выполняется')
        key = (binding[0], phone)
        try:
            pending = self._contact_create_pending.get(key)
            if pending is None:
                raise ValueError('Нет неподтверждённого сохранения для этого номера')
            try:
                client = self._require_history_context(binding)
                if not int64(pending.get('contact_id')):
                    found = client.contact_by_phone(phone, expected_session=context)
                    self._require_history_context(binding)
                    if not isinstance(found, dict) or not int64(found.get('id')):
                        raise ValueError('MAX не вернул контакт')
                    pending['contact_id'] = found['id']
                return self._finish_contact_creation(binding, key, pending)
            except Exception:
                raise MaxWriteOutcomeUnknown(UNKNOWN) from None
        finally:
            lock.release()
