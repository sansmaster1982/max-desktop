"""Ephemeral receiving-side QR login, isolated from the active account.

Uses the official 288 -> 289 -> 291 -> optional 115 flow. Nothing is persisted
here. The caller must verify LOGIN with the returned device UUID before saving.
"""
from __future__ import annotations

import math
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable

from ..protocol.client import MaxClient


@dataclass(frozen=True)
class QrChallenge:
    qr_link: str = field(repr=False)
    expires_at_ms: float
    polling_interval_ms: int


@dataclass(frozen=True)
class QrAuthorized:
    token: str = field(repr=False)
    device_id: str
    profile_id: int


class QrLoginController:
    def __init__(self, app_version: str, locale='ru', *, client_factory: Callable = MaxClient, clock=time.time):
        self.app_version, self.locale = app_version, locale
        self._factory, self._clock = client_factory, clock
        self._lock = threading.RLock()
        self._client = None
        self._generation = 0
        self._track_id = None
        self._password_track = None
        self._expires_ms = 0.0
        self._state = 'idle'

    @property
    def state(self):
        with self._lock:
            return self._state

    def start(self) -> QrChallenge:
        with self._lock:
            # A panel creates a new controller for every explicit refresh.
            # Cancellation must remain terminal even if this worker was queued
            # before close() and only begins running afterwards.
            if self._state != 'idle':
                raise ValueError('QR-вход остановлен. Создайте новый код.')
            generation = self._generation
            client = self._factory(app_version=self.app_version, locale=self.locale,
                                   device_id=str(uuid.uuid4()), auto_reconnect=False)
            self._client = client
            self._state = 'starting'
        try:
            client.connect(device_type='WEB')
            with self._lock:
                if generation != self._generation:
                    raise ValueError('QR-вход остановлен.')
            response = client.qr_create()
            track, link = response['trackId'], response['qrLink']
            ttl, interval = response['ttl'], response['pollingInterval']
            if (not isinstance(track, str) or not track or not isinstance(link, str)
                    or not 1 <= len(link) <= 4096 or not _positive_number(ttl)
                    or not _positive_number(interval) or interval > 60000):
                raise ValueError('MAX вернул некорректный QR-код. Создайте новый.')
            with self._lock:
                if generation != self._generation:
                    raise ValueError('QR-вход остановлен.')
                self._track_id = track
                self._expires_ms = self._clock() * 1000 + ttl
                self._state = 'waiting'
                return QrChallenge(link, self._expires_ms, max(250, math.ceil(interval)))
        except Exception:
            client.close()
            with self._lock:
                if generation == self._generation:
                    self._client = None
                    self._state = 'error'
            raise

    def poll(self) -> dict:
        with self._lock:
            if self._state != 'waiting' or self._client is None:
                raise ValueError('QR-вход не ожидает подтверждения.')
            client, track, generation = self._client, self._track_id, self._generation
            if self._clock() * 1000 >= self._expires_ms:
                self.close()
                return {'state':'expired'}
            self._state = 'polling'
        try:
            status = client.qr_poll(track)['status']
            with self._lock:
                if generation != self._generation:
                    raise ValueError('QR-вход остановлен.')
                if not status['loginAvailable']:
                    ttl = status['ttl']
                    if not _positive_number(ttl):
                        self.close()
                        return {'state':'expired'}
                    self._expires_ms = self._clock() * 1000 + ttl
                    self._state = 'waiting'
                    return {'state':'waiting', 'expires_at_ms':self._expires_ms}
                # Set before the request. Unknown outcomes are never re-polled
                # or finalized again under the same challenge.
                self._state = 'finishing'
            result = client.qr_finish(track)
            return self._consume(result, client, generation)
        except Exception:
            if generation == self._generation:
                self.close()
            raise

    def check_password(self, password: str) -> dict:
        with self._lock:
            if self._state != 'password_required' or self._client is None or not password:
                raise ValueError('Введите пароль 2FA для этого QR-входа.')
            client, track, generation = self._client, self._password_track, self._generation
            self._state = 'checking_password'
        try:
            return self._consume(client.qr_check_password(track, password), client, generation)
        except Exception:
            if generation == self._generation:
                self.close()
            raise

    def _consume(self, result: dict, client, generation: int) -> dict:
        with self._lock:
            if generation != self._generation:
                raise ValueError('QR-вход остановлен.')
            challenge = result.get('passwordChallenge')
            if isinstance(challenge, dict) and isinstance(challenge.get('trackId'), str) and challenge['trackId']:
                self._password_track = challenge['trackId']
                self._state = 'password_required'
                return {'state':'password_required', 'hint':challenge.get('hint') or ''}
            attrs, profile = result.get('tokenAttrs'), result.get('profile')
            login = attrs.get('LOGIN') if isinstance(attrs, dict) else None
            contact = profile.get('contact') if isinstance(profile, dict) else None
            if (not isinstance(login, dict) or not isinstance(login.get('token'), str) or not login['token']
                    or not isinstance(contact, dict) or type(contact.get('id')) is not int or contact['id'] <= 0):
                raise ValueError('MAX не подтвердил существующий профиль. QR-вход остановлен.')
            authorized = QrAuthorized(login['token'], client.device_id, contact['id'])
            # Keep the completed state until its queued UI callback is delivered.
            # Closing the receiver is separate from cancelling this result.
            self._generation += 1
            self._client = None
            self._track_id = self._password_track = None
            self._state = 'authorized'
            self._expires_ms = 0.0
        client.close()
        return {'state':'authorized', 'authorization':authorized}

    def cancel(self):
        """Invalidate work immediately without blocking on socket shutdown."""
        with self._lock:
            self._generation += 1
            self._track_id = self._password_track = None
            self._state = 'closed'
            self._expires_ms = 0.0

    def close(self):
        with self._lock:
            self.cancel()
            client, self._client = self._client, None
        if client is not None:
            client.close()


def _positive_number(value) -> bool:
    return type(value) in (int, float) and math.isfinite(value) and value > 0
