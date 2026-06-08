"""MaxClient — одно TLS-соединение к api.oneme.ru:443 + фоновый читающий поток.

Запросы идут через request() и матчатся с ответами по seq. Кадры без нашего seq
считаются server-push и отдаются в on_push. Класс не зависит от GUI: колбэки
on_push/on_state вызываются из читающего потока, обёртку в Qt-сигналы делает UI.

Портирование рабочего test5.py (Python) + Flutter max_client.dart.
"""
from __future__ import annotations

import random
import socket
import ssl
import threading
import time
import uuid
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Optional

from . import opcodes
from .codec import (
    HEADER_LEN,
    build_frame,
    decompress_body,
    parse_header,
    raw_ascii,
    unpack_payload,
)
from .errors import MaxError, MaxLoginFailed, MaxNotConnected, MaxTimeout
from . import raw_parsers as rp

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
    "verifycode", "email",
}


class ConnectionState(Enum):
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    RECONNECTING = "reconnecting"


@dataclass
class MaxFrame:
    cmd: int
    seq: int
    opcode: int
    body: bytes
    decoded: Any

    @property
    def ok(self) -> bool:
        return self.cmd == 1

    def error_text(self) -> str:
        d = self.decoded
        if isinstance(d, dict):
            for k in ("localizedMessage", "message", "error", "title"):
                v = d.get(k)
                if isinstance(v, str) and v:
                    return v
        return raw_ascii(self.body)[:200]


class _Pending:
    __slots__ = ("event", "frame", "error")

    def __init__(self) -> None:
        self.event = threading.Event()
        self.frame: Optional[MaxFrame] = None
        self.error: Optional[BaseException] = None


