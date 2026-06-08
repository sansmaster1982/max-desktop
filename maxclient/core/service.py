"""MaxService — высокоуровневая оркестрация: client + store + session + settings.

GUI-агностичен. Методы блокирующие (вызывать из рабочего потока). Push с сервера
парсится в Message, кладётся в store и проксируется в колбэки on_message /
on_chat_changed, которые UI оборачивает в Qt-сигналы.
"""
from __future__ import annotations

import re
import time
import uuid
from collections import deque
from typing import Callable, Optional

from ..protocol.client import ConnectionState, MaxClient, MaxFrame
from ..protocol import raw_parsers as rp
from ..protocol.uploader import AttachKind, upload_and_build_attach
from .config import AppPaths, Session, Settings
from .models import Attach, Chat, Contact, Message, Profile, clean_message_text
from .store import Store


class MaxService:
    def __init__(self, paths: AppPaths, settings: Settings, session: Session) -> None:
        self.paths = paths
        self.settings = settings
        self.session = session
        self.store = Store(paths.db_file)

        # Лог протокола для диагностики: %APPDATA%\MAX Desktop\debug.log,
        # перезаписывается при старте сессии.
        self._log_path = paths.dir / "debug.log"
        try:
            self._log_path.write_text("", encoding="utf-8")
        except OSError:
            pass

        self.client = MaxClient(
            app_version=settings.app_version,
            locale=settings.locale,
            device_id=session.device_id,
            on_push=self._on_push,
            on_state=self._on_state,
            on_debug=self._log_debug,
            on_auth_invalid=self._on_auth_invalid,
            auto_reconnect=True,
        )
        # Колбэки в UI (устанавливаются снаружи).
        self.on_message: Optional[Callable[[Message], None]] = None
        self.on_chat_changed: Optional[Callable[[int], None]] = None
        self.on_state_changed: Optional[Callable[[ConnectionState], None]] = None
        # Сервер отверг сохранённый токен (протух/отозван) — UI должен уйти на
        # экран входа. Колбэк зовётся из reconnect-потока, UI оборачивает в сигнал.
        self.on_auth_invalid: Optional[Callable[[], None]] = None

        # Дедуп push: ограниченный набор (set + deque на вытеснение).
        self._processed_ids: set[int] = set()
        self._processed_order: deque[int] = deque()
        self._contacts_cache: Optional[dict[int, Contact]] = None
        self._chat_fetch_ts: dict[int, float] = {}  # троттлинг history-фетча
        self._last_sms_ts = 0.0  # кулдаун повторных SMS (анти «too many attempts»)

    @property
    def my_id(self) -> Optional[int]:
        return self.session.my_user_id

    def _mark_processed(self, mid: int) -> bool:
        """Отметить id обработанным. False — уже обрабатывали (дубль)."""
        if mid in self._processed_ids:
            return False
        self._processed_ids.add(mid)
        self._processed_order.append(mid)
        if len(self._processed_order) > 8000:
            old = self._processed_order.popleft()
            self._processed_ids.discard(old)
        return True

    def _contacts_map(self) -> dict[int, Contact]:
        if self._contacts_cache is None:
            self._contacts_cache = {c.id: c for c in self.store.list_contacts()}
        return self._contacts_cache

    def _invalidate_contacts(self) -> None:
        self._contacts_cache = None

    def _log_debug(self, msg: str) -> None:
        try:
            with open(self._log_path, "a", encoding="utf-8") as f:
                f.write(msg + "\n")
        except OSError:
            pass

    def log(self, msg: str) -> None:
        """Публичный лог для UI-хлебных крошек (тот же debug.log)."""
        self._log_debug("[UI] " + msg)

    # ───────────────────────── auth ─────────────────────────

    def try_restore(self) -> bool:
        """Восстановить сессию по токену. Самоисцеление device_type: пробуем
        сохранённый тип (WEB/ANDROID), при FAIL_WRONG_PASSWORD — другой, и
        запоминаем рабочий. Это лечит ситуацию, когда web-токен на диске
        ошибочно помечен как android."""
        if not self.session.token:
            return False
        primary = self.session.device_type
        other = "ANDROID" if primary == "WEB" else "WEB"
        for device_type in (primary, other):
            try:
                if self.client.is_connected:
                    self.client.close()
                self.client.connect(device_type=device_type)
                frame = self.client.login(self.session.token)
            except Exception:
                continue
            kind = "web" if device_type == "WEB" else "android"
            if self.session.token_kind != kind:
                self.session.token_kind = kind
                self.session.save()
            self._ingest_login(frame)
            self._load_my_profile()
            return True
        return False

    SMS_COOLDOWN = 45.0  # сек между запросами SMS — чтобы не словить «too many attempts»

    def request_sms(self, phone: str) -> str:
        wait = self.SMS_COOLDOWN - (time.time() - self._last_sms_ts)
        if wait > 0:
            raise ValueError(
                f"Подождите {int(wait) + 1} c перед повторным запросом SMS "
                "(частые запросы могут временно заблокировать номер)."
            )
        phone = normalize_phone(phone)
        self.session.phone = phone
        if not self.client.is_connected:
            self.client.connect(device_type="ANDROID")
        token = self.client.start_auth_sms(phone)
        self._last_sms_ts = time.time()
        return token

    def confirm_sms(self, verify_token: str, code: str) -> tuple[Optional[str], Optional[str]]:
        if not self.client.is_connected:
            self.client.connect(device_type="ANDROID")
        return self.client.confirm_sms(verify_token, code)

    def confirm_2fa(self, track_id: str, password: str) -> str:
        if not self.client.is_connected:
            self.client.connect(device_type="ANDROID")
        return self.client.confirm_2fa(track_id, password)

    def complete_login(self, token: str, kind: str = "android") -> None:
        self.log("complete_login: start")
        self.session.set_token(token, kind)
        frame = self.client.login(token)
        self.log("complete_login: login ok, ingesting chats")
        self._ingest_login(frame)
        self.log("complete_login: loading profile")
        self._load_my_profile()
        self.log("complete_login: done")

    def login_with_token(self, token: str) -> None:
        """Вход по готовому auth-token (web.max.ru). Веб-токен требует deviceType=WEB."""
        token = token.strip()
        if not token:
            raise ValueError("Пустой токен")
        if self.client.is_connected:
            self.client.close()
        self.client.connect(device_type="WEB")
        frame = self.client.login(token)
        self.session.set_token(token, "web")
        self._ingest_login(frame)
        self._load_my_profile()

    def _ingest_login(self, frame) -> None:
        """Достать список чатов из ответа LOGIN и наполнить кэш (без повторного входа)."""
        try:
            ids = rp.extract_chat_ids_from_login_raw(frame.body)
        except Exception:
            ids = []
        for cid in ids:
            self.store.ensure_chat(cid)
        if ids:
            try:
                info = self.client.chat_info(ids)
                for chat in _extract_chats(info):
                    self.store.upsert_chat(chat)
            except Exception:
                pass

    def logout(self) -> None:
        try:
            self.client.logout()
        except Exception:
            pass
        self.session.clear_token()
        self.client.close()

    def _load_my_profile(self) -> Profile:
        prof = Profile()
        try:
            data = self.client.current_profile()
            prof.id = _to_int(data.get("id"))
            prof.name = _clean_name(data.get("name"))
            prof.phone = str(data.get("phone")) if data.get("phone") is not None else None
        except Exception:
            pass
        if prof.id is not None:
            self.session.my_user_id = prof.id
        if prof.name:
            self.session.my_name = prof.name
        self.session.save()
        return prof

    def my_profile(self) -> Profile:
        return Profile(
            id=self.session.my_user_id,
            name=self.session.my_name,
            phone=self.session.phone,
        )

    # ───────────────────────── chats ─────────────────────────

    def sync_chats(self) -> list[Chat]:
        """Обновить названия/аватары известных чатов через CHAT_INFO.

        Список чатов уже наполнен из ответа LOGIN (_ingest_login), поэтому
        повторный вход (opcode 19) здесь не нужен.
        """
        chats = self.store.list_chats()
        ids = [c.id for c in chats]
        if ids:
            try:
                info = self.client.chat_info(ids)
                for chat in _extract_chats(info):
                    self.store.upsert_chat(chat)
            except Exception:
                pass
        return self.store.list_chats()

    def list_chats(self) -> list[Chat]:
        chats = self.store.list_chats()
        # Диалоги без имени titлуем по импортированным контактам (id чата = id контакта).
        contacts = self._contacts_map()
        for ch in chats:
            if ch.id in contacts and (not ch.title or ch.title.startswith("Чат ")):
                ch.title = contacts[ch.id].display
        return chats

    def open_chat(self, chat_id: int, count: int = 50, force: bool = False) -> list[Message]:
        """Подтянуть историю с сервера, закэшировать, вернуть из кэша.

        Троттлинг: если историю этого чата тянули недавно (<4 c) и в кэше уже
        что-то есть — не ходим в сеть (новые сообщения и так приходят push'ом).
        """
        self.store.ensure_chat(chat_id)
        now = time.time()
        if (not force and now - self._chat_fetch_ts.get(chat_id, 0.0) < 4.0
                and self.store.messages(chat_id, limit=1)):
            self.store.reset_unread(chat_id)
            return self.store.messages(chat_id)
        self._chat_fetch_ts[chat_id] = now
        try:
            raw_msgs, _ = self.client.chat_history(chat_id, from_id=0, count=count)
            batch: list[Message] = []
            last_time = None
            last_preview = ""
            for rawm in raw_msgs:
                msg = Message.from_server(chat_id, rawm, self.my_id)
                if not msg.text and not msg.has_attaches:
                    continue
                batch.append(msg)
                if last_time is None or msg.time_ms > last_time:
                    last_time = msg.time_ms
                    last_preview = msg.preview
            if batch:
                self.store.insert_messages(batch)  # одна транзакция
            if last_time is not None:
                self.store.update_chat_preview(chat_id, last_time, last_preview)
        except Exception:
            pass
        self.store.reset_unread(chat_id)
        return self.store.messages(chat_id)

    def local_history(self, chat_id: int) -> list[Message]:
        return self.store.messages(chat_id)

    # ───────────────────────── send ─────────────────────────

    def send_text(self, chat_id: int, text: str) -> Message:
        local_id = str(uuid.uuid4())
        pending = Message(
            chat_id=chat_id, sender=self.my_id, text=text,
            time_ms=int(time.time() * 1000), outgoing=True,
            status="pending", local_id=local_id,
        )
        self.store.insert_message(pending)
        self.store.update_chat_preview(chat_id, pending.time_ms, text)
        try:
            res = self.client.send_message(chat_id, text)
            server_id = _extract_sent_id(res)
            self.store.update_message_by_local_id(local_id, server_id=server_id, status="sent")
            pending.id = server_id
            pending.status = "sent"
        except Exception as e:  # noqa: BLE001
            self.store.update_message_by_local_id(local_id, status="failed")
            pending.status = "failed"
            pending.error = str(e) or e.__class__.__name__
        return pending

    def send_media(
        self,
        chat_id: int,
        path: str,
        kind: AttachKind,
        *,
        text: str = "",
        duration_ms: Optional[int] = None,
        on_progress: Optional[Callable[[float], None]] = None,
    ) -> Message:
        local_id = str(uuid.uuid4())
        pending = Message(
            chat_id=chat_id, sender=self.my_id, text=text,
            time_ms=int(time.time() * 1000), outgoing=True,
            status="pending", local_id=local_id,
            attaches=[Attach(type=kind.value, local_path=path, duration_ms=duration_ms)],
        )
        self.store.insert_message(pending)
        self.store.update_chat_preview(chat_id, pending.time_ms, pending.preview)
        try:
            attach = upload_and_build_attach(
                self.client, path, kind, duration_ms=duration_ms, on_progress=on_progress
            )
            res = self.client.send_message(chat_id, text, attaches=[attach])
            server_id = _extract_sent_id(res)
            self.store.update_message_by_local_id(local_id, server_id=server_id, status="sent")
            pending.id = server_id
            pending.status = "sent"
        except Exception as e:  # noqa: BLE001
            self.store.update_message_by_local_id(local_id, status="failed")
            pending.status = "failed"
            pending.error = str(e) or e.__class__.__name__
        return pending

    def send_voice(
        self, chat_id: int, path: str, duration_ms: Optional[int] = None,
        on_progress: Optional[Callable[[float], None]] = None,
    ) -> Message:
        return self.send_media(
            chat_id, path, AttachKind.AUDIO, duration_ms=duration_ms, on_progress=on_progress
        )

    # ───────────────────────── download ─────────────────────────

    def resolve_attach_url(self, attach: Attach, chat_id: int, message_id: int) -> Optional[str]:
        """Получить актуальный URL вложения (видео — opcode 83, остальное — 88)."""
        if attach.url:
            return attach.url
        if attach.base_url:
            return attach.base_url
        if attach.file_id is None:
            return None
        try:
            if attach.is_video:
                res = self.client.request_video_play(
                    video_id=attach.file_id, chat_id=chat_id, message_id=message_id,
                    token=attach.token,
                )
            else:
                res = self.client.request_file_download(
                    file_id=attach.file_id, chat_id=chat_id, message_id=message_id
                )
            url = res.get("url")
            return str(url) if url else None
        except Exception:
            return None

    def transcribe(self, attach: Attach, chat_id: int, message_id: int) -> Optional[str]:
        if attach.file_id is None:
            return None
        try:
            res = self.client.transcribe_media(attach.file_id, chat_id, message_id)
        except Exception:
            return None
        text = res.get("text") or res.get("transcription")
        if not text and isinstance(res.get("result"), dict):
            text = res["result"].get("text")
        return str(text) if text else None

    # ───────────────────────── contacts ─────────────────────────

    def list_contacts(self, query: str = "") -> list[Contact]:
        return self.store.list_contacts(query)

    def add_contact_by_phone(self, phone: str, name: str = "") -> Contact:
        phone = normalize_phone(phone)
        data = self.client.contact_by_phone(phone)
        cid = _to_int(data.get("id"))
        if cid is None:
            raise ValueError("Пользователь MAX с таким номером не найден")
        # Имя из адресной книги/пользователя чище, чем из компактного ответа.
        display = _clean_name(name) or _clean_name(data.get("name")) or phone
        contact = Contact(id=cid, name=display, phone=data.get("phone") or phone)
        self.store.upsert_contact(contact)
        self.store.ensure_chat(cid, display)
        self._invalidate_contacts()
        return contact

    def import_contacts_from_file(
        self, file_path: str, on_progress: Optional[Callable[[int, int], None]] = None
    ) -> int:
        """Импорт контактов из .vcf или .csv. Каждый номер проверяется через
        CONTACT_INFO_BY_PHONE; найденные в MAX сохраняются. Возвращает их число."""
        entries = parse_contacts_file(file_path)
        total = len(entries)
        found = 0
        for i, (name, phone) in enumerate(entries, 1):
            phone = normalize_phone(phone)
            if phone:
                try:
                    data = self.client.contact_by_phone(phone)
                    cid = _to_int(data.get("id"))
                    if cid is not None:
                        # Имя из файла (чистое) приоритетнее имени из компактного ответа.
                        display = _clean_name(name) or _clean_name(data.get("name")) or phone
                        self.store.upsert_contact(
                            Contact(id=cid, name=display, phone=data.get("phone") or phone)
                        )
                        # Чтобы диалог в списке чатов показывал имя контакта.
                        self.store.ensure_chat(cid, display)
                        found += 1
                except Exception:
                    pass
            if on_progress:
                on_progress(i, total)
        self._invalidate_contacts()
        return found

    def delete_contact(self, contact_id: int) -> None:
        self.store.delete_contact(contact_id)
        self._invalidate_contacts()

    # ───────────────────────── sessions ─────────────────────────

    @staticmethod
    def _sessions_from_frame(frame) -> list[dict]:
        """Сессии из распакованного кадра. После LZ4-распаковки decoded —
        чистый dict {sessions:[{time, client, info, location, current}]}."""
        d = frame.decoded
        if isinstance(d, dict) and isinstance(d.get("sessions"), list):
            out = []
            for s in d["sessions"]:
                if not isinstance(s, dict):
                    continue
                client = _str(s.get("client")) or "Сессия"
                info = _str(s.get("info"))
                label = f"{client} · {info}" if info and info != "null, null" else client
                out.append({
                    "label": label,
                    "location": _str(s.get("location")) or "",
                    "current": bool(s.get("current")),
                    "time": _to_int(s.get("time")),
                })
            if out:
                return out
        return rp.parse_sessions(frame.body)  # fallback

    def list_sessions(self) -> list[dict]:
        """Список сессий: [{label, location, current, time}]."""
        return self._sessions_from_frame(self.client.sessions_info())

    def terminate_other_sessions(self) -> bool:
        """Завершить чужие сессии: SESSIONS_CLOSE по времени старта не-текущих
        сессий (id у сессии нет, time — единственный идентификатор). Самый
        надёжный способ — смена пароля 2FA, см. UI."""
        sessions = self._sessions_from_frame(self.client.sessions_info())
        times = [s["time"] for s in sessions if s.get("time") and not s.get("current")]
        if not times:
            return False
        try:
            return self.client.sessions_close(times).ok
        except Exception:
            return False

    # ───────────────────────── 2FA ─────────────────────────

    def get_2fa_status(self) -> dict:
        """Состояние 2FA: {enabled:bool, email:str|None, hint:str|None}.
        AUTH_2FA_DETAILS(104) -> {password:{enabled, hint, email}}."""
        frame = self.client.auth_2fa_details()
        return _parse_2fa_details(frame)

    def enable_2fa(self, password: str, hint: Optional[str] = None) -> None:
        """Включить 2FA, когда он выключен — только по токену, без старого пароля.
        CREATE_TRACK(112) -> SET_2FA(111, expectedCapabilities:[1])."""
        if not password:
            raise ValueError("Введите пароль 2FA")
        track_id = self.client.auth_create_track(0)
        result = self.client.auth_set_2fa(track_id, password, hint)
        if not result.ok:
            raise ValueError(result.error_text() or "Не удалось включить 2FA")

    def change_2fa(self, old_password: str, new_password: str, hint: Optional[str] = None) -> None:
        """Сменить пароль 2FA: CREATE_TRACK -> CHECK(old) -> SET(new).
        Бросает ValueError с понятным текстом при ошибке."""
        track_id = self.client.auth_create_track(0)
        check = self.client.auth_check_password(track_id, old_password)
        if not check.ok:
            raise ValueError("Неверный текущий пароль 2FA")
        result = self.client.auth_set_2fa(track_id, new_password, hint)
        if not result.ok:
            raise ValueError(result.error_text() or "Не удалось изменить пароль")

    def start_set_recovery_email(self, current_password: str, email: str) -> str:
        """Шаг 1 привязки/смены recovery-email (нужен включённый 2FA).
        CREATE_TRACK -> CHECK_PASSWORD(текущий) -> VERIFY_EMAIL(109, шлёт код).

        ИНФЕРЕНС: что 113 «авторизует» трек для 109 — APK прямо НЕ подтверждает
        (поток скопирован с change_2fa, где 113 предшествует 111). enable_2fa
        доказывает, что свежий трек работает и без 113. Поэтому если 109 падает
        ПОСЛЕ успешной проверки пароля — пробуем 109 на свежем треке без 113
        (на случай, если 113 не нужен или «съедает» трек). Возвращает trackId
        для шага 2 (CHECK_EMAIL 110)."""
        email = (email or "").strip()
        if "@" not in email or "." not in email:
            raise ValueError("Введите корректный email")
        track_id = self.client.auth_create_track(0)
        check = self.client.auth_check_password(track_id, current_password)
        if not check.ok:
            self.log(f"recovery-email: CHECK_PASSWORD non-OK cmd={check.cmd}")
            raise ValueError(check.error_text() or "Неверный текущий пароль 2FA")
        res = self.client.auth_verify_email(track_id, email)
        if res.ok:
            return track_id
        # 109 отвергнут после успешной проверки пароля — фолбэк: свежий трек без 113.
        self.log(f"recovery-email: VERIFY_EMAIL non-OK cmd={res.cmd}; retry on fresh track")
        track2 = self.client.auth_create_track(0)
        res2 = self.client.auth_verify_email(track2, email)
        if res2.ok:
            return track2
        self.log(f"recovery-email: VERIFY_EMAIL retry non-OK cmd={res2.cmd}")
        raise ValueError(
            res2.error_text() or res.error_text() or "Не удалось отправить код на email"
        )

    def confirm_recovery_email(self, track_id: str, code: str) -> None:
        """Шаг 2: подтвердить код из письма (CHECK_EMAIL 110)."""
        code = (code or "").strip()
        if not code:
            raise ValueError("Введите код из письма")
        res = self.client.auth_check_email(track_id, code)
        if not res.ok:
            raise ValueError(res.error_text() or "Неверный код подтверждения")

    # ───────────────────────── push ─────────────────────────

    def _on_push(self, frame: MaxFrame) -> None:
        msg = self._parse_push(frame)
        if msg is None:
            return
        if msg.id is not None and not self._mark_processed(msg.id):
            return
        self.store.ensure_chat(msg.chat_id)
        self.store.insert_message(msg)
        inc = 0 if msg.outgoing else 1
        self.store.update_chat_preview(msg.chat_id, msg.time_ms, msg.preview, inc_unread=inc)
        if self.on_message:
            self.on_message(msg)
        if self.on_chat_changed:
            self.on_chat_changed(msg.chat_id)

    def _parse_push(self, frame: MaxFrame) -> Optional[Message]:
        # Кадры mark/reactions/delete текст не несут — отфильтруются ниже
        # по отсутствию chatId+контента. Пытаемся извлечь сообщение из любого push.
        chat_id = text = sender = msg_id = time_ms = None
        attaches: list[dict] = []

        d = frame.decoded
        if isinstance(d, dict):
            dm = {str(k): v for k, v in d.items()}
            chat_id = _to_int(dm.get("chatId"))
            m = dm.get("message")
            if isinstance(m, dict):
                mm = {str(k): v for k, v in m.items()}
                text = _str(mm.get("text"))
                sender = _to_int(mm.get("sender"))
                msg_id = _to_int(mm.get("id"))
                time_ms = _to_int(mm.get("time"))
                at = mm.get("attaches") or mm.get("attachments")
                if isinstance(at, list):
                    attaches = [a for a in at if isinstance(a, dict)]
            else:
                text = _str(dm.get("text"))
                sender = _to_int(dm.get("sender"))
                msg_id = _to_int(dm.get("id"))
                time_ms = _to_int(dm.get("time"))

        body = frame.body
        if chat_id is None:
            chat_id = rp.read_int_after_key(body, b"\xa6chatId")
        if text is None:
            text = rp.read_str_after_key(body, b"\xa4text")
        if sender is None:
            sender = rp.read_int_after_key(body, b"\xa6sender")
        if msg_id is None:
            msg_id = rp.read_int_after_key(body, b"\xa2id")
        if time_ms is None:
            time_ms = rp.read_int_after_key(body, b"\xa4time")

        if chat_id is None:
            return None
        has_content = bool(text) or bool(attaches)
        if not has_content:
            return None

        return Message(
            chat_id=chat_id,
            id=msg_id,
            sender=sender,
            text=clean_message_text(text),
            time_ms=time_ms or int(time.time() * 1000),
            outgoing=(self.my_id is not None and sender == self.my_id),
            attaches=[Attach.from_server(a) for a in attaches],
        )

    def _on_state(self, state: ConnectionState) -> None:
        if self.on_state_changed:
            self.on_state_changed(state)

    def _on_auth_invalid(self) -> None:
        """Токен на сервере мёртв (reconnect получил FAIL по LOGIN): чистим
        локальный токен и сигналим UI уйти на экран входа."""
        try:
            self.session.clear_token()
        except Exception:
            pass
        if self.on_auth_invalid:
            self.on_auth_invalid()

    def shutdown(self) -> None:
        try:
            self.client.close()
        finally:
            self.store.close()


