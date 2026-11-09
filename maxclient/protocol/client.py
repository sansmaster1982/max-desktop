"""MaxClient — одно TLS-соединение к api.oneme.ru:443 + фоновый читающий поток.

Запросы идут через request() и матчатся с ответами по seq. Кадры без нашего seq
считаются server-push и отдаются в on_push. Класс не зависит от GUI: колбэки
on_push/on_state вызываются из читающего потока, обёртку в Qt-сигналы делает UI.

Портирование рабочего test5.py (Python) + Flutter max_client.dart.
"""
from __future__ import annotations

import random
import copy
import re
import math
import socket
import ssl
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Optional

from . import opcodes
from .codec import (
    HEADER_LEN,
    MAX_PAYLOAD_BYTES,
    build_frame,
    decompress_body,
    parse_header,
    unpack_payload,
)
from .errors import MaxError, MaxLoginFailed, MaxNotConnected, MaxTimeout
from .settings_contracts import privacy_values, user_setting_values
from .search_contracts import (build_message_search, parse_message_search,
                               build_global_search, parse_global_search)
from .conversation_contracts import (build_common_chats, parse_common_chats,
                                     build_info_media, parse_info_media)
from .conversation_actions_contracts import (
    build_chat_mute, parse_chat_mute_ack, build_story_visibility,
    parse_story_visibility_ack, build_stop_bot, parse_stop_bot_ack,
)
from .file_download_contracts import build_file_download, parse_file_download, FileDownloadReply
from .miniapp_contracts import build_miniapp_launch, parse_miniapp_launch, MiniAppReply
from .conversation_lifecycle_contracts import (build_chat_lifecycle, parse_lifecycle_ack,
                                                build_contact_share, parse_contact_share_ack,
                                                build_chat_leave, parse_leave_ack)
from .conversation_report_contracts import (build_report_reasons, parse_report_reasons,
                                            build_conversation_report, parse_report_ack)

HOST = "api.oneme.ru"
PORT = 443
PROTO_VER = 10
DEFAULT_APP_VERSION = "26.15.0"
DEFAULT_LOCALE = "ru"
# versionCode официального APK, согласован с DEFAULT_APP_VERSION (26.15.0 → 6689).
# Идёт в userAgent.buildNumber для ANDROID-INIT (см. _build_user_agent).
APP_BUILD = 6689

# Чувствительные ключи маскируются в логах (PII/секреты). _redact приводит
# ключ к нижнему регистру перед проверкой. email — PII, verifycode — одноразовый
# код из письма (recovery-email флоу 109/110).
_SECRET_KEYS = {
    "token", "password", "oldpassword", "newpassword", "trackid",
    "verifycode", "email", "qrlink", "phototoken",
}


class ConnectionState(Enum):
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    RECONNECTING = "reconnecting"


class MaxWriteOutcomeUnknown(MaxError):
    """A sent write did not yield a complete authoritative acknowledgement.

    Callers must check server state before considering another submission.
    """


class MaxWriteNotStarted(ValueError):
    """An expected authenticated context changed before any packet was sent."""


@dataclass(frozen=True, eq=False)
class AuthenticatedSession:
    """Local identity of one accepted LOGIN on one transport; contains no token."""
    owner_id: int
    device_id: str = field(repr=False)
    device_type: str
    transport: object = field(repr=False)


@dataclass
class MaxFrame:
    cmd: int
    seq: int
    opcode: int
    body: bytes
    decoded: Any
    auth_context: Optional[AuthenticatedSession] = field(default=None, repr=False, compare=False)

    @property
    def ok(self) -> bool:
        return self.cmd == 1

    def error_text(self) -> str:
        d = self.decoded
        if isinstance(d, dict):
            value = d.get("error")
            if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", value):
                return value
        return "server_rejected_request"


class _Pending:
    __slots__ = ("event", "frame", "error", "expected_opcode", "expected_socket")

    def __init__(self, expected_opcode: int, expected_socket=None) -> None:
        self.event = threading.Event()
        self.frame: Optional[MaxFrame] = None
        self.error: Optional[BaseException] = None
        self.expected_opcode = expected_opcode
        self.expected_socket = expected_socket


class _WebSocketTransport:
    """Binary stream adapter for the official WEB transport, with verified TLS."""

    def __init__(self, timeout: float) -> None:
        import websocket

        self._pending = bytearray()
        try:
            self.ws = websocket.create_connection(
                "wss://api.oneme.ru/websocket",
                origin="https://web.max.ru",
                timeout=timeout,
                enable_multithread=True,
                sslopt={"cert_reqs": ssl.CERT_REQUIRED, "check_hostname": True},
            )
            self.ws.settimeout(None)
        except Exception:
            raise MaxNotConnected("Could not connect to the verified MAX WebSocket") from None

    def sendall(self, data: bytes) -> None:
        try:
            self.ws.send_binary(data)
        except Exception:
            raise OSError("WebSocket send failed") from None

    def recv(self, count: int) -> bytes:
        if not self._pending:
            try:
                frame = self.ws.recv()
            except Exception:
                raise OSError("WebSocket receive failed") from None
            if frame in (None, b"", ""):
                return b""
            if not isinstance(frame, bytes) or len(frame) > MAX_PAYLOAD_BYTES + HEADER_LEN:
                raise OSError("Invalid or oversized MAX WebSocket frame")
            self._pending.extend(frame)
        data = bytes(self._pending[:count])
        del self._pending[:count]
        return data

    def shutdown(self, _how=None) -> None:
        self.ws.abort()

    def close(self) -> None:
        self.ws.shutdown()


