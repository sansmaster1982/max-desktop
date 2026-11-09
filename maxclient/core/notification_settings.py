"""Owner-bound notification preferences with single-write/readback semantics."""
from __future__ import annotations

from threading import RLock

from ..protocol.client import MaxError, MaxWriteNotStarted, MaxWriteOutcomeUnknown
from ..protocol.settings_contracts import notification_values

DEFAULTS = {
    'DONT_DISTURB_UNTIL': 0, 'PUSH_DETAILS': True, 'PUSH_SOUND': '_DEFAULT_',
    'CHATS_PUSH_NOTIFICATION': 'ON', 'CHATS_PUSH_SOUND': '_DEFAULT_',
}


def same_values(actual, expected):
    return all(key in actual and type(actual[key]) is type(value) and actual[key] == value
               for key, value in expected.items())


class NotificationSettings:
    def __init__(self, service):
        self.service = service
        self._lock = RLock()
        self._pending = {}
        self._owner = None

    def read(self, *, context=None):
        with self._lock:
            if context is not None:
                self.service._require_history_context(context)
                owner = context[0]
                config = self.service._history_cache_write(
                    context, lambda: self.service.store.load_config(owner))
            else:
                owner = self.service._confirmed_profile_id()
                config = self.service.store.load_config(owner)
            available = 'user' in config
            supplied = notification_values(config.get('user', {}))
            if context is not None:
                self.service._require_history_context(context)
            if self.service._confirmed_profile_id() != owner:
                raise ValueError('Аккаунт изменился во время чтения настроек')
            # Do not discard an unresolved intent just because the transport was
            # replaced. Only a valid different-owner read resets this guard.
            if owner != self._owner:
                self._owner, self._pending = owner, {}
            if self._pending and same_values(supplied, self._pending):
                self._pending = {}
            # Defaults are established by the WEB model, only after CONFIG exists.
            values = {**DEFAULTS, **supplied} if available else {}
            return {'available': available, 'settings': values,
                    'supplied': supplied, 'pending': bool(self._pending)}

    def update(self, patch, *, context=None):
        patch = notification_values(patch, writable=True)
        with self._lock:
            before = self.read(context=context) if context is not None else self.read()
            if not before['available']:
                raise ValueError('MAX пока не передал настройки')
            if before['pending']:
                raise ValueError('Предыдущее изменение ещё не подтверждено')
            owner = self._owner
            client = (self.service._require_history_context(context)
                      if context is not None else self.service.client)
            self._pending = dict(patch)
            try:
                if context is not None:
                    result = client.update_user_settings(patch, expected_session=context[2])
                else:
                    result = client.update_user_settings(patch)
            except MaxWriteNotStarted:
                self._pending = {}  # Transport positively rejected before send.
                raise
            except MaxWriteOutcomeUnknown:
                raise
            except MaxError:
                self._pending = {}  # explicit negative server ACK
                raise
            except Exception:
                raise MaxWriteOutcomeUnknown('Результат изменения уведомлений неизвестен') from None
            try:
                if context is not None:
                    self.service._require_history_context(context)
                if self.service._confirmed_profile_id() != owner:
                    raise ValueError('owner changed')
                if result is not None:
                    if context is not None:
                        self.service._history_cache_write(
                            context, lambda: self.service._remember_config(result, owner_id=owner))
                    else:
                        self.service._remember_config(result, owner_id=owner)
                current = self.read(context=context) if context is not None else self.read()
            except Exception:
                raise MaxWriteOutcomeUnknown('MAX не подтвердил состояние уведомлений') from None
            return {'accepted': True, **current}


def notification_settings(service):
    """One shared pending-write guard for the UI and runtime of this session."""
    adapter = getattr(service, '_desktop_notification_settings', None)
    if adapter is None:
        adapter = NotificationSettings(service)
        service._desktop_notification_settings = adapter
    return adapter