# ───────────────────────── module helpers ─────────────────────────

_EMAIL_RE = re.compile(rb"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")


def _parse_2fa_details(frame) -> dict:
    """Разобрать ответ AUTH_2FA_DETAILS(104): {password:{enabled, hint, email}}.
    Если msgpack распаковался — берём из decoded; иначе сниффим по байтам
    (как pretty_2fa_details в test5: \\xa7enabled\\xc3/\\xc2 и строка с '@')."""
    d = frame.decoded
    if isinstance(d, dict):
        p = d.get("password")
        if isinstance(p, dict):
            pm = {str(k): v for k, v in p.items()}
            return {
                "enabled": bool(pm.get("enabled")),
                "email": _str(pm.get("email")),
                "hint": _str(pm.get("hint")),
            }
    raw = frame.body or b""
    enabled = b"\xa7enabled\xc3" in raw  # fixstr 'enabled' + msgpack true
    m = _EMAIL_RE.search(raw)
    email = m.group(0).decode("ascii", "ignore") if m else None
    return {"enabled": enabled, "email": email, "hint": None}


def _extract_chats(info: dict) -> list[Chat]:
    arr = info.get("chats") or info.get("items") or info.get("result")
    if not isinstance(arr, list):
        return []
    out: list[Chat] = []
    for m in arr:
        if not isinstance(m, dict):
            continue
        mm = {str(k): v for k, v in m.items()}
        cid = _to_int(mm.get("id"))
        if cid is None:
            continue
        type_str = str(mm.get("type") or "").lower()
        members = _to_int(mm.get("membersCount"))
        is_group = "group" in type_str or "channel" in type_str or (members is not None and members > 2)
        out.append(
            Chat(
                id=cid,
                title=_str(mm.get("title") or mm.get("name")),
                avatar_url=_str(mm.get("avatar") or mm.get("photo")),
                is_group=is_group,
            )
        )
    return out