class MaxClient:
    def __init__(
        self,
        *,
        app_version: str = DEFAULT_APP_VERSION,
        locale: str = DEFAULT_LOCALE,
        device_id: Optional[str] = None,
        device_profile: Optional[dict] = None,
        on_push: Optional[Callable[[MaxFrame], None]] = None,
        on_state: Optional[Callable[[ConnectionState], None]] = None,
        on_debug: Optional[Callable[[str], None]] = None,
        on_auth_invalid: Optional[Callable[[], None]] = None,
        on_reauthenticated: Optional[Callable[[MaxFrame, AuthenticatedSession], None]] = None,
        auto_reconnect: bool = True,
    ) -> None:
        self.app_version = app_version
        self.locale = locale
        self.device_id = device_id or str(uuid.uuid4())
        # Сохранённые поля исторического ANDROID userAgent. При отсутствии
        # метаданных используются прежние значения; серверная оценка неизвестна.
        self.device_profile = device_profile or {}
        self.on_push = on_push
        self.on_state = on_state
        self.on_debug = on_debug
        # Вызывается из reconnect-потока, когда сервер отверг сохранённый токен
        # (мёртвый/протухший): UI должен разлогинить и показать экран входа, а
        # не висеть в «не в сети». Порт onAuthInvalid из max iso.
        self.on_auth_invalid = on_auth_invalid
        self.on_reauthenticated = on_reauthenticated
        self.auto_reconnect = auto_reconnect

        self._sock: Optional[ssl.SSLSocket | _WebSocketTransport] = None
        self._reader: Optional[threading.Thread] = None
        self._send_lock = threading.Lock()
        self._seq = 0
        self._pending: dict[int, _Pending] = {}
        self._pending_lock = threading.Lock()
        self._closed = False
        self._state = ConnectionState.DISCONNECTED

        self._token: Optional[str] = None
        self._device_type = "ANDROID"
        self._auth_session: Optional[AuthenticatedSession] = None

        self._reconnect_thread: Optional[threading.Thread] = None
        self._reconnect_running = False
        self._reconnect_stop: Optional[threading.Event] = None
        # Сериализует жизненный цикл реконнекта (старт/стоп/успех-коммит и
        # решение «пришёл дроп» в reader-потоке). В Dart-reference это один
        # event-loop; в Python reader/reconnect/UI — разные потоки, поэтому
        # нужен общий замок. RLock — _handle_drop под локом зовёт _start_reconnect.
        self._reconnect_lock = threading.RLock()

        # Keepalive и ограниченный reconnect предотвращают локальный цикл
        # повторных соединений при простое или обрыве транспорта.
        self._keepalive_thread: Optional[threading.Thread] = None
        self._keepalive_stop: Optional[threading.Event] = None

        # Анти-шторм (порт ReconnectPolicy из max iso). Главный инвариант: не
        # делать LOGIN (op19) чаще, чем раз в MIN_AUTH_INTERVAL. _last_login_ts —
        # монотонное время последнего успешного LOGIN; НЕ сбрасывается при дропе
        # (считаем время с АВТОРИЗАЦИИ, а не с разрыва). _reconnect_ok_ts —
        # времена успешных реконнектов для предохранителя флаппинга.
        self._last_login_ts: Optional[float] = None
        self._reconnect_ok_ts: list[float] = []

        # Кэш chatId -> peer userId для 1:1 диалогов. В личный диалог сервер
        # принимает userId собеседника, НЕ chatId (zg9.java: chatId XOR userId).
        # Узнаём peer из ошибки сервера common.finder и шлём дальше уже по userId.
        self._chat_peer: dict[int, int] = {}
        self._message_cid_lock = threading.Lock()
        self._last_message_cid = 0

    KEEPALIVE_INTERVAL = 20.0   # локальный интервал PING, секунды
    RECONNECT_BASE = 5.0        # база экспоненциального backoff
    RECONNECT_MAX = 60.0        # потолок backoff
    MIN_AUTH_INTERVAL = 30.0    # локальная нижняя граница частоты LOGIN
    BREAKER_WINDOW = 300.0      # окно подсчёта успешных реконнектов (5 мин)
    BREAKER_MAX_OK = 6          # столько реконнектов за окно = флаппинг
    BREAKER_COOLDOWN = 480.0    # длинная пауза при флаппинге (8 мин)

    # ───────────────────────── state / lifecycle ─────────────────────────

    @property
    def state(self) -> ConnectionState:
        return self._state

    @property
    def is_connected(self) -> bool:
        return self._sock is not None and not self._closed

    @property
    def token(self) -> Optional[str]:
        return self._token

    @property
    def authenticated_session(self) -> Optional[AuthenticatedSession]:
        context = self._auth_session
        if (context is not None and context.transport is self._sock and
                self._sock is not None and not self._closed and
                self.state == ConnectionState.CONNECTED and
                context.device_id == self.device_id and context.device_type == self._device_type):
            return context
        return None

    def _set_state(self, s: ConnectionState) -> None:
        if self._state == s:
            return
        self._state = s
        if self.on_state:
            try:
                self.on_state(s)
            except Exception:
                pass

    def _debug(self, msg: str) -> None:
        if self.on_debug:
            try:
                self.on_debug(msg)
            except Exception:
                pass

    def connect(
        self,
        device_type: str = "ANDROID",
        timeout: float = 15.0,
        *,
        reset_closed: bool = True,
    ) -> None:
        """Открыть TLS, запустить читающий поток и выполнить INIT-хендшейк.

        reset_closed=True — пользовательский вход: сбрасывает флаг «закрыто
        навсегда» и гасит фоновый реконнект, чтобы тот не сделал параллельный
        второй connect+LOGIN и не перезаписал сокет пользовательского входа.
        Авто-реконнект зовёт connect(reset_closed=False): НЕ трогает _closed
        (иначе воскресил бы сессию, которую пользователь явно закрыл) и
        прерывается, если попытку успели отменить. Порт reconnect() из max iso,
        где auto-путь не трогает _closed.
        """
        self._device_type = device_type
        if reset_closed:
            # Пользовательский connect: стопаем возможный фоновый реконнект ДО
            # любого I/O, иначе его поток мог бы параллельно сделать второй
            # connect+LOGIN на том же токене.
            self._stop_reconnect()
            self._closed = False
        else:
            # auto-путь: если пока мы выходили из ожидания, пользователь
            # закрыл/перелогинился — отменяем попытку до I/O, чтобы не порвать
            # свежий пользовательский сокет и не сделать лишний LOGIN.
            with self._reconnect_lock:
                if self._closed or not self._reconnect_running:
                    raise MaxNotConnected("reconnect cancelled")
        # Один MaxClient владеет одним текущим транспортом; старый закрывается
        # перед новым connect, чтобы его reader не вмешивался в новую сессию.
        if self._sock is not None:
            self._teardown_socket()
        self._set_state(ConnectionState.CONNECTING)
        tls = None
        try:
            if device_type == "WEB":
                tls = _WebSocketTransport(timeout)
            else:
                ctx = ssl.create_default_context()
                raw = self._open_raw_socket(timeout)
                raw.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                tls = ctx.wrap_socket(raw, server_hostname=HOST)
                tls.settimeout(None)  # blocking reader; close() wakes it.
            self._sock = tls
            self._seq = 0

            self._reader = threading.Thread(
                target=self._read_loop, name="max-reader", daemon=True
            )
            self._reader.start()

            self._init_session()
            self._set_state(ConnectionState.CONNECTED)
            self._start_keepalive()
        except Exception:
            # Рвём ТОЛЬКО наш сокет: если пока мы висели в сетевом I/O
            # пользовательский connect() уже поставил свой self._sock (барьер
            # join мог истечь раньше нашего таймаута), его не трогаем — иначе
            # убили бы живую сессию. tls=None → свой сокет не публиковали.
            if tls is not None:
                self._teardown_socket(only=tls)
            if not self.is_connected:
                self._set_state(ConnectionState.DISCONNECTED)
            raise

    def _open_raw_socket(self, timeout: float) -> socket.socket:
        """Открыть TCP-сокет к серверу. Если системный DNS не резолвит хост
        (частый случай при фильтрующем провайдере — браузер обходит это через
        DoH), резолвим адрес через DNS-over-HTTPS и коннектимся по IP."""
        try:
            return socket.create_connection((HOST, PORT), timeout=timeout)
        except socket.gaierror as e:
            self._debug(f"system DNS failed for {HOST} ({e}); trying DoH")
            ip = _doh_resolve(HOST)
            if not ip:
                raise MaxNotConnected(
                    f"Не удалось разрешить адрес {HOST}. Проверьте интернет, "
                    "DNS или VPN."
                ) from e
            self._debug(f"DoH resolved {HOST} -> {ip}")
            return socket.create_connection((ip, PORT), timeout=timeout)

    def close(self) -> None:
        """Явное закрытие пользователем — блокирует авто-реконнект."""
        self._closed = True
        # join_timeout=0: при выходе из приложения не подвисаем на догорающем
        # реконнекте (поток daemon — всё равно умрёт). Корректность держат флаг
        # _closed (его видят re-check'и в цикле) + сверка сокета в _handle_drop.
        self._stop_reconnect(join_timeout=0.0)
        self._teardown_socket()
        self._set_state(ConnectionState.DISCONNECTED)

    def _teardown_socket(self, only: Optional[ssl.SSLSocket | _WebSocketTransport] = None) -> None:
        # only!=None — рвём, ТОЛЬКО если это всё ещё текущий сокет. Иначе
        # «опоздавший» reconnect-поток (его connect() висел в сетевом I/O дольше,
        # чем барьер join ждал) закрыл бы свежий сокет пользовательского connect()
        # и убил бы живую сессию. Та же сокет-идентичность, что в _handle_drop.
        if only is not None and self._sock is not only:
            return
        self._auth_session = None
        self._stop_keepalive()
        sock = self._sock
        self._sock = None
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except Exception:
                pass
            try:
                sock.close()
            except Exception:
                pass
        self._fail_all_pending(MaxNotConnected("disconnected"))

    def _fail_all_pending(self, error: BaseException) -> None:
        with self._pending_lock:
            pendings = list(self._pending.values())
            self._pending.clear()
        for p in pendings:
            p.error = error
            p.event.set()

    # ───────────────────────── reader thread ─────────────────────────

    def _read_loop(self) -> None:
        buffer = bytearray()
        sock = self._sock
        try:
            while not self._closed and sock is not None:
                try:
                    data = sock.recv(65536)
                except OSError:
                    break
                if not data:
                    break
                buffer += data

                while len(buffer) >= HEADER_LEN:
                    _ver, cmd, seq, opcode, plen, comp = parse_header(buffer[:HEADER_LEN])
                    total = HEADER_LEN + plen
                    if len(buffer) < total:
                        break
                    raw_body = bytes(buffer[HEADER_LEN:total])
                    del buffer[:total]
                    # Current WEB/native byte255 is LZ4. The historical Android
                    # Zstd variant is selected only by the existing mobile role.
                    # из заголовка. Дальше весь разбор идёт по «чистым» байтам.
                    body = decompress_body(raw_body, comp, legacy_android_zstd=self._device_type == 'ANDROID')
                    frame = MaxFrame(
                        cmd=cmd,
                        seq=seq,
                        opcode=opcode,
                        body=body,
                        decoded=unpack_payload(body),
                    )
                    self._dispatch(frame, source=sock)
        except Exception as error:
            # Packet bodies, server messages and secrets never enter debug.log.
            self._debug(f"reader stopped: {type(error).__name__}")
        finally:
            self._handle_drop(sock)

    def _dispatch(self, frame: MaxFrame, *, source=None) -> None:
        received_context = self.authenticated_session
        incoming_socket = self._sock if source is None else source
        if incoming_socket is not self._sock:
            return  # A former reader cannot reply for the replacement connection.
        frame.auth_context = (received_context if received_context is not None and
                              received_context.transport is incoming_socket else None)
        if frame.cmd == 0 and frame.opcode == opcodes.PING:
            sock = incoming_socket
            if sock is not None and not self._closed:
                acknowledgement = bytearray(HEADER_LEN)
                acknowledgement[0] = PROTO_VER
                acknowledgement[1] = 1
                acknowledgement[2:4] = frame.seq.to_bytes(2, "big")
                acknowledgement[4:6] = frame.opcode.to_bytes(2, "big")
                with self._send_lock:
                    sock.sendall(acknowledgement)
            return
        if frame.cmd == 0 and frame.opcode == opcodes.NOTIF_MESSAGE:
            data = frame.decoded if isinstance(frame.decoded, dict) else {}
            message = data.get("message") if isinstance(data.get("message"), dict) else {}
            chat_id, message_id = data.get("chatId"), message.get("id")
            if (incoming_socket is not None and not self._closed
                    and type(chat_id) is int and type(message_id) is int):
                # Receipt of a server-delivered message, not a human message or
                # CHAT_MARK read receipt. Keep its sequence outside our requests.
                acknowledgement = build_frame(frame.seq, frame.opcode,
                                              {"chatId": chat_id, "messageId": message_id}, cmd=1)
                with self._send_lock:
                    incoming_socket.sendall(acknowledgement)
        with self._pending_lock:
            pending = self._pending.get(frame.seq)
            if (pending is not None and frame.cmd in (1, 3) and frame.opcode == pending.expected_opcode
                    and (pending.expected_socket is None or pending.expected_socket is incoming_socket)):
                self._pending.pop(frame.seq)
            else:
                pending = None
        if pending is not None:
            pending.frame = frame
            pending.event.set()
            return
        # Кадр без нашего seq — это push от сервера.
        if frame.cmd == 0 and self.on_push:
            try:
                self.on_push(frame)
            except Exception:
                pass

    def _handle_drop(self, sock: Optional[ssl.SSLSocket | _WebSocketTransport] = None) -> None:
        # Идентификация сокета: устаревший reader (от уже заменённого сокета) не
        # должен рвать сокет-преемник, поднятый параллельным connect() (иначе
        # закрыл бы свежую сессию пользователя и обнулил бы pending нового
        # сокета). Dart отменяет подписку до переустановки _socket; у нас этого
        # нет — поэтому сверяем по объекту сокета. Проверка ДО _fail_all_pending.
        if sock is not None and sock is not self._sock:
            return
        if self._sock is None and self._closed:
            return
        self._auth_session = None
        self._fail_all_pending(MaxNotConnected("socket closed"))
        try:
            if self._sock is not None:
                self._sock.close()
        except Exception:
            pass
        self._sock = None
        if self._closed:
            self._set_state(ConnectionState.DISCONNECTED)
            return
        # Под локом, чтобы решение «пришёл дроп → стартуем реконнект» не
        # разъезжалось с «успех-коммитом» reconnect-потока (иначе дроп в окне
        # успеха мог быть потерян и клиент завис бы в RECONNECTING).
        with self._reconnect_lock:
            if self.auto_reconnect and self._token:
                self._set_state(ConnectionState.RECONNECTING)
                self._start_reconnect()
            else:
                self._set_state(ConnectionState.DISCONNECTED)

    # ───────────────────────── keepalive ─────────────────────────

    def _start_keepalive(self) -> None:
        self._stop_keepalive()
        self._keepalive_stop = threading.Event()
        self._keepalive_thread = threading.Thread(
            target=self._keepalive_loop, name="max-keepalive", daemon=True
        )
        self._keepalive_thread.start()

    def _stop_keepalive(self) -> None:
        ev = self._keepalive_stop
        if ev is not None:
            ev.set()
        self._keepalive_thread = None

    def _keepalive_loop(self) -> None:
        ev = self._keepalive_stop
        if ev is None:
            return
        while not self._closed:
            # Прерываемый сон: stop.set() будит сразу.
            if ev.wait(self.KEEPALIVE_INTERVAL):
                return
            if self._closed or self._sock is None:
                return
            self._send_fire(opcodes.PING, {})

    def _send_fire(self, opcode: int, payload: dict) -> None:
        """Отправить кадр без ожидания ответа (для PING). Ответ придёт как
        кадр без нашего seq и тихо отбросится в push-обработчике."""
        sock = self._sock
        if sock is None or self._closed:
            return
        with self._send_lock:
            seq = self._seq
            self._seq = (self._seq + 1) & 0xFFFF
            frame = build_frame(seq, opcode, payload)
            try:
                sock.sendall(frame)
            except OSError:
                return  # сокет мёртв — дроп обработает reader
        self._debug(f">> {opcodes.name(opcode)} seq={seq} (keepalive)")

    # ───────────────────────── reconnect ─────────────────────────

    def _start_reconnect(self) -> None:
        with self._reconnect_lock:
            # Гард по ЖИВОСТИ потока, а не по флагу: при гонке «дроп в окне
            # успеха» флаг _reconnect_running мог остаться True у уже выходящего
            # потока — флаговый гард тогда навсегда заглушил бы авто-реконнект.
            t = self._reconnect_thread
            if t is not None and t.is_alive():
                return
            self._reconnect_running = True
            self._reconnect_stop = threading.Event()
            self._reconnect_thread = threading.Thread(
                target=self._reconnect_loop, name="max-reconnect", daemon=True
            )
            self._reconnect_thread.start()

    def _stop_reconnect(self, join_timeout: float = 10.0) -> None:
        """Остановить авто-реконнект. join_timeout>0 — БАРЬЕР: дождаться, пока
        reconnect-поток реально выйдет, прежде чем продолжить. Без барьера
        пользовательский connect() мог бы параллельно с догорающим реконнектом
        писать self._sock и сделать второй LOGIN на том же токене. Из самого reconnect-потока
        (ветка мёртвого токена) join пропускаем: self-join → deadlock."""
        with self._reconnect_lock:
            self._reconnect_running = False
            ev = self._reconnect_stop
            t = self._reconnect_thread
        if ev is not None:
            ev.set()  # будит спящий реконнект сразу
        if (
            join_timeout > 0
            and t is not None
            and t is not threading.current_thread()
            and t.is_alive()
        ):
            t.join(timeout=join_timeout)

    def _since_last_login(self) -> float:
        """Секунд с последнего успешного LOGIN. Очень большое число, если в этой
        сессии ещё не логинились — тогда первый реконнект не тормозим."""
        if self._last_login_ts is None:
            return 1e9
        return time.monotonic() - self._last_login_ts

    def _auth_throttle(self) -> float:
        """Сколько ещё ждать, чтобы соблюсти MIN_AUTH_INTERVAL между LOGIN.
        Это и отличает честный дроп после долгой стабильной сессии (логинились
        давно → 0) от шторма (логинились только что → ждём остаток интервала)."""
        since = self._since_last_login()
        if since >= self.MIN_AUTH_INTERVAL:
            return 0.0
        return self.MIN_AUTH_INTERVAL - since

    def _prune_breaker_window(self) -> None:
        cutoff = time.monotonic() - self.BREAKER_WINDOW
        self._reconnect_ok_ts = [t for t in self._reconnect_ok_ts if t >= cutoff]

    def _reconnect_delay(self, attempt: int) -> float:
        """Итоговая пауза перед попыткой: max(backoff, auth-throttle,
        cooldown-при-флаппинге) + джиттер. Порт ReconnectPolicy.nextDelay из
        max iso. throttle и cooldown НЕ ограничены RECONNECT_MAX — это
        намеренно более длинные паузы (потолок частоты LOGIN и анти-флаппинг)."""
        a = max(0, min(attempt, 16))
        delay = min(self.RECONNECT_BASE * float(1 << a), self.RECONNECT_MAX)
        throttle = self._auth_throttle()
        if throttle > delay:
            delay = throttle
        self._prune_breaker_window()
        if len(self._reconnect_ok_ts) >= self.BREAKER_MAX_OK and self.BREAKER_COOLDOWN > delay:
            delay = self.BREAKER_COOLDOWN
            self._debug(
                f"reconnect breaker: {len(self._reconnect_ok_ts)} re-auth за "
                f"{self.BREAKER_WINDOW:.0f}s → cooldown {self.BREAKER_COOLDOWN:.0f}s"
            )
        return delay + random.uniform(0.0, self.RECONNECT_BASE / 2.0)

    def _reconnect_loop(self) -> None:
        ev = self._reconnect_stop
        if ev is None:
            return
        attempt = 0
        while (self._reconnect_stop is ev and not ev.is_set()
               and self._reconnect_running and not self._closed):
            delay = self._reconnect_delay(attempt)
            self._debug(
                f"reconnect через {delay:.1f}s (попытка {attempt}, "
                f"успехов в окне {len(self._reconnect_ok_ts)}, "
                f"с LOGIN {self._since_last_login():.0f}s)"
            )
            if ev.wait(delay):
                return  # запрошен стоп (close) — выходим без попытки
            if self._reconnect_stop is not ev or self._closed or not self._reconnect_running:
                return
            my_sock = None
            try:
                self.connect(device_type=self._device_type, reset_closed=False)
                if self._reconnect_stop is not ev:
                    return  # A replacement generation owns any current socket.
                # Сокет, поднятый ИМЕННО этой попыткой. Все teardown'ы ниже рвут
                # только его (only=my_sock): если параллельный пользовательский
                # connect() истёк по барьеру join раньше нашего I/O и уже поставил
                # свой self._sock — мы его не тронем (иначе убили бы живую сессию).
                my_sock = self._sock
                # close() мог прийти, пока шёл connect (он поднял сокет+keepalive,
                # но reset_closed=False не трогал _closed). Если так — рвём свой
                # сокет и выходим БЕЗ LOGIN, чтобы не воскрешать закрытую сессию.
                if self._closed or not self._reconnect_running:
                    self._teardown_socket(only=my_sock)
                    if not self.is_connected:
                        self._set_state(ConnectionState.DISCONNECTED)
                    return
                if self._token:
                    login_frame = self.login(self._token)
                    context = self.authenticated_session
                    if self.on_reauthenticated is not None:
                        if context is None or context.transport is not my_sock:
                            raise MaxWriteOutcomeUnknown("Reconnected LOGIN session was replaced")
                        try:
                            accepted = self.on_reauthenticated(login_frame, context)
                        except Exception:
                            if self._reconnect_stop is not ev:
                                return
                            if self.authenticated_session is not context:
                                with self._reconnect_lock:
                                    if self._reconnect_stop is ev:
                                        self._stop_reconnect(join_timeout=0.0)
                                return  # A newer LOGIN owns the same or a replacement socket.
                            raise MaxWriteOutcomeUnknown("Reconnected account state was not confirmed") from None
                        if self._reconnect_stop is not ev:
                            return
                        if self.authenticated_session is not context:
                            with self._reconnect_lock:
                                if self._reconnect_stop is ev:
                                    self._stop_reconnect(join_timeout=0.0)
                            return
                        if accepted is False:
                            raise MaxWriteOutcomeUnknown("Reconnected account state was not confirmed")
                if self._reconnect_stop is not ev:
                    return
                if self._closed or not self._reconnect_running:
                    self._teardown_socket(only=my_sock)
                    if not self.is_connected:
                        self._set_state(ConnectionState.DISCONNECTED)
                    return
                # LOGIN состоялся. Кормим предохранитель СРАЗУ, не дожидаясь,
                # переживёт ли сокет окно успеха: учитываем каждую re-auth
                # (op19). Иначе «connect-OK → мгновенный
                # дроп» крутил бы ~120 LOGIN/час МИМО счётчика флаппинга и cooldown
                # никогда не срабатывал бы. Порт max_client.dart:1184 — там тоже
                # считают каждую успешную авторизацию, безусловно.
                with self._reconnect_lock:
                    if self._reconnect_stop is not ev:
                        return
                    self._reconnect_ok_ts.append(time.monotonic())
                    if self.is_connected:
                        # Сокет жив — успех зафиксирован, гасим цикл. Обнуляем
                        # хэндл потока, чтобы гонка «дроп в лаге reap» (is_alive()
                        # ещё True у уже вышедшего потока) не заглушила новый
                        # реконнект навсегда — _start_reconnect увидит None.
                        self._debug("reconnect succeeded")
                        self._reconnect_running = False
                        self._reconnect_thread = None
                        return
                # Сокет отвалился в окне успеха: _handle_drop не смог стартовать
                # новый поток (наш ещё жив) — крутимся сами, иначе клиент завис бы
                # в RECONNECTING. На следующем круге _auth_throttle не даст
                # повторный LOGIN раньше MIN_AUTH_INTERVAL.
                self._debug("socket dropped during success window — retry")
                attempt = 0
                continue
            except MaxWriteOutcomeUnknown:
                # An incomplete ACK is not evidence that the saved credential
                # was rejected. Stop this attempt without clearing it or
                # automatically submitting LOGIN again.
                with self._reconnect_lock:
                    if self._reconnect_stop is not ev:
                        return
                    self._debug("reconnect stopped: LOGIN outcome unconfirmed")
                    if self._reconnect_running:
                        self._stop_reconnect(join_timeout=0.0)
                    if my_sock is not None:
                        self._teardown_socket(only=my_sock)
                    if not self.is_connected:
                        self._set_state(ConnectionState.DISCONNECTED)
                return
            except MaxLoginFailed:
                # Токен мёртв — нет смысла долбить сервер протухшим токеном.
                # ОБЯЗАТЕЛЬНО рвём уже поднятый (connect успел) сокет, иначе
                # keepalive пинговал бы неавторизованное соединение (аномальный
                # трафик). Чистим токен, уходим в DISCONNECTED и сигналим UI
                # (on_auth_invalid) уйти на экран входа.
                with self._reconnect_lock:
                    # An old failure must not clear a newer credential or stop
                    # its reconnect worker, even when that worker is running.
                    if self._reconnect_stop is not ev:
                        return
                    self._debug("reconnect aborted: saved credential rejected")
                    active = self._reconnect_running
                    if active:
                        self._token = None
                        self._stop_reconnect(join_timeout=0.0)
                    if my_sock is not None:
                        self._teardown_socket(only=my_sock)
                    if active:
                        if not self.is_connected:
                            self._set_state(ConnectionState.DISCONNECTED)
                        if self.on_auth_invalid:
                            try:
                                self.on_auth_invalid()
                            except Exception:
                                pass
                return
            except Exception as e:  # noqa: BLE001
                if self._reconnect_stop is not ev:
                    return
                self._debug(f"reconnect failed: {type(e).__name__}")
                attempt += 1

    # ───────────────────────── request / response ─────────────────────────

    def request(
        self,
        opcode: int,
        payload: dict | None,
        *,
        timeout: float = 30.0,
        cmd: int = 0,
        expected_session: Optional[AuthenticatedSession] = None,
    ) -> MaxFrame:
        """Синхронно отправить запрос и дождаться ответа с тем же seq.

        Вызывать из рабочего потока, не из GUI-треда (блокирует до ответа).
        """
        sock = self._sock
        if expected_session is not None:
            self._require_request_session(expected_session, sock)
            sock = expected_session.transport
        if sock is None or self._closed:
            raise MaxNotConnected("socket is null")

        pending = _Pending(opcode, sock)
        with self._send_lock:
            if expected_session is not None:
                self._require_request_session(expected_session, sock)
            seq = self._seq
            self._seq = (self._seq + 1) & 0xFFFF
            with self._pending_lock:
                self._pending[seq] = pending
            frame = build_frame(seq, opcode, payload, cmd=cmd)
            self._debug(f">> {opcodes.name(opcode)} seq={seq}")
            try:
                if expected_session is not None:
                    self._require_request_session(expected_session, sock)
                sock.sendall(frame)
            except MaxWriteNotStarted:
                with self._pending_lock:
                    self._pending.pop(seq, None)
                raise
            except OSError as e:
                with self._pending_lock:
                    self._pending.pop(seq, None)
                raise MaxNotConnected("send failed") from None

        if not pending.event.wait(timeout):
            with self._pending_lock:
                self._pending.pop(seq, None)
            raise MaxTimeout(f"{opcodes.name(opcode)} timeout")

        if pending.error is not None:
            raise pending.error
        assert pending.frame is not None
        f = pending.frame
        if f.cmd == 1:
            self._debug(f"<< {opcodes.name(opcode)} seq={seq} cmd=1 len={len(f.body)}")
        else:
            self._debug(
                f"<< {opcodes.name(opcode)} seq={seq} cmd={f.cmd} NOT-OK "
                f"error={'redacted' if opcode == opcodes.BOT_WEBAPP_OPEN else f.error_text()} len={len(f.body)}"
            )
        return f

    def _require_request_session(self, expected_session, sock):
        if (not isinstance(expected_session, AuthenticatedSession)
                or self.authenticated_session is not expected_session
                or expected_session.transport is not sock or self._sock is not sock):
            raise MaxWriteNotStarted('The authenticated conversation changed before sending.')

    # ───────────────────────── auth ─────────────────────────

    def _init_session(self) -> None:
        f = self.request(
            opcodes.INIT,
            {
                "userAgent": self._build_user_agent(),
                "deviceId": self.device_id,
            },
        )
        if not f.ok:
            raise MaxError(f"INIT failed cmd={f.cmd}")

    def _build_user_agent(self) -> dict:
        """userAgent для INIT (op6).

        ANDROID сохраняет поля и значения исторического reference-клиента.
        WEB отправляет поддерживаемое здесь подмножество optional-полей
        опубликованной схемы. Текущий официальный WEB producer включает и
        дополнительные поля окружения; их отсутствие не доказывает отказ входа.
        Серверные проверки fingerprint/TLS/UA и значимость порядка ключей
        этим кодом и статическим исследованием не установлены.
        """
        if self._device_type == "ANDROID":
            ua: dict[str, Any] = {
                "deviceType": "ANDROID",
                "pushDeviceType": "GCM",
                "appVersion": self.app_version,
                "arch": "arm64-v8a",
            }
            # buildNumber согласован только с дефолтной версией (26.15.0 → 6689).
            # Для переопределённой версии соответствующий buildNumber неизвестен.
            if self.app_version == DEFAULT_APP_VERSION:
                ua["buildNumber"] = APP_BUILD
            # Модель/экран/osVersion — из per-install профиля (стабильны на
            # установку, но разные между инсталляциями), с дефолтами как раньше.
            prof = self.device_profile
            ua.update(
                {
                    "osVersion": str(prof.get("osVersion") or "34"),
                    "locale": self.locale,
                    "deviceLocale": "ru_RU",
                    "deviceName": str(prof.get("deviceName") or "Android"),
                    "screen": str(prof.get("screen") or "1080x2340"),
                    "timezone": _iana_timezone(),
                }
            )
            return ua
        return {
            "deviceType": self._device_type,
            "pushDeviceType": "WEBPUSH",
            "locale": self.locale,
            "appVersion": self.app_version,
        }

    def start_auth_sms(self, phone: str) -> str:
        """Запросить SMS. Возвращает verify-token для confirm_sms."""
        f = self._auth_frame(opcodes.AUTH_REQUEST, {"phone": phone, "type": "START_AUTH"})
        data = f.decoded
        if (not isinstance(data, dict)
                or not _is_bounded_qr_string(data.get("token"), limit=65536)):
            raise MaxWriteOutcomeUnknown("AUTH_REQUEST: verify-токен не подтверждён MAX.")
        # The named token in17 is a verification token, not a LOGIN credential.
        return data["token"]

    def confirm_sms(self, verify_token: str, code: str) -> tuple[Optional[str], Optional[str]]:
        """Подтвердить SMS-код. Возвращает (auth_token, track_id_for_2fa)."""
        f = self._auth_frame(
            opcodes.AUTH_CONFIRM,
            {
                "token": verify_token,
                "verifyCode": code,
                "authTokenType": "CHECK_CODE",
            },
        )
        data = self._typed_auth_result(f, allow_password_challenge=True, label="AUTH_CONFIRM")
        if "passwordChallenge" in data:
            return None, data["passwordChallenge"]["trackId"]
        return data["tokenAttrs"]["LOGIN"]["token"], None

    def confirm_2fa(self, track_id: str, password: str) -> str:
        f = self._auth_frame(opcodes.TWO_FA, {"trackId": track_id, "password": password})
        data = self._typed_auth_result(f, allow_password_challenge=False, label="AUTH_2FA")
        return data["tokenAttrs"]["LOGIN"]["token"]

    def _auth_frame(self, opcode: int, payload: dict, *,
                    expected_session: Optional[AuthenticatedSession] = None) -> MaxFrame:
        """One auth submission; malformed/absent ACKs never invalidate a token."""
        try:
            kwargs = {'expected_session': expected_session} if expected_session is not None else {}
            frame = self.request(opcode, payload, **kwargs)
        except (MaxTimeout, MaxNotConnected):
            raise MaxWriteOutcomeUnknown(
                "Результат входа не подтверждён. Повторный запрос не отправлен."
            ) from None
        if frame.cmd == 3 and frame.opcode == opcode:
            raise MaxLoginFailed(f"{opcodes.name(opcode)}: {frame.error_text()}")
        if frame.cmd != 1 or frame.opcode != opcode:
            raise MaxWriteOutcomeUnknown("MAX не подтвердил ожидаемый ответ входа.")
        return frame

    def login(self, token: str) -> MaxFrame:
        """Логин по auth-token. Возвращает кадр LOGIN (raw содержит чаты/контакты)."""
        sock = self._sock
        login_device_id, login_device_type = self.device_id, self._device_type
        self._auth_session = None
        try:
            f = self._auth_frame(
                opcodes.LOGIN,
                {
                    "token": token,
                    "interactive": False,
                    "chatsCount": 40,
                    "chatsSync": 0,
                    "contactsSync": 0,
                    "presenceSync": 0,
                    "draftsSync": 0,
                },
            )
        except MaxLoginFailed:
            # Сервер отклонил токен. Рвём ИМЕННО это соединение (only=sock — не
            # трогаем чужой свежий сокет, если пользователь перелогинился
            # параллельно): иначе keepalive продолжал бы пинговать
            # неавторизованный сокет. Реконнект-цикл рвёт свой сокет сам (except
            # MaxLoginFailed); здесь закрываем дыру для прямых вызовов login().
            self._teardown_socket(only=sock)
            raise
        profile = f.decoded.get("profile") if isinstance(f.decoded, dict) else None
        if f.cmd != 1 or f.opcode != opcodes.LOGIN or not _has_profile_identity(profile):
            raise MaxWriteOutcomeUnknown("LOGIN did not confirm a typed account profile")
        if (sock is not self._sock or self._closed or self.device_id != login_device_id or
                self._device_type != login_device_type):
            raise MaxWriteOutcomeUnknown("Connection changed before LOGIN was committed")
        # LOGIN's top-level response token is not substituted for the supplied
        # credential: it has not been established to be another LOGIN token.
        self._token = token
        # A connected socket before LOGIN is not an authenticated account. A
        # new object also invalidates events queued before re-login/reconnect.
        if sock is not None and sock is self._sock and self.is_connected:
            contact = profile.get('contact', profile)
            self._auth_session = AuthenticatedSession(
                contact['id'], login_device_id, login_device_type, sock)
        # Метка для анти-шторм-троттла: следующий реконнект не будет логиниться
        # раньше, чем через MIN_AUTH_INTERVAL после этого момента.
        self._last_login_ts = time.monotonic()
        return f

    def logout(self) -> MaxFrame:
        self._auth_session = None
        return self.request(opcodes.LOGOUT, {})

    def _require_qr_receiver(self) -> None:
        if self._device_type != "WEB" or self._token:
            raise MaxError("Для QR-входа нужно отдельное неавторизованное WEB-соединение.")

    def qr_create(self) -> dict:
        """Create one official QR challenge on a separate WEB connection.

        Only the server's qrLink is suitable for rendering. No saved LOGIN
        credential is encoded, stored as a challenge, or substituted here.
        """
        self._require_qr_receiver()
        frame = self._write_frame(opcodes.QR_GET, None)
        if not frame.ok:
            raise MaxLoginFailed(f"QR_GET: {frame.error_text()}")
        data = frame.decoded
        if (not isinstance(data, dict)
                or not _is_bounded_qr_string(data.get("trackId"))
                or not _is_bounded_qr_string(data.get("qrLink"), limit=8192)
                or not _is_qr_number(data.get("expiresAt"))
                or not _is_qr_number(data.get("pollingInterval"), positive=True)
                or not _is_qr_number(data.get("ttl"), positive=True)):
            raise MaxWriteOutcomeUnknown("QR_GET: сервер не подтвердил данные QR-входа.")
        return _str_keys(data)

    def qr_poll(self, track_id: str) -> dict:
        """Read QR status once; the caller owns the server-provided timer."""
        self._require_qr_receiver()
        if not _is_bounded_qr_string(track_id):
            raise ValueError("A bounded QR track ID is required")
        frame = self.request(opcodes.QR_STATUS, {"trackId": track_id})
        if not frame.ok:
            raise MaxLoginFailed(f"QR_STATUS: {frame.error_text()}")
        status = frame.decoded.get("status") if isinstance(frame.decoded, dict) else None
        if (not isinstance(status, dict)
                or type(status.get("loginAvailable", False)) is not bool
                or not _is_qr_number(status.get("expiresAt", 0))
                or not _is_qr_number(status.get("ttl", 0))):
            raise MaxError("QR_STATUS: сервер не подтвердил состояние QR-входа.")
        # These defaults are explicit in the official _re schema.
        return {**_str_keys(frame.decoded), "status": {
            **_str_keys(status), "loginAvailable": status.get("loginAvailable", False),
            "expiresAt": status.get("expiresAt", 0), "ttl": status.get("ttl", 0)}}

    def qr_finish(self, track_id: str) -> dict:
        """Consume an approved challenge once, preserving any 2FA requirement.

        The caller must first observe loginAvailable. This method does not
        switch the current account, issue LOGIN, submit a password, or retry.
        """
        self._require_qr_receiver()
        if not _is_bounded_qr_string(track_id):
            raise ValueError("A bounded QR track ID is required")
        frame = self._write_frame(opcodes.QR_LOGIN, {"trackId": track_id})
        if not frame.ok:
            raise MaxLoginFailed(f"QR_LOGIN: {frame.error_text()}")
        return self._qr_auth_result(frame, allow_password_challenge=True)

    def qr_check_password(self, track_id: str, password: str) -> dict:
        """Submit an explicitly entered password to this QR challenge's115.

        This is authentication only. It does not change account security,
        substitute a saved password, install credentials, or retry a write.
        """
        self._require_qr_receiver()
        if not _is_bounded_qr_string(track_id):
            raise ValueError("A bounded QR track ID is required")
        if not isinstance(password, str) or not password or len(password) > 4096:
            raise ValueError("An explicitly entered password is required")
        frame = self._write_frame(opcodes.TWO_FA, {"trackId": track_id, "password": password})
        if not frame.ok:
            raise MaxLoginFailed(f"QR_2FA: {frame.error_text()}")
        return self._qr_auth_result(frame, allow_password_challenge=False)

    @staticmethod
    def _qr_auth_result(frame: MaxFrame, *, allow_password_challenge: bool) -> dict:
        return MaxClient._typed_auth_result(frame, allow_password_challenge=allow_password_challenge,
                                            label="QR_LOGIN")

    @staticmethod
    def _typed_auth_result(frame: MaxFrame, *, allow_password_challenge: bool, label: str) -> dict:
        data = frame.decoded
        if not isinstance(data, dict) or not isinstance(data.get("tokenAttrs"), dict):
            raise MaxWriteOutcomeUnknown(f"{label}: результат входа не подтверждён.")
        if any(not isinstance(kind, str) or not isinstance(attribute, dict)
               or not _is_bounded_qr_string(attribute.get("token"), limit=65536)
               or ("ttl" in attribute and (type(attribute["ttl"]) is not int
                                            or not -(2**63) <= attribute["ttl"] < 2**63))
               for kind, attribute in data["tokenAttrs"].items()):
            raise MaxWriteOutcomeUnknown(f"{label}: типизированные данные входа не подтверждены.")
        challenge = data.get("passwordChallenge")
        if "passwordChallenge" in data:
            if (not allow_password_challenge or not isinstance(challenge, dict)
                    or not _is_bounded_qr_string(challenge.get("trackId"))
                    or any(key in challenge and not isinstance(challenge[key], str)
                           for key in ("hint", "email"))):
                raise MaxWriteOutcomeUnknown(f"{label}: запрос 2FA не подтверждён.")
            config = challenge.get("config")
            if "config" in challenge and (not isinstance(config, dict)
                    or any(key in config and (type(config[key]) is not int
                                              or not -(2**31) <= config[key] < 2**31)
                           for key in ("passMinLen", "passMaxLen", "hintMaxLen"))):
                raise MaxWriteOutcomeUnknown(f"{label}: параметры 2FA не подтверждены.")
            return _str_keys(data)
        profile = data.get("profile")
        login = data["tokenAttrs"].get("LOGIN")
        if (not _has_profile_identity(profile)
                or not isinstance(login, dict)
                or not _is_bounded_qr_string(login.get("token"), limit=65536)):
            raise MaxWriteOutcomeUnknown(f"{label}: сервер не подтвердил профиль и LOGIN-токен.")
        return _str_keys(data)

    # ───────────────────────── messaging ─────────────────────────

    def current_profile(self) -> dict:
        f = self.request(opcodes.PROFILE, {})
        if not f.ok:
            raise MaxError(f"PROFILE: {f.error_text()}")
        if (f.opcode != opcodes.PROFILE or not isinstance(f.decoded, dict)
                or not _has_profile_identity(f.decoded.get("profile"))):
            raise MaxError("PROFILE did not return a typed account profile")
        return _str_keys(f.decoded)

    def update_profile_name(self, first_name: str, last_name: Optional[str] = None) -> dict:
        """Compatibility name edit using the current public PROFILE16 shape."""
        if not isinstance(first_name, str) or (last_name is not None and not isinstance(last_name, str)):
            raise ValueError("Profile names must be strings")
        return self.update_profile(first_name=first_name.strip(),
                                   last_name=last_name.strip() if last_name is not None else None)

    def update_profile(self, *, first_name: Optional[str] = None,
                       last_name: Optional[str] = None,
                       description: Optional[str] = None,
                       link: Optional[str] = None,
                       photo_token: Optional[str] = None,
                       photo_id: Optional[int | float] = None,
                       avatar_type: Optional[str] = None) -> dict:
        """Update explicitly selected current-profile fields once.

        Absent fields are omitted. Text fields may be explicitly empty, as in
        the official current profile caller. No clear-avatar sentinel is guessed.
        """
        payload: dict[str, Any] = {}
        for key, value in (("firstName", first_name), ("lastName", last_name),
                           ("description", description), ("link", link), ("photoToken", photo_token)):
            if value is not None:
                if not isinstance(value, str):
                    raise ValueError("Profile text fields must be strings")
                payload[key] = value
        if photo_token == "":
            raise ValueError("A nonempty uploaded profile-photo token is required")
        if photo_id is not None:
            if type(photo_id) not in (int, float) or not math.isfinite(photo_id):
                raise ValueError("Profile photo ID must be a finite number")
            payload["photoId"] = photo_id
        if avatar_type is not None:
            if avatar_type not in {"USER_AVATAR", "PRESET_AVATAR"}:
                raise ValueError("Unsupported profile avatar type")
            payload["avatarType"] = avatar_type
        elif photo_token is not None:
            # The current profile-upload producer supplies this exact pair.
            payload["avatarType"] = "USER_AVATAR"
        if not payload:
            raise ValueError("At least one explicit profile change is required")
        frame = self._write_frame(opcodes.PROFILE, payload)
        if not frame.ok:
            raise MaxError(f"PROFILE update: {frame.error_text()}")
        response = frame.decoded
        profile = response.get("profile") if isinstance(response, dict) else None
        contact = profile.get("contact") if isinstance(profile, dict) else None
        if not isinstance(contact, dict) or type(contact.get("id")) is not int:
            raise MaxWriteOutcomeUnknown("PROFILE: сервер не подтвердил профиль после изменения.")
        return _str_keys(response)

    def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        attaches: Optional[list[dict]] = None,
        peer_user_id: Optional[int] = None,
        allow_dialog_fallback: bool = True,
        reply_to_message_id: Optional[int] = None,
        forward_source_chat_id: Optional[int] = None,
        forward_source_message_id: Optional[int] = None,
        source_post_id: Optional[int] = None,
        post_id: Optional[int] = None,
        elements: Optional[list[dict]] = None,
        expected_session: Optional[AuthenticatedSession] = None,
    ) -> dict:
        """Отправить сообщение. SEND_MESSAGE(64) адресуется РОВНО одним полем
        (zg9.java): `chatId` — для группы/канала/существующего чата; `userId` —
        для личного диалога (peer). Если для chat_id известен peer (из кэша или
        передан явно) — шлём userId. Иначе пробуем chatId; при ответе
        not.found common.finder[User,<id>] вытаскиваем peer из ошибки,
        кэшируем и повторяем по userId (как делает официальный клиент)."""
        _require_packet_int(chat_id, "chat_id")
        if not isinstance(text, str):
            raise ValueError("Message text must be a string")
        if peer_user_id is not None:
            _require_packet_int(peer_user_id, "peer_user_id")
        if type(allow_dialog_fallback) is not bool:
            raise ValueError("allow_dialog_fallback must be boolean")
        link = None
        forwarding = forward_source_chat_id is not None or forward_source_message_id is not None
        if reply_to_message_id is not None and forwarding:
            raise ValueError("Reply and forward are mutually exclusive")
        if source_post_id is not None and not forwarding:
            raise ValueError("source_post_id requires a forward source")
        if reply_to_message_id is not None:
            link = {"type": "REPLY", "messageId": _require_packet_int(reply_to_message_id, "reply_to_message_id")}
        elif forwarding:
            if forward_source_chat_id is None or forward_source_message_id is None:
                raise ValueError("Forward needs both the source chat and message")
            link = {"type": "FORWARD",
                    "chatId": _require_packet_int(forward_source_chat_id, "forward_source_chat_id"),
                    "messageId": _require_packet_int(forward_source_message_id, "forward_source_message_id")}
            if source_post_id is not None:
                link["postId"] = _require_packet_int(source_post_id, "source_post_id")
        # New contextual actions never inherit the older finder-error retry.
        if link is not None:
            allow_dialog_fallback = False
        with self._message_cid_lock:
            cid = max(int(time.time() * 1000), self._last_message_cid + 1)
            self._last_message_cid = cid
        message: dict[str, Any] = {
            "cid": cid,
            "text": text,
        }
        if attaches is not None:
            message["attaches"] = _require_object_array(attaches, "attaches")
        if elements is not None:
            message["elements"] = _require_object_array(elements, "elements")
        if link is not None:
            message["link"] = link
        if post_id is not None:
            _require_packet_int(post_id, "post_id")

        def _send(target: dict) -> MaxFrame:
            payload = dict(target)
            payload["message"] = message
            payload["notify"] = True
            if post_id is not None:
                payload["postId"] = post_id
            return self._write_frame(opcodes.SEND_MESSAGE, payload, expected_session=expected_session)

        peer = peer_user_id or self._chat_peer.get(chat_id)
        if peer:
            f = _send({"userId": peer})
        else:
            f = _send({"chatId": chat_id})
            if not f.ok and allow_dialog_fallback:
                # 1:1 диалог: сервер не нашёл User по chatId и подсказал его id.
                # learned может РАВНЯТЬСЯ chat_id (диалог открыт из контактов, где
                # chatId = userId) — всё равно повторяем: userId это ДРУГОЕ поле.
                # Кэшируем peer ТОЛЬКО при успехе retry, иначе не отравляем кэш.
                learned = _peer_from_finder_error(f)
                if learned:
                    self._debug("send: explicit finder rejection; legacy target-type retry")
                    f2 = _send({"userId": learned})
                    if f2.ok:
                        self._chat_peer[chat_id] = learned
                        f = f2

        if not f.ok:
            raise MaxError(f"SEND_MESSAGE: {f.error_text()}")
        return self._confirmed_message_write(f, "SEND_MESSAGE", require_chat=True)

    def _write_frame(self, opcode: int, payload: dict | None, *,
                     expected_session: Optional[AuthenticatedSession] = None) -> MaxFrame:
        try:
            if expected_session is None:
                return self.request(opcode, payload)
            return self.request(opcode, payload, expected_session=expected_session)
        except (MaxTimeout, MaxNotConnected):
            raise MaxWriteOutcomeUnknown(
                "Результат действия не подтверждён. Перед повтором проверьте чат."
            ) from None

    def _write_object(self, opcode: int, payload: dict, *,
                      expected_session: Optional[AuthenticatedSession] = None) -> dict:
        frame = self._write_frame(opcode, payload, expected_session=expected_session)
        if not frame.ok:
            raise MaxError(f"{opcodes.name(opcode)}: {frame.error_text()}")
        if not isinstance(frame.decoded, dict):
            raise MaxWriteOutcomeUnknown(
                "Результат действия не подтверждён. Перед повтором проверьте чат."
            )
        return _str_keys(frame.decoded)

    @staticmethod
    def _confirmed_message_write(frame: MaxFrame, label: str, *, require_chat: bool = False) -> dict:
        data = frame.decoded if isinstance(frame.decoded, dict) else None
        message = data.get("message") if data else None
        if (not isinstance(message, dict) or type(message.get("id")) is not int
                or (require_chat and type(data.get("chatId")) is not int)):
            raise MaxWriteOutcomeUnknown(
                f"{label}: сервер не подтвердил сообщение. Перед повтором проверьте чат."
            )
        return _str_keys(data)

    def reply_to_message(self, chat_id: int, message_id: int, text: str, *,
                         attaches: Optional[list[dict]] = None,
                         elements: Optional[list[dict]] = None,
                         post_id: Optional[int] = None,
                         peer_user_id: Optional[int] = None,
                         expected_session: Optional[AuthenticatedSession] = None) -> dict:
        """Reply with the current web client's message.link shape, no retry."""
        return self.send_message(chat_id, text, attaches=attaches, elements=elements,
                                 post_id=post_id, peer_user_id=peer_user_id,
                                 reply_to_message_id=message_id, allow_dialog_fallback=False,
                                 expected_session=expected_session)

    def forward_message(self, source_chat_id: int, message_id: int, target_chat_id: int, *,
                        source_post_id: Optional[int] = None, post_id: Optional[int] = None,
                        text: str = "", peer_user_id: Optional[int] = None,
                        expected_session: Optional[AuthenticatedSession] = None) -> dict:
        """Forward one known source message; top-level target stays distinct."""
        return self.send_message(target_chat_id, text, peer_user_id=peer_user_id,
                                 post_id=post_id, forward_source_chat_id=source_chat_id,
                                 forward_source_message_id=message_id, source_post_id=source_post_id,
                                 allow_dialog_fallback=False, expected_session=expected_session)

    def peer_for(self, chat_id: int) -> Optional[int]:
        """peer userId диалога, если он уже выяснен (для персиста на стороне service)."""
        return self._chat_peer.get(chat_id)

    def set_peer(self, chat_id: int, peer_user_id: int) -> None:
        """Подсказать клиенту peer диалога (из кэша store), чтобы первая же
        отправка шла сразу по userId, без неудачной попытки по chatId."""
        if peer_user_id:
            self._chat_peer[chat_id] = peer_user_id

    def typing(self, chat_id: int, is_typing: bool = True) -> None:
        try:
            if is_typing:
                self.send_typing(chat_id)
            else:
                self.stop_typing(chat_id)
        except MaxError:
            pass  # тайпинг — не критичная операция

    def send_typing(self, chat_id: int, kind: str = "TEXT") -> None:
        """Send the documented typing kind; this is not a text message."""
        _require_packet_int(chat_id, "chat_id")
        if kind not in {"TEXT", "PHOTO", "VIDEO", "AUDIO", "FILE", "STICKER", "VIDEO_MSG"}:
            raise ValueError("Unsupported typing kind")
        self._request_object(opcodes.TYPING, {"chatId": chat_id, "type": kind}, timeout=10)

    def stop_typing(self, chat_id: int) -> None:
        _require_packet_int(chat_id, "chat_id")
        self._request_object(opcodes.CHAT_TYPING_STOP, {"chatId": chat_id}, timeout=10)

    def _request_object(self, opcode: int, payload: dict, *, timeout: float = 30,
                        expected_session: Optional[AuthenticatedSession] = None) -> dict:
        kwargs = {'expected_session': expected_session} if expected_session is not None else {}
        frame = self.request(opcode, payload, timeout=timeout, **kwargs)
        if not frame.ok:
            raise MaxError(f"{opcodes.name(opcode)}: {frame.error_text()}")
        return _str_keys(frame.decoded) if isinstance(frame.decoded, dict) else {}

    def search_messages(self, query: str, *, count: int = 30, marker=None) -> dict:
        payload = build_message_search(query, count=count, marker=marker)
        return parse_message_search(self._request_object(opcodes.MESSAGE_SEARCH, payload))

    def search_global(self, query: str, *, count: int = 40, marker=None,
                      search_type: str = 'ALL') -> dict:
        payload = build_global_search(query, count=count, marker=marker, search_type=search_type)
        return parse_global_search(self._request_object(opcodes.GLOBAL_SEARCH, payload))

    def common_chats(self, peer_id, marker=None, count=None):
        payload = build_common_chats(peer_id, marker=marker, count=count)
        return parse_common_chats(self._request_object(opcodes.COMMON_CHATS, payload))

    def conversation_media(self, chat_id, tab, marker=None, *, initial=False):
        payload = build_info_media(chat_id, tab, marker=marker, initial=initial)
        return parse_info_media(self._request_object(opcodes.CHAT_MEDIA, payload))

    def mark_read(self, chat_id: int, message_id: Optional[int] = None,
                  mark: Optional[int | float] = None, *,
                  expected_session: Optional[AuthenticatedSession] = None, explicit: bool = False) -> dict:
        """Mark the explicitly selected chat/message read; never called on login."""
        _require_packet_int(chat_id, "chat_id")
        if type(explicit) is not bool:
            raise ValueError('explicit must be a boolean')
        payload: dict[str, Any] = {"chatId": chat_id, "type": "READ_MESSAGE"}
        if message_id is not None:
            payload["messageId"] = _require_packet_int(message_id, "message_id")
        if mark is not None:
            payload["mark"] = _require_packet_timestamp(mark, "mark")
        if explicit:
            return self._write_object(opcodes.CHAT_MARK, payload, expected_session=expected_session)
        return self._request_object(opcodes.CHAT_MARK, payload, expected_session=expected_session)

    def mark_unread(self, chat_id: int, *, mark: int | float,
                    expected_session: Optional[AuthenticatedSession] = None) -> dict:
        """Explicit SET_AS_UNREAD; mark is captured lastMessage.time - 1, no ID."""
        payload = {'chatId': _require_packet_int(chat_id, 'chat_id'),
                   'type': 'SET_AS_UNREAD', 'mark': _require_packet_timestamp(mark, 'mark')}
        return self._write_object(opcodes.CHAT_MARK, payload, expected_session=expected_session)

    def set_reaction(self, chat_id: int, message_id: int, reaction_id: str,
                     *, reaction_type: str = "EMOJI", post_id: Optional[int] = None,
                     expected_session: Optional[AuthenticatedSession] = None) -> dict:
        """Use only the complete documented reaction shape; return server state."""
        _require_packet_int(chat_id, "chat_id")
        _require_packet_int(message_id, "message_id")
        if reaction_type not in {"EMOJI", "STICKER"}:
            raise ValueError("Unsupported reaction type")
        if not isinstance(reaction_id, str) or not reaction_id or len(reaction_id) > 256:
            raise ValueError("A bounded reaction ID is required")
        payload = {"chatId": chat_id, "messageId": message_id,
                   "reaction": {"reactionType": reaction_type, "id": reaction_id}}
        if post_id is not None:
            payload["postId"] = _require_packet_int(post_id, "post_id")
        return self._write_object(opcodes.REACTION_SET, payload, expected_session=expected_session)

    def remove_reaction(self, chat_id: int, message_id: int,
                        *, post_id: Optional[int] = None,
                        expected_session: Optional[AuthenticatedSession] = None) -> dict:
        payload = {"chatId": _require_packet_int(chat_id, "chat_id"),
                   "messageId": _require_packet_int(message_id, "message_id")}
        if post_id is not None:
            payload["postId"] = _require_packet_int(post_id, "post_id")
        return self._write_object(opcodes.REACTION_REMOVE, payload, expected_session=expected_session)

    def get_reactions(self, chat_id: int, message_ids: list[int],
                      *, post_id: Optional[int] = None) -> dict:
        payload = {"chatId": _require_packet_int(chat_id, "chat_id"),
                   "messageIds": _require_packet_ids(message_ids)}
        if post_id is not None:
            payload["postId"] = _require_packet_int(post_id, "post_id")
        response = self._request_object(opcodes.REACTIONS_GET, payload)
        if not isinstance(response.get("messagesReactions"), dict):
            raise MaxError("REACTIONS_GET did not return a structured reaction map")
        return response

    def reaction_users(self, chat_id: int, message_id: int, *,
                       reaction: Optional[str] = None,
                       marker: Optional[int] = None, count: int = 100) -> dict:
        payload = {"chatId": _require_packet_int(chat_id, "chat_id"),
                   "messageId": _require_packet_int(message_id, "message_id"),
                   "count": _require_count(count, allow_zero=False)}
        if reaction is not None:
            if not isinstance(reaction, str) or not reaction or len(reaction) > 256:
                raise ValueError("A bounded reaction ID is required")
            payload["reaction"] = reaction
        if marker is not None:
            payload["marker"] = _require_packet_int(marker, "marker")
        return self._request_object(opcodes.REACTION_USERS, payload)

    def get_messages(self, chat_id: int, message_ids: list[int],
                     *, post_id: Optional[int] = None) -> dict:
        payload = {"chatId": _require_packet_int(chat_id, "chat_id"),
                   "messageIds": _require_packet_ids(message_ids)}
        if post_id is not None:
            payload["postId"] = _require_packet_int(post_id, "post_id")
        return self._request_object(opcodes.MSG_GET_BY_IDS, payload)

    def edit_message(self, chat_id: int, message_id: int, text: str, *,
                     attachments: Optional[list[dict]] = None,
                     elements: Optional[list[dict]] = None,
                     post_id: Optional[int] = None,
                     expected_session: Optional[AuthenticatedSession] = None) -> dict:
        if not isinstance(text, str):
            raise ValueError("Message text must be a string")
        payload = {"chatId": _require_packet_int(chat_id, "chat_id"),
                   "messageId": _require_packet_int(message_id, "message_id"), "text": text}
        if attachments is not None:
            payload["attachments"] = _require_object_array(attachments, "attachments")
        if elements is not None:
            payload["elements"] = _require_object_array(elements, "elements")
        if post_id is not None:
            payload["postId"] = _require_packet_int(post_id, "post_id")
        f = self._write_frame(opcodes.MSG_EDIT, payload, expected_session=expected_session)
        if not f.ok:
            raise MaxError(f"MSG_EDIT: {f.error_text()}")
        response = self._confirmed_message_write(f, "MSG_EDIT")
        if response["message"]["id"] != message_id:
            raise MaxWriteOutcomeUnknown(
                "MSG_EDIT: сообщение не подтверждено. Перед повтором проверьте чат."
            )
        return response

    def delete_messages(self, chat_id: int, message_ids: list[int], *,
                        for_all: bool = False, post_id: Optional[int] = None,
                        expected_session: Optional[AuthenticatedSession] = None) -> dict:
        """Delete only on an explicit caller action; forMe is the inverse flag."""
        if type(for_all) is not bool:
            raise ValueError("for_all must be boolean")
        payload = {"chatId": _require_packet_int(chat_id, "chat_id"),
                   "messageIds": _require_packet_ids(message_ids), "forMe": not for_all}
        if post_id is not None:
            payload["postId"] = _require_packet_int(post_id, "post_id")
        frame = self._write_frame(opcodes.MSG_DELETE, payload, expected_session=expected_session)
        if not frame.ok:
            raise MaxError(f"MSG_DELETE: {frame.error_text()}")
        data = frame.decoded if isinstance(frame.decoded, dict) else {}
        identities = data.get("messageIds")
        if (type(data.get("chatId")) is not int or data.get("chatId") != chat_id or not isinstance(identities, list)
                or any(type(identity) is not int for identity in identities)):
            raise MaxWriteOutcomeUnknown(
                "MSG_DELETE: удаление не подтверждено. Перед повтором проверьте чат."
            )
        return _str_keys(data)

    def chat_info(self, chat_ids: list[int], *, expected_session: Optional[AuthenticatedSession] = None) -> dict:
        kwargs = {'expected_session': expected_session} if expected_session is not None else {}
        f = self.request(opcodes.CHAT_INFO, {"chatIds": chat_ids}, **kwargs)
        if not f.ok:
            raise MaxError(f"CHAT_INFO: {f.error_text()}")
        return _str_keys(f.decoded) if isinstance(f.decoded, dict) else {}

    def list_chats(self, marker: int = 0, count: int = 40, *,
                   chat_types: Optional[list[str]] = None,
                   filter_unanswered: Optional[bool] = None,
                   no_messages: Optional[bool] = None) -> dict:
        payload: dict[str, Any] = {"marker": _require_packet_int(marker, "marker"),
                                   "count": _require_count(count, allow_zero=False)}
        if chat_types is not None:
            if (not isinstance(chat_types, list) or not chat_types
                    or any(kind not in {"DIALOG", "CHAT", "CHANNEL"} for kind in chat_types)):
                raise ValueError("Unsupported chat types")
            payload["chatTypes"] = list(chat_types)
        for key, value in (("filterUnanswered", filter_unanswered), ("noMessages", no_messages)):
            if value is not None:
                if type(value) is not bool:
                    raise ValueError("Chat filter must be boolean")
                payload[key] = value
        return self._request_object(opcodes.CHATS_LIST, payload)

    def chat_history_window(self, chat_id: int, *, from_cursor: int | float,
                            forward: int = 0, backward: int = 50,
                            get_chat: bool = True, post_id: Optional[int] = None,
                            forward_time: Optional[int] = None,
                            backward_time: Optional[int] = None,
                            expected_session: Optional[AuthenticatedSession] = None) -> dict:
        """Request a history window from an explicit server message timestamp.

        Current public web schema defines `from` as a timestamp, not a message ID.
        Direction and the selected chat's read/last-message marks remain explicit.
        Return the full structured response so chat/messageIds are not discarded.
        """
        if type(get_chat) is not bool:
            raise ValueError("get_chat must be boolean")
        forward = _require_count(forward)
        backward = _require_count(backward)
        if not forward and not backward:
            raise ValueError("At least one history direction must have a count")
        payload = {"chatId": _require_packet_int(chat_id, "chat_id"),
                   "from": _require_packet_timestamp(from_cursor, "from_cursor"),
                   "forward": forward, "backward": backward,
                   "getMessages": True, "getChat": get_chat}
        for key, value in (("postId", post_id), ("forwardTime", forward_time), ("backwardTime", backward_time)):
            if value is not None:
                payload[key] = _require_packet_int(value, key)
        kwargs = {'expected_session': expected_session} if expected_session is not None else {}
        frame = self.request(opcodes.CHAT_HISTORY, payload, timeout=30, **kwargs)
        if not frame.ok:
            raise MaxError(f"CHAT_HISTORY: {frame.error_text()}")
        if not isinstance(frame.decoded, dict):
            raise MaxError("CHAT_HISTORY did not return a structured response")
        response = _str_keys(frame.decoded)
        if "messages" in response and not isinstance(response["messages"], list):
            raise MaxError("CHAT_HISTORY did not return a structured message list")
        return response

    def call_history(self, sync_cursor: Optional[int] = None) -> dict:
        """Read call-log items; this method does not initiate or accept calls.

        Preserve the server item DTOs, cursor and reset flag without inferring
        undocumented fields such as direction, duration or participant names.
        """
        payload = {}
        if sync_cursor is not None:
            payload["callHistorySync"] = _require_packet_int(sync_cursor, "sync_cursor")
        response = self._request_object(opcodes.CALL_HISTORY, payload)
        if not isinstance(response.get("callHistoryItems"), list):
            raise MaxError("CALL_HISTORY did not return a structured item list")
        return response

    def get_folders(self, folder_sync: Optional[int] = None, *,
                    expected_session: Optional[AuthenticatedSession] = None) -> dict:
        """Read folder metadata, preserving omitted fields in a delta response."""
        payload = {}
        if folder_sync is not None:
            payload["folderSync"] = _require_packet_int(folder_sync, "folder_sync")
        kwargs = {'expected_session': expected_session} if expected_session is not None else {}
        response = self._request_object(opcodes.FOLDERS_LIST, payload, **kwargs)
        if type(response.get("folderSync")) is not int:
            raise MaxError("FOLDERS_LIST did not return a folder sync timestamp")
        if "folders" in response and (not isinstance(response["folders"], list)
                or any(not isinstance(folder, dict) for folder in response["folders"])):
            raise MaxError("FOLDERS_LIST did not return structured folders")
        for key in ("foldersOrder", "allFilterExcludeFolders"):
            if key in response and (not isinstance(response[key], list)
                    or any(not isinstance(identity, str) for identity in response[key])):
                raise MaxError("FOLDERS_LIST did not return structured folder IDs")
        return response

    def get_folder_details(self, folder_ids: list[str], *,
                           expected_session: Optional[AuthenticatedSession] = None) -> dict:
        """Read exactly the requested folder IDs; missing DTOs stay missing."""
        if (not isinstance(folder_ids, list) or not 1 <= len(folder_ids) <= 1000
                or any(not isinstance(identity, str) or not identity for identity in folder_ids)):
            raise ValueError("A bounded non-empty list of folder IDs is required")
        kwargs = {'expected_session': expected_session} if expected_session is not None else {}
        response = self._request_object(opcodes.FOLDER_DETAILS, {"folderIds": list(folder_ids)}, **kwargs)
        if (not isinstance(response.get("folders"), list)
                or any(not isinstance(folder, dict) for folder in response["folders"])):
            raise MaxError("FOLDER_DETAILS did not return structured folders")
        return response

    def update_folder(self, folder: dict, *,
                       expected_session: Optional[AuthenticatedSession] = None) -> dict:
        """One current274 create/update; only a matching authoritative folder ACK."""
        from .folder_contracts import folder_payload, folder_dto, int64
        payload = folder_payload(folder)
        kwargs = {'expected_session': expected_session} if expected_session is not None else {}
        frame = self._write_frame(opcodes.FOLDER_UPDATE, payload, **kwargs)
        if not frame.ok:
            raise MaxError('MAX отклонил изменение папки')
        try:
            data = frame.decoded
            if frame.opcode != opcodes.FOLDER_UPDATE or not isinstance(data, dict):
                raise ValueError()
            actual = folder_dto(data.get('folder'))
            if actual['id'] != payload['id'] or ('folderSync' in data and not int64(data['folderSync'])):
                raise ValueError()
            if 'foldersOrder' in data and (not isinstance(data['foldersOrder'], list)
                    or any(not isinstance(item, str) for item in data['foldersOrder'])):
                raise ValueError()
        except ValueError:
            raise MaxWriteOutcomeUnknown('Результат изменения папки не подтверждён. Обновите список.') from None
        return _str_keys(data)

    def reorder_folders(self, order: list[str], *,
                        expected_session: Optional[AuthenticatedSession] = None) -> dict:
        from .folder_contracts import folder_ids
        return self._folder_order_write(opcodes.FOLDERS_ORDER, {'foldersOrder': folder_ids(order)},
                                        expected_session=expected_session)

    def delete_folders(self, ids: list[str], *,
                       expected_session: Optional[AuthenticatedSession] = None) -> dict:
        from .folder_contracts import folder_ids
        return self._folder_order_write(opcodes.FOLDERS_DELETE, {'folderIds': folder_ids(ids)},
                                        expected_session=expected_session)

    def _folder_order_write(self, opcode: int, payload: dict, *,
                            expected_session: Optional[AuthenticatedSession] = None) -> dict:
        from .folder_contracts import int64
        kwargs = {'expected_session': expected_session} if expected_session is not None else {}
        frame = self._write_frame(opcode, payload, **kwargs)
        if not frame.ok:
            raise MaxError('MAX отклонил изменение списка папок')
        data = frame.decoded
        if (frame.opcode != opcode or not isinstance(data, dict) or not int64(data.get('folderSync'))
                or not isinstance(data.get('foldersOrder'), list)
                or any(not isinstance(item, str) for item in data['foldersOrder'])):
            raise MaxWriteOutcomeUnknown('Результат изменения списка папок не подтверждён. Обновите список.')
        return _str_keys(data)

    def story_archive(self, owner_id: int, *, owner_type: int = 0,
                      marker: int = 0, count: int = 15) -> dict:
        """Read one archive page; marker is the actual timestamp cursor."""
        if type(owner_type) is not int or owner_type not in (0, 1, 2):
            raise ValueError("Unsupported story owner type")
        payload = {"owner": {"type": owner_type,
                             "ownerId": _require_packet_int(owner_id, "owner_id")},
                   "marker": _require_packet_int(marker, "marker"),
                   "count": _require_count(count, allow_zero=False)}
        response = self._request_object(opcodes.STORY_ARCHIVE, payload)
        if (not isinstance(response.get("stories"), list)
                or any(not isinstance(story, dict) for story in response["stories"])):
            raise MaxError("STORY_ARCHIVE did not return structured stories")
        if "marker" in response and type(response["marker"]) is not int:
            raise MaxError("STORY_ARCHIVE did not return a timestamp marker")
        return response

    def chat_history(self, chat_id: int, from_id: int = 0, count: int = 50) -> tuple[list[dict], bytes]:
        f = self.request(
            opcodes.CHAT_HISTORY,
            {"chatId": chat_id, "from": from_id, "forward": count},
        )
        if not f.ok:
            raise MaxError(f"CHAT_HISTORY: {f.error_text()}")
        msgs = f.decoded.get("messages") if isinstance(f.decoded, dict) else None
        if (not isinstance(msgs, list) or any(not isinstance(message, dict) for message in msgs)):
            raise MaxError("CHAT_HISTORY did not return a structured message list")
        return [_str_keys(message) for message in msgs], f.body

    def chat_media(
        self,
        chat_id: int,
        *,
        attach_types: Optional[list[str]] = None,
        forward: int = 50,
        backward: int = 0,
        message_id: Optional[int] = None,
    ) -> dict:
        payload: dict[str, Any] = {
            "chatId": chat_id,
            "attachTypes": attach_types or ["PHOTO", "VIDEO"],
            "forward": forward,
            "backward": backward,
        }
        if message_id is not None:
            payload["messageId"] = message_id
        f = self.request(opcodes.CHAT_MEDIA, payload)
        if not f.ok:
            raise MaxError(f"CHAT_MEDIA: {f.error_text()}")
        return _str_keys(f.decoded) if isinstance(f.decoded, dict) else {}

    # ───────────────────────── contacts ─────────────────────────

    def contact_info(self, contact_ids: list[int], *, expected_session: Optional[AuthenticatedSession] = None) -> dict:
        kwargs = {'expected_session': expected_session} if expected_session is not None else {}
        f = self.request(opcodes.CONTACT_INFO, {"contactIds": contact_ids}, **kwargs)
        if not f.ok:
            raise MaxError(f"CONTACT_INFO: {f.error_text()}")
        return _str_keys(f.decoded) if isinstance(f.decoded, dict) else {}

    def list_blocked_contacts(self, *, expected_session: Optional[AuthenticatedSession] = None) -> dict:
        """Actual current36 producer: first page only, no invented cursor."""
        from .folder_contracts import contact_dto
        kwargs = {'expected_session': expected_session} if expected_session is not None else {}
        result = self._request_object(opcodes.CONTACTS_LIST, {'status': 'BLOCKED', 'from': 0, 'count': 100}, **kwargs)
        try:
            if not isinstance(result.get('contacts'), list):
                raise ValueError()
            contacts = [contact_dto(contact) for contact in result['contacts']]
            if any('status' in contact and contact['status'] != 'BLOCKED' for contact in contacts):
                raise ValueError()
        except ValueError:
            raise MaxError('MAX не вернул подтверждённый список заблокированных контактов') from None
        return result

    def block_contact(self, contact_id: int, *, expected_session: Optional[AuthenticatedSession] = None) -> dict:
        kwargs = {'expected_session': expected_session} if expected_session is not None else {}
        return self._contact_block_action(contact_id, 'BLOCK', **kwargs)

    def unblock_contact(self, contact_id: int, *, expected_session: Optional[AuthenticatedSession] = None) -> dict:
        kwargs = {'expected_session': expected_session} if expected_session is not None else {}
        return self._contact_block_action(contact_id, 'UNBLOCK', **kwargs)

    def _contact_block_action(self, contact_id: int, action: str, *,
                              expected_session: Optional[AuthenticatedSession] = None) -> dict:
        from .folder_contracts import contact_dto
        identity = _require_packet_int(contact_id, 'contact_id')
        kwargs = {'expected_session': expected_session} if expected_session is not None else {}
        frame = self._write_frame(opcodes.CONTACT_ACTION, {'contactId': identity, 'action': action}, **kwargs)
        if not frame.ok:
            raise MaxError('MAX отклонил изменение блокировки контакта')
        data = frame.decoded
        try:
            if frame.opcode != opcodes.CONTACT_ACTION or not isinstance(data, dict):
                raise ValueError()
            if 'contact' in data and contact_dto(data['contact'])['id'] != identity:
                raise ValueError()
        except ValueError:
            raise MaxWriteOutcomeUnknown('Результат блокировки контакта не подтверждён. Обновите список.') from None
        return _str_keys(data)

    def contact_photos(self, contact_id: int, *, from_cursor: Optional[int] = None,
                       count: int = 20) -> dict:
        payload = {"contactId": _require_packet_int(contact_id, "contact_id"),
                   "count": _require_count(count, allow_zero=False)}
        if from_cursor is not None:
            payload["from"] = _require_packet_int(from_cursor, "from_cursor")
        return self._request_object(opcodes.CONTACT_PHOTOS, payload)

    def contact_by_phone(self, phone: str, *, expected_session: Optional[AuthenticatedSession] = None) -> dict:
        if not isinstance(phone, str) or not phone.strip():
            raise ValueError("phone must be a nonempty string")
        kwargs = {'expected_session': expected_session} if expected_session is not None else {}
        f = self.request(opcodes.CONTACT_INFO_BY_PHONE, {"phone": phone}, **kwargs)
        if not f.ok:
            raise MaxError(f"CONTACT_INFO_BY_PHONE: {f.error_text()}")
        return _parse_contact(f.decoded)

    # ───────────────────────── media: request upload params ─────────────────────────

    def request_photo_upload(self, count: int = 1, profile: bool = False) -> dict:
        f = self.request(opcodes.PHOTO_UPLOAD, {"count": count, "profile": profile})
        if not f.ok:
            raise MaxError(f"PHOTO_UPLOAD: {f.error_text()}")
        return _str_keys(f.decoded) if isinstance(f.decoded, dict) else {}

    def request_video_upload(
        self, type_: str = "VIDEO", count: int = 1, uploader_type: str = "VIDEO"
    ) -> dict:
        f = self.request(
            opcodes.VIDEO_UPLOAD,
            {"type": type_, "count": count, "uploaderType": uploader_type},
        )
        if not f.ok:
            raise MaxError(f"VIDEO_UPLOAD: {f.error_text()}")
        return _str_keys(f.decoded) if isinstance(f.decoded, dict) else {}

    def request_file_upload(self, count: int = 1) -> dict:
        f = self.request(opcodes.FILE_UPLOAD, {"count": count})
        if not f.ok:
            raise MaxError(f"FILE_UPLOAD: {f.error_text()}")
        return _str_keys(f.decoded) if isinstance(f.decoded, dict) else {}

    def request_video_play(
        self, video_id: int, chat_id: Optional[int] = None, message_id: Optional[int] = None,
        token: Optional[str] = None,
    ) -> dict:
        payload: dict[str, Any] = {"videoId": video_id}
        if chat_id is not None:
            payload["chatId"] = chat_id
        if message_id is not None:
            payload["messageId"] = message_id
        if token is not None:
            payload["token"] = token
        f = self.request(opcodes.VIDEO_PLAY, payload)
        if not f.ok:
            raise MaxError(f"VIDEO_PLAY: {f.error_text()}")
        return _str_keys(f.decoded) if isinstance(f.decoded, dict) else {}

    def open_bot_app(self, bot_id: int, chat_id: int, start_param=None, *,
                     expected_session: AuthenticatedSession) -> MiniAppReply:
        """One ordinary bound launch160; do not log signed URLs or launch data."""
        payload = build_miniapp_launch(bot_id, chat_id, start_param)
        if not isinstance(expected_session, AuthenticatedSession):
            raise MaxWriteNotStarted('A current authenticated mini-app context is required')
        frame = self.request(opcodes.BOT_WEBAPP_OPEN, payload, expected_session=expected_session)
        if frame.opcode != opcodes.BOT_WEBAPP_OPEN or not frame.ok:
            raise MaxError('MAX rejected the mini-app launch request')
        try:
            return parse_miniapp_launch(frame.decoded)
        except ValueError:
            raise MaxError('MAX did not confirm a supported mini-app launch address') from None

    def conversation_file_download(self, file_id, chat_id: int, message_id: int, *,
                                    expected_session: AuthenticatedSession,
                                    item_type: str = 'REGULAR') -> FileDownloadReply:
        """One bound read-only88; returns no filename/default safety value."""
        payload = build_file_download(file_id, chat_id, message_id, item_type=item_type)
        if not isinstance(expected_session, AuthenticatedSession):
            raise MaxWriteNotStarted('A current authenticated file context is required')
        frame = self.request(opcodes.FILE_DOWNLOAD, payload, expected_session=expected_session)
        if frame.opcode != opcodes.FILE_DOWNLOAD or not frame.ok:
            raise MaxError('MAX rejected the file address request')
        try:
            return parse_file_download(frame.decoded)
        except ValueError:
            raise MaxError('MAX did not confirm the file address and safety result') from None

    def request_file_download(self, file_id: int, chat_id: int, message_id: int, *,
                              expected_session: Optional[AuthenticatedSession] = None) -> dict:
        kwargs = {'expected_session': expected_session} if expected_session is not None else {}
        f = self.request(
            opcodes.FILE_DOWNLOAD,
            {"fileId": file_id, "chatId": chat_id, "messageId": message_id},
            **kwargs,
        )
        if not f.ok:
            raise MaxError(f"FILE_DOWNLOAD: {f.error_text()}")
        return _str_keys(f.decoded) if isinstance(f.decoded, dict) else {}

    def transcribe_media(self, media_id: int, chat_id: int, message_id: int) -> dict:
        f = self.request(
            opcodes.TRANSCRIBE_MEDIA,
            {"mediaId": media_id, "chatId": chat_id, "messageId": message_id},
        )
        if not f.ok:
            raise MaxError(f"TRANSCRIBE_MEDIA: {f.error_text()}")
        return _str_keys(f.decoded) if isinstance(f.decoded, dict) else {}

    # ───────────────────────── sessions / 2FA ─────────────────────────

    def change_conversation_lifecycle(self, action, chat_id, last_event_time, for_all, *,
                                      expected_session):
        if action not in ('clear', 'delete'):
            raise ValueError('Unsupported conversation action')
        if expected_session is None or self.authenticated_session is not expected_session:
            raise MaxWriteNotStarted('The authenticated conversation session changed')
        payload = build_chat_lifecycle(chat_id, last_event_time, for_all)
        frame = self._write_frame(54 if action == 'clear' else 52, payload,
                                  expected_session=expected_session)
        if not frame.ok:
            raise MaxError('MAX отклонил действие с чатом')
        if action == 'delete' and (type(frame.body) is not bytes or frame.body != b''):
            # The decoder also returns None for malformed bytes. Only the actual
            # empty VOID52 success frame may authorize a deletion boundary.
            raise MaxWriteOutcomeUnknown('MAX не подтвердил действие с чатом. Повтор не выполнен.')
        try:
            parse_lifecycle_ack(frame.decoded)
        except ValueError:
            raise MaxWriteOutcomeUnknown('MAX не подтвердил действие с чатом. Повтор не выполнен.') from None

    def edit_conversation_fields(self, chat_id, chat_type, patch, *, expected_session):
        from .conversation_edit_contracts import build_conversation_edit, parse_conversation_edit_ack
        if expected_session is None or self.authenticated_session is not expected_session:
            raise MaxWriteNotStarted('The authenticated conversation session changed')
        if chat_type not in ('CHAT', 'CHANNEL'):
            raise ValueError('Сведения этого вида чата не редактируются')
        frame = self._write_frame(55, build_conversation_edit(chat_id, patch), expected_session=expected_session)
        if not frame.ok:
            raise MaxError('MAX отклонил изменение сведений')
        try:
            return parse_conversation_edit_ack(frame.decoded, chat_id, chat_type, patch)
        except ValueError:
            raise MaxWriteOutcomeUnknown('Изменение сведений не подтверждено. Повтор не выполнен.') from None

    def transfer_conversation_owner(self, chat_id, successor_id, *, expected_session):
        from .owner_transfer_contracts import build_owner_transfer, parse_owner_transfer_ack
        if expected_session is None or self.authenticated_session is not expected_session:
            raise MaxWriteNotStarted('The authenticated conversation session changed')
        frame = self._write_frame(55, build_owner_transfer(chat_id, successor_id),
                                  expected_session=expected_session)
        if not frame.ok:
            raise MaxError('MAX отклонил передачу прав')
        try:
            return parse_owner_transfer_ack(frame.decoded, chat_id, successor_id)
        except ValueError:
            raise MaxWriteOutcomeUnknown('Передача прав не подтверждена. Повтор не выполнен.') from None

    def conversation_transfer_members(self, chat_id, member_type, *, query='', marker=None, expected_session):
        from .owner_transfer_contracts import build_transfer_members, parse_transfer_members
        if expected_session is None or self.authenticated_session is not expected_session:
            raise MaxWriteNotStarted('The authenticated conversation session changed')
        payload = build_transfer_members(chat_id, member_type, query=query, marker=marker)
        frame = self.request(59, payload, expected_session=expected_session)
        if not frame.ok:
            raise MaxError('MAX не предоставил участников')
        return parse_transfer_members(frame.decoded)

    def leave_conversation(self, chat_id, *, expected_session):
        if expected_session is None or self.authenticated_session is not expected_session:
            raise MaxWriteNotStarted('The authenticated conversation session changed')
        payload = build_chat_leave(chat_id)
        frame = self._write_frame(58, payload, expected_session=expected_session)
        if not frame.ok:
            raise MaxError('MAX отклонил выход из чата')
        try:
            return parse_leave_ack(frame.decoded)
        except ValueError:
            raise MaxWriteOutcomeUnknown('MAX не подтвердил выход из чата. Повтор не выполнен.') from None

    def conversation_report_reasons(self, sync, *, expected_session):
        if expected_session is None or self.authenticated_session is not expected_session:
            raise MaxWriteNotStarted('The authenticated conversation session changed')
        frame = self.request(162, build_report_reasons(sync), expected_session=expected_session)
        if not frame.ok:
            raise MaxError('MAX не предоставил причины жалобы')
        return parse_report_reasons(frame.decoded)

    def report_conversation(self, type_id, target_id, reason_id, *, expected_session):
        if expected_session is None or self.authenticated_session is not expected_session:
            raise MaxWriteNotStarted('The authenticated conversation session changed')
        payload = build_conversation_report(type_id, target_id, reason_id)
        frame = self._write_frame(161, payload, expected_session=expected_session)
        if not frame.ok:
            raise MaxError('MAX отклонил жалобу')
        try:
            result = parse_report_ack(frame.decoded)
        except ValueError:
            raise MaxWriteOutcomeUnknown('Результат отправки не подтверждён. Повторная отправка остановлена.') from None
        if result['success'] is not True:
            raise MaxError('MAX отклонил жалобу')
        return result

    def share_contact_message(self, chat_id, contact_id, cid, *, expected_session):
        if expected_session is None or self.authenticated_session is not expected_session:
            raise MaxWriteNotStarted('The authenticated conversation session changed')
        payload = build_contact_share(chat_id, contact_id, cid)
        frame = self._write_frame(64, payload, expected_session=expected_session)
        if not frame.ok:
            raise MaxError('MAX отклонил передачу контакта')
        try:
            return parse_contact_share_ack(frame.decoded, chat_id, cid)
        except ValueError:
            raise MaxWriteOutcomeUnknown('MAX не подтвердил передачу контакта. Повтор не выполнен.') from None

    def update_chat_mute(self, chat_id: int, dont_disturb_until, *,
                         expected_session: Optional[AuthenticatedSession] = None) -> Optional[dict]:
        """One explicit Info Sound write; optional CONFIG is never a value echo."""
        payload = build_chat_mute(chat_id, dont_disturb_until)
        if expected_session is not None and self.authenticated_session is not expected_session:
            raise MaxWriteNotStarted('The authenticated conversation session changed')
        frame = self._write_frame(opcodes.USER_SETTINGS, payload, expected_session=expected_session)
        if not frame.ok:
            raise MaxError('MAX rejected the conversation notification change')
        try:
            return parse_chat_mute_ack(frame.decoded)
        except (ValueError, TypeError, OverflowError):
            raise MaxWriteOutcomeUnknown(
                'MAX did not confirm the conversation notification state.'
            ) from None

    def set_contact_stories_hidden(self, contact_id: int, hidden: bool, *,
                                   expected_session: Optional[AuthenticatedSession] = None) -> dict:
        payload = build_story_visibility(contact_id, hidden)
        if expected_session is not None and self.authenticated_session is not expected_session:
            raise MaxWriteNotStarted('The authenticated conversation session changed')
        frame = self._write_frame(opcodes.CONTACT_ACTION, payload, expected_session=expected_session)
        if not frame.ok:
            raise MaxError('MAX rejected the story visibility change')
        try:
            return parse_story_visibility_ack(frame.decoded, contact_id)
        except (ValueError, TypeError, OverflowError):
            raise MaxWriteOutcomeUnknown('MAX did not confirm story visibility.') from None

    def stop_bot(self, bot_id: int, *,
                 expected_session: Optional[AuthenticatedSession] = None) -> None:
        payload = build_stop_bot(bot_id)
        if expected_session is not None and self.authenticated_session is not expected_session:
            raise MaxWriteNotStarted('The authenticated conversation session changed')
        frame = self._write_frame(opcodes.BOT_STOP, payload, expected_session=expected_session)
        if not frame.ok:
            raise MaxError('MAX rejected the bot-stop action')
        try:
            return parse_stop_bot_ack(frame.decoded)
        except ValueError:
            raise MaxWriteOutcomeUnknown('MAX did not confirm the bot-stop result.') from None

    def update_user_settings(self, patch: dict, *,
                             expected_session: Optional[AuthenticatedSession] = None) -> Optional[dict]:
        """Apply an explicit ordinary privacy patch; no config means pending readback."""
        supplied = user_setting_values(patch, writable=True)
        try:
            kwargs = {'expected_session': expected_session} if expected_session is not None else {}
            frame = self._write_frame(opcodes.USER_SETTINGS, {"settings": {"user": supplied}}, **kwargs)
        except MaxWriteOutcomeUnknown:
            raise MaxWriteOutcomeUnknown(
                "Результат изменения настроек неизвестен. Перед повтором проверьте состояние."
            ) from None
        if not frame.ok:
            raise MaxError(f"USER_SETTINGS: {frame.error_text()}")
        if frame.decoded is None:  # The current public ACK is optional.
            return None
        data = _str_keys(frame.decoded) if isinstance(frame.decoded, dict) else None
        try:
            if data is None or not isinstance(data.get("hash"), str):
                raise ValueError("Unconfirmed settings response")
            if "user" in data:
                user_setting_values(data["user"])
            if "chats" in data and not isinstance(data["chats"], dict):
                raise ValueError("Unconfirmed chat settings")
        except ValueError:
            raise MaxWriteOutcomeUnknown(
                "Изменение настроек не подтверждено. Перед повтором проверьте состояние."
            ) from None
        return data

    def sessions_info(self, *, page_size: Optional[int] = None,
                      marker: Optional[str] = None) -> MaxFrame:
        """Read documented session DTOs; their time is not a session ID."""
        payload = {}
        if page_size is not None:
            payload["pageSize"] = _require_count(page_size, allow_zero=False)
        if marker is not None:
            if not isinstance(marker, str):
                raise ValueError("Session marker must be a string")
            payload["marker"] = marker
        frame = self.request(opcodes.SESSIONS_INFO, payload)
        if not frame.ok:
            raise MaxError(f"SESSIONS_INFO: {frame.error_text()}")
        if not isinstance(frame.decoded, dict) or not isinstance(frame.decoded.get("sessions"), list):
            raise MaxError("SESSIONS_INFO did not return structured sessions")
        return frame

    def sessions_close(self, session_ids: list[int]) -> MaxFrame:
        """The historical selected-ID request is unsupported by current source."""
        raise NotImplementedError("Индивидуальные идентификаторы сессий MAX не подтверждены")

    def close_other_sessions(self) -> dict:
        """Explicit close-all-but-current97; never infer IDs or rotate credentials."""
        try:
            frame = self._write_frame(opcodes.SESSIONS_CLOSE, {})
        except MaxWriteOutcomeUnknown:
            raise MaxWriteOutcomeUnknown(
                "Результат завершения сессий неизвестен. Перед повтором проверьте устройства."
            ) from None
        if not frame.ok:
            raise MaxError(f"SESSIONS_CLOSE: {frame.error_text()}")
        data = _str_keys(frame.decoded) if isinstance(frame.decoded, dict) else None
        if data is None or not isinstance(data.get("token"), str) or not data["token"]:
            raise MaxWriteOutcomeUnknown(
                "Закрытие других сессий не подтверждено. Перед повтором проверьте устройства."
            )
        # The actual current WEB producer ignores this opaque ACK token.
        return data

    def auth_2fa_details(self, *, expected_session: Optional[AuthenticatedSession] = None) -> MaxFrame:
        kwargs = {'expected_session': expected_session} if expected_session is not None else {}
        return self.request(opcodes.AUTH_2FA_DETAILS, {}, **kwargs)

    # 2FA: смена пароля (поток из test5.py: CREATE_TRACK 112 -> CHECK 113 -> SET 111)

    def auth_create_track(self, track_type: int = 0, *,
                          expected_session: Optional[AuthenticatedSession] = None) -> str:
        f = self._auth_frame(opcodes.AUTH_CREATE_TRACK, {"type": track_type}, expected_session=expected_session)
        track_id = self._find_track_id(f)
        if not track_id:
            raise MaxWriteOutcomeUnknown("AUTH_CREATE_TRACK: trackId не подтверждён MAX.")
        return track_id

    def auth_check_password(self, track_id: str, password: str, *,
                            expected_session: Optional[AuthenticatedSession] = None) -> MaxFrame:
        kwargs = {'expected_session': expected_session} if expected_session is not None else {}
        return self.request(
            opcodes.AUTH_CHECK_PASSWORD, {"trackId": track_id, "password": password}, **kwargs
        )

    def auth_set_2fa(
        self,
        track_id: str,
        new_password: str,
        hint: Optional[str] = None,
        *,
        capability: int = 1,
        expected_session: Optional[AuthenticatedSession] = None,
    ) -> MaxFrame:
        # capability (lti enum, декомпил): 0=SET_PASSWORD (включить 2FA с нуля),
        # 1=UPDATE_PASSWORD (сменить включённый), 2=RESTORE_PASSWORD (сброс по
        # email). Сервер РАЗЛИЧАЕТ их: SET_2FA с [1] на ВЫКЛЮЧЕННОМ 2FA → ошибка
        # password.is.off «2fa is not enabled» (проверено вживую 2026-06-08).
        payload: dict[str, Any] = {
            "trackId": track_id,
            "password": new_password,
            "expectedCapabilities": [capability],
        }
        if hint:
            payload["hint"] = hint
        kwargs = {'expected_session': expected_session} if expected_session is not None else {}
        return self.request(opcodes.AUTH_SET_2FA, payload, **kwargs)

    def auth_verify_email(self, track_id: str, email: str, *,
                          expected_session: Optional[AuthenticatedSession] = None) -> MaxFrame:
        """AUTH_VERIFY_EMAIL (109): отправить код подтверждения на recovery-email.
        Пустой email — повторно отправить код на уже заданный адрес (resend)."""
        payload: dict[str, Any] = {"trackId": track_id}
        if email:
            payload["email"] = email
        kwargs = {'expected_session': expected_session} if expected_session is not None else {}
        return self.request(opcodes.AUTH_VERIFY_EMAIL, payload, **kwargs)

    def auth_check_email(self, track_id: str, code: str, *,
                         expected_session: Optional[AuthenticatedSession] = None) -> MaxFrame:
        """AUTH_CHECK_EMAIL (110): подтвердить код из письма -> привязка email."""
        kwargs = {'expected_session': expected_session} if expected_session is not None else {}
        return self.request(
            opcodes.AUTH_CHECK_EMAIL, {"trackId": track_id, "verifyCode": code}, **kwargs
        )

    @staticmethod
    def _find_track_id(f: MaxFrame) -> Optional[str]:
        if isinstance(f.decoded, dict):
            value = f.decoded.get("trackId")
            if _is_bounded_qr_string(value):
                return value
        return None