class MaxClient:
    def __init__(
        self,
        *,
        app_version: str = DEFAULT_APP_VERSION,
        locale: str = DEFAULT_LOCALE,
        device_id: Optional[str] = None,
        on_push: Optional[Callable[[MaxFrame], None]] = None,
        on_state: Optional[Callable[[ConnectionState], None]] = None,
        on_debug: Optional[Callable[[str], None]] = None,
        on_auth_invalid: Optional[Callable[[], None]] = None,
        auto_reconnect: bool = True,
    ) -> None:
        self.app_version = app_version
        self.locale = locale
        self.device_id = device_id or str(uuid.uuid4())
        self.on_push = on_push
        self.on_state = on_state
        self.on_debug = on_debug
        # Вызывается из reconnect-потока, когда сервер отверг сохранённый токен
        # (мёртвый/протухший): UI должен разлогинить и показать экран входа, а
        # не висеть в «не в сети». Порт onAuthInvalid из max iso.
        self.on_auth_invalid = on_auth_invalid
        self.auto_reconnect = auto_reconnect

        self._sock: Optional[ssl.SSLSocket] = None
        self._reader: Optional[threading.Thread] = None
        self._send_lock = threading.Lock()
        self._seq = 0
        self._pending: dict[int, _Pending] = {}
        self._pending_lock = threading.Lock()
        self._closed = False
        self._state = ConnectionState.DISCONNECTED

        self._token: Optional[str] = None
        self._device_type = "ANDROID"

        self._reconnect_thread: Optional[threading.Thread] = None
        self._reconnect_running = False
        self._reconnect_stop: Optional[threading.Event] = None
        # Сериализует жизненный цикл реконнекта (старт/стоп/успех-коммит и
        # решение «пришёл дроп» в reader-потоке). В Dart-reference это один
        # event-loop; в Python reader/reconnect/UI — разные потоки, поэтому
        # нужен общий замок. RLock — _handle_drop под локом зовёт _start_reconnect.
        self._reconnect_lock = threading.RLock()

        # Keepalive: держим сокет живым PING'ом, чтобы сервер не дропал по
        # простою (иначе reconnect-шторм -> бан номера антифродом).
        self._keepalive_thread: Optional[threading.Thread] = None
        self._keepalive_stop: Optional[threading.Event] = None

        # Анти-шторм (порт ReconnectPolicy из max iso). Главный инвариант: не
        # делать LOGIN (op19) чаще, чем раз в MIN_AUTH_INTERVAL. _last_login_ts —
        # монотонное время последнего успешного LOGIN; НЕ сбрасывается при дропе
        # (считаем время с АВТОРИЗАЦИИ, а не с разрыва). _reconnect_ok_ts —
        # времена успешных реконнектов для предохранителя флаппинга.
        self._last_login_ts: Optional[float] = None
        self._reconnect_ok_ts: list[float] = []

    KEEPALIVE_INTERVAL = 20.0   # сек; окно сервера 11..61 c, шлём с запасом
    RECONNECT_BASE = 5.0        # база экспоненциального backoff
    RECONNECT_MAX = 60.0        # потолок backoff
    MIN_AUTH_INTERVAL = 30.0    # не логиниться чаще раза в 30 c (главный анти-бан)
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
        второй connect+LOGIN (двойная сессия с одним токеном = сигнал угона).
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
        # Не плодим параллельные соединения: одно устройство — одна сессия
        # (несколько живых сокетов с одним токеном = сигнал угона для антифрода).
        if self._sock is not None:
            self._teardown_socket()
        self._set_state(ConnectionState.CONNECTING)
        tls = None
        try:
            ctx = ssl.create_default_context()
            raw = self._open_raw_socket(timeout)
            raw.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            # server_hostname=HOST даёт правильный SNI и проверку сертификата,
            # даже если соединились по IP из DoH.
            tls = ctx.wrap_socket(raw, server_hostname=HOST)
            tls.settimeout(None)  # блокирующее чтение; стоп — через close()
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

    def _teardown_socket(self, only: Optional[ssl.SSLSocket] = None) -> None:
        # only!=None — рвём, ТОЛЬКО если это всё ещё текущий сокет. Иначе
        # «опоздавший» reconnect-поток (его connect() висел в сетевом I/O дольше,
        # чем барьер join ждал) закрыл бы свежий сокет пользовательского connect()
        # и убил бы живую сессию. Та же сокет-идентичность, что в _handle_drop.
        if only is not None and self._sock is not only:
            return
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
                    # MAX сжимает тело кадра (LZ4/zstd) — распаковываем по флагу
                    # из заголовка. Дальше весь разбор идёт по «чистым» байтам.
                    body = decompress_body(raw_body, comp)
                    frame = MaxFrame(
                        cmd=cmd,
                        seq=seq,
                        opcode=opcode,
                        body=body,
                        decoded=unpack_payload(body),
                    )
                    self._dispatch(frame)
        finally:
            self._handle_drop(sock)

    def _dispatch(self, frame: MaxFrame) -> None:
        with self._pending_lock:
            pending = self._pending.pop(frame.seq, None)
        if pending is not None:
            pending.frame = frame
            pending.event.set()
            return
        # Кадр без нашего seq — это push от сервера.
        if self.on_push:
            try:
                self.on_push(frame)
            except Exception:
                pass

    def _handle_drop(self, sock: Optional[ssl.SSLSocket] = None) -> None:
        # Идентификация сокета: устаревший reader (от уже заменённого сокета) не
        # должен рвать сокет-преемник, поднятый параллельным connect() (иначе
        # закрыл бы свежую сессию пользователя и обнулил бы pending нового
        # сокета). Dart отменяет подписку до переустановки _socket; у нас этого
        # нет — поэтому сверяем по объекту сокета. Проверка ДО _fail_all_pending.
        if sock is not None and sock is not self._sock:
            return
        if self._sock is None and self._closed:
            return
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
        писать self._sock и сделать второй LOGIN на том же токене (сигнал угона
        антифроду — ровно то, что мы устраняем). Из самого reconnect-потока
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
        while self._reconnect_running and not self._closed:
            delay = self._reconnect_delay(attempt)
            self._debug(
                f"reconnect через {delay:.1f}s (попытка {attempt}, "
                f"успехов в окне {len(self._reconnect_ok_ts)}, "
                f"с LOGIN {self._since_last_login():.0f}s)"
            )
            if ev.wait(delay):
                return  # запрошен стоп (close) — выходим без попытки
            if self._closed or not self._reconnect_running:
                return
            try:
                self.connect(device_type=self._device_type, reset_closed=False)
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
                    self.login(self._token)
                if self._closed or not self._reconnect_running:
                    self._teardown_socket(only=my_sock)
                    if not self.is_connected:
                        self._set_state(ConnectionState.DISCONNECTED)
                    return
                # LOGIN состоялся. Кормим предохранитель СРАЗУ, не дожидаясь,
                # переживёт ли сокет окно успеха: риск бана — сама частота re-auth
                # (op19), а не выживание сокета. Иначе «connect-OK → мгновенный
                # дроп» крутил бы ~120 LOGIN/час МИМО счётчика флаппинга и cooldown
                # никогда не срабатывал бы. Порт max_client.dart:1184 — там тоже
                # считают каждую успешную авторизацию, безусловно.
                with self._reconnect_lock:
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
            except MaxLoginFailed as e:
                # Токен мёртв — нет смысла долбить сервер протухшим токеном.
                # ОБЯЗАТЕЛЬНО рвём уже поднятый (connect успел) сокет, иначе
                # keepalive пинговал бы неавторизованное соединение (аномальный
                # трафик). Чистим токен, уходим в DISCONNECTED и сигналим UI
                # (on_auth_invalid) уйти на экран входа.
                self._debug(f"reconnect aborted (token invalid): {e}")
                # Дискриминатор «мы всё ещё активный владелец» — ТОЛЬКО флаг
                # _reconnect_running, а НЕ сверка сокета: пользовательский вход
                # (connect(reset_closed=True)/close()) всегда снимает running
                # через _stop_reconnect; а вот наш собственный сокет мог обнулить
                # _handle_drop (сервер отверг токен и сразу закрыл соединение) —
                # тогда running ещё True и разлогинить НУЖНО. Сверка self._sock
                # здесь теряла бы logout в гонке NACK+drop (сокет уже None).
                if self._reconnect_running:
                    self._token = None
                    self._stop_reconnect()
                    self._teardown_socket(only=my_sock)
                    if not self.is_connected:
                        self._set_state(ConnectionState.DISCONNECTED)
                    if self.on_auth_invalid:
                        try:
                            self.on_auth_invalid()
                        except Exception:
                            pass
                else:
                    # Нас вытеснил пользовательский вход (running снят) — молча
                    # тушим свой (вероятно уже закрытый) сокет и выходим, не
                    # трогая свежий токен/сессию пользователя.
                    self._teardown_socket(only=my_sock)
                return
            except Exception as e:  # noqa: BLE001
                self._debug(f"reconnect failed: {e}")
                attempt += 1

    # ───────────────────────── request / response ─────────────────────────

    def request(
        self,
        opcode: int,
        payload: dict,
        *,
        timeout: float = 30.0,
        cmd: int = 0,
    ) -> MaxFrame:
        """Синхронно отправить запрос и дождаться ответа с тем же seq.

        Вызывать из рабочего потока, не из GUI-треда (блокирует до ответа).
        """
        sock = self._sock
        if sock is None or self._closed:
            raise MaxNotConnected("socket is null")

        pending = _Pending()
        with self._send_lock:
            seq = self._seq
            self._seq = (self._seq + 1) & 0xFFFF
            with self._pending_lock:
                self._pending[seq] = pending
            frame = build_frame(seq, opcode, payload, cmd=cmd)
            self._debug(f">> {opcodes.name(opcode)} seq={seq} {_redact(payload)}")
            try:
                sock.sendall(frame)
            except OSError as e:
                with self._pending_lock:
                    self._pending.pop(seq, None)
                raise MaxNotConnected(f"send failed: {e}") from e

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
            # Не-OK ответ — логируем подробно для диагностики (decoded + ascii).
            self._debug(
                f"<< {opcodes.name(opcode)} seq={seq} cmd={f.cmd} NOT-OK "
                f"decoded={f.decoded!r} ascii={raw_ascii(f.body)[:400]!r}"
            )
        return f

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

        Для ANDROID — полный правдоподобный набор из 11 полей в строгом порядке
        официального клиента (pushDeviceType обязан идти ВТОРЫМ; порт
        DeviceProfile из max iso). Урезанный UA из трёх полей сам по себе выдаёт
        сторонний клиент антифроду — это дешёвый сигнал, его и убираем. Сервер
        MAX не проверяет TLS/JA3, поэтому самосогласованный UA безопасен.

        Для WEB и прочего — минимум: вход по веб-токену уже работает, а
        официальный WEB-UA не реверснут, ломать рабочий путь смысла нет.

        Порядок ключей важен: msgpack сериализует dict в порядке вставки.
        """
        if self._device_type == "ANDROID":
            ua: dict[str, Any] = {
                "deviceType": "ANDROID",
                "pushDeviceType": "GCM",
                "appVersion": self.app_version,
                "arch": "arm64-v8a",
            }
            # buildNumber согласован только с дефолтной версией (26.15.0 → 6689).
            # Если app_version переопределили в настройках — не шлём рассинхрон
            # версии и билда (это был бы новый сигнал «не родной клиент»).
            if self.app_version == DEFAULT_APP_VERSION:
                ua["buildNumber"] = APP_BUILD
            ua.update(
                {
                    "osVersion": "34",
                    "locale": self.locale,
                    "deviceLocale": "ru_RU",
                    "deviceName": "Android",
                    "screen": "1080x2340",
                    "timezone": _iana_timezone(),
                }
            )
            return ua
        return {
            "deviceType": self._device_type,
            "locale": self.locale,
            "appVersion": self.app_version,
        }

    def start_auth_sms(self, phone: str) -> str:
        """Запросить SMS. Возвращает verify-token для confirm_sms."""
        f = self.request(opcodes.AUTH_REQUEST, {"phone": phone, "type": "START_AUTH"})
        if not f.ok:
            raise MaxLoginFailed(f"AUTH_REQUEST: {f.error_text()}")
        token = rp.find_long_token(f.body)
        if not token:
            raise MaxLoginFailed("verify-token не найден в ответе")
        return token

    def confirm_sms(self, verify_token: str, code: str) -> tuple[Optional[str], Optional[str]]:
        """Подтвердить SMS-код. Возвращает (auth_token, track_id_for_2fa)."""
        f = self.request(
            opcodes.AUTH_CONFIRM,
            {
                "token": verify_token,
                "verifyCode": code,
                "authTokenType": "CHECK_CODE",
            },
        )
        if not f.ok:
            if f.cmd == 3:
                raise MaxLoginFailed("SMS-код неверный или истёк. Запросите новый код.")
            raise MaxLoginFailed(f"AUTH_CONFIRM: {f.error_text()}")

        auth_token = rp.find_long_token(f.body)
        if auth_token:
            return auth_token, None

        if b"passwordChallenge" in f.body:
            track_id = rp.find_uuid(f.body)
            if not track_id:
                raise MaxLoginFailed("Нужен 2FA, но trackId не найден")
            return None, track_id

        raise MaxLoginFailed("Нет auth-token и нет 2FA-челленджа")

    def confirm_2fa(self, track_id: str, password: str) -> str:
        f = self.request(opcodes.TWO_FA, {"trackId": track_id, "password": password})
        if not f.ok:
            raise MaxLoginFailed(f"2FA: {f.error_text()}")
        token = rp.find_long_token(f.body)
        if not token:
            raise MaxLoginFailed("auth-token не найден после 2FA")
        return token

    def login(self, token: str) -> MaxFrame:
        """Логин по auth-token. Возвращает кадр LOGIN (raw содержит чаты/контакты)."""
        f = self.request(
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
        if not f.ok:
            raise MaxLoginFailed(f"LOGIN: {f.error_text()}")
        self._token = token
        # Метка для анти-шторм-троттла: следующий реконнект не будет логиниться
        # раньше, чем через MIN_AUTH_INTERVAL после этого момента.
        self._last_login_ts = time.monotonic()
        return f

    def logout(self) -> MaxFrame:
        return self.request(opcodes.LOGOUT, {})

    # ───────────────────────── messaging ─────────────────────────

    def current_profile(self) -> dict:
        f = self.request(opcodes.PROFILE, {})
        if not f.ok:
            raise MaxError(f"PROFILE: {f.error_text()}")
        if isinstance(f.decoded, dict):
            return _str_keys(f.decoded)
        return {
            "id": rp.read_int_after_key(f.body, b"\xa2id"),
            "name": rp.read_str_after_key(f.body, b"\xa4name"),
        }

    def update_profile_name(self, first_name: str, last_name: Optional[str] = None) -> dict:
        """Сменить своё имя профиля (видно другим). PROFILE(16) на запись:
        {requestId, firstName, lastName?} — порт updateProfileName из max iso
        (defpackage r2e.java Tasks.Profile). Опкод тот же, что на чтение."""
        payload: dict[str, Any] = {
            "requestId": int(time.time() * 1000),
            "firstName": first_name.strip(),
        }
        # last_name=None -> ключ опускаем (сервер сохраняет старую фамилию);
        # last_name="" -> шлём пустую строку (очищает фамилию). Семантика
        # подтверждена декомпилом r2e.java: omit=preserve, ""=clear.
        if last_name is not None:
            payload["lastName"] = last_name.strip()
        f = self.request(opcodes.PROFILE, payload)
        if not f.ok:
            raise MaxError(f"PROFILE update: {f.error_text()}")
        return _str_keys(f.decoded) if isinstance(f.decoded, dict) else {}

    def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        attaches: Optional[list[dict]] = None,
    ) -> dict:
        # Payload по декомпилу (docs/MEDIA_OPCODES.md): message несёт cid
        # (клиентский id сообщения). test5.py его не слал — вероятная причина
        # отказа сервера. Шлём cid + notify, randomId дублирует cid для дедупа.
        cid = int(time.time() * 1000)
        # cid/detectShare/isLive в декомпиле идут без "?" — реальный клиент шлёт
        # их всегда. detectShare=True включает превью ссылок, как в оригинале.
        message: dict[str, Any] = {
            "cid": cid,
            "text": text,
            "detectShare": True,
            "isLive": False,
        }
        if attaches:
            message["attaches"] = attaches
        f = self.request(
            opcodes.SEND_MESSAGE,
            {
                "chatId": chat_id,
                "message": message,
                "notify": True,
                "randomId": cid,
            },
        )
        if not f.ok:
            raise MaxError(f"SEND_MESSAGE: {f.error_text()}")
        return _str_keys(f.decoded) if isinstance(f.decoded, dict) else {}

    def typing(self, chat_id: int, is_typing: bool = True) -> None:
        try:
            self.request(opcodes.TYPING, {"chatId": chat_id, "typing": is_typing}, timeout=10)
        except MaxError:
            pass  # тайпинг — не критичная операция

    def edit_message(self, chat_id: int, message_id: int, text: str) -> dict:
        f = self.request(
            opcodes.MSG_EDIT,
            {"chatId": chat_id, "messageId": message_id, "text": text},
        )
        if not f.ok:
            raise MaxError(f"MSG_EDIT: {f.error_text()}")
        return _str_keys(f.decoded) if isinstance(f.decoded, dict) else {}

    def chat_info(self, chat_ids: list[int]) -> dict:
        f = self.request(opcodes.CHAT_INFO, {"chatIds": chat_ids})
        if not f.ok:
            raise MaxError(f"CHAT_INFO: {f.error_text()}")
        return _str_keys(f.decoded) if isinstance(f.decoded, dict) else {}

    def chat_history(self, chat_id: int, from_id: int = 0, count: int = 50) -> tuple[list[dict], bytes]:
        f = self.request(
            opcodes.CHAT_HISTORY,
            {"chatId": chat_id, "from": from_id, "forward": count},
        )
        if not f.ok:
            raise MaxError(f"CHAT_HISTORY: {f.error_text()}")
        if isinstance(f.decoded, dict):
            msgs = f.decoded.get("messages")
            if isinstance(msgs, list):
                return [_str_keys(m) for m in msgs if isinstance(m, dict)], f.body
        # Fallback: msgpack не распаковал — парсим сырьё.
        return rp.parse_history_messages(chat_id, f.body), f.body

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

    def contact_info(self, contact_ids: list[int]) -> dict:
        f = self.request(opcodes.CONTACT_INFO, {"contactIds": contact_ids})
        if not f.ok:
            raise MaxError(f"CONTACT_INFO: {f.error_text()}")
        return _str_keys(f.decoded) if isinstance(f.decoded, dict) else {}

    def contact_by_phone(self, phone: str) -> dict:
        f = self.request(opcodes.CONTACT_INFO_BY_PHONE, {"phone": phone})
        if not f.ok:
            raise MaxError(f"CONTACT_INFO_BY_PHONE: {f.error_text()}")
        if isinstance(f.decoded, dict):
            return _parse_contact(_str_keys(f.decoded), f.body)
        return _parse_contact({}, f.body)

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

    def request_file_download(self, file_id: int, chat_id: int, message_id: int) -> dict:
        f = self.request(
            opcodes.FILE_DOWNLOAD,
            {"fileId": file_id, "chatId": chat_id, "messageId": message_id},
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

    def sessions_info(self) -> MaxFrame:
        """Вернуть кадр SESSIONS_INFO целиком (decoded часто None -> парсим raw)."""
        return self.request(opcodes.SESSIONS_INFO, {})

    def sessions_close(self, session_ids: list[int]) -> MaxFrame:
        return self.request(opcodes.SESSIONS_CLOSE, {"sessionIds": session_ids})

    def auth_2fa_details(self) -> MaxFrame:
        return self.request(opcodes.AUTH_2FA_DETAILS, {})

    # 2FA: смена пароля (поток из test5.py: CREATE_TRACK 112 -> CHECK 113 -> SET 111)

    def auth_create_track(self, track_type: int = 0) -> str:
        f = self.request(opcodes.AUTH_CREATE_TRACK, {"type": track_type})
        if not f.ok:
            raise MaxError(f"AUTH_CREATE_TRACK: {f.error_text()}")
        track_id = self._find_track_id(f)
        if not track_id:
            raise MaxError("trackId не найден в ответе")
        return track_id

    def auth_check_password(self, track_id: str, password: str) -> MaxFrame:
        return self.request(
            opcodes.AUTH_CHECK_PASSWORD, {"trackId": track_id, "password": password}
        )

    def auth_set_2fa(
        self,
        track_id: str,
        new_password: str,
        hint: Optional[str] = None,
        *,
        capability: int = 1,
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
        return self.request(opcodes.AUTH_SET_2FA, payload)

    def auth_verify_email(self, track_id: str, email: str) -> MaxFrame:
        """AUTH_VERIFY_EMAIL (109): отправить код подтверждения на recovery-email.
        Пустой email — повторно отправить код на уже заданный адрес (resend)."""
        payload: dict[str, Any] = {"trackId": track_id}
        if email:
            payload["email"] = email
        return self.request(opcodes.AUTH_VERIFY_EMAIL, payload)

    def auth_check_email(self, track_id: str, code: str) -> MaxFrame:
        """AUTH_CHECK_EMAIL (110): подтвердить код из письма -> привязка email."""
        return self.request(
            opcodes.AUTH_CHECK_EMAIL, {"trackId": track_id, "verifyCode": code}
        )

    @staticmethod
    def _find_track_id(f: MaxFrame) -> Optional[str]:
        if isinstance(f.decoded, dict):
            for k in ("trackId", "track_id", "id"):
                v = f.decoded.get(k)
                if isinstance(v, str) and len(v) >= 20:
                    return v
        return rp.find_uuid(f.body)


# ───────────────────────── helpers ─────────────────────────

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
    """Best-effort IANA-таймзона по смещению UTC. Сервер таймзону жёстко не
    валидирует (у реальных клиентов она разная); важна правдоподобность.
    Порт DeviceProfile._ianaTimezone из max iso."""
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


def _str_keys(d: Any) -> dict:
    if isinstance(d, dict):
        return {str(k): v for k, v in d.items()}
    return {}


def _parse_contact(decoded: dict, body: bytes) -> dict:
    for key in ("contact", "contacts", "user"):
        v = decoded.get(key)
        if isinstance(v, list) and v:
            v = v[0]
        if isinstance(v, dict):
            m = _str_keys(v)
            return {
                "id": _to_int(m.get("id")),
                "name": m.get("name") or m.get("names"),
                "phone": str(m["phone"]) if m.get("phone") is not None else None,
            }
    return {
        "id": rp.read_int_after_key(body, b"\xa2id"),
        "name": rp.read_str_after_key(body, b"\xa4name"),
        "phone": None,
    }


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