def _extract_sent_id(res: dict) -> Optional[int]:
    m = res.get("message")
    if isinstance(m, dict):
        return _to_int(m.get("id"))
    return _to_int(res.get("id"))


def normalize_phone(phone: str) -> str:
    if not phone:
        return ""
    phone = phone.strip()
    keep_plus = phone.startswith("+")
    digits = re.sub(r"\D", "", phone)
    if not digits:
        return ""
    if keep_plus:
        return "+" + digits
    if len(digits) == 11 and digits[0] == "8":
        return "+7" + digits[1:]
    if len(digits) == 11 and digits[0] == "7":
        return "+" + digits
    if len(digits) == 10:
        return "+7" + digits
    return "+" + digits


def parse_contacts_file(path: str) -> list[tuple[str, str]]:
    """Распарсить .vcf (vCard) или .csv в список (имя, телефон)."""
    lower = path.lower()
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as fh:
            content = fh.read()
    except OSError:
        return []

    if lower.endswith(".vcf") or "BEGIN:VCARD" in content.upper():
        return _parse_vcard(content)
    return _parse_csv(content)


def _parse_vcard(content: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    name = ""
    for raw_line in content.splitlines():
        line = raw_line.strip()
        up = line.upper()
        if up.startswith("BEGIN:VCARD"):
            name = ""
        elif up.startswith("FN"):
            parts = line.split(":", 1)
            if len(parts) == 2:
                name = parts[1].strip()
        elif up.startswith("TEL"):
            parts = line.split(":", 1)
            if len(parts) == 2 and parts[1].strip():
                out.append((name, parts[1].strip()))
    return out


def _parse_csv(content: str) -> list[tuple[str, str]]:
    import csv
    import io

    out: list[tuple[str, str]] = []
    reader = csv.reader(io.StringIO(content))
    rows = list(reader)
    if not rows:
        return out
    header = [h.strip().lower() for h in rows[0]]
    name_idx = _col(header, ("name", "имя", "fullname", "first name", "display name"))
    phone_idx = _col(header, ("phone", "телефон", "tel", "mobile", "phone 1 - value", "number"))
    start = 1 if (name_idx is not None or phone_idx is not None) else 0
    for row in rows[start:]:
        if not row:
            continue
        name = row[name_idx].strip() if (name_idx is not None and name_idx < len(row)) else ""
        if phone_idx is not None and phone_idx < len(row):
            phone = row[phone_idx].strip()
        else:
            phone = next((c for c in row if re.search(r"\d{5,}", c)), "")
            if not name:
                name = next((c for c in row if c and c != phone), "")
        if phone:
            out.append((name, phone))
    return out


def _col(header: list[str], candidates: tuple[str, ...]) -> Optional[int]:
    for i, h in enumerate(header):
        if any(c in h for c in candidates):
            return i
    return None


def _to_int(v) -> Optional[int]:
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return int(v)
    try:
        return int(str(v))
    except (TypeError, ValueError):
        return None


def _str(v) -> Optional[str]:
    if v is None:
        return None
    s = str(v)
    return s if s else None


def _clean_name(v) -> Optional[str]:
    """Очистить имя от управляющих/битых символов (артефакты компактного формата).
    Кириллица и обычный текст сохраняются."""
    if not v:
        return None
    s = "".join(ch for ch in str(v) if ch == " " or ch.isprintable())
    s = re.sub(r"\s+", " ", s).strip()
    return s or None