# ───────────────────────── helpers ─────────────────────────


def _is_bounded_qr_string(value, *, limit: int = 4096) -> bool:
    return isinstance(value, str) and 0 < len(value) <= limit


def _has_profile_identity(profile) -> bool:
    contact = profile.get("contact") if isinstance(profile, dict) else None
    identity = contact.get("id") if isinstance(contact, dict) else None
    return type(identity) is int and -(2**63) <= identity < 2**63


def _is_qr_number(value, *, positive: bool = False) -> bool:
    return (type(value) in (int, float) and math.isfinite(value)
            and (value > 0 if positive else value >= 0))


def _require_packet_int(value, label: str) -> int:
    if type(value) is not int or not -(2 ** 63) <= value < 2 ** 63:
        raise ValueError(f"{label} must be a signed 64-bit integer")
    return value


def _require_packet_timestamp(value, label: str) -> int | float:
    try:
        valid = type(value) in (int, float) and math.isfinite(value)
    except (OverflowError, ValueError):
        valid = False
    if not valid:
        raise ValueError(f"{label} must be a finite timestamp")
    return value


def _require_packet_ids(values) -> list[int]:
    if not isinstance(values, list) or not 1 <= len(values) <= 1000:
        raise ValueError("A bounded, non-empty list of message IDs is required")
    return [_require_packet_int(value, "message_id") for value in values]


