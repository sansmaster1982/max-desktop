"""Bounded calls RPC adapter for the public browser SDK; never accepts LOGIN tokens."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
import base64
import json
import math
import threading
from urllib.parse import urlsplit

from PySide6.QtCore import QObject, Signal, Slot

from ..protocol.client import MaxWriteOutcomeUnknown
from ..protocol.codec import lz4_block_decompress

PUBLIC_CALLS_APP_KEY = 'CNHIJPLGDIHBABABA'  # pinned B92 char2519; public application config


class CallRuntimeError(ValueError):
    """Safe UI error; no server packet or credential is formatted here."""


class _CallCancelled(CallRuntimeError):
    pass


@dataclass(frozen=True)
class CallConfig:
    app_version: str
    api_base_url: str
    api_key: str = field(repr=False)
    domain: str = ''
    fast_hangup: bool = False

    def public_dict(self) -> dict:
        if (not isinstance(self.app_version, str) or not isinstance(self.api_base_url, str)
                or not isinstance(self.api_key, str) or not isinstance(self.domain, str)
                or type(self.fast_hangup) is not bool):
            raise CallRuntimeError('Не получена подтверждённая конфигурация звонков MAX.')
        parsed = urlsplit(self.api_base_url)
        if (not self.app_version or not self.api_key or parsed.scheme != 'https'
                or not parsed.hostname or parsed.username or parsed.password
                or parsed.query or parsed.fragment):
            raise CallRuntimeError('Не получена подтверждённая конфигурация звонков MAX.')
        return {'appVersion': self.app_version, 'apiBaseUrl': self.api_base_url,
                'apiKey': self.api_key, 'domain': self.domain or None,
                'fastHangup': bool(self.fast_hangup)}


def _text(value, *, maximum=262144):
    if not isinstance(value, str) or not 1 <= len(value) <= maximum:
        raise CallRuntimeError('Некорректные параметры звонка.')
    return value


def _id(value):
    if isinstance(value, str) and value.isascii() and (value.isdigit() or value.startswith('-') and value[1:].isdigit()):
        value = int(value)
    if type(value) is not int or not -(2**63) <= value < 2**63:
        raise CallRuntimeError('Некорректный идентификатор звонка.')
    return value


def _origin(value):
    parsed = urlsplit(_text(value, maximum=8192))
    if parsed.scheme not in {'https', 'wss'} or not parsed.hostname or parsed.username or parsed.password:
        raise CallRuntimeError('Не получен подтверждённый адрес звонка.')
    authority = parsed.netloc.lower()
    if authority.endswith(':443'):
        authority = authority[:-4]
    return f'{parsed.scheme}://{authority}'


def _parameter_origins(params, *, incoming=False):
    """Only origin metadata is exposed; signaling tokens never leave opaque DTOs."""
    if incoming:
        size, encoded = _text(params).split(':', 1)
        if not size.isascii() or not size.isdigit() or not 0 < int(size) <= 1048576:
            raise CallRuntimeError('Не получены параметры входящего звонка.')
        data = base64.b64decode(encoded + '=' * (-len(encoded) % 4), validate=True)
        decoded = lz4_block_decompress(data, max_output_size=int(size))
        if len(decoded) != int(size):
            raise CallRuntimeError('Некорректные параметры входящего звонка.')
        record = json.loads(decoded.decode('utf-8'))
        keys = ('wse', 'wte')
    else:
        record = json.loads(_text(params))
        keys = ('endpoint', 'wtEndpoint')
    if not isinstance(record, dict) or not isinstance(record.get(keys[0]), str):
        raise CallRuntimeError('Не получен подтверждённый адрес звонка.')
    return {_origin(record[key]) for key in keys if record.get(key)}


class CallRuntime(QObject):
    """Backend must expose rpc(opcode,payload), scoped to the active WEB account.

    Only explicit arm_* calls permit operations. Generation/account checks run
    before and after the blocking backend. Qt signals carry errors from workers
    without str(exception), and old generations never deliver credentials.
    """
    reply = Signal(str, str)
    event = Signal(str, str)
    failure = Signal(str)
    idle = Signal()
    origins = Signal(object)
    _completed = Signal(str, int, object, str)

    def __init__(self, backend, *, is_authorized, parent=None):
        super().__init__(parent)
        self._backend = backend
        self._authorized = is_authorized
        self._lock = threading.RLock()
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='max-calls')
        self._generation = 0
        self._intent = None
        self._conversation = None
        self._used = set()
        self._request_ids = set()
        self._token_requests = 0
        self._closed = False
        self._failed = False
        self._cancelling = False
        self._busy = 0
        self._completed.connect(self._deliver)

    @property
    def generation(self):
        return self._generation

    @property
    def busy(self):
        return self._busy > 0

    def cancel_start(self):
        """Cancel unsent work while permitting a bound, single final hangup."""
        with self._lock:
            self._cancelling = True

    def _arm(self, intent):
        with self._lock:
            if self._closed or self._failed or self._intent is not None or not self._authorized():
                raise CallRuntimeError('Звонок сейчас недоступен.')
            self._generation += 1
            self._intent = intent
            self._conversation = intent.get('conversationId')
            return {**intent, 'generation': self._generation}

    def arm_outgoing(self, contact_id, *, is_video=False):
        return self._arm({'mode': 'outgoing', 'contactId': str(_id(contact_id)), 'isVideo': bool(is_video)})

    def arm_join(self, join_link, *, is_video=False):
        return self._arm({'mode': 'join', 'joinLink': _text(join_link, maximum=4096), 'isVideo': bool(is_video)})

    def arm_incoming(self, conversation_id, vcp, *, accept=True):
        try:
            origins = _parameter_origins(vcp, incoming=True)
        except Exception:
            raise CallRuntimeError('Не подтверждены параметры входящего звонка.') from None
        result = self._arm({'mode': 'incoming', 'conversationId': _text(conversation_id, maximum=256),
                            'vcp': _text(vcp), 'accept': bool(accept), 'isVideo': False})
        self.origins.emit(origins)
        return result

    def _validate(self, operation, payload, generation):
        if (self._closed or self._failed or self._intent is None or generation != self._generation
                or not self._authorized() or not isinstance(payload, dict)):
            raise CallRuntimeError('Сессия звонка остановлена.')
        if self._cancelling and operation != 'fast_hangup':
            raise CallRuntimeError('Начало звонка отменено.')
        intent = self._intent
        if operation == 'call_token':
            if payload or self._token_requests >= 2:
                raise CallRuntimeError('Авторизация звонка остановлена.')
            self._token_requests += 1
            return 158, None
        if operation in self._used:
            raise CallRuntimeError('Этот запрос звонка уже отправлен.')
        if operation == 'fast_start':
            if intent['mode'] != 'outgoing' or set(payload) != {'conversationId', 'internalParams', 'isVideo'}:
                raise CallRuntimeError('Некорректный запрос начала звонка.')
            if type(payload['isVideo']) is not bool or payload['isVideo'] != intent['isVideo']:
                raise CallRuntimeError('Изменился режим звонка.')
            conversation = _text(payload['conversationId'], maximum=256)
            wire = {'conversationId': conversation, 'internalParams': _text(payload['internalParams']),
                    'isVideo': payload['isVideo'], 'calleeIds': [_id(intent['contactId'])]}
            self._conversation = conversation
            opcode = 78
        elif operation == 'fast_join':
            if intent['mode'] != 'join' or set(payload) != {'joinLink', 'internalParams', 'isVideo'}:
                raise CallRuntimeError('Некорректный запрос присоединения.')
            if payload['joinLink'] != intent['joinLink'] or type(payload['isVideo']) is not bool or payload['isVideo'] != intent['isVideo']:
                raise CallRuntimeError('Изменился адрес или режим звонка.')
            wire = {'joinLink': intent['joinLink'], 'isVideo': intent['isVideo'],
                    'internalParams': _text(payload['internalParams'])}
            opcode = 166
        elif operation == 'fast_hangup':
            if set(payload) - {'conversationId', 'reason', 'internalParams'} or self._conversation is None:
                raise CallRuntimeError('Некорректное завершение звонка.')
            if payload.get('conversationId') != self._conversation:
                raise CallRuntimeError('Изменился идентификатор звонка.')
            wire = {'conversationId': self._conversation, 'reason': _text(payload.get('reason'), maximum=256)}
            if 'internalParams' in payload:
                wire['internalParams'] = _text(payload['internalParams'])
            opcode = 167
        else:
            raise CallRuntimeError('Операция звонка недоступна.')
        self._used.add(operation)  # Before I/O: an unknown outcome must never be replayed.
        return opcode, wire

    def execute(self, operation, payload, generation):
        """Synchronous validated path, also used by synthetic offline tests."""
        with self._lock:
            opcode, wire = self._validate(operation, payload, generation)
        try:
            result = self._backend.rpc(opcode, wire)
            with self._lock:
                if self._closed or generation != self._generation or not self._authorized():
                    raise CallRuntimeError('Сессия звонка остановлена.')
                if opcode == 158 and self._cancelling:
                    raise _CallCancelled('Авторизация звонка отменена.')
                return self._response(opcode, result)
        except _CallCancelled:
            raise
        except MaxWriteOutcomeUnknown:
            with self._lock:
                self._failed = True
            raise CallRuntimeError('Результат запроса звонка неизвестен. Повтор отключён.') from None
        except Exception:
            with self._lock:
                self._failed = True
            raise CallRuntimeError('Не удалось выполнить запрос звонка MAX.') from None

    def _response(self, opcode, data):
        if not isinstance(data, dict):
            raise CallRuntimeError('MAX не подтвердил запрос звонка.')
        if opcode == 158:
            token = _text(data.get('token'), maximum=32768)
            clocks = [data.get('token_lifetime_ts'), data.get('token_refresh_ts')]
            if any(type(v) not in (int, float) or not math.isfinite(v) for v in clocks):
                raise CallRuntimeError('MAX не подтвердил токен звонка.')
            return {'token': token, 'lifetimeTs': clocks[0], 'refreshTs': clocks[1]}
        if opcode == 78:
            rejected = data.get('rejectedParticipants')
            if not isinstance(rejected, list):
                raise CallRuntimeError('MAX не подтвердил начало звонка.')
            normalized = []
            for item in rejected:
                if not isinstance(item, dict) or not isinstance(item.get('errorCode'), str):
                    raise CallRuntimeError('MAX не подтвердил начало звонка.')
                normalized.append({'id': str(_id(item.get('id'))), 'status': item['errorCode']})
            # An empty caller-params response is a server rejection; SDK shows its normal error.
            params = data.get('internalCallerParams')
            if params is not None:
                params = _text(params)
                self.origins.emit(_parameter_origins(params))
            return {'internalCallerParams': params, 'rejectedParticipants': normalized}
        if opcode == 166:
            self._conversation = _text(data.get('conversationId'), maximum=256)
            params = _text(data.get('internalParams'))
            self.origins.emit(_parameter_origins(params))
            return {'conversationId': self._conversation, 'internalCallerParams': params}
        if type(data.get('success')) is not bool:
            raise CallRuntimeError('MAX не подтвердил завершение звонка.')
        return {'success': data['success']}

    @Slot(str, str, str, int)
    def rpc(self, request_id, operation, payload_json, generation):
        try:
            if (not isinstance(request_id, str) or not 1 <= len(request_id) <= 80
                    or len(payload_json) > 300000 or request_id in self._request_ids
                    or len(self._request_ids) >= 128):
                raise CallRuntimeError('Некорректный запрос звонка.')
            self._request_ids.add(request_id)
            payload = json.loads(payload_json)
        except Exception:
            self.reply.emit(request_id, json.dumps({'ok': False, 'error': 'Некорректный запрос звонка.'}, ensure_ascii=False))
            return
        if self._closed:
            self.reply.emit(request_id, json.dumps({'ok': False, 'error': 'Сессия звонка остановлена.'}, ensure_ascii=False))
            return

        def work():
            try:
                result = self.execute(operation, payload, generation)
            except CallRuntimeError as error:
                result, error_text = None, str(error)
            else:
                error_text = ''
            try:
                self._completed.emit(request_id, generation, result, error_text)
            except RuntimeError:
                pass  # Parent account/window may have been destroyed while I/O finished.
        try:
            self._busy += 1
            self._pool.submit(work)
        except RuntimeError:
            self._busy -= 1
            pass  # A closing renderer has no remaining RPC consumer.

    @Slot(str, int, object, str)
    def _deliver(self, request_id, generation, result, error):
        try:
            if self._closed or generation != self._generation or not self._authorized():
                return
            if self._cancelling and isinstance(result, dict) and 'token' in result:
                result, error = None, 'Авторизация звонка отменена.'
            if error:
                self.failure.emit(error)
            self.reply.emit(request_id, json.dumps({'ok': not bool(error), 'result': result if not error else None,
                                                   'error': error}, ensure_ascii=False))
        finally:
            self._busy = max(0, self._busy - 1)
            if not self._busy:
                self.idle.emit()

    @Slot(str, str)
    def report(self, name, metadata_json):
        # Renderer error messages/SDK objects can contain tokens and endpoints.
        if self._closed or name not in {'ready', 'ringing', 'active', 'ended', 'error', 'permissions', 'device_change'}:
            return
        self.event.emit(name, '{}')

    def close(self):
        with self._lock:
            self._closed = True
            self._generation += 1
            self._intent = None
            self._conversation = None
        self._pool.shutdown(wait=False, cancel_futures=True)