def _require_object_array(values, label: str) -> list[dict]:
    if not isinstance(values, list) or len(values) > 100 or any(not isinstance(value, dict) for value in values):
        raise ValueError(f"{label} must be a bounded list of objects")
    return list(values)


def _require_count(value, *, allow_zero: bool = True) -> int:
    if type(value) is not int or not (0 if allow_zero else 1) <= value <= 100:
        raise ValueError("Page count must be within the local limit of 100")
    return value

def _doh_resolve(host: str) -> Optional[str]:
    """Резолв A-записи через DNS-over-HTTPS. Endpoint'ы заданы по IP
    (1.1.1.1, 8.8.8.8), чтобы самим не зависеть от системного DNS."""
    import json
    import urllib.request

    endpoints = (
        f"https://1.1.1.1/dns-query?name={host}&type=A",
        f"https://8.8.8.8/resolve?name={host}&type=A",
    )
    for url in endpoints:
        try:
            req = urllib.request.Request(
                url, headers={"accept": "application/dns-json"}
            )
            with urllib.request.urlopen(req, timeout=8) as resp:
                data = json.load(resp)
            for ans in data.get("Answer", []):
                if ans.get("type") == 1 and ans.get("data"):
                    return str(ans["data"])
        except Exception:
            continue
    return None


def _iana_timezone() -> str:
    """Историческое приближение IANA-таймзоны по локальному смещению UTC.

    Смещение не определяет IANA-зону однозначно. Серверные правила проверки
    timezone неизвестны; эта функция не подтверждает реальную зону устройства.
    """
    try:
        from datetime import datetime, timezone

        off = datetime.now(timezone.utc).astimezone().utcoffset()
        hours = int(off.total_seconds() // 3600) if off is not None else 3
    except Exception:
        hours = 3
    return {
        2: "Europe/Kaliningrad",
        3: "Europe/Moscow",
        4: "Asia/Tbilisi",
        5: "Asia/Yekaterinburg",
        6: "Asia/Omsk",
        7: "Asia/Krasnoyarsk",
        8: "Asia/Irkutsk",
        9: "Asia/Yakutsk",
        10: "Asia/Vladivostok",
        11: "Asia/Magadan",
        12: "Asia/Kamchatka",
    }.get(hours, "Europe/Moscow")


_FINDER_USER_RE = re.compile(r"User[,\s]+(\d+)")


def _peer_from_finder_error(frame: "MaxFrame") -> Optional[int]:
    """Из ошибки SEND_MESSAGE not.found вида
    'Key: common.finder, args: [User, 335225177,]' достать peer userId — сервер
    так подсказывает, что chatId на самом деле 1:1-диалог.

    Срабатывает ТОЛЬКО на finder-промахе. Чужую ошибку (blocked/rate-limit и
    т.п.), где в теле случайно встретилось 'User N', трактовать как «это диалог»
    нельзя — иначе групповое сообщение уйдёт постороннему по userId."""
    d = frame.decoded
    if (frame.cmd != 3 or frame.opcode != opcodes.SEND_MESSAGE or not isinstance(d, dict)
            or d.get("error") not in ("not.found", "common.finder")
            or not isinstance(d.get("message"), str)):
        return None
    text = d["message"]
    m = _FINDER_USER_RE.search(text)
    if m:
        try:
            identity = int(m.group(1))
            return identity if 0 < identity < 2**63 else None
        except ValueError:
            return None
    return None


def _str_keys(d: Any) -> dict:
    if isinstance(d, dict):
        return {str(k): v for k, v in d.items()}
    return {}


def _parse_contact(decoded: dict) -> dict:
    contact = decoded.get("contact") if isinstance(decoded, dict) else None
    if not _has_profile_identity({"contact": contact}):
        raise MaxError("CONTACT_INFO_BY_PHONE did not return a typed contact")
    result = {
        "id": contact["id"],
        "name": contact.get("name") if isinstance(contact.get("name"), str) else None,
    }
    # Keep name records as records; their string representation is never a name.
    # Retain only ordinary contact metadata consumed by the core parser.
    for key in ('names', 'firstName', 'lastName', 'baseUrl', 'baseRawUrl', 'photoId',
                'options', 'flags', 'status', 'accountStatus', 'link', 'description'):
        if key in contact:
            result[key] = copy.deepcopy(contact[key])
    phone = contact.get('phone')
    if 'phone' not in contact:
        return result
    result['phone'] = None
    if type(phone) is int:
        result['phone'] = str(phone)
    elif type(phone) is float and math.isfinite(phone) and phone.is_integer():
        result['phone'] = str(int(phone))
    elif isinstance(phone, str):
        result['phone'] = phone
    return result


def _to_int(v: Any) -> Optional[int]:
    if v is None:
        return None
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        return int(v)
    try:
        return int(str(v))
    except (TypeError, ValueError):
        return None


def _redact(payload: dict) -> dict:
    out = {}
    for k, v in payload.items():
        if str(k).lower() in _SECRET_KEYS:
            out[k] = "***"
        elif isinstance(v, dict):
            out[k] = _redact(v)
        elif isinstance(v, str) and len(v) > 80:
            out[k] = v[:8] + "…" + v[-6:]
        else:
            out[k] = v
    return out
