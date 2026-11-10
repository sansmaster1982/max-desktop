"""MaxService — высокоуровневая оркестрация: client + store + session + settings.

GUI-агностичен. Методы блокирующие (вызывать из рабочего потока). Push с сервера
парсится в Message, кладётся в store и проксируется в колбэки on_message /
on_chat_changed, которые UI оборачивает в Qt-сигналы.
"""
from __future__ import annotations

import ast
import re
import copy
import math
import threading
import time
import uuid
from collections import deque, OrderedDict
from typing import Callable, Optional
from urllib.parse import urlsplit

from ..protocol.client import (ConnectionState, MaxClient, MaxFrame, MaxLoginFailed,
                               MaxNotConnected, MaxTimeout, MaxWriteOutcomeUnknown)
from ..protocol.settings_contracts import privacy_values, user_setting_values, chat_notification_values
from ..protocol.uploader import AttachKind, upload_and_build_attach
from .config import AppPaths, Session, Settings, _load_or_create_device_profile
from .models import Attach, Chat, Contact, Message, Profile, parse_reaction_info, parse_message_link, server_attaches
from .store import Store
from .folder_block_service import FolderBlockServiceMixin
from .incoming_calls import IncomingCallNotice, parse_incoming_call
from .message_delivery import MessageDelivery, MessageChange
from .search_service import SearchServiceMixin
from .conversation_info_service import ConversationInfoServiceMixin
from .conversation_actions_service import ConversationActionsServiceMixin
from .conversation_lifecycle_service import ConversationLifecycleServiceMixin
from .conversation_report_service import ConversationReportServiceMixin
from .conversation_file_service import ConversationFileServiceMixin
from .timeline_file_service import TimelineFileServiceMixin
from .message_app_service import MessageAppServiceMixin
from .contact_create import ContactCreateServiceMixin
from .conversation_create import ConversationCreateServiceMixin
from .invite_link import InviteLinkServiceMixin
from .conversation_read_service import ConversationReadServiceMixin
from .conversation_pin_service import ConversationPinServiceMixin
from .conversation_preview_service import ConversationPreviewServiceMixin
from .contact_profile_service import ContactProfileServiceMixin


class _ChatMetadataHydration:
    """Bounded daemon reads, outside the transport reader and cache locks."""
    MAX_PENDING = 64
    MAX_ATTEMPTS = 256
    MAX_WORKERS = 2
    MAX_FAILED_READS = 3

    def __init__(self, service):
        self.service = service
        self.lock = threading.Lock()
        self.queue = deque()
        self.pending = set()
        self.attempts = OrderedDict()
        self.failures = {}
        self.workers = {}
        self.context = None
        self.stopped = False
        self.idle = threading.Event()
        self.idle.set()

    def _start_locked(self):
        if (self.stopped or not self.queue or len(self.workers) >= self.MAX_WORKERS
                or self.context in self.workers.values()):
            return
        worker = threading.Thread(target=self._run, args=(self.context,),
                                  name='max-chat-metadata', daemon=True)
        self.workers[worker] = self.context
        worker.start()

    def schedule(self, binding, chat_ids, *, tracked_only=False):
        self.service._require_history_context(binding)
        with self.lock:
            self.service._require_history_context(binding)
            if self.stopped:
                return
            if self.context is not binding[2]:
                self.context = binding[2]
                self.queue.clear()
                self.pending.clear()
                self.attempts.clear()
                self.failures.clear()
        for identity in chat_ids:
            if not _is_read_int(identity):
                continue
            with self.lock:
                if tracked_only and identity not in self.attempts:
                    continue
            need = self.service._history_cache_write(binding, lambda:
                self.service._chat_identity_need(identity, binding[0]))
            if need is None:
                continue
            with self.lock:
                key = (binding[2], identity)
                if (self.stopped or self.context is not binding[2] or key in self.pending
                        or self.attempts.get(identity) == need or len(self.pending) >= self.MAX_PENDING):
                    continue
                self.attempts[identity] = need
                self.attempts.move_to_end(identity)
                while len(self.attempts) > self.MAX_ATTEMPTS:
                    self.attempts.popitem(last=False)
                self.pending.add(key)
                self.queue.append((binding, identity))
                self.idle.clear()
                self._start_locked()

    def _run(self, context):
        worker = threading.current_thread()
        try:
            while True:
                with self.lock:
                    if self.stopped or self.context is not context or not self.queue:
                        return
                    binding, identity = self.queue.popleft()
                failed = False
                try:
                    self.service._hydrate_chat_identity(binding, identity)
                except Exception:
                    # A metadata read failure is not an authentication verdict.
                    failed = True
                finally:
                    try:
                        need = self.service._history_cache_write(binding, lambda:
                            self.service._chat_identity_need(identity, binding[0]))
                    except Exception:
                        need = None
                    with self.lock:
                        self.pending.discard((context, identity))
                        if self.context is context and need is not None:
                            self.attempts[identity] = need
                            if failed:
                                # A failed read (timeout, dropped socket) is not
                                # an answer. Let a later push/open of this chat
                                # ask again, at most MAX_FAILED_READS times per
                                # session; never from a timer or a loop here.
                                if len(self.failures) > self.MAX_ATTEMPTS:
                                    self.failures.clear()
                                count = self.failures[identity] = self.failures.get(identity, 0) + 1
                                if count < self.MAX_FAILED_READS:
                                    self.attempts.pop(identity, None)
                        if not self.pending:
                            self.idle.set()
        finally:
            with self.lock:
                self.workers.pop(worker, None)
                self._start_locked()
                if not self.pending:
                    self.idle.set()

    def close(self):
        with self.lock:
            self.stopped = True
            self.queue.clear()
            self.pending.clear()
            self.attempts.clear()
            self.failures.clear()
            self.idle.set()

    def invalidate(self):
        with self.lock:
            self.context = None
            self.queue.clear()
            self.pending.clear()
            self.attempts.clear()
            self.failures.clear()
            self.idle.set()


class MaxService(FolderBlockServiceMixin, SearchServiceMixin, ConversationInfoServiceMixin,
                 ConversationActionsServiceMixin, ConversationFileServiceMixin, TimelineFileServiceMixin,
                 MessageAppServiceMixin, ContactCreateServiceMixin, ConversationCreateServiceMixin,
                 InviteLinkServiceMixin, ConversationReadServiceMixin, ConversationPinServiceMixin,
                 ContactProfileServiceMixin, ConversationLifecycleServiceMixin, ConversationPreviewServiceMixin,
                 ConversationReportServiceMixin):
    def __init__(self, paths: AppPaths, settings: Settings, session: Session) -> None:
        self.paths = paths
        self.settings = settings
        self.session = session
        self.store = Store(paths.db_file)

        # Лог протокола для диагностики: %APPDATA%\MAX Desktop\debug.log,
        # перезаписывается при старте сессии. Непустой лог прошлой сессии
        # сначала заменяет debug.prev.log.
        self._log_path = paths.dir / "debug.log"
        try:
            if self._log_path.stat().st_size:
                self._log_path.replace(paths.dir / "debug.prev.log")
        except OSError:
            pass
        try:
            self._log_path.write_text("", encoding="utf-8")
        except OSError:
            pass

        self.client = MaxClient(
            app_version=settings.app_version,
            locale=settings.locale,
            device_id=session.device_id,
            device_profile=_load_or_create_device_profile(
                paths.device_profile_file, session.device_id
            ),
            on_push=self._on_push,
            on_state=self._on_state,
            on_debug=self._log_debug,
            on_auth_invalid=self._on_auth_invalid,
            auto_reconnect=True,
        )
        source_client = self.client
        source_client.on_reauthenticated = lambda frame, context: self._on_reauthenticated(
            source_client, frame, context)
        # Колбэки в UI (устанавливаются снаружи).
        self.on_message: Optional[Callable[[MessageDelivery], None]] = None
        self.on_message_changed: Optional[Callable[[MessageChange], None]] = None
        self.on_chat_changed: Optional[Callable[[int], None]] = None
        self.on_state_changed: Optional[Callable[[ConnectionState], None]] = None
        # Сервер отверг сохранённый токен (протух/отозван) — UI должен уйти на
        # экран входа. Колбэк зовётся из reconnect-потока, UI оборачивает в сигнал.
        self.on_auth_invalid: Optional[Callable[[], None]] = None
        self.on_config_changed: Optional[Callable[[], None]] = None
        self.on_incoming_call: Optional[Callable[[IncomingCallNotice], None]] = None
        self._incoming_call_lock = threading.RLock()
        self._incoming_call_context = None
        self._incoming_call_seen = set()
        self._incoming_call_order = deque()

        # Дедуп push: ограниченный набор (set + deque на вытеснение).
        self._processed_ids: set[int] = set()
        self._processed_order: deque[int] = deque()
        self._contacts_cache: Optional[dict[int, Contact]] = None
        self._server_chats: dict[int, dict] = {}
        self._server_contacts: dict[int, dict] = {}
        self._server_metadata_owner: Optional[int] = None
        self._metadata_lock = threading.RLock()
        self._chat_metadata_hydration = _ChatMetadataHydration(self)
        self._code_refresh_lock = threading.Lock()
        self._chat_fetch_ts: dict[int, float] = {}  # троттлинг history-фетча
        self._chat_fetch_anchor: dict[int, int] = {}
        self._last_sms_ts = 0.0  # кулдаун повторных SMS (анти «too many attempts»)
        self.last_restore_error: Optional[Exception] = None
        # Кулдаун 2FA-операций: серия set_2fa/create_track в коротком окне
        # триггерит серверный лимит error.user.restricted.set_2fa (так номер и
        # блокировался). Офиц. клиент свой троттл НЕ имеет — добавляем сами.
        self._twofa_cooldown_until = 0.0
        # run_async раскидывает задачи по QThreadPool (>1 поток) — check-then-set
        # кулдауна 2FA должен быть атомарным, иначе два клика проскочат гонкой.
        self._twofa_lock = threading.Lock()

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
        """Try the saved session once, preserving it when the server rejects it."""
        self.last_restore_error = None
        if not self.session.token:
            return False
        try:
            self._login_token(self.session.token, self.session.token_kind,
                              self.session.device_id, reconnect=True)
        except Exception as error:
            self.last_restore_error = error
            return False
        return True

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

    def _finish_login(self, frame, prev_uid: Optional[int]) -> None:
        """Use the confirmed LOGIN profile, without an extra profile request."""
        data = frame.decoded if isinstance(frame.decoded, dict) else {}
        profile_data = data.get("profile")
        if (frame.cmd != 1 or frame.opcode != 19
                or not isinstance(profile_data, dict) or not profile_data):
            raise MaxLoginFailed("LOGIN не подтвердил профиль аккаунта")
        prof = _profile_from_data(profile_data, own=True)
        if prof.id is None:
            raise MaxLoginFailed("LOGIN не вернул идентификатор профиля")
        cache_changed = self.store.bind_owner(prof.id)
        if cache_changed or (prev_uid is not None and prof.id != prev_uid):
            self.log("account changed: clearing local cache")
            self._invalidate_contacts()
            self._processed_ids.clear()
            self._processed_order.clear()
            self._chat_fetch_ts.clear()
            self._chat_fetch_anchor.clear()
            with self._metadata_lock:
                self._server_chats.clear()
                self._server_contacts.clear()
                self._server_metadata_owner = None
        try:
            self._ingest_login(frame, my_id=prof.id)
        except Exception as error:
            # A local cache issue is separate from the validated login result.
            self.log("LOGIN cache update failed: " + type(error).__name__)
            self.log("LOGIN cache failure stack: " + _login_cache_failure_stack(error))
        self.store.save_profile(prof)
        self.session.my_user_id = prof.id
        self.session.my_name = prof.name
        self.session.phone = prof.phone

    def complete_login(self, token: str, kind: str = "android") -> None:
        self._login_token(token, kind, self.session.device_id, reconnect=False)

    def login_with_token(self, token: str, device_id: Optional[str] = None, *,
                          expected_profile_id: Optional[int] = None) -> None:
        """Sign in with a WEB token and its original exported device UUID."""
        if expected_profile_id is not None and type(expected_profile_id) is not int:
            raise ValueError("Ожидаемый профиль должен иметь целочисленный идентификатор")
        candidate_device = (str(uuid.UUID(device_id)) if device_id is not None
                            else self.session.device_id)
        self._login_token(token, "web", candidate_device, reconnect=True,
                          expected_profile_id=expected_profile_id)

    def _login_token(self, token: str, kind: str, device_id: str, *, reconnect: bool,
                     expected_profile_id: Optional[int] = None) -> None:
        token = token.strip()
        if not token:
            raise ValueError("Пустой токен")
        if kind not in {"web", "android"}:
            raise ValueError("Неизвестный тип сохранённой сессии")
        prev_uid = self.session.my_user_id
        original_device = self.client.device_id
        original_version = self.client.app_version
        original_reconnect = self.client.auto_reconnect
        # A deliberate login must not race a background login using an old token.
        self.client.auto_reconnect = False
        try:
            if reconnect and self.client.is_connected:
                self.client.close()
            self.client.device_id = device_id
            self.client.app_version = (self.settings.web_app_version if kind == "web"
                                       else self.settings.app_version)
            if reconnect or not self.client.is_connected:
                self.client.connect(device_type="WEB" if kind == "web" else "ANDROID")
            frame = self.client.login(token)
            if expected_profile_id is not None:
                actual_profile = frame.decoded.get("profile") if isinstance(frame.decoded, dict) else None
                if _profile_from_data(actual_profile).id != expected_profile_id:
                    raise MaxLoginFailed("MAX вернул другой профиль; импорт сессии остановлен")
            self._finish_login(frame, prev_uid)
        except Exception:
            self.client.close()
            self.client.device_id = original_device
            self.client.app_version = original_version
            raise
        finally:
            self.client.auto_reconnect = original_reconnect
        # Failed LOGINs never write either the candidate token or its device ID.
        if device_id != self.session.device_id:
            self.session.set_device_id(device_id)
        self.session.set_token(token, kind)
        self.last_restore_error = None

    def _ingest_login(self, frame, my_id: Optional[int] = None) -> None:
        """Cache decoded LOGIN chats/contacts, without raw ID scans or re-login."""
        if not isinstance(frame.decoded, dict):
            return
        data = frame.decoded
        owner = my_id if my_id is not None else _profile_from_data(data.get("profile")).id
        if owner is None:
            owner = self.my_id
        with self._metadata_lock:
            if self._server_metadata_owner != owner:
                self._server_chats.clear()
                self._server_contacts.clear()
            self._server_metadata_owner = owner
        if isinstance(data.get("config"), dict):
            self._remember_config(data["config"], owner_id=owner)
        # LOGIN of a newly authenticated session is the server's current state,
        # newer than any receipt applied on the previous connection.
        self._remember_chat_info({"chats": data.get("chats")}, my_id=owner, fresh_session=True)
        self._remember_contact_info({"contacts": data.get("contacts")})
        profile_record = data.get('profile', {}).get('contact') if isinstance(data.get('profile'), dict) else None
        if (isinstance(profile_record, dict) and type(profile_record.get('id')) is int
                and profile_record['id'] == owner and owner in self._contacts_map()):
            # Repair an already-existing local self contact from the confirmed
            # LOGIN profile, without lookup/ADD, a new contact or a fake chat.
            self._remember_contact_info({'contacts': [profile_record]})
            own_profile = _profile_from_data(profile_record, own=True)
            if own_profile.name is not None:
                self.store.upsert_contact(Contact(id=owner, name=own_profile.name,
                                                   server_fields=frozenset()))
                self._invalidate_contacts()

    def _remember_chat_info(self, info: dict, *, my_id: Optional[int] = None, message_clock_ids=(),
                            fresh_session: bool = False) -> None:
        """Retain structured server metadata; local titles are not identity proof."""
        entries = copy.deepcopy(_chat_entries(info))
        clock_guard = set(message_clock_ids)
        with self._metadata_lock:
            for entry in entries:
                chat_id = _strict_id(entry.get("id"))
                if chat_id is not None:
                    merged = copy.deepcopy(entry)
                    boundary = self.store.history_boundary(chat_id)
                    incoming_last = merged.get('lastMessage')
                    deleted_last = (isinstance(incoming_last, dict)
                                    and self.store.message_was_deleted(chat_id, _strict_id(incoming_last.get('id'))))
                    if deleted_last:
                        clock_guard.add(chat_id)
                    if deleted_last or (boundary and self.store.history_was_cleared(
                            chat_id, _message_time(incoming_last))):
                        merged.pop('lastMessage', None)
                        entry.pop('lastMessage', None)
                    previous = copy.deepcopy(self._server_chats.get(chat_id, {}))
                    previous_last = previous.get('lastMessage')
                    if isinstance(previous_last, dict) and self.store.message_was_deleted(
                            chat_id, _strict_id(previous_last.get('id'))):
                        previous.pop('lastMessage', None)
                    old_time = _message_time(previous.get("lastMessage"))
                    new_time = _message_time(merged.get("lastMessage"))
                    if old_time is not None and (deleted_last or new_time is not None and old_time > new_time):
                        merged["lastMessage"] = copy.deepcopy(previous["lastMessage"])
                    known_mark = _own_read_mark(previous, my_id)
                    supplied_mark = _own_read_mark(merged, my_id)
                    if (not fresh_session and known_mark is not None and supplied_mark is not None
                            and supplied_mark < known_mark and merged.get('markedAsUnread') is not True):
                        # The snapshot was built before our newer confirmed read
                        # receipt (ACK50/push130). Only an explicit unread mark
                        # may move the own marker back; keep the newer marker and
                        # the counter that belongs to it until fresher evidence.
                        participants = dict(merged['participants'])
                        participants.pop(my_id, None)
                        participants[str(my_id)] = known_mark
                        merged['participants'] = participants
                        merged['markedAsUnread'] = previous.get('markedAsUnread') is True
                        cached_row = self.store.get_chat(chat_id)
                        if cached_row is not None:
                            merged['newMessages'] = entry['newMessages'] = cached_row.unread
                        else:
                            merged.pop('newMessages', None)
                            entry.pop('newMessages', None)
                    self._server_chats[chat_id] = merged
                    if my_id is not None and self.store.cache_owner == my_id:
                        self._reconcile_conversation_departure(merged, my_id)
        saved_entries = [entry for entry in entries if _strict_id(entry.get('id')) == 0]
        projected_entries = copy.deepcopy(entries)
        for entry in projected_entries:
            chat_id = _strict_id(entry.get('id'))
            if chat_id in clock_guard and 'lastMessage' not in entry:
                current = self.store.get_chat(chat_id)
                if current is not None and current.last_time_ms is not None:
                    # A deleted/omitted lastMessage keeps the repaired row's own
                    # message time; an event clock is not a visible message.
                    entry.pop('lastEventTime', None)
            boundary = self.store.history_boundary(chat_id) if chat_id is not None else None
            if boundary and boundary['deleted']:
                last = entry.get('lastMessage')
                timestamp = _message_time(last)
                new_message = (isinstance(last, dict) and _strict_id(last.get('id')) is not None
                    and timestamp is not None and timestamp > boundary['cutoff']
                    and last.get('deleted') is not True and last.get('removed') is not True
                    and last.get('status') != 'REMOVED')
                if not new_message:
                    # An event/config/participant update is not evidence that
                    # a deleted conversation gained a new visible message.
                    entry.pop('lastMessage', None)
                    entry.pop('lastEventTime', None)
        for chat in _extract_chats({'chats': projected_entries}, my_id=my_id):
            if chat.id == 0 and len(saved_entries) == 1:
                preview = self._cached_saved_preview(chat, saved_entries[0], owner=my_id)
                if preview is not None:
                    chat.last_preview = preview
            previous = self.store.get_chat(chat.id)
            if (previous is not None and previous.last_time_ms is not None
                    and chat.last_time_ms is not None and chat.last_time_ms < previous.last_time_ms
                    and chat.server_fields is not None):
                chat.server_fields = chat.server_fields - {"last_time_ms", "last_preview"}
            self.store.upsert_chat(chat)
            if chat.peer_user_id is not None:
                self.store.set_chat_peer(chat.id, chat.peer_user_id)
            if (chat.last_time_ms is not None and chat.last_preview is not None
                    and (previous is None or previous.last_time_ms is None
                         or chat.last_time_ms >= previous.last_time_ms)):
                self.store.update_chat_preview(chat.id, chat.last_time_ms, chat.last_preview)
        # Some CHAT_INFO replies also include their participants' contacts.
        if isinstance(info, dict) and isinstance(info.get("contacts"), list):
            self._remember_contact_info(info)

    def _remember_contact_info(self, info: dict) -> None:
        contacts = _contact_entries(info)
        with self._metadata_lock:
            for entry in contacts:
                record = entry.get("contact") if isinstance(entry.get("contact"), dict) else entry
                profile = _profile_from_data(record)
                if profile.id is not None:
                    self._server_contacts[profile.id] = copy.deepcopy(record)
                    supplied = frozenset({"is_verified"} if "options" in record or "flags" in record else ())
                    if "baseUrl" in record:
                        supplied |= {"avatar_url"}
                    self.store.upsert_contact(Contact(id=profile.id, name=profile.name,
                                                      phone=profile.phone,
                                                      avatar_url=_server_avatar_url(record),
                                                      is_verified=_has_contact_flag(record, "OFFICIAL"),
                                                      server_fields=supplied))
        if contacts:
            self._invalidate_contacts()

    def logout(self) -> None:
        self._clear_incoming_calls()
        worker = getattr(self, '_chat_metadata_hydration', None)
        if worker is not None:
            worker.invalidate()
        try:
            self.client.logout()
        except Exception:
            pass
        self.session.clear_token()
        # Чистим локальный кэш: иначе при входе другим аккаунтом покажутся
        # старые чаты/контакты. Так «выйти → войти новым» даёт чистый старт.
        self.store.clear_cache()
        self._invalidate_contacts()
        self._chat_fetch_ts.clear()
        self._chat_fetch_anchor.clear()
        with self._metadata_lock:
            self._server_chats.clear()
            self._server_contacts.clear()
            self._server_metadata_owner = None
        self.client.close()

    def _load_my_profile(self) -> Profile:
        prof = _profile_from_data(self.client.current_profile(), own=True)
        if prof.id is None:
            raise MaxLoginFailed("PROFILE не вернул идентификатор аккаунта")
        if self.my_id is not None and prof.id != self.my_id:
            raise MaxLoginFailed("PROFILE вернул другой аккаунт; локальные данные не изменены")
        self.store.save_profile(prof)
        self.session.my_user_id = prof.id
        self.session.my_name = prof.name
        self.session.phone = prof.phone
        self.session.save()
        return prof

    def my_profile(self) -> Profile:
        cached = self.store.load_profile()
        if cached is not None and cached.id == self.my_id:
            return cached
        return Profile(
            id=self.session.my_user_id,
            name=self.session.my_name,
            phone=self.session.phone,
        )

    def update_my_name(self, name: str) -> str:
        """Сменить своё имя профиля (видно другим): PROFILE(16) на запись.
        Имя из одного поля кладём в firstName, остаток (после первого пробела) —
        в lastName. Обновляет локальную сессию. Возвращает применённое имя."""
        name = (name or "").strip()
        if not name:
            raise ValueError("Имя не может быть пустым")
        parts = name.split(" ", 1)
        first = parts[0]
        # Имя из одного слова -> last="" (очищает прежнюю фамилию на сервере),
        # иначе остаток. "" != None: пустая строка явно очищает (r2e.java).
        last = parts[1].strip() if len(parts) > 1 else ""
        return self.update_my_profile(first_name=first, last_name=last).name or ""

    def update_my_profile(self, *, first_name: Optional[str] = None, last_name: Optional[str] = None,
                          description: Optional[str] = None, link: Optional[str] = None,
                          photo_token: Optional[str] = None, photo_id: Optional[int] = None,
                          avatar_type: Optional[str] = None) -> Profile:
        """Apply the current PROFILE16 fields only after an ACK for this account."""
        if self.my_id is None:
            raise MaxLoginFailed("Сначала войдите в свой профиль MAX")
        result = self.client.update_profile(first_name=first_name, last_name=last_name,
                                               description=description, link=link,
                                               photo_token=photo_token, photo_id=photo_id,
                                               avatar_type=avatar_type)
        profile = _profile_from_data(result.get("profile") if isinstance(result, dict) else None, own=True)
        if profile.id != self.my_id:
            raise MaxWriteOutcomeUnknown("MAX не подтвердил изменения нужного профиля. Повтор не выполнен.")
        try:
            self.store.save_profile(profile)
            self.session.my_name = profile.name
            self.session.phone = profile.phone
            self.session.save()
        except Exception:
            raise MaxWriteOutcomeUnknown("Профиль подтверждён сервером, но не сохранён локально. Проверьте состояние перед повтором.") from None
        return profile

    def set_my_avatar(self, path: str, on_progress: Optional[Callable[[float], None]] = None) -> Profile:
        """Upload an explicitly chosen profile photo using the server's profile URL."""
        if self.my_id is None:
            raise MaxLoginFailed("Сначала войдите в свой профиль MAX")
        attach = upload_and_build_attach(self.client, path, AttachKind.PHOTO,
                                          on_progress=on_progress, profile=True)
        token = attach.get("photoToken") if isinstance(attach, dict) else None
        if not isinstance(token, str) or not token:
            raise ValueError("MAX не вернул токен загруженного фото профиля")
        return self.update_my_profile(photo_token=token, avatar_type="USER_AVATAR")

    # ───────────────────────── chats ─────────────────────────

    def sync_chats(self) -> list[Chat]:
        """Обновить названия/аватары известных чатов через CHAT_INFO.

        Список чатов уже наполнен из ответа LOGIN (_ingest_login), поэтому
        повторный вход (opcode 19) здесь не нужен.
        """
        binding = self._history_read_context()
        chats = self.store.list_chats()
        ids = [c.id for c in chats]
        if ids:
            info = binding[1].chat_info(ids, expected_session=binding[2])
            self._require_history_context(binding)
            if not _has_chat_field(info):
                raise ValueError("MAX не вернул метаданные чатов")
            matches = [chat for chat in _chat_entries(info) if type(chat.get('id')) is int and chat['id'] in ids]
            self._history_cache_write(binding, lambda: self._remember_chat_info({'chats': matches}, my_id=binding[0]))
            self._resolve_chat_contacts(ids, _read_context=binding)
        self._require_history_context(binding)
        return self.list_chats()

    def _chat_identity_need(self, chat_id, owner):
        from .conversation_info_service import _peer, ConversationUnavailable
        chat = self.store.get_chat(chat_id)
        raw = self._server_chats.get(chat_id, {})
        if chat is None or raw.get('type') not in ('DIALOG', 'CHAT', 'CHANNEL'):
            return ('chat', None)
        if raw['type'] != 'DIALOG':
            return None if _clean_name(raw.get('title')) or _clean_name(raw.get('name')) else ('chat', None)
        try:
            peer = _peer(chat, raw, owner)
        except ConversationUnavailable:
            return ('chat', None)
        if peer is None:
            return ('chat', None)
        if peer == owner:
            return None
        record = self._server_contacts.get(peer, {})
        cached = self._contacts_map().get(peer)
        if (_person_name(record) or _profile_from_data(record).phone
                or cached is not None and (_clean_name(cached.name) or cached.phone)):
            return None
        return ('contact', peer)

    def _schedule_chat_identity(self, binding, chat_ids, *, tracked_only=False):
        worker = getattr(self, '_chat_metadata_hydration', None)
        if worker is not None:
            try:
                worker.schedule(binding, chat_ids, tracked_only=tracked_only)
            except (ValueError, AttributeError):
                pass

    def _hydrate_chat_identity(self, binding, chat_id):
        need = self._chat_metadata_write(binding, lambda: self._chat_identity_need(chat_id, binding[0]))
        client = self._require_history_context(binding)
        if need == ('chat', None):
            info = client.chat_info([chat_id], expected_session=binding[2])
            self._require_history_context(binding)
            matches = [raw for raw in _chat_entries(info)
                       if type(raw.get('id')) is int and raw['id'] == chat_id]
            if (len(matches) != 1 or not self._valid_chat_identity_payload(matches[0])
                    or matches[0].get('type') not in ('DIALOG', 'CHAT', 'CHANNEL')):
                return
            if matches[0]['type'] == 'DIALOG':
                from .conversation_info_service import _peer
                _peer(None, matches[0], binding[0])
            def remember_chat():
                if self._chat_identity_need(chat_id, binding[0]) != ('chat', None):
                    return
                cached = self.store.get_chat(chat_id)
                if cached is None:
                    return
                selected = copy.deepcopy(matches[0])
                # This request resolves identity, not message/read counters.
                selected['newMessages'] = cached.unread
                self._remember_chat_info({'chats': [selected]}, my_id=binding[0])
            self._chat_metadata_write(binding, remember_chat)
            need = self._chat_metadata_write(binding, lambda: self._chat_identity_need(chat_id, binding[0]))
        if need is not None and need[0] == 'contact':
            peer = need[1]
            info = client.contact_info([peer], expected_session=binding[2])
            self._require_history_context(binding)
            matches = [raw.get('contact', raw) for raw in _contact_entries(info)
                       if type(raw.get('contact', raw).get('id')) is int
                       and raw.get('contact', raw)['id'] == peer]
            if len(matches) != 1:
                return
            def remember_contact():
                if self._chat_identity_need(chat_id, binding[0]) == ('contact', peer):
                    self._remember_contact_info({'contacts': matches})
            self._chat_metadata_write(binding, remember_contact)
        self._require_history_context(binding)
        if self.on_chat_changed:
            self.on_chat_changed(chat_id)

    def _chat_metadata_write(self, binding, apply):
        worker = getattr(self, '_chat_metadata_hydration', None)
        if worker is None:
            raise ValueError('Обновление метаданных остановлено')
        with worker.lock:
            if worker.stopped or worker.context is not binding[2]:
                raise ValueError('Сессия обновления метаданных изменилась')
            return self._history_cache_write(binding, apply)

    def _resolve_chat_contacts(self, chat_ids: list[int], *, _read_context=None) -> None:
        """Resolve missing dialog recipients with one bounded CONTACT_INFO read."""
        selected = set(chat_ids)
        peers = {chat.peer_user_id for chat in self.store.list_chats()
                 if chat.id in selected and not chat.is_group and chat.peer_user_id is not None
                 and chat.peer_user_id != self.my_id}
        cached = self._contacts_map()
        with self._metadata_lock:
            records = copy.deepcopy(self._server_contacts)
        repair = [records[identity] for identity in peers
                  if identity in records and (_person_name(records[identity]) or _profile_from_data(records[identity]).phone)
                  and (identity not in cached or not (cached[identity].name or cached[identity].phone))]
        if repair:
            apply = lambda: self._remember_contact_info({"contacts": repair})
            if _read_context is None:
                apply()
            else:
                self._history_cache_write(_read_context, apply)
            cached = self._contacts_map()
        missing = sorted(identity for identity in peers
                         if not (identity in records and (_person_name(records[identity])
                             or _profile_from_data(records[identity]).phone)
                             or identity in cached and (cached[identity].name or cached[identity].phone)))
        if not missing:
            return
        client = self.client if _read_context is None else self._require_history_context(_read_context)
        kwargs = {'expected_session': _read_context[2]} if _read_context is not None else {}
        info = client.contact_info(missing, **kwargs)
        if not _has_list_field(info, ("contacts",)) and not isinstance(
                info.get("contact") if isinstance(info, dict) else None, dict):
            raise ValueError("MAX не вернул данные собеседников")
        if _read_context is None:
            self._remember_contact_info(info)
        else:
            self._history_cache_write(_read_context, lambda: self._remember_contact_info(info))
        if self.on_chat_changed:
            for chat in self.store.list_chats():
                if chat.id in selected and chat.peer_user_id in missing:
                    self.on_chat_changed(chat.id)

    def load_chat_page(self, marker: int = 0, count: int = 40, *, section: str = "all") -> dict:
        """Fetch one documented CHATS_LIST page, preserving its server cursor."""
        if section not in {"all", "new", "channels"}:
            raise ValueError("Неизвестный раздел чатов")
        result = self.client.list_chats(marker=marker, count=count,
                                        chat_types=["CHANNEL"] if section == "channels" else None)
        if (not isinstance(result, dict) or not isinstance(result.get("chats"), list)
                or type(result.get("marker")) is not int):
            raise ValueError("MAX не вернул список чатов и курсор страницы")
        self._remember_chat_info(result, my_id=self.my_id)
        return {"chats": self.list_chats(section), "marker": result["marker"]}

    def verification_code_messages(self, *, context=None) -> dict:
        """Read visible messages of the verified official code bot, without auth.

        This makes one CHAT_INFO refresh and, only when needed, one CONTACT_INFO
        lookup for service-chat peers. It never requests an SMS, logs in, sends a
        message or guesses history pagination. RPC failures remain visible to UI.
        A supplied history context binds queued reads to their original session.
        """
        with self._code_refresh_lock:
            binding = self._history_read_context() if context is None else context
            client = self._require_history_context(binding)
            owner = binding[0]
            if not client.is_connected:
                raise ConnectionError("Нет активного подключения к MAX")
            with self._metadata_lock:
                known_ids = set(self._server_chats)
            known_ids.update(chat.id for chat in self.store.list_chats())
            if not known_ids:
                return {"status": "chat_not_available", "messages": []}

            info = client.chat_info(sorted(known_ids), expected_session=binding[2])
            self._require_history_context(binding)
            if not _has_chat_field(info):
                raise ValueError("MAX не вернул метаданные чатов")
            self._history_cache_write(binding, lambda: self._remember_chat_info(info, my_id=owner))
            fresh_chats = {chat.id: chat for chat in _extract_chats(info, my_id=owner)
                           if chat.id in known_ids}
            # The code viewer reports this response's actual lastMessage. A
            # general cache may retain a newer anchor while merging old pages.
            metadata = {_strict_id(raw.get("id")): copy.deepcopy(raw) for raw in _chat_entries(info)
                        if _strict_id(raw.get("id")) in fresh_chats}
            with self._metadata_lock:
                contacts = copy.deepcopy(self._server_contacts)
            candidates = [chat for chat in fresh_chats.values()
                          if chat.peer_user_id is not None
                          and _has_option(metadata.get(chat.id, {}), "SERVICE_CHAT")]
            needed = sorted({chat.peer_user_id for chat in candidates
                             if not _is_verification_contact(contacts.get(chat.peer_user_id))})
            if needed:
                contact_info = self._require_history_context(binding).contact_info(needed, expected_session=binding[2])
                self._require_history_context(binding)
                if not _has_list_field(contact_info, ("contacts",)) and not isinstance(
                        contact_info.get("contact") if isinstance(contact_info, dict) else None, dict):
                    raise ValueError("MAX не вернул метаданные служебных контактов")
                self._history_cache_write(binding, lambda: self._remember_contact_info(contact_info))
                with self._metadata_lock:
                    contacts = copy.deepcopy(self._server_contacts)
            trusted = [chat for chat in candidates
                       if _is_verification_contact(contacts.get(chat.peer_user_id))]
            if not trusted:
                return {"status": "chat_not_available", "messages": []}

            # If the server supplies multiple verified code conversations, use
            # the one with its newest actual lastMessage, never a local title.
            chat = max(trusted, key=lambda item:
                       _message_time(metadata[item.id].get("lastMessage")) or 0)
            latest_data = metadata[chat.id].get("lastMessage")
            latest = _visible_message(latest_data, include_clipboard=True)
            messages = []
            if latest is not None:
                sender = _strict_id(latest_data.get("sender"))
                if sender != owner and sender in (None, chat.peer_user_id):
                    messages.append(latest)
            # General cache entries may have parser-normalized or locally
            # supplied times. They are not proof of a fresh server code.
            self._require_history_context(binding)
            return {"status": "available" if messages else "empty", "chat_id": chat.id,
                    "messages": messages}

    def _cached_saved_preview(self, chat: Chat, record: dict, *, owner: Optional[int]) -> Optional[str]:
        """Project a thin Saved Messages DTO from the exact proven cached row.

        Omission is not replacement: explicit attachments, edits and forwards
        must never inherit an older CONTROL label just because the text matches.
        This reads only the current owner's cache; it does not request history.
        """
        with self._metadata_lock:
            if (type(owner) is not int or self.my_id != owner
                    or self._server_metadata_owner != owner or self.store.cache_owner != owner
                    or chat.id != 0 or chat.chat_type != 'DIALOG'
                    or not isinstance(record, dict) or record.get('type') != 'DIALOG'
                    or type(record.get('id')) is not int or record['id'] != 0):
                return None
            raw = record.get('lastMessage')
            if (not isinstance(raw, dict) or not _is_read_int(raw.get('id'))
                    or raw.get('type') != 'USER' or not _is_read_int(raw.get('sender'))
                    or not isinstance(raw.get('text'), str)
                    or raw.get('text') != chat.last_preview
                    or _message_time(raw) is None or _message_time(raw) != chat.last_time_ms
                    or any(key in raw for key in ('attaches', 'attachments', 'link', 'forwarded'))
                    or raw.get('deleted') is True or raw.get('removed') is True
                    or ('status' in raw and raw['status'] != 'EDITED')
                    or ('updateTime' in raw and _message_time({'time': raw['updateTime']}) is None)
                    or self.store.history_was_cleared(0, chat.last_time_ms)):
                return None
            # The flat chat preview has no message ID. A second row at the same
            # timestamp could be an ordinary message with the same literal text.
            cached = self.store.sole_message_at_time(0, raw['time'])
            if (cached is None or cached.id != raw['id'] or cached.status != 'sent'
                    or cached.display_source != 'outer'
                    or cached.text != raw['text'] or cached.time_ms != raw['time']
                    or cached.sender != raw['sender'] or cached.update_time_ms != raw.get('updateTime')):
                return None
            return cached.control_display_text

    def _decorate_chat_presentation(self, chat, *, contacts, records, server_chats, owner, confirmed):
        """One detached presentation model, independent of sidebar visibility."""
        ch = copy.deepcopy(chat)
        if confirmed and ch.id == 0:
            preview = self._cached_saved_preview(ch, server_chats.get(ch.id, {}), owner=owner)
            if preview is not None:
                ch.last_preview = preview
        if ch.peer_user_id is not None:
            contact = contacts.get(ch.peer_user_id)
            if contact is not None:
                if not ch.avatar_url:
                    ch.avatar_url = contact.avatar_url
                ch.is_verified = ch.is_verified or contact.is_verified
        if ch.title_locked:
            return ch
        if confirmed and ch.id == 0 and ch.chat_type == 'DIALOG':
            ch.title = 'Избранное'
            return ch
        # A chat ID is never a contact ID fallback. Explicit recipient metadata
        # repairs stale repr titles without interpreting their contents.
        if not ch.is_group and ch.peer_user_id is not None:
            contact = contacts.get(ch.peer_user_id)
            canonical = _person_name(records.get(ch.peer_user_id, {})) if confirmed else None
            if canonical and server_chats.get(ch.id, {}).get('type') == 'DIALOG':
                ch.title = canonical
            elif contact is not None and (not ch.title or ch.title.startswith('Чат ')):
                ch.title = contact.display
        return ch

    def present_chat(self, chat_id, *, context=None):
        """Read an exact cached chat with its current owner-bound display name.

        This does not reveal it in the sidebar, prime routing, or alter SQL/raw
        metadata. Callers may retain their captured history binding in context.
        """
        if not _is_read_int(chat_id):
            raise ValueError('Не подтверждён идентификатор чата')
        binding = self._history_read_context() if context is None else context
        self._require_history_context(binding)
        with self._metadata_lock:
            with self.store.account_scope(binding[0]):
                self._require_history_context(binding)
                chat = self.store.get_chat(chat_id)
                result = None if chat is None else self._decorate_chat_presentation(
                    chat, contacts={c.id: c for c in self.store.list_contacts()},
                    records=self._server_contacts, server_chats=self._server_chats,
                    owner=binding[0], confirmed=True)
                self._require_history_context(binding)
                return result

    def list_chats(self, section: str = "all") -> list[Chat]:
        if section not in {"all", "new", "channels"}:
            raise ValueError("Неизвестный раздел чатов")
        chats = self.store.list_chats()
        chats = [chat for chat in chats if self.store.conversation_departure(chat.id) is None]
        chats = [chat for chat in chats if not (
            (boundary := self.store.history_boundary(chat.id)) and boundary['deleted']
            and (chat.last_time_ms is None or chat.last_time_ms <= boundary['cutoff']))]
        contacts = self._contacts_map()
        owner = self.my_id
        with self._metadata_lock:
            records = copy.deepcopy(self._server_contacts)
            server_chats = copy.deepcopy(self._server_chats)
            confirmed = (type(owner) is int and self._server_metadata_owner == owner
                         and self.store.cache_owner == owner)
        saved = any(ch.id == 0 and ch.chat_type == 'DIALOG' for ch in chats) if confirmed else False
        # The old lookup created a local self placeholder with userId as chatId.
        # Hide only that exact unconfirmed shape. Do not delete history or guess
        # arbitrary IDs from a displayed name or a serialized name record.
        if saved:
            chats = [ch for ch in chats if not (
                ch.id == owner and ch.id != 0 and ch.peer_user_id == owner
                and ch.chat_type is None and not ch.is_group and ch.id not in server_chats)]
        chats = [self._decorate_chat_presentation(
            ch, contacts=contacts, records=records, server_chats=server_chats,
            owner=owner, confirmed=confirmed) for ch in chats]
        for ch in chats:
            # Прайм клиента peer'ами диалогов: отправка пойдёт сразу по userId.
            if ch.peer_user_id is not None:
                self.client.set_peer(ch.id, ch.peer_user_id)
        if section == "new":
            return [chat for chat in chats if chat.unread > 0]
        if section == "channels":
            return [chat for chat in chats if chat.is_channel]
        return chats

    def chat_section_counts(self) -> dict[str, int]:
        chats = self.list_chats()
        return {"all": len(chats), "new": sum(chat.unread > 0 for chat in chats),
                "channels": sum(chat.is_channel for chat in chats)}

    def _history_read_context(self):
        """Capture one confirmed account/transport, without starting authentication."""
        from ..protocol.client import AuthenticatedSession
        owner = self._confirmed_profile_id()
        client = self.client
        context = getattr(client, "authenticated_session", None)
        if (not isinstance(context, AuthenticatedSession) or context.owner_id != owner
                or self.store.cache_owner != owner or self.session.device_id != context.device_id):
            raise ValueError("Не подтверждена сессия для чтения истории")
        binding = (owner, client, context)
        self._require_history_context(binding)
        return binding

    def _require_history_context(self, binding):
        owner, client, context = binding
        if (self.client is not client or getattr(client, "authenticated_session", None) is not context
                or self._confirmed_profile_id() != owner or self.store.cache_owner != owner
                or self.session.device_id != context.device_id or client.device_id != context.device_id):
            raise ValueError("Аккаунт или соединение изменились во время чтения истории")
        return client

    def _history_cache_write(self, binding, apply):
        # Contacts already acquire metadata then store, so preserve that lock order.
        # bind_owner cannot clear/reassign SQL between the guard and these writes.
        with self._metadata_lock:
            with self.store.account_scope(binding[0]):
                self._require_history_context(binding)
                result = apply()
                self._require_history_context(binding)
                return result

    def open_chat(self, chat_id: int, count: int = 50, force: bool = False) -> list[Message]:
        """Read the latest window from the actual last-message timestamp.

        Current public web source types opcode49.from as a timestamp and opens
        read conversations at lastMessage.time. No wall-clock/message-ID cursor
        or legacy forward-from-zero fallback is used here.
        """
        if type(count) is not int or count <= 0:
            raise ValueError("Количество сообщений должно быть положительным целым числом")
        binding = self._history_read_context()
        draft = self._history_cache_write(binding, lambda: self._contact_dialog_draft(chat_id, binding))
        if draft is not None:
            with self._metadata_lock:
                latest = self._server_chats.get(chat_id, {}).get('lastMessage')
            if latest is None:
                # GET48 already established absence for this captured draft.
                # Opening it must not send an invented history cursor or create
                # a conversation. Pending local text remains visible if any.
                return self._history_cache_write(binding, lambda: self.store.messages(chat_id))
        anchor = self._latest_history_anchor(chat_id, _read_context=binding)
        self._resolve_chat_contacts([chat_id], _read_context=binding)
        self._schedule_chat_identity(binding, (chat_id,))
        self._require_history_context(binding)
        if anchor is None:
            # A refreshed matching chat DTO has no lastMessage, the same branch
            # in which the official web initial-open caller makes no history RPC.
            self._chat_fetch_ts[chat_id] = time.time()
            self._chat_fetch_anchor.pop(chat_id, None)
            return []
        now = time.time()
        if (not force and now - self._chat_fetch_ts.get(chat_id, 0.0) < 4.0
                and self._chat_fetch_anchor.get(chat_id) == anchor
                and self.store.messages(chat_id, limit=1)):
            return self._history_cache_write(binding, lambda: self.store.messages(chat_id))
        messages = self.open_chat_window(chat_id, from_cursor=anchor, forward=0, backward=count,
                                         _read_context=binding)
        self._chat_fetch_anchor[chat_id] = anchor
        return messages

    def _latest_history_anchor(self, chat_id: int, *, _read_context=None) -> Optional[int | float]:
        binding = _read_context if _read_context is not None else self._history_read_context()
        client = self._require_history_context(binding)
        with self._metadata_lock:
            cached = copy.deepcopy(self._server_chats.get(chat_id))
        anchor = _message_time(cached.get("lastMessage")) if isinstance(cached, dict) else None
        if anchor is not None:
            return anchor

        # Exactly one sequential metadata request when a confirmed timestamp is
        # unavailable. Failures do not silently become an empty/cached history.
        info = client.chat_info([chat_id], expected_session=binding[2])
        if not _has_chat_field(info):
            raise ValueError("MAX не вернул метаданные выбранного чата")
        matches = [chat for chat in _chat_entries(info) if _strict_id(chat.get("id")) == chat_id]
        if len(matches) != 1:
            raise ValueError("MAX не подтвердил выбранный чат для загрузки истории")
        self._history_cache_write(binding, lambda: self._remember_chat_info(info, my_id=binding[0]))
        metadata = matches[0]
        if "lastMessage" not in metadata:
            return None
        if not isinstance(metadata["lastMessage"], dict):
            raise ValueError("MAX вернул неподтверждённый формат последнего сообщения")
        anchor = _message_time(metadata["lastMessage"])
        if anchor is None:
            raise ValueError("MAX не вернул время последнего сообщения; курсор истории не подставлен")
        return anchor

    def _cache_history_messages(self, chat_id: int, raw_messages: list[dict]) -> None:
        batch = []
        dated = []
        for raw in raw_messages:
            if not isinstance(raw, dict):
                continue
            if (self.store.history_was_cleared(chat_id, _message_time(raw))
                    or self.store.message_was_deleted(chat_id, _strict_id(raw.get('id')))):
                continue
            if raw.get('status') == 'REMOVED':
                identity = _strict_id(raw.get('id'))
                cached = self.store.message(chat_id, identity) if identity is not None else None
                if cached is not None and cached.status != 'removed':
                    # Removed on the server while this session missed the 142
                    # (offline, another device): mark the cached row like push128
                    # does and move a preview that still shows it.
                    self.store.apply_message_ack(chat_id, raw, my_id=self.my_id, replay=True)
                    self.store.repair_preview_after_removal(chat_id, cached.time_ms)
                continue  # A server-removed message is never a new visible row or preview.
            message = Message.from_server(chat_id, raw, self.my_id)
            self.store.refresh_message_control_metadata(chat_id, raw)
            self.store.refresh_message_file_metadata(chat_id, raw)
            self.store.refresh_message_photo_metadata(chat_id, raw)
            self.store.refresh_message_forward_metadata(chat_id, raw)
            self.store.refresh_message_display_metadata(chat_id, raw)
            if message.has_display_content:
                batch.append(message)
                if _message_time(raw) is not None:
                    dated.append(self.store.message(chat_id, message.id) or message)
        if batch:
            self.store.insert_messages(batch)
        if dated:
            latest = max(dated, key=lambda message: message.time_ms)
            if any(attach.type == 'CONTROL' for attach in latest.attaches):
                cached = self.store.message(chat_id, latest.id)
                if cached is None or cached.status != 'sent' or cached.time_ms != latest.time_ms:
                    return  # No matching current anchor for this control preview.
                latest = cached  # Reflect the actual validated enrichment, not an ignored old row.
            previous = self.store.get_chat(chat_id)
            if (previous is None or previous.last_time_ms is None
                    or latest.time_ms >= previous.last_time_ms):
                self.store.update_chat_preview(chat_id, latest.time_ms, latest.preview)

    def open_chat_window(self, chat_id: int, *, from_cursor: int | float, forward: int = 0,
                         backward: int = 50, _read_context=None) -> list[Message]:
        """Read one window around a caller-established server cursor.

        Opcode49.from is a timestamp in the current public schema/callers.
        No fallback cursor or repeated request is used.
        """
        if type(from_cursor) not in (int, float) or not math.isfinite(from_cursor):
            raise ValueError("Не подтверждено время для окна истории")
        binding = _read_context if _read_context is not None else self._history_read_context()
        client = self._require_history_context(binding)
        result = client.chat_history_window(chat_id, from_cursor=from_cursor,
                                             forward=forward, backward=backward, get_chat=True)
        if not isinstance(result, dict) or not isinstance(result.get("messages"), list):
            raise ValueError("MAX не вернул структурированную историю сообщений")
        if (isinstance(result.get("chat"), dict)
                and _strict_id(result["chat"].get("id")) != chat_id):
            raise ValueError("MAX вернул историю другого чата")
        def cache():
            self.store.ensure_chat(chat_id)
            if isinstance(result.get("chat"), dict):
                self._remember_chat_info({"chats": [result["chat"]]}, my_id=binding[0])
            self._cache_history_messages(chat_id, result["messages"])
            self._chat_fetch_ts[chat_id] = time.time()
            return self.store.messages(chat_id)
        return self._history_cache_write(binding, cache)

    def local_history(self, chat_id: int) -> list[Message]:
        return self.store.messages(chat_id)

    def read_receipt_state(self, chat_id: int, *, context=None) -> dict:
        """Read current-owner counters/mark without connecting or sending anything."""
        if not _is_read_int(chat_id):
            raise ValueError('Некорректный чат MAX')
        binding = self._contact_lookup_binding(context)
        def state():
            chat = self.store.get_chat(chat_id)
            if chat is None:
                raise ValueError('Чат больше недоступен')
            raw = self._server_chats.get(chat_id, {})
            participants = raw.get('participants', {})
            mark = (participants.get(str(binding[0]), participants.get(binding[0]))
                    if isinstance(participants, dict) else None)
            return {'mark': _message_time({'time': mark}),
                    'unread': chat.unread, 'markedAsUnread': raw.get('markedAsUnread') is True}
        return self._history_cache_write(binding, state)

    def read_receipt_frontier(self, chat_id: int, *, context=None):
        """(id, time) of the newest server-confirmed message, or None; no RPC."""
        if not _is_read_int(chat_id):
            raise ValueError('Некорректный чат MAX')
        binding = self._contact_lookup_binding(context)
        def frontier():
            last = self._server_chats.get(chat_id, {}).get('lastMessage')
            if (not isinstance(last, dict) or not _is_read_int(last.get('id'))
                    or _message_time(last) is None):
                return None
            return last['id'], _message_time(last)
        return self._history_cache_write(binding, frontier)

    def _apply_read_receipt(self, chat_id, owner_id, mark, unread, *,
                            set_as_unread=False, apply_count=True, allow_equal=False):
        """Apply an ordered own receipt inside the caller's account/cache scope."""
        if (not _is_read_int(chat_id) or _message_time({'time': mark}) is None
                or type(set_as_unread) is not bool
                or unread is not None and (type(unread) is not int or not 0 <= unread < 2**31)):
            return False
        if self.store.get_chat(chat_id) is None:
            return False
        raw = self._server_chats.get(chat_id)
        if not isinstance(raw, dict):
            # This is counter evidence, not sufficient metadata to invent a chat.
            raw = self._server_chats[chat_id] = {'id': chat_id}
        participants = raw.get('participants')
        if not isinstance(participants, dict):
            participants = {}
        previous = participants.get(str(owner_id), participants.get(owner_id))
        if (not set_as_unread and _message_time({'time': previous}) is not None
                and (mark < previous or mark == previous and not allow_equal)):
            return False
        participants = dict(participants)
        participants.pop(owner_id, None)
        participants[str(owner_id)] = mark
        raw['participants'] = participants
        raw['markedAsUnread'] = set_as_unread
        if not hasattr(self, '_read_receipt_versions'):
            self._read_receipt_versions = {}
        receipt_key = (owner_id, chat_id)
        self._read_receipt_versions[receipt_key] = self._receipt_version(owner_id, chat_id) + 1
        if apply_count and unread is not None:
            self.store.set_unread(chat_id, unread)
            raw['newMessages'] = unread
        return True

    def mark_chat_read(self, chat_id: int, message_id: Optional[int] = None,
                       mark: Optional[int | float] = None, *, context=None) -> dict:
        binding = self._contact_lookup_binding(context)
        with self._reserve_chat_read(binding, chat_id):
            return self._mark_chat_read_unreserved(chat_id, message_id, mark, context=context)

    def _mark_chat_read_unreserved(self, chat_id: int, message_id: Optional[int] = None,
                                   mark: Optional[int | float] = None, *, context=None) -> dict:
        """Mark a known message; apply only a current, ordered server receipt."""
        if (not _is_read_int(chat_id) or not _is_read_int(message_id)
                or mark is not None and _message_time({'time': mark}) is None):
            raise ValueError('Для прочтения нужны подтверждённые ID сообщения и чата')
        binding = self._contact_lookup_binding(context)
        def prepare():
            chat = self.store.get_chat(chat_id)
            message = self.store.message(chat_id, message_id)
            raw = self._server_chats.get(chat_id, {})
            last = raw.get('lastMessage', {})
            timestamp = (message.time_ms if message is not None
                         and message.status not in ('pending', 'failed', 'unknown', 'removed') else
                         _message_time(last) if isinstance(last, dict)
                         and type(last.get('id')) is int and last['id'] == message_id else None)
            if chat is None or _message_time({'time': timestamp}) is None or mark is not None and mark != timestamp:
                raise ValueError('Сообщение для отметки прочтения не подтверждено')
            participants = raw.get('participants', {})
            previous_mark = (participants.get(str(binding[0]), participants.get(binding[0]))
                             if isinstance(participants, dict) else None)
            return (timestamp, (chat.unread, chat.last_time_ms, chat.last_preview),
                    (previous_mark, raw.get('markedAsUnread') is True,
                     self._receipt_version(binding[0], chat_id)))
        selected_mark, snapshot, receipt_snapshot = self._history_cache_write(binding, prepare)
        client = self._require_history_context(binding)
        response = client.mark_read(chat_id, message_id=message_id, mark=selected_mark,
                                    expected_session=binding[2])
        if (not isinstance(response, dict) or response.get('success') is False
                or 'success' in response and type(response['success']) is not bool):
            raise ValueError('MAX не подтвердил прочтение сообщений')
        def apply():
            # A newer incoming notification may have arrived while this request
            # waited. Its count must not be erased by an older queued ACK.
            chat = self.store.get_chat(chat_id)
            if chat is None:
                return False
            raw = self._server_chats.get(chat_id, {})
            participants = raw.get('participants', {})
            current_mark = (participants.get(str(binding[0]), participants.get(binding[0]))
                            if isinstance(participants, dict) else None)
            if (current_mark, raw.get('markedAsUnread') is True,
                    self._receipt_version(binding[0], chat_id)) != receipt_snapshot:
                # A newer own read/unread notification is authoritative even
                # when SET_AS_UNREAD deliberately moved its timestamp back.
                return False
            unchanged = (chat.unread, chat.last_time_ms, chat.last_preview) == snapshot
            if ('unread' not in response or _message_time({'time': response.get('mark')}) is None
                    or type(response['unread']) is not int or not 0 <= response['unread'] < 2**31):
                return False
            # The receipt snapshot above proves nothing newer arrived while we
            # waited, so an ACK repeating the current marker is still the newest
            # server count (used by the visible-frontier reconcile request).
            return self._apply_read_receipt(chat_id, binding[0], response['mark'],
                                            response['unread'], apply_count=unchanged,
                                            allow_equal=True)
        changed = self._history_cache_write(binding, apply)
        if changed and self.on_chat_changed:
            self.on_chat_changed(chat_id)
        return response

    def _message_mutation_binding(self, context):
        from ..protocol.client import AuthenticatedSession
        if not isinstance(context, AuthenticatedSession):
            raise ValueError('Заново откройте меню сообщения в текущей сессии MAX.')
        return self._contact_lookup_binding(context)

    def _message_mutation_commit(self, binding, apply, chat_id, *, notify=True):
        """Commit only to the captured owner and emit no stale-account callback."""
        def commit():
            result = apply()
            self._require_history_context(binding)
            callback = getattr(self, 'on_chat_changed', None)
            if notify and callback:
                callback(chat_id)
            return result
        try:
            return self._history_cache_write(binding, commit)
        except MaxWriteOutcomeUnknown:
            raise
        except Exception:
            raise MaxWriteOutcomeUnknown(
                'Действие отправлено, но его результат не установлен в текущей сессии. Проверьте чат.'
            ) from None

    def _cache_reaction_response(self, chat_id: int, message_id: int, response: dict, *, notify=True) -> dict:
        if not isinstance(response, dict) or response.get("success") is False:
            raise ValueError("MAX не подтвердил изменение реакции")
        for field, expected in (('chatId', chat_id), ('messageId', message_id)):
            if field in response and (type(response[field]) is not int or response[field] != expected):
                raise MaxWriteOutcomeUnknown('Сервер не подтвердил реакцию для выбранного сообщения.')
        if isinstance(response.get("reactionInfo"), dict):
            # Explicit reactionInfo is a complete snapshot in the public model:
            # missing counters/yourReaction inside it mean cleared values.
            # Omitting reactionInfo itself remains an unknown/unchanged state.
            self.store.update_message_reactions(chat_id, message_id, response["reactionInfo"], replace=True)
            if notify and self.on_chat_changed:
                self.on_chat_changed(chat_id)
        return response

    def set_message_reaction(self, chat_id: int, message_id: int, reaction_id: str, *,
                             reaction_type: str = "EMOJI", post_id: Optional[int] = None, context=None) -> dict:
        binding = self._message_mutation_binding(context)
        client = self._require_history_context(binding)
        response = client.set_reaction(chat_id, message_id, reaction_id,
            reaction_type=reaction_type, post_id=post_id, expected_session=binding[2])
        return self._message_mutation_commit(binding,
            lambda: self._cache_reaction_response(chat_id, message_id, response, notify=False), chat_id,
            notify=isinstance(response, dict) and isinstance(response.get('reactionInfo'), dict))

    def remove_message_reaction(self, chat_id: int, message_id: int, *,
                                post_id: Optional[int] = None, context=None) -> dict:
        binding = self._message_mutation_binding(context)
        client = self._require_history_context(binding)
        response = client.remove_reaction(chat_id, message_id, post_id=post_id, expected_session=binding[2])
        return self._message_mutation_commit(binding,
            lambda: self._cache_reaction_response(chat_id, message_id, response, notify=False), chat_id,
            notify=isinstance(response, dict) and isinstance(response.get('reactionInfo'), dict))

    def get_message_reactions(self, chat_id: int, message_ids: list[int], *,
                              post_id: Optional[int] = None) -> dict:
        # The complete messagesReactions value DTO is not expanded in the
        # available primary schema. Preserve the server result without guessing.
        return self.client.get_reactions(chat_id, message_ids, post_id=post_id)

    # ───────────────────────── send ─────────────────────────

    def _prime_peer(self, chat_id: int) -> bool:
        """Подсказать клиенту peer диалога из БД (чтобы первая отправка шла сразу
        по userId, без неудачной попытки по chatId) и вернуть is_group — в группу
        диалоговый фолбэк по userId делать НЕЛЬЗЯ (ушло бы постороннему)."""
        ch = self.store.get_chat(chat_id)
        if ch and ch.peer_user_id:
            self.client.set_peer(chat_id, ch.peer_user_id)
        return bool(ch and ch.is_group)

    def _persist_peer(self, chat_id: int) -> None:
        """Сохранить peer, который клиент выяснил из ошибки сервера (1:1 диалог)."""
        peer = self.client.peer_for(chat_id)
        if peer:
            self.store.set_chat_peer(chat_id, peer)

    def _record_server_last_message(self, chat_id: int, raw: dict, *, replay: bool = False) -> bool:
        """Keep only actual server message data as future timestamp anchors."""
        if not isinstance(raw, dict):
            return False
        if (self.store.history_was_cleared(chat_id, _message_time(raw))
                or self.store.message_was_deleted(chat_id, _strict_id(raw.get('id')))):
            return False
        with self._metadata_lock:
            chat = self._server_chats.setdefault(chat_id, {"id": chat_id})
            previous = chat.get("lastMessage")
            previous = previous if isinstance(previous, dict) else {}
            same = type(raw.get("id")) is int and raw.get("id") == previous.get("id")
            known_version = _message_time({'time': previous.get('updateTime')})
            incoming_version = _message_time({'time': raw.get('updateTime')})
            if same and known_version is not None and (
                    incoming_version is not None and incoming_version < known_version
                    or replay and incoming_version is None):
                return False
            current_time, new_time = _message_time(previous), _message_time(raw)
            if not same and (new_time is None or (current_time is not None and new_time < current_time)):
                return False
            latest = copy.deepcopy(previous) if same else {}
            latest.update(copy.deepcopy(raw))
            chat["lastMessage"] = latest
            return _message_time(latest) is not None

    def _accept_message_ack(self, requested_chat_id: int, result: dict, *,
                            expected_message_id: Optional[int] = None,
                            local_id: Optional[str] = None, outgoing: Optional[bool] = None,
                            binding=None, notify=True) -> Message:
        client = self._require_history_context(binding) if binding is not None else self.client
        owner = binding[0] if binding is not None else self.my_id
        raw = result.get("message") if isinstance(result, dict) else None
        actual_chat_id = result.get("chatId", requested_chat_id) if isinstance(result, dict) else None
        if (not isinstance(raw, dict) or type(raw.get("id")) is not int
                or type(actual_chat_id) is not int
                or (expected_message_id is not None and raw["id"] != expected_message_id)):
            raise MaxWriteOutcomeUnknown("Сервер не подтвердил нужное сообщение. Перед повтором проверьте чат.")
        requested_chat = self.store.get_chat(requested_chat_id)
        if actual_chat_id != requested_chat_id:
            if binding is not None:
                raise MaxWriteOutcomeUnknown('Сервер вернул другой чат. Повтор не выполнен.')
            with self._metadata_lock:
                known_server_chat = requested_chat_id in self._server_chats
            if known_server_chat or requested_chat is None or requested_chat.peer_user_id is None or requested_chat.is_group:
                raise MaxWriteOutcomeUnknown("Сервер вернул другой чат. Перед повтором проверьте результат действия.")
        server_chat = result.get("chat")
        if isinstance(server_chat, dict) and _strict_id(server_chat.get("id")) != actual_chat_id:
            raise MaxWriteOutcomeUnknown("Сервер вернул неоднозначный чат сообщения. Повтор не выполнен.")
        try:
            if self.store.message_was_deleted(actual_chat_id, raw['id']):
                return self.store.apply_message_ack(actual_chat_id, raw, local_id=local_id,
                                                    my_id=owner, outgoing=outgoing)
            self.store.ensure_chat(actual_chat_id)
            if isinstance(server_chat, dict):
                self._remember_chat_info({"chats": [server_chat]}, my_id=owner)
            elif requested_chat is not None and requested_chat.peer_user_id is not None:
                self.store.set_chat_peer(actual_chat_id, requested_chat.peer_user_id)
            learned_peer = client.peer_for(requested_chat_id)
            if learned_peer is not None:
                self.store.set_chat_peer(actual_chat_id, learned_peer)
            message = self.store.apply_message_ack(actual_chat_id, raw, local_id=local_id,
                                                     my_id=owner, outgoing=outgoing)
            if self._record_server_last_message(actual_chat_id, raw):
                with self._metadata_lock:
                    timestamp = _message_time(self._server_chats[actual_chat_id].get("lastMessage"))
                self.store.update_chat_preview(actual_chat_id, timestamp, message.preview)
        except Exception:
            raise MaxWriteOutcomeUnknown(
                "Сервер ответил на действие, но локальный кэш не обновлён. Перед повтором проверьте чат."
            ) from None
        if notify and self.on_chat_changed:
            self.on_chat_changed(actual_chat_id)
        return message

    def _pending_failure(self, pending: Message, error: Exception, *, unknown: bool = False) -> Message:
        pending.status = "unknown" if unknown else "failed"
        pending.error = str(error) or type(error).__name__
        try:
            self.store.update_message_by_local_id(pending.local_id, status=pending.status)
        except Exception:
            raise MaxWriteOutcomeUnknown("Не удалось сохранить состояние действия. Перед повтором проверьте чат.") from None
        return pending

    def send_text(self, chat_id: int, text: str, *, context=None, on_pending=None) -> Message:
        return self._send_text_bound(chat_id, text, context=context, on_pending=on_pending)

    def _send_text_bound(self, chat_id, text, *, context=None, reply_id=None, on_pending=None):
        if not isinstance(text, str) or not text.strip():
            raise ValueError('Введите текст сообщения')
        binding = self._contact_lookup_binding(context)
        client = self._require_history_context(binding)

        def prepare():
            from .conversation_info_service import _peer
            chat = self.store.get_chat(chat_id)
            if chat is None:
                raise ValueError('Откройте выбранный чат заново')
            draft = self._contact_dialog_draft(chat_id, binding)
            raw = self._server_chats.get(chat_id, {})
            peer = draft.contact_id if draft else _peer(chat, raw, binding[0])
            if chat.is_group:
                if peer is not None or client.peer_for(chat_id) is not None:
                    raise ValueError('Не подтверждён адрес группового чата')
            elif peer is None:
                raise ValueError('Не подтверждён получатель сообщения')
            if reply_id is not None and self.store.message(chat_id, reply_id) is None:
                raise ValueError('Исходное сообщение не относится к выбранному чату')
            return peer
        peer = self._history_cache_write(binding, prepare)
        local_id = str(uuid.uuid4())
        pending = Message(
            chat_id=chat_id, sender=binding[0], text=text,
            time_ms=int(time.time() * 1000), outgoing=True,
            status="pending", local_id=local_id,
            link={'type': 'REPLY', 'messageId': reply_id} if reply_id is not None else None,
        )
        def remember_pending():
            self.store.insert_message(pending)
            self.store.update_chat_preview(chat_id, pending.time_ms, text)
        self._history_cache_write(binding, remember_pending)
        if callable(on_pending):
            try:
                on_pending()  # Display hint for the stored pending row; never alters the send.
            except Exception:
                pass
        try:
            self._require_history_context(binding)
            if reply_id is None:
                res = client.send_message(chat_id, text, peer_user_id=peer,
                    allow_dialog_fallback=False, expected_session=binding[2])
            else:
                res = client.reply_to_message(chat_id, reply_id, text, peer_user_id=peer,
                                              expected_session=binding[2])
            pending = self._message_mutation_commit(binding, lambda: self._accept_message_ack(
                chat_id, res, local_id=local_id, outgoing=True, binding=binding, notify=False), chat_id)
        except Exception as error:  # noqa: BLE001
            unknown = isinstance(error, MaxWriteOutcomeUnknown)
            try:
                self._history_cache_write(binding, lambda: self._pending_failure(pending, error, unknown=unknown))
            except Exception:
                # Never write old-owner status into a switched account/cache.
                pending.status = 'unknown'
                pending.error = 'Сессия изменилась; проверьте результат отправки перед повтором.'
                try:
                    # Same account after a reconnect: the stored row must not stay
                    # "pending" forever. account_scope refuses any other owner.
                    with self._metadata_lock, self.store.account_scope(binding[0]):
                        self.store.settle_unsent_message(chat_id, local_id, 'unknown')
                except Exception:
                    pass
        return pending

    def reply_to_message(self, chat_id: int, message_id: int, text: str, *, context=None,
                         on_pending=None) -> Message:
        if not isinstance(text, str) or not text.strip():
            raise ValueError("Введите текст ответа")
        if type(message_id) is not int:
            raise ValueError("Выберите сообщение для ответа")
        return self._send_text_bound(chat_id, text, context=context, reply_id=message_id,
                                     on_pending=on_pending)

    def edit_message(self, chat_id: int, message_id: int, text: str, *, context=None) -> dict:
        binding = self._message_mutation_binding(context)
        client = self._require_history_context(binding)
        result = client.edit_message(chat_id, message_id, text, expected_session=binding[2])
        self._message_mutation_commit(binding, lambda: self._accept_message_ack(chat_id, result,
            expected_message_id=message_id, binding=binding, notify=False), chat_id)
        return result

    def delete_messages(self, chat_id: int, message_ids: list[int], *, for_all: bool = False, context=None) -> dict:
        binding = self._message_mutation_binding(context)
        client = self._require_history_context(binding)
        try:
            result = client.delete_messages(chat_id, message_ids, for_all=for_all, expected_session=binding[2])
            affected = result.get("messageIds") if isinstance(result, dict) else None
            if (not isinstance(result, dict) or type(result.get('chatId')) is not int or result.get("chatId") != chat_id
                    or not isinstance(affected, list) or any(type(identity) is not int for identity in affected)
                    or not set(affected).issubset(set(message_ids))):
                raise MaxWriteOutcomeUnknown("Сервер не подтвердил выбранные сообщения для удаления. Повтор не выполнен.")
        except MaxWriteOutcomeUnknown:
            if not for_all and self._deletion_confirmed_by_event(binding, chat_id, message_ids):
                return {'chatId': chat_id, 'messageIds': list(dict.fromkeys(message_ids))}
            raise
        if not set(affected) & set(message_ids):
            # An explicit ACK without the requested IDs deleted nothing; the
            # official caller rolls such optimistic removals back.
            if not for_all and self._deletion_confirmed_by_event(binding, chat_id, message_ids):
                return {'chatId': chat_id, 'messageIds': list(dict.fromkeys(message_ids))}
            from ..protocol.errors import MaxError
            raise MaxError("MSG_DELETE: сервер не удалил выбранные сообщения")
        self._message_mutation_commit(binding, lambda: self._remember_deleted_messages(
            binding[0], chat_id, affected), chat_id)
        # Patch any currently open view by exact confirmed IDs, as for 142;
        # a reopened/reloaded view must not keep showing the removed row.
        change = MessageChange(*binding, chat_id, tuple(dict.fromkeys(affected)))
        callback = getattr(self, 'on_message_changed', None)
        if callback is not None and self.is_current_message_change(change):
            callback(change)
        return result

    def _deletion_confirmed_by_event(self, binding, chat_id, message_ids):
        """True only when a current-owner 142 already tombstoned every requested ID."""
        try:
            return self._history_cache_write(binding, lambda: isinstance(message_ids, list) and bool(message_ids)
                and all(self.store.message_was_deleted(chat_id, identity) for identity in message_ids))
        except ValueError:
            return False

    def _remember_deleted_messages(self, owner_id, chat_id, identities):
        """Called inside the bound metadata/store scope for confirmed66 or142."""
        metadata = self._server_chats.get(chat_id)
        latest = metadata.get('lastMessage') if isinstance(metadata, dict) else None
        deleted_latest = isinstance(latest, dict) and _strict_id(latest.get('id')) in identities
        self.store.confirm_message_deletions(owner_id, chat_id, identities,
            deleted_last_time=_message_time(latest) if deleted_latest else None)
        if deleted_latest:
            metadata.pop('lastMessage', None)
            self._chat_fetch_anchor.pop(chat_id, None)

    def forward_messages(self, source_chat_id: int, message_ids: list[int], target_chat_id: int, *, context=None) -> dict:
        binding = self._message_mutation_binding(context)
        if (not isinstance(message_ids, list) or not message_ids
                or any(type(identity) is not int for identity in message_ids)
                or len(set(message_ids)) != len(message_ids)):
            raise ValueError("Выберите сообщения для пересылки без повторяющихся идентификаторов")
        self._history_cache_write(binding, lambda: self._prime_peer(target_chat_id))
        results = []
        for identity in message_ids:
            # The source schema forwards one message per packet. Stop at the
            # first failure/unknown outcome; never retry or guess a batch DTO.
            client = self._require_history_context(binding)
            result = client.forward_message(source_chat_id, identity, target_chat_id, expected_session=binding[2])
            self._message_mutation_commit(binding, lambda: self._accept_message_ack(target_chat_id, result,
                outgoing=True, binding=binding, notify=False), target_chat_id)
            results.append(result)
        return {"chatId": results[-1]["chatId"],
                "messageIds": [result["message"]["id"] for result in results], "responses": results}

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
            is_group = self._prime_peer(chat_id)
            res = self.client.send_message(
                chat_id, text, attaches=[attach], allow_dialog_fallback=not is_group
            )
            pending = self._accept_message_ack(chat_id, res, local_id=local_id, outgoing=True)
        except MaxWriteOutcomeUnknown as error:
            self._pending_failure(pending, error, unknown=True)
        except Exception as e:  # noqa: BLE001
            self._pending_failure(pending, e)
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
        """Current-owner display projection; never migrate names or make RPCs."""
        with self._metadata_lock:
            owner = self.my_id
            if not _is_read_int(owner) or self.store.cache_owner != owner:
                raise ValueError('Кэш контактов принадлежит другой сессии')
            with self.store.account_scope(owner):
                contacts = self.store.list_contacts()
                hidden = self.store.hidden_contact_ids(owner)
                records = (self._server_contacts if self._server_metadata_owner == owner else {})
                own_profile = self.store.load_profile()
                own_name = (_clean_name(own_profile.name) if own_profile is not None
                            and type(own_profile.id) is int and own_profile.id == owner else None)
                projected = []
                for contact in contacts:
                    if contact.id in hidden:
                        continue
                    name = _contact_display_name(contact.name)
                    record = records.get(contact.id)
                    # Metadata refreshes may upsert a deleted contact to keep
                    # its dialog/profile readable. Do not re-add that explicit
                    # server-removed member to the address-book projection.
                    if (isinstance(record, dict) and type(record.get('id')) is int
                            and record['id'] == contact.id and record.get('status') == 'REMOVED'
                            and type(record.get('flags')) is int and -(2**31) <= record['flags'] < 2**31
                            and not record['flags'] & 512):
                        continue
                    names = record.get('names') if isinstance(record, dict) else None
                    # Repair only the exact former stringify(names) artifact
                    # backed by this same owner's actual same-ID record. A
                    # normal alias or an unmatched repr-like string is retained.
                    if (isinstance(contact.name, str) and isinstance(names, list)
                            and all(isinstance(item, dict) for item in names)
                            and type(record.get('id')) is int and record['id'] == contact.id
                            and contact.name == str(names)):
                        name = _person_name({'names': names}) or name
                    if (type(contact.id) is int and contact.id == owner and own_name
                            and _legacy_names_display_name(contact.name) == own_name):
                        # The persisted confirmed profile is independent of the
                        # transient contacts metadata. Change only this display
                        # copy, with an exact same-owner/name admission.
                        name = own_name
                    projected.append(Contact(
                        id=contact.id, name=name, phone=_clean_name(contact.phone),
                        avatar_url=contact.avatar_url if isinstance(contact.avatar_url, str) else None,
                        is_verified=contact.is_verified, server_fields=contact.server_fields))
                needle = (_clean_name(query) or '').casefold()
                if needle:
                    # A number typed as 8 (900) 000-00-01 or a name written with
                    # the other yo/ye letter is the same saved contact.
                    from .search_service import contact_matches
                    projected = [contact for contact in projected if needle in contact.display.casefold()
                                 or needle in (contact.phone or '').casefold()
                                 or contact_matches(needle, contact.display, contact.phone)]
                projected.sort(key=lambda contact: (contact.display.casefold(), contact.id))
                if self.my_id != owner or self.store.cache_owner != owner:
                    raise ValueError('Аккаунт изменился во время чтения контактов')
                return projected

    def dialog_for_contact(self, contact_id: int) -> Optional[Chat]:
        """Return an existing confirmed dialog, never synthesize chatId=userId."""
        from .conversation_info_service import _peer, ConversationUnavailable
        if not _is_read_int(contact_id):
            raise ValueError('Некорректный контакт MAX')
        binding = self._history_read_context()
        def select():
            owner = binding[0]
            for chat in self.list_chats():
                raw = self._server_chats.get(chat.id, {})
                if ((owner, chat.id) in getattr(self, '_contact_dialog_drafts', {})
                        and raw.get('type') != 'DIALOG'):
                    continue
                if contact_id == owner:
                    if chat.id == 0 and chat.chat_type == 'DIALOG':
                        try:
                            if _peer(chat, raw, owner) == owner:
                                return chat
                        except ConversationUnavailable:
                            continue
                elif (raw.get('type') == 'DIALOG' and not chat.is_group
                      and chat.peer_user_id == contact_id):
                    try:
                        if _peer(chat, raw, owner) == contact_id:
                            return chat
                    except ConversationUnavailable:
                        continue
            return None
        return self._history_cache_write(binding, select)

    def _contact_lookup_binding(self, context=None):
        binding = self._history_read_context()
        if context is not None and context is not binding[2]:
            raise ValueError('Аккаунт или соединение поиска контакта изменились')
        return binding

    def _lookup_contact(self, binding, phone: str, name: str, *, restore_local=False) -> Contact:
        client = self._require_history_context(binding)
        data = client.contact_by_phone(phone, expected_session=binding[2])
        self._require_history_context(binding)
        cid = data.get('id') if isinstance(data, dict) else None
        if not _is_read_int(cid):
            raise ValueError('Пользователь MAX с таким номером не найден')
        def remember():
            current = self._server_contacts.get(cid)
            # RPC46 yields a clockless projection (no updateTime), not a
            # versioned ContactDTO. It must not replace this owner's versioned
            # record: membership, block state and a card's observation stay.
            # An explicit REMOVED record is still replaced, as before.
            if not (isinstance(current, dict) and _is_read_int(current.get('updateTime'))
                    and current.get('status') != 'REMOVED'):
                self._remember_contact_info({'contacts': [data]})
            canonical = _person_name(data)
            # A supplied label is local only. RPC46 performs a lookup, not ADD.
            display = _clean_name(name) or canonical or phone
            actual_phone = data.get('phone')
            actual_phone = normalize_phone(actual_phone) if isinstance(actual_phone, str) else ''
            old = self._contacts_map().get(cid)
            contact = Contact(id=cid, name=display, phone=actual_phone or phone,
                              avatar_url=old.avatar_url if old else None,
                              is_verified=old.is_verified if old else False,
                              server_fields=frozenset())
            self.store.upsert_contact(contact)
            self._invalidate_contacts()
            if _clean_name(name) and cid != binding[0]:
                dialog = self.dialog_for_contact(cid)
                if dialog is not None:
                    self.store.rename_chat(dialog.id, display)
            # An explicit import restores the local address-list entry. Plain
            # phone lookup (including the legacy add_contact_by_phone API) is
            # only a search and must preserve an earlier visibility choice.
            if restore_local:
                self.store.set_contact_hidden(binding[0], cid, False)
            return contact
        return self._history_cache_write(binding, remember)

    def add_contact_by_phone(self, phone: str, name: str = "", *, context=None) -> Contact:
        """Look up and cache one contact locally, without remote ADD or a fake chat."""
        if not isinstance(phone, str):
            raise ValueError('Укажите номер телефона')
        phone = normalize_phone(phone)
        if not phone:
            raise ValueError('Укажите номер телефона')
        binding = self._contact_lookup_binding(context)
        return self._lookup_contact(binding, phone, name)

    def import_contacts_from_file(
        self, file_path: str, on_progress: Optional[Callable[[int, int], None]] = None, *,
        context=None, cancel_event=None,
    ) -> int:
        """Импорт контактов из .vcf или .csv. Каждый номер проверяется через
        CONTACT_INFO_BY_PHONE; найденные в MAX сохраняются. Возвращает их число."""
        binding = self._contact_lookup_binding(context)
        if cancel_event is not None and cancel_event.is_set():
            return 0
        entries = parse_contacts_file(file_path)
        self._require_history_context(binding)
        total = len(entries)
        found = 0
        for i, (name, phone) in enumerate(entries, 1):
            # Cancellation stops the next lookup; a request already sent may
            # still finish and cache its confirmed result for this owner.
            if cancel_event is not None and cancel_event.is_set():
                break
            self._require_history_context(binding)
            phone = normalize_phone(phone)
            if phone:
                try:
                    self._lookup_contact(binding, phone, name, restore_local=True)
                    found += 1
                except Exception:
                    # A failed number can be skipped. A switched account or
                    # transport must stop the import before any further lookup.
                    self._require_history_context(binding)
            if on_progress:
                on_progress(i, total)
            if cancel_event is not None and cancel_event.is_set():
                break
            self._require_history_context(binding)
        self._invalidate_contacts()
        return found

    def delete_contact(self, contact_id: int, *, context=None) -> None:
        """Hide this owner's address-list entry durably, without any RPC.

        Keep the profile cache for names/Info; later metadata cannot undo an
        explicit local removal. This does not remove MAX membership or chats.
        """
        if not _is_read_int(contact_id):
            raise ValueError('Некорректный контакт MAX')
        binding = self._contact_lookup_binding(context)

        def delete():
            self.store.set_contact_hidden(binding[0], contact_id, True)
            self._invalidate_contacts()

        self._history_cache_write(binding, delete)

    def rename_contact(self, contact_id: int, new_name: str, *, context=None) -> Contact:
        """Rename a local alias and its confirmed dialog, without any RPC.

        Contact IDs are not chat IDs. Saved messages keep their separate title;
        missing/unconfirmed dialogs never become placeholder chats.
        """
        if not _is_read_int(contact_id):
            raise ValueError('Некорректный контакт MAX')
        if not isinstance(new_name, str) or not new_name.strip():
            raise ValueError("Имя не может быть пустым")
        new_name = new_name.strip()
        binding = self._contact_lookup_binding(context)

        def rename():
            # Read the actual row under the owner lock, not a potentially older
            # display/cache copy. Renaming must not create a missing contact.
            existing = next((item for item in self.store.list_contacts()
                             if type(item.id) is int and item.id == contact_id), None)
            self._require_history_context(binding)
            if existing is None:
                raise ValueError('Контакт больше недоступен. Обновите список контактов.')
            dialog = self.dialog_for_contact(contact_id) if contact_id != binding[0] else None
            self._require_history_context(binding)
            contact = Contact(id=existing.id, name=new_name, phone=existing.phone,
                              avatar_url=existing.avatar_url, is_verified=existing.is_verified,
                              server_fields=existing.server_fields)
            self.store.upsert_contact(contact)
            self._invalidate_contacts()
            if dialog is not None:
                self._require_history_context(binding)
                self.store.rename_chat(dialog.id, new_name)
            return contact

        return self._history_cache_write(binding, rename)

    def rename_chat(self, chat_id: int, title: str) -> None:
        """Локально переименовать чат (групповой или диалог). Только наша БД."""
        title = (title or "").strip()
        if not title:
            raise ValueError("Название не может быть пустым")
        self.store.rename_chat(chat_id, title)

    def rename_conversation(self, chat_id: int, name: str, *, context=None) -> None:
        """Rename only an existing current-owner conversation and its actual peer.

        Group/channel titles remain local. A contact sharing the chat's numeric
        ID is unrelated unless its DIALOG peer metadata proves that relation.
        """
        from .conversation_info_service import _peer, ConversationUnavailable

        if not _is_read_int(chat_id):
            raise ValueError('Некорректный чат MAX')
        if not isinstance(name, str) or not name.strip():
            raise ValueError('Название не может быть пустым')
        name = name.strip()
        binding = self._contact_lookup_binding(context)

        def rename():
            chat = self.store.get_chat(chat_id)
            self._require_history_context(binding)
            if chat is None:
                raise ValueError('Чат больше недоступен. Обновите список чатов.')
            raw = self._server_chats.get(chat_id, {})
            raw = raw if isinstance(raw, dict) else {}
            existing = None
            if chat.chat_type == 'DIALOG' or raw.get('type') == 'DIALOG':
                if (chat.is_group or chat.is_channel or raw.get('type') != 'DIALOG'
                        or not _is_read_int(raw.get('id')) or raw['id'] != chat_id):
                    raise ValueError('Не подтверждён собеседник выбранного чата.')
                try:
                    peer = _peer(chat, raw, binding[0])
                except ConversationUnavailable as exc:
                    raise ValueError('Не подтверждён собеседник выбранного чата.') from exc
                if not _is_read_int(peer):
                    raise ValueError('Не подтверждён собеседник выбранного чата.')
                if peer == binding[0]:
                    raise ValueError('Название «Избранное» не изменяется.')
                existing = next((item for item in self.store.list_contacts()
                                 if type(item.id) is int and item.id == peer), None)
            elif not (chat.chat_type in {'CHAT', 'CHANNEL'} or chat.is_group or chat.is_channel):
                raise ValueError('Не подтверждён тип выбранного чата.')
            self._require_history_context(binding)
            if existing is not None:
                self.store.upsert_contact(Contact(
                    id=existing.id, name=name, phone=existing.phone,
                    avatar_url=existing.avatar_url, is_verified=existing.is_verified,
                    server_fields=existing.server_fields))
                self._invalidate_contacts()
            self._require_history_context(binding)
            self.store.rename_chat(chat_id, name)

        self._history_cache_write(binding, rename)

    # ───────────────────────── sessions ─────────────────────────

    @staticmethod
    def _sessions_from_frame(frame) -> list[dict]:
        """Current96 DTOs: no guessed identifiers or raw-text fallback."""
        d = frame.decoded
        if frame.cmd != 1 or not isinstance(d, dict) or not isinstance(d.get("sessions"), list):
            raise ValueError("MAX не вернул подтверждённый список устройств")
        if "marker" in d and not isinstance(d["marker"], str):
            raise ValueError("MAX вернул неверный курсор списка устройств")
        out = []
        for raw in d["sessions"]:
            if (not isinstance(raw, dict) or not _is_read_int(raw.get("time"))
                    or not isinstance(raw.get("client"), str)):
                raise ValueError("MAX вернул неподтверждённый формат устройства")
            item = {"client": raw["client"], "time": raw["time"]}
            for key in ("info", "location"):
                if key in raw:
                    if not isinstance(raw[key], str):
                        raise ValueError("MAX вернул неверные сведения об устройстве")
                    item[key] = raw[key]
            if "current" in raw:
                if type(raw["current"]) is not bool:
                    raise ValueError("MAX не подтвердил текущую сессию")
                item["current"] = raw["current"]
            client = _session_display_text(item["client"])
            info = _session_display_text(item.get("info"))
            item["label"] = " · ".join(part for part in (client, info) if part) or "Устройство"
            out.append(item)
        return out

    def list_sessions(self) -> list[dict]:
        """Список сессий: [{label, location, current, time}]."""
        return self._sessions_from_frame(self.client.sessions_info())

    def _remember_config(self, raw: dict, *, owner_id: Optional[int] = None) -> None:
        """Merge only supplied known privacy fields and safe server feature flags."""
        owner = owner_id if owner_id is not None else self._confirmed_profile_id()
        if not isinstance(raw, dict):
            raise ValueError("MAX вернул неверный формат настроек")
        supplied = {}
        if "user" in raw:
            supplied["user"] = user_setting_values(raw["user"])
        if 'chats' in raw:
            supplied['chats'] = chat_notification_values(raw['chats'])
        if "hash" in raw:
            if not isinstance(raw["hash"], str):
                raise ValueError("MAX вернул неверный хеш настроек")
            supplied["hash"] = raw["hash"]
        if "server" in raw:
            if not isinstance(raw["server"], dict):
                raise ValueError("MAX вернул неверные параметры сервера")
            flags = {}
            for key in ("sessions-end-disabled", "2fa-creation-disabled", 'p2p-calls-enabled', 'calls-fast-hangup',
                        'bots-mute-restriction', 'bot-complaint-enabled', 'delete-for-all'):
                if key in raw["server"]:
                    if type(raw["server"][key]) is not bool:
                        raise ValueError("MAX вернул неподтверждённый серверный флаг")
                    flags[key] = raw["server"][key]
            for key in ('calls-endpoint', 'calls-test-domain'):
                if key in raw['server']:
                    if not isinstance(raw['server'][key], str):
                        raise ValueError('MAX вернул неподтверждённую конфигурацию звонков')
                    flags[key] = raw['server'][key]
            from ..protocol.invite_link_contracts import invite_link_config
            from ..protocol.conversation_create_contracts import conversation_creation_limits, DEFAULT_LIMITS
            flags.update(invite_link_config(raw['server']))
            # Keep supplied limits, not defaults, so a later CREATE honours
            # actual CONFIG instead of silently falling back to shipped values.
            edit_keys = ('max-cname-length', 'max-description-length')
            limits = conversation_creation_limits({key: value for key, value in raw['server'].items()
                                                    if key not in edit_keys})
            flags.update({key: limits[key] for key in DEFAULT_LIMITS if key in raw['server'] and key not in edit_keys})
            for key in edit_keys:
                if key in raw['server']:
                    value = raw['server'][key]
                    # Preserve the shared CONFIG rejection contract used by
                    # creation: malformed server limits never install a partial
                    # snapshot. Null is the evidenced edit nullish fallback.
                    if value is not None and (type(value) is not int or not 1 <= value <= 100000):
                        raise ValueError('MAX вернул неверные ограничения создания и редактирования')
                    flags[key] = value
            if 'max-favorite-chats' in raw['server']:
                value = raw['server']['max-favorite-chats']
                # Preserve null for the confirmed WEB nullish default (5).
                # An invalid update must invalidate a previously valid value:
                # False is a bounded malformed marker, not a numeric limit.
                flags['max-favorite-chats'] = value if (value is None or
                    type(value) is int and 0 <= value <= 1000) else False
            supplied["server"] = flags
        if raw.get('experiments') is not None:
            if not isinstance(raw['experiments'], dict):
                raise ValueError('MAX вернул неподтверждённые эксперименты')
            supplied['experiments'] = {}
            for key in ('max-cname-length', 'max-description-length'):
                if key in raw['experiments']:
                    value = raw['experiments'][key]
                    # Keep nullish fallback and invalidate a previous valid
                    # experiment on malformed updates instead of retaining it.
                    supplied['experiments'][key] = value if (value is None or
                        type(value) is int and 1 <= value <= 100000) else False
        if supplied:
            self.store.merge_config(owner, supplied)
            callback = getattr(self, "on_config_changed", None)
            if callback is not None:
                try:
                    callback()
                except Exception:
                    self.log("Config UI notification failed")

    def get_privacy_settings(self, *, context=None) -> dict:
        """Supplied current-owner fields only; no implicit GET or auth request."""
        if context is not None:
            return self._history_cache_write(context, lambda: privacy_values(
                self.store.load_config(context[0]).get("user", {})))
        owner = self._confirmed_profile_id()
        return privacy_values(self.store.load_config(owner).get("user", {}))

    def update_privacy_settings(self, patch: dict, *, context=None) -> dict:
        """One explicit22 patch; accepted-without-config is pending readback."""
        if context is not None:
            self._require_history_context(context)
        owner = self._confirmed_profile_id()
        values = privacy_values(patch, writable=True)
        current = self.get_privacy_settings(context=context) if context is not None else self.get_privacy_settings()
        guarded = {"SEARCH_BY_PHONE", "INCOMING_CALL", "CHATS_INVITE", "CONTENT_LEVEL_ACCESS"}
        if guarded.intersection(values):
            if current.get("SAFE_MODE") is True or current.get("FAMILY_PROTECTION") == "MANAGEABLE":
                raise ValueError("Эта настройка управляется безопасным режимом или семейной защитой")
        if context is not None:
            client = self._require_history_context(context)
            result = client.update_user_settings(values, expected_session=context[2])
        else:
            result = self.client.update_user_settings(values)
        try:
            if context is not None:
                self._require_history_context(context)
            current_owner = self._confirmed_profile_id()
        except ValueError:
            current_owner = None
        if current_owner != owner:
            raise MaxWriteOutcomeUnknown("Аккаунт изменился; результат изменения настроек не подтверждён")
        if result is not None:
            try:
                if context is not None:
                    self._history_cache_write(context, lambda: self._remember_config(result, owner_id=owner))
                else:
                    self._remember_config(result, owner_id=owner)
            except Exception:
                raise MaxWriteOutcomeUnknown("MAX не вернул подтверждённое состояние настроек") from None
        try:
            settings = self.get_privacy_settings(context=context) if context is not None else self.get_privacy_settings()
        except Exception:
            raise MaxWriteOutcomeUnknown("Не удалось прочитать подтверждённое состояние настроек") from None
        returned = result.get("user", {}) if isinstance(result, dict) else {}
        pending = any(key not in returned or returned[key] != value for key, value in values.items())
        return {"accepted": True, "pending": pending, "settings": settings}

    def list_call_history(self, sync_cursor: Optional[int] = None) -> dict:
        """Read the documented call-log DTO; never initiate or accept a call."""
        result = self.client.call_history(sync_cursor=sync_cursor)
        if not isinstance(result, dict) or not isinstance(result.get("callHistoryItems"), list):
            raise ValueError("MAX не вернул журнал звонков")
        items = []
        for raw in result["callHistoryItems"]:
            if (not isinstance(raw, dict)
                    or any(type(raw.get(key)) is not int for key in ("historyId", "callerId", "chatId"))
                    or not isinstance(raw.get("callId"), str)
                    or raw.get("callType") not in {"AUDIO", "VIDEO"}
                    or _message_time(raw) is None):
                raise ValueError("MAX вернул неподтверждённый формат записи звонка")
            item = {key: raw[key] for key in ("historyId", "callId", "callerId", "chatId", "callType", "time")}
            for key in ("callName", "joinLink"):
                if key in raw:
                    if not isinstance(raw[key], str):
                        raise ValueError("MAX вернул неверное текстовое поле журнала звонков")
                    item[key] = raw[key]
            if "messageId" in raw:
                if type(raw["messageId"]) is not int:
                    raise ValueError("MAX вернул неверный идентификатор сообщения звонка")
                item["messageId"] = raw["messageId"]
            if "hangupType" in raw:
                if raw["hangupType"] not in {"CANCELED", "REJECTED", "HUNGUP", "MISSED"}:
                    raise ValueError("MAX вернул неподтверждённый статус звонка")
                item["hangupType"] = raw["hangupType"]
            if "groupCallType" in raw:
                if type(raw["groupCallType"]) is not int or raw["groupCallType"] not in (0, 1):
                    raise ValueError("MAX вернул неподтверждённый тип группового звонка")
                item["groupCallType"] = raw["groupCallType"]
            if "durationMs" in raw:
                duration = raw["durationMs"]
                if (isinstance(duration, bool) or not isinstance(duration, (int, float))
                        or not math.isfinite(duration) or duration < 0):
                    raise ValueError("MAX вернул неподтверждённую длительность звонка")
                item["durationMs"] = duration
            items.append(item)
        output = {"callHistoryItems": items}
        if "callHistorySync" in result:
            if type(result["callHistorySync"]) is not int:
                raise ValueError("MAX вернул неверный курсор журнала звонков")
            output["callHistorySync"] = result["callHistorySync"]
        if "reset" in result:
            if type(result["reset"]) is not bool:
                raise ValueError("MAX вернул неверный признак обновления журнала звонков")
            output["reset"] = result["reset"]
        return output

    def calls_available(self, chat):
        """Local readiness only: this never fetches a call token or starts media."""
        from .call_backend import MaxCallBackend
        if (chat is None or chat.chat_type != 'DIALOG' or type(chat.peer_user_id) is not int
                or chat.peer_user_id == self.my_id):
            return False
        try:
            MaxCallBackend(self).config()
            return True
        except Exception:
            return False

    def get_folders(self, folder_sync: Optional[int] = None) -> dict:
        """Read list metadata and resolve missing ordered folder DTOs once.

        Omitted delta fields stay omitted. `orderedFolders` mirrors the actual
        foldersOrder and uses None for an unavailable detail, never a fake folder.
        Folder favorites remain folder-local ordered IDs, not global pin flags.
        """
        owner = self._confirmed_profile_id()
        result = self.client.get_folders(folder_sync=folder_sync)
        if not isinstance(result, dict) or not _is_read_int(result.get("folderSync")):
            raise ValueError("MAX не вернул подтверждённый список папок")
        output = {"folderSync": result["folderSync"]}
        folders = []
        if "folders" in result:
            if not isinstance(result["folders"], list):
                raise ValueError("MAX вернул неверный формат папок")
            folders = [_folder_from_data(folder) for folder in result["folders"]]
            output["folders"] = folders
        for key in ("foldersOrder", "allFilterExcludeFolders"):
            if key in result:
                if (not isinstance(result[key], list)
                        or any(not isinstance(identity, str) for identity in result[key])):
                    raise ValueError("MAX вернул неверный порядок папок")
                output[key] = list(result[key])
        if "foldersOrder" in output:
            by_id = {folder["id"]: folder for folder in folders}
            missing = list(dict.fromkeys(identity for identity in output["foldersOrder"] if identity not in by_id))
            if missing:
                details = self.client.get_folder_details(missing)
                if not isinstance(details, dict) or not isinstance(details.get("folders"), list):
                    raise ValueError("MAX не вернул запрошенные сведения о папках")
                resolved = [_folder_from_data(folder) for folder in details["folders"]]
                if any(folder["id"] not in missing for folder in resolved):
                    raise ValueError("MAX вернул сведения о другой папке")
                for folder in resolved:
                    by_id[folder["id"]] = folder
                # Keep the list DTO's order, appending only actually returned details.
                output["folders"] = folders + resolved
            output["orderedFolders"] = [by_id.get(identity) for identity in output["foldersOrder"]]
            output["missingFolderIds"] = [identity for identity in missing if identity not in by_id]
        if self._confirmed_profile_id() != owner:
            raise ValueError("Аккаунт изменился во время чтения папок")
        self._reconcile_folder_snapshot(owner, output)
        if hasattr(self, '_pending_folder_write'):
            pending = self._folder_pending(owner)
            output['writePending'] = pending is not None
            if pending and pending['kind'] == 'upsert':
                output['pendingFolderId'] = pending['expected']['id']
        return output

    def get_folder_view(self, folder, chats=None):
        """Pure projection of confirmed metadata; no background RPC from the UI."""
        from .folder_view import build_folder_view
        owner = self._confirmed_profile_id()
        if self.store.cache_owner != owner:
            raise ValueError('Кэш чатов принадлежит другой сессии')
        with self._metadata_lock:
            raw_chats = copy.deepcopy(self._server_chats)
            contacts = copy.deepcopy(self._server_contacts)
        config = self.store.load_config(owner)
        view = build_folder_view(folder, self.list_chats() if chats is None else chats,
                                 raw_chats=raw_chats, contacts=contacts, viewer_id=owner,
                                 chat_settings=config.get('chats'), now_ms=int(time.time() * 1000))
        if self._confirmed_profile_id() != owner or self.store.cache_owner != owner:
            raise ValueError('Аккаунт изменился во время чтения папки')
        return view

    def list_own_stories(self, marker: int = 0, count: int = 15) -> dict:
        """Read one own archive page without playback, view marks or writes.

        The supplied/returned marker is a timestamp. expiration remains its raw
        server integer because the verified source does not establish its units.
        """
        owner = self._confirmed_profile_id()
        result = self.client.story_archive(owner, owner_type=0, marker=marker, count=count)
        if not isinstance(result, dict) or not isinstance(result.get("stories"), list):
            raise ValueError("MAX не вернул архив историй")
        output = {"stories": [_story_from_data(story, owner) for story in result["stories"]]}
        if "marker" in result:
            if not _is_read_int(result["marker"]):
                raise ValueError("MAX вернул неверный курсор архива историй")
            output["marker"] = result["marker"]
        if self._confirmed_profile_id() != owner:
            raise ValueError("Аккаунт изменился во время чтения историй")
        return output

    def _confirmed_profile_id(self) -> int:
        owner = self.my_id
        with self._metadata_lock:
            confirmed = self._server_metadata_owner
        if not _is_read_int(owner) or confirmed != owner:
            raise ValueError("Сначала войдите в подтверждённый профиль MAX")
        return owner

    def can_terminate_other_sessions(self) -> bool:
        owner = self._confirmed_profile_id()
        return self.store.load_config(owner).get("server", {}).get("sessions-end-disabled") is not True

    def terminate_other_sessions(self) -> bool:
        """Explicit current-source close-all-others, never selected time/IDs."""
        owner = self._confirmed_profile_id()
        if self.store.load_config(owner).get("server", {}).get("sessions-end-disabled") is True:
            raise ValueError("MAX временно запретил завершение других сессий")
        result = self.client.close_other_sessions()
        if not isinstance(result, dict) or not isinstance(result.get("token"), str) or not result["token"]:
            raise MaxWriteOutcomeUnknown("MAX не подтвердил завершение других сессий")
        try:
            current_owner = self._confirmed_profile_id()
        except ValueError:
            current_owner = None
        if current_owner != owner:
            raise MaxWriteOutcomeUnknown("Аккаунт изменился; результат завершения сессий не подтверждён")
        # The actual WEB consumer does not assign the opaque97 ACK token to auth.
        return True

    # ───────────────────────── 2FA ─────────────────────────

    TWOFA_COOLDOWN = 30.0  # сек между 2FA-операциями (анти error.user.restricted.set_2fa)

    def _guard_2fa(self) -> None:
        """Не дать частить 2FA-операциями — именно серия и блокирует номер."""
        wait = self._twofa_cooldown_until - time.time()
        if wait > 0:
            raise ValueError(
                f"Подождите {int(wait) + 1} c перед следующей операцией 2FA — "
                "частые попытки временно блокируют 2FA на номере."
            )

    def _arm_2fa_cooldown(self, seconds: Optional[float] = None) -> None:
        # seconds — серверный blockingDuration (если пришёл), иначе наш минимум.
        self._twofa_cooldown_until = time.time() + max(self.TWOFA_COOLDOWN, seconds or 0.0)

    def _begin_2fa(self) -> None:
        """Атомарно проверить кулдаун и взвести его. Под локом — иначе два
        параллельных вызова (run_async кладёт в QThreadPool, maxThreadCount>1)
        проскочат check-then-set гонкой и оба уйдут в сеть подряд."""
        with self._twofa_lock:
            self._guard_2fa()
            self._arm_2fa_cooldown()

    def _respect_blocking(self, frame) -> None:
        """Если сервер вернул blockingDuration (сек) в ответе на 2FA-операцию —
        выдержать ИМЕННО его (при error.user.restricted.set_2fa он длиннее нашего
        минимума). Раньше учитывался только для recovery-email."""
        if isinstance(frame.decoded, dict):
            blocking = _to_int(frame.decoded.get("blockingDuration"))
            if blocking:
                self._arm_2fa_cooldown(float(blocking))

    def get_2fa_status(self, *, context=None) -> dict:
        """Состояние 2FA: {enabled:bool, email:str|None, hint:str|None}.
        AUTH_2FA_DETAILS(104) -> {password:{enabled, hint, email}}."""
        if context is not None:
            client = self._require_history_context(context)
            frame = client.auth_2fa_details(expected_session=context[2])
            self._require_history_context(context)
        else:
            frame = self.client.auth_2fa_details()
        if not frame.ok:
            raise ValueError(frame.error_text() or "MAX не подтвердил состояние 2FA")
        return _parse_2fa_details(frame)

    def _twofa_operation_context(self, context):
        binding = self._history_read_context() if context is None else context
        self._require_history_context(binding)
        return binding

    def _twofa_call(self, context, method, *args, **kwargs):
        """Keep every step on the captured transport; never retry on a new session."""
        client = self._require_history_context(context)
        try:
            result = getattr(client, method)(*args, expected_session=context[2], **kwargs)
        except (MaxTimeout, MaxNotConnected):
            raise MaxWriteOutcomeUnknown(
                "Результат операции безопасности не подтверждён. Повторный запрос не отправлен."
            ) from None
        try:
            self._require_history_context(context)
        except ValueError:
            raise MaxWriteOutcomeUnknown(
                "Сессия изменилась во время операции безопасности. Результат не подтверждён."
            ) from None
        return result

    def enable_2fa(self, password: str, hint: Optional[str] = None, *, context=None) -> None:
        """Включить 2FA, когда он выключен — только по токену, без старого пароля.
        CREATE_TRACK(112) -> SET_2FA(111, expectedCapabilities:[0] = SET_PASSWORD)."""
        if not password:
            raise ValueError("Введите пароль 2FA")
        context = self._twofa_operation_context(context)
        self._begin_2fa()
        track_id = self._twofa_call(context, 'auth_create_track', 0)
        # capability=0 (SET_PASSWORD) — включение с нуля. [1] (UPDATE_PASSWORD)
        # сервер отвергает на выключенном 2FA (error password.is.off).
        result = self._twofa_call(context, 'auth_set_2fa', track_id, password, hint, capability=0)
        if not result.ok:
            self._respect_blocking(result)
            raise ValueError(result.error_text() or "Не удалось включить 2FA")

    def change_2fa(self, new_password: str, hint: Optional[str] = None, *,
                   current_password: Optional[str] = None, context=None) -> None:
        """Ordinary password change: verify current password, then commit once."""
        if not new_password:
            raise ValueError("Введите новый пароль 2FA")
        if not current_password:
            raise ValueError("Введите текущий пароль 2FA")
        context = self._twofa_operation_context(context)
        self._begin_2fa()
        track_id = self._twofa_call(context, 'auth_create_track', 0)
        track_id = self._checked_password_track(track_id, current_password, context=context)
        result = self._twofa_call(context, 'auth_set_2fa', track_id, new_password, hint, capability=1)
        if not result.ok:
            self._respect_blocking(result)
            raise ValueError(result.error_text() or "Не удалось изменить пароль 2FA")

    def _checked_password_track(self, track_id: str, current_password: str, *, context=None) -> str:
        context = self._twofa_operation_context(context)
        checked = self._twofa_call(context, 'auth_check_password', track_id, current_password)
        if not checked.ok:
            self._respect_blocking(checked)
            raise ValueError(checked.error_text() or "Текущий пароль 2FA не подтверждён")
        confirmed = checked.decoded.get("trackId") if isinstance(checked.decoded, dict) else None
        if not isinstance(confirmed, str) or not confirmed.strip():
            raise ValueError("MAX не вернул подтверждённый трек проверки пароля")
        return confirmed

    def start_set_recovery_email(self, email: str, *,
                                 current_password: Optional[str] = None, context=None) -> str:
        """Verify the current password before one ordinary EMAIL_START request."""
        email = (email or "").strip()
        if "@" not in email or "." not in email:
            raise ValueError("Введите корректный email")
        if not current_password:
            raise ValueError("Введите текущий пароль 2FA")
        context = self._twofa_operation_context(context)
        self._begin_2fa()
        track_id = self._twofa_call(context, 'auth_create_track', 0)
        track_id = self._checked_password_track(track_id, current_password, context=context)
        res = self._twofa_call(context, 'auth_verify_email', track_id, email)
        if not res.ok:
            self.log(f"recovery-email: VERIFY_EMAIL non-OK cmd={res.cmd}")
            raise ValueError(res.error_text() or "Не удалось отправить код на email")
        # blockingDuration (сек) — серверный кулдаун до повторной отправки кода.
        if isinstance(res.decoded, dict):
            blocking = _to_int(res.decoded.get("blockingDuration"))
            if blocking:
                self._arm_2fa_cooldown(float(blocking))
        confirmed = res.decoded.get("trackId") if isinstance(res.decoded, dict) else None
        if not isinstance(confirmed, str) or not confirmed.strip():
            raise ValueError("MAX не вернул трек подтверждения email; повторный запрос не выполнен")
        return confirmed

    def confirm_recovery_email(self, track_id: str, code: str, *, context=None) -> None:
        """Шаг 2: подтвердить код из письма (CHECK_EMAIL 110)."""
        code = (code or "").strip()
        if not code:
            raise ValueError("Введите код из письма")
        context = self._twofa_operation_context(context)
        res = self._twofa_call(context, 'auth_check_email', track_id, code)
        if not res.ok:
            raise ValueError(res.error_text() or "Неверный код подтверждения")

    # ───────────────────────── push ─────────────────────────

    def is_current_incoming_call(self, notice) -> bool:
        """Recheck at GUI consumption and again before any explicit SDK action."""
        if not isinstance(notice, IncomingCallNotice) or notice.source_client is not self.client:
            return False
        context = getattr(self.client, 'authenticated_session', None)
        if (context is None or context is not notice.session_context or
                context.device_type != 'WEB' or context.owner_id != notice.owner_id or
                self.client.state != ConnectionState.CONNECTED or
                self.client.device_id != context.device_id or
                self.session.device_id != context.device_id or
                notice.caller_id == notice.owner_id):
            return False
        try:
            if (self._confirmed_profile_id() != notice.owner_id or
                    self.store.cache_owner != notice.owner_id):
                return False
            config = self.store.load_config(notice.owner_id)
            with self._metadata_lock:
                caller = self._server_contacts.get(notice.caller_id)
                if isinstance(caller, dict) and _has_contact_flag(caller, 'BOT'):
                    return False
            return (config.get('server', {}).get('p2p-calls-enabled') is True and
                    notice.source_client is self.client and
                    getattr(self.client, 'authenticated_session', None) is context and
                    self.session.device_id == context.device_id and
                    self._confirmed_profile_id() == notice.owner_id and
                    self.store.cache_owner == notice.owner_id)
        except Exception:
            return False

    def _dispatch_incoming_call(self, frame):
        callback = self.on_incoming_call
        if callback is None:
            return
        notice = parse_incoming_call(frame.decoded, source_client=self.client,
                                     session_context=frame.auth_context)
        if not self.is_current_incoming_call(notice):
            return
        # Reserve before emitting to Qt so concurrent duplicate pushes cannot
        # create multiple dialogs. No credential is persisted or logged here.
        with self._incoming_call_lock:
            if not self.is_current_incoming_call(notice):
                return
            if self._incoming_call_context is not notice.session_context:
                self._incoming_call_context = notice.session_context
                self._incoming_call_seen.clear()
                self._incoming_call_order.clear()
            if notice.conversation_id in self._incoming_call_seen:
                return
            self._incoming_call_seen.add(notice.conversation_id)
            self._incoming_call_order.append(notice.conversation_id)
            if len(self._incoming_call_order) > 2048:
                self._incoming_call_seen.discard(self._incoming_call_order.popleft())
        callback(notice)

    def _clear_incoming_calls(self):
        with self._incoming_call_lock:
            self._incoming_call_context = None
            self._incoming_call_seen.clear()
            self._incoming_call_order.clear()

    def _on_push(self, frame: MaxFrame) -> None:
        if frame.cmd != 0 or not isinstance(frame.decoded, dict):
            return
        if frame.opcode == 137:
            self._dispatch_incoming_call(frame)
            return
        try:
            binding = self._history_read_context()
            if frame.auth_context is not binding[2]:
                return
        except ValueError:
            return
        try:
            delivery, changed, message_change = self._history_cache_write(
                binding, lambda: self._apply_account_push(frame, binding))
            self._require_history_context(binding)
        except ValueError:
            try:
                self._require_history_context(binding)
            except ValueError:
                return
            raise  # Preserve validation errors from the current account's DTO.
        if frame.opcode == 128:
            self._schedule_chat_identity(binding, changed)
        elif frame.opcode == 135:
            self._schedule_chat_identity(binding, changed, tracked_only=True)
        # Queued GUI delivery rechecks this same context. A final producer
        # check alone cannot prevent a switch after emitting a Qt signal.
        if delivery is not None and self.on_message:
            self.on_message(delivery)
        callback = getattr(self, 'on_message_changed', None)
        if message_change is not None and callback is not None and self.is_current_message_change(message_change):
            callback(message_change)
        if self.on_chat_changed:
            for identity in changed:
                self.on_chat_changed(identity)

    def is_current_message_event(self, delivery) -> bool:
        """Accept one local delivery only while its confirmed LOGIN owns the UI."""
        from ..protocol.client import AuthenticatedSession
        if (not isinstance(delivery, MessageDelivery)
                or not isinstance(delivery.message, Message)
                or not isinstance(delivery.session_context, AuthenticatedSession)
                or type(delivery.owner_id) is not int
                or delivery.session_context.owner_id != delivery.owner_id):
            return False
        try:
            self._require_history_context((delivery.owner_id, delivery.source_client,
                                           delivery.session_context))
            return True
        except (ValueError, AttributeError):
            return False

    def is_current_message_change(self, event) -> bool:
        """Validate one body-free invalidation against its exact LOGIN context."""
        from ..protocol.client import AuthenticatedSession
        if (not isinstance(event, MessageChange) or not _is_read_int(event.owner_id)
                or not isinstance(event.session_context, AuthenticatedSession)
                or event.session_context.owner_id != event.owner_id
                or not _is_read_int(event.chat_id) or type(event.message_ids) is not tuple
                or not event.message_ids or any(not _is_read_int(identity) for identity in event.message_ids)
                or len(set(event.message_ids)) != len(event.message_ids)):
            return False
        try:
            self._require_history_context((event.owner_id, event.source_client, event.session_context))
            return True
        except (ValueError, AttributeError):
            return False

    def cached_messages_for_change(self, event) -> dict[int, Optional[Message]]:
        """Read exact IDs atomically; None means that confirmed ID is absent."""
        if not self.is_current_message_change(event):
            raise ValueError('Сессия события изменения сообщения уже не действует')
        binding = (event.owner_id, event.source_client, event.session_context)
        return self._history_cache_write(binding, lambda: {
            identity: self.store.message(event.chat_id, identity) for identity in event.message_ids})

    def _inline_message_chat(self, data, message, binding, previous):
        from .conversation_info_service import _peer, ConversationUnavailable
        raw = data.get('chat')
        if not self._valid_chat_identity_payload(raw) or raw['id'] != message.chat_id:
            return False
        if (previous is not None and previous.update_time_ms is not None
                and (message.update_time_ms is None or message.update_time_ms < previous.update_time_ms)):
            return False
        merged = copy.deepcopy(self._server_chats.get(message.chat_id, {}))
        merged.update(copy.deepcopy(raw))
        if ('type' in merged and merged['type'] not in ('DIALOG', 'CHAT', 'CHANNEL')
                or any(key in raw and not isinstance(raw[key], str) for key in ('title', 'name'))):
            return False
        cached = self.store.get_chat(message.chat_id)
        if merged.get('type') == 'DIALOG':
            try:
                _peer(cached, merged, binding[0])
            except ConversationUnavailable:
                return False
        fresh_count = (previous is None and (cached is None or cached.last_time_ms is None
                                            or message.time_ms >= cached.last_time_ms))
        known_mark = _own_read_mark(self._server_chats.get(message.chat_id, {}), binding[0])
        supplied_mark = _own_read_mark(merged, binding[0])
        # Same rule as _remember_chat_info: a snapshot older than our confirmed
        # read marker carries a count from before that read. It is not
        # authoritative, so this delivery is counted by the ordinary increment.
        stale_read = (known_mark is not None and supplied_mark is not None
                      and supplied_mark < known_mark and merged.get('markedAsUnread') is not True)
        authoritative_count = (fresh_count and not stale_read
                               and type(raw.get('newMessages')) is int and raw['newMessages'] >= 0)
        if not authoritative_count:
            if cached is not None:
                merged['newMessages'] = cached.unread
            else:
                merged.pop('newMessages', None)
        self._remember_chat_info({'chats': [merged]}, my_id=binding[0])
        return authoritative_count

    @staticmethod
    def _valid_chat_identity_payload(raw):
        from .conversation_info_service import _participant_id
        if (not isinstance(raw, dict) or not _is_read_int(raw.get('id'))
                or 'type' in raw and raw['type'] not in ('DIALOG', 'CHAT', 'CHANNEL')
                or any(key in raw and not isinstance(raw[key], str) for key in ('title', 'name'))
                or 'owner' in raw and not _is_read_int(raw['owner'])
                or 'lastMessage' in raw and not isinstance(raw['lastMessage'], dict)):
            return False
        if 'participants' in raw:
            values = raw['participants']
            if not isinstance(values, dict) or any(_participant_id(key) is None
                    or _message_time({'time': value}) is None for key, value in values.items()):
                return False
        return True

    def _apply_account_push(self, frame, binding):
        """Mutate account DTOs only inside metadata/store owner scope; no RPC."""
        data = frame.decoded
        if frame.opcode == 134:
            if not isinstance(data.get("config"), dict):
                raise ValueError("MAX не вернул подтверждённые настройки в уведомлении")
            self._remember_config(data["config"], owner_id=binding[0])
            return None, (), None
        if frame.opcode == 131 and isinstance(data.get("contact"), dict):
            self._remember_contact_info({"contact": data["contact"]})
            identity = _profile_from_data(data["contact"]).id
            return None, tuple(chat.id for chat in self.store.list_chats()
                               if chat.peer_user_id == identity), None
        if frame.opcode == 135 and isinstance(data.get("chat"), dict):
            self._remember_chat_info({"chats": [data["chat"]]}, my_id=binding[0])
            identity = _strict_id(data["chat"].get("id"))
            return None, (identity,) if identity is not None else (), None
        if frame.opcode == 130:
            chat_id, user_id = _strict_id(data.get("chatId")), _strict_id(data.get("userId"))
            if (chat_id is not None and user_id == binding[0]
                    and ('unread' not in data or data['unread'] is not None)
                    and self._apply_read_receipt(chat_id, binding[0], data.get('mark'),
                        data.get('unread'), set_as_unread=data.get('setAsUnread', False))):
                return None, (chat_id,), None
            return None, (), None
        if frame.opcode == 155:
            chat_id, message_id, total = data.get('chatId'), data.get('messageId'), data.get('totalCount')
            if ('postId' in data or not all(_is_read_int(value) for value in (chat_id, message_id))
                    or type(total) is not int or not 0 <= total < 2**31):
                return None, (), None
            counters = data.get('counters', [])
            if (not isinstance(counters, list) or any(not isinstance(item, dict)
                    or not isinstance(item.get('reaction'), str)
                    or type(item.get('count', 0)) is not int
                    or not 0 <= item.get('count', 0) < 2**31 for item in counters)):
                return None, (), None
            previous = self.store.message(chat_id, message_id)
            if previous is None:
                return None, (chat_id,), None
            snapshot = {'totalCount': total, 'counters': [
                {'reaction': item['reaction'], 'count': item.get('count', 0)} for item in counters]}
            if isinstance(previous.reaction_info.get('yourReaction'), str):
                snapshot['yourReaction'] = previous.reaction_info['yourReaction']
            self.store.update_message_reactions(chat_id, message_id, snapshot, replace=True)
            current = self.store.message(chat_id, message_id)
            change = (MessageChange(*binding, chat_id, (message_id,)) if current != previous else None)
            return None, (chat_id,), change
        if frame.opcode == 156:
            chat_id, message_id = data.get('chatId'), data.get('messageId')
            if ('postId' in data or not all(_is_read_int(value) for value in (chat_id, message_id))):
                return None, (), None
            info = data.get('reactionInfo', {})
            if not isinstance(info, dict):
                return None, (), None
            counters = info.get('counters', [])
            if (not isinstance(counters, list) or any(not isinstance(item, dict)
                    or not isinstance(item.get('reaction'), str)
                    or type(item.get('count', 0)) is not int
                    or not 0 <= item.get('count', 0) < 2**31 for item in counters)
                    or 'totalCount' in info and (type(info['totalCount']) is not int
                                                or not 0 <= info['totalCount'] < 2**31)
                    or 'yourReaction' in info and not isinstance(info['yourReaction'], str)):
                return None, (), None
            previous = self.store.message(chat_id, message_id)
            if previous is None:
                return None, (chat_id,), None
            snapshot = {key: info[key] for key in ('totalCount', 'yourReaction') if key in info}
            if 'counters' in info:
                snapshot['counters'] = [{'reaction': item['reaction'], 'count': item.get('count', 0)}
                                        for item in counters]
            self.store.update_message_reactions(chat_id, message_id, snapshot, replace=True)
            current = self.store.message(chat_id, message_id)
            change = (MessageChange(*binding, chat_id, (message_id,)) if current != previous else None)
            return None, (chat_id,), change
        if frame.opcode == 142 and isinstance(data.get("chat"), dict):
            chat_id = data['chat'].get('id')
            message_ids = data.get("messageIds")
            if ('postId' not in data and _is_read_int(chat_id) and type(data.get('ttl')) is bool
                    and isinstance(message_ids, list) and all(_is_read_int(identity) for identity in message_ids)):
                identities = tuple(dict.fromkeys(message_ids))
                self._remember_deleted_messages(binding[0], chat_id, identities)
                deletion_chat = copy.deepcopy(data['chat'])
                current_last = self._server_chats.get(chat_id, {}).get('lastMessage')
                if ('lastMessage' not in deletion_chat and isinstance(current_last, dict)
                        and _message_time(current_last) is not None):
                    # A deletion snapshot may precede an unrelated new delivery.
                    # Only IDs in this event were removed by the helper above.
                    deletion_chat['lastMessage'] = copy.deepcopy(current_last)
                self._remember_chat_info({'chats': [deletion_chat]}, my_id=binding[0],
                    message_clock_ids=() if 'lastMessage' in deletion_chat else (chat_id,))
                change = MessageChange(*binding, chat_id, identities) if identities else None
                return None, (chat_id,), change
            return None, (), None
        msg = self._parse_push(frame)
        if msg is None:
            return None, (), None
        if (self.store.history_was_cleared(msg.chat_id, msg.time_ms)
                or self.store.message_was_deleted(msg.chat_id, msg.id)):
            return None, (), None
        previous = self.store.message(msg.chat_id, msg.id)
        if previous is None and data['message'].get('status') == 'REMOVED':
            return None, (), None  # A removed message is never a new visible arrival.
        cached_chat = self.store.get_chat(msg.chat_id)
        fresh_count = (previous is None and (cached_chat is None or cached_chat.last_time_ms is None
                                            or msg.time_ms >= cached_chat.last_time_ms))
        inline_count = self._inline_message_chat(data, msg, binding, previous)
        self.store.ensure_chat(msg.chat_id)
        if fresh_count and type(data.get('unread')) is int and data['unread'] >= 0:
            self.store.set_unread(msg.chat_id, data['unread'])
            inline_count = True
        inserted = self.store.insert_message(msg)
        cached = msg if inserted else self.store.apply_message_ack(
            msg.chat_id, data["message"], my_id=binding[0],
            outgoing=msg.outgoing if 'sender' in data['message'] else None, replay=True)
        if (cached.update_time_ms is None or msg.update_time_ms is not None
                and msg.update_time_ms >= cached.update_time_ms):
            self._record_server_last_message(msg.chat_id, data['message'], replay=True)
        if cached.status == 'removed':
            # A server-removed row is never the chat preview; a preview that
            # still shows it moves to the latest visible message.
            self.store.repair_preview_after_removal(msg.chat_id, cached.time_ms)
        else:
            self.store.update_chat_preview(
                msg.chat_id, cached.time_ms, cached.preview,
                inc_unread=int(inserted and not cached.outgoing and not inline_count), monotonic=True)
        if msg.chat_id in self._server_chats:
            # Folder badges consume the canonical projection, not only SQL.
            self._server_chats[msg.chat_id]['newMessages'] = self.store.get_chat(msg.chat_id).unread
        # The SQL (chat,id) identity survives history loads and reconnects. A
        # repeated DTO can reconcile server fields/status, but is not a new alert.
        delivery = (MessageDelivery(binding[0], binding[1], binding[2], copy.deepcopy(cached))
                    if inserted else None)
        change = (MessageChange(*binding, cached.chat_id, (cached.id,))
                  if not inserted and previous != cached else None)
        return delivery, (cached.chat_id,), change

    def _parse_push(self, frame: MaxFrame) -> Optional[Message]:
        # Only the confirmed NOTIF_MESSAGE DTO may create a message. Binary
        # marker scans cannot establish field boundaries or event identity.
        d = frame.decoded
        if frame.cmd != 0 or frame.opcode != 128 or not isinstance(d, dict) or 'postId' in d:
            return None
        m = d.get('message')
        if not isinstance(m, dict):
            return None
        chat_id, msg_id = d.get('chatId'), m.get('id')
        sender, time_ms = m.get('sender'), _message_time(m)
        update_time_ms = _message_time({'time': m.get('updateTime')})
        if (not all(_is_read_int(value) for value in (chat_id, msg_id)) or time_ms is None
                or m.get('type') not in ('USER', 'CHANNEL', 'CHANNEL_ADMIN')
                or ('sender' in m and not _is_read_int(sender))
                or ('updateTime' in m and update_time_ms is None)):
            return None
        text, attaches = m.get('text', ''), m.get('attaches', [])
        if (not isinstance(text, str) or not isinstance(attaches, list)
                or any(not isinstance(attach, dict) for attach in attaches)):
            return None
        parsed = Message.from_server(chat_id, m, self.my_id)
        if not parsed.has_display_content:
            return None

        return Message(
            chat_id=chat_id,
            id=msg_id,
            sender=sender,
            text=text,
            time_ms=time_ms,
            outgoing=(self.my_id is not None and sender == self.my_id),
            attaches=server_attaches(m),
            reaction_info=parse_reaction_info(m.get('reactionInfo')),
            link=parse_message_link(m.get('link')),
            update_time_ms=update_time_ms,
            forwarded=parsed.forwarded,
        )

    def _on_reauthenticated(self, source_client, frame, context):
        """Consume reconnect LOGIN; no extra LOGIN or synchronous metadata RPC."""
        from ..protocol.client import AuthenticatedSession
        if (source_client is not self.client or not isinstance(context, AuthenticatedSession)
                or frame.cmd != 1 or frame.opcode != 19 or not isinstance(frame.decoded, dict)
                or _profile_from_data(frame.decoded.get("profile"), own=True).id != context.owner_id):
            raise ValueError("Не подтверждено состояние аккаунта после восстановления")
        binding = (context.owner_id, source_client, context)
        self._require_history_context(binding)
        def ingest():
            self._ingest_login(frame, my_id=context.owner_id)
            self._chat_fetch_ts.clear()
            self._chat_fetch_anchor.clear()
        self._history_cache_write(binding, ingest)
        self._require_history_context(binding)
        ids = self._history_cache_write(binding, lambda: [chat.id for chat in self.store.list_chats()])
        self._schedule_chat_identity(binding, ids)
        if self.on_chat_changed:
            for chat in _chat_entries({"chats": frame.decoded.get("chats")}):
                identity = _strict_id(chat.get("id"))
                if identity is not None:
                    self.on_chat_changed(identity)
        self._on_state(ConnectionState.CONNECTED)
        return True

    def _on_state(self, state: ConnectionState) -> None:
        if state != ConnectionState.CONNECTED:
            self._clear_incoming_calls()
            worker = getattr(self, '_chat_metadata_hydration', None)
            if worker is not None:
                worker.invalidate()
        elif getattr(self.client, "authenticated_session", None) is None:
            return  # INIT opened a transport; reconnect LOGIN/cache is not confirmed yet.
        if self.on_state_changed:
            self.on_state_changed(state)

    def _on_auth_invalid(self) -> None:
        """Report an ended or rejected session without deleting the saved credential."""
        self._clear_incoming_calls()
        worker = getattr(self, '_chat_metadata_hydration', None)
        if worker is not None:
            worker.invalidate()
        ended = getattr(self.client, 'auth_end_reason', None) == 'server_logout'
        code = getattr(self.client, 'auth_end_code', None)
        error = MaxLoginFailed("Сервер MAX завершил сессию" if ended
                               else "Сервер MAX отклонил сохранённый токен")
        # server_logout: push LOGOUT 20; login_rejected: reconnect LOGIN refused.
        error.reason = 'server_logout' if ended else 'login_rejected'
        error.server_code = code if isinstance(code, str) else None
        self.last_restore_error = error
        self.client.close()
        if self.on_auth_invalid:
            self.on_auth_invalid()

    def shutdown(self) -> None:
        self._clear_incoming_calls()
        worker = getattr(self, '_chat_metadata_hydration', None)
        if worker is not None:
            worker.close()
        try:
            self.client.close()
        finally:
            self.store.close()


# ───────────────────────── module helpers ─────────────────────────

_EMAIL_RE = re.compile(rb"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")


def _login_cache_failure_stack(error: BaseException) -> str:
    """Static function/line locations only; never format exception or frame data."""
    locations = []
    current = error.__traceback__
    while current is not None and len(locations) < 8:
        name = current.tb_frame.f_code.co_name
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", name) is None:
            name = "function"
        locations.append(f"{name}:{current.tb_lineno}")
        current = current.tb_next
    return " > ".join(locations) or "unavailable"


def _session_display_text(value: Optional[str]) -> Optional[str]:
    """Omit only empty/null-only display parts; never modify the source DTO."""
    if value is None:
        return None
    text = value.strip()
    parts = text.split(" · ")
    meaningful = [part for part in parts if any(
        token.strip() and token.strip().casefold() != "null" for token in part.split(","))]
    if len(meaningful) == len(parts):
        return text
    return " · ".join(part.strip() for part in meaningful) or None


def _contact_display_name(value) -> Optional[str]:
    """Reuse the canonical structured-name reader; never stringify containers."""
    if isinstance(value, Contact):
        return _contact_display_name(value.name) or _clean_name(value.phone) or f'Контакт {value.id}'
    if isinstance(value, str):
        return _clean_name(value)
    if isinstance(value, list):
        return _person_name({'names': value})
    if isinstance(value, dict):
        return _person_name(value)
    return None


def _legacy_names_display_name(value) -> Optional[str]:
    """Recognize a small historical str(names) shape without evaluating code."""
    if type(value) is not str or len(value) > 4096 or not value.startswith('[{'):
        return None
    try:
        expression = ast.parse(value, mode='eval')
    except (SyntaxError, ValueError, RecursionError):
        return None
    body = expression.body
    if type(body) is not ast.List or not 1 <= len(body.elts) <= 16:
        return None
    records = []
    allowed = {'name', 'firstName', 'lastName', 'type'}
    for element in body.elts:
        if type(element) is not ast.Dict or not 1 <= len(element.keys) <= 4:
            return None
        record = {}
        for key, field in zip(element.keys, element.values):
            if (type(key) is not ast.Constant or type(key.value) is not str
                    or key.value not in allowed or key.value in record
                    or type(field) is not ast.Constant or type(field.value) is not str
                    or len(field.value) > 256):
                return None
            record[key.value] = field.value
        if ('type' in record and record['type'] not in {'ONEME', 'CUSTOM'}
                or not any(_clean_name(record.get(key)) for key in ('name', 'firstName', 'lastName'))):
            return None
        records.append(record)
    # Historical Python str(list-of-dicts), not arbitrary literal/JSON spelling.
    if str(records) != value:
        return None
    return _person_name({'names': records})


def _person_name(data: dict) -> Optional[str]:
    if not isinstance(data, dict):
        return None
    names = data.get("names")
    flags = data.get('flags')
    if (type(flags) is int and -(2 ** 31) <= flags < 2 ** 31 and flags & 512
            and isinstance(names, list)):
        # A confirmed added contact's unique CUSTOM record is its saved alias.
        # This display rule is deliberately narrower than reordering every
        # lookup/profile record. Own profile still selects explicit ONEME below.
        custom = [item for item in names if isinstance(item, dict) and item.get('type') == 'CUSTOM']
        if len(custom) == 1:
            saved_name = _person_name({key: custom[0].get(key) for key in ('firstName', 'lastName', 'name')})
            if saved_name:
                return saved_name
    direct = " ".join(value for key in ("firstName", "lastName")
                      if isinstance((value := data.get(key)), str) and value.strip())
    if _clean_name(direct):
        return _clean_name(direct)
    name = _clean_name(data.get('name'))
    if name:
        return name
    if isinstance(names, list):
        # Without confirmed ADDED membership and an unambiguous saved alias,
        # preserve the shipped contact getter's supplied record order.
        for item in names:
            if isinstance(item, str) and _clean_name(item):
                return _clean_name(item)
            if isinstance(item, dict):
                # Do not stringify dictionaries or concatenate alternate names.
                candidate = " ".join(value for key in ("firstName", "lastName")
                                     if isinstance((value := item.get(key)), str) and value.strip())
                if _clean_name(candidate):
                    return _clean_name(candidate)
                if isinstance(item.get("name"), str) and _clean_name(item["name"]):
                    return _clean_name(item["name"])
    return None


def _profile_from_data(data, *, own: bool = False) -> Profile:
    """Parse PROFILE or LOGIN.profile.contact from structured server data."""
    if not isinstance(data, dict):
        return Profile()
    if isinstance(data.get("profile"), dict):
        data = data["profile"]
    if isinstance(data.get("contact"), dict):
        data = data["contact"]
    identity = data.get("id")
    if isinstance(identity, bool) or not isinstance(identity, (int, str)):
        identity = None
    identity = _to_int(identity)
    phone = data.get("phone")
    if isinstance(phone, bool) or not isinstance(phone, (int, str)):
        phone = None
    first_name, last_name = _canonical_name_parts(data)
    name = _person_name(data)
    if own and (first_name is not None or last_name is not None):
        name = " ".join(value for value in (first_name, last_name) if isinstance(value, str) and value.strip())
    return Profile(id=identity, name=name,
                   phone=normalize_phone(str(phone)) if phone is not None else None,
                   first_name=first_name, last_name=last_name,
                   description=data.get("description") if isinstance(data.get("description"), str) else None,
                   avatar_url=_server_avatar_url(data),
                   photo_id=data.get("photoId") if type(data.get("photoId")) is int else None,
                   link=data.get("link") if isinstance(data.get("link"), str) else None)


def _canonical_name_parts(data: dict) -> tuple[Optional[str], Optional[str]]:
    """Use explicit ONEME name fields; never guess first/last from display text."""
    if any(isinstance(data.get(key), str) for key in ("firstName", "lastName")):
        return tuple(data.get(key) if isinstance(data.get(key), str) else None
                     for key in ("firstName", "lastName"))
    names = data.get("names")
    if isinstance(names, list):
        records = [item for item in names if isinstance(item, dict)]
        canonical = [item for item in records if item.get("type") == "ONEME"]
        unspecified = [item for item in records if "type" not in item]
        for record in canonical + unspecified:
            if any(isinstance(record.get(key), str) for key in ("firstName", "lastName")):
                return tuple(record.get(key) if isinstance(record.get(key), str) else None
                             for key in ("firstName", "lastName"))
    return None, None


def _parse_2fa_details(frame) -> dict:
    """Parse a confirmed structured status; missing fields do not mean disabled."""
    d = frame.decoded
    if isinstance(d, dict):
        p = d.get("password", d)
        if isinstance(p, dict) and type(p.get("enabled")) is bool:
            pm = {str(k): v for k, v in p.items()}
            return {
                "enabled": pm["enabled"],
                "email": _str(pm.get("email")),
                "hint": _str(pm.get("hint")),
            }
    raise ValueError("MAX не вернул подтверждённое состояние 2FA")


def _has_list_field(info, keys: tuple[str, ...]) -> bool:
    return isinstance(info, dict) and any(isinstance(info.get(key), list) for key in keys)


def _has_chat_field(info) -> bool:
    return (_has_list_field(info, ("chats", "items", "result"))
            or isinstance(info, dict) and isinstance(info.get("chat"), dict))


def _chat_entries(info) -> list[dict]:
    if isinstance(info, dict):
        for key in ("chats", "items", "result"):
            if isinstance(info.get(key), list):
                return [item for item in info[key] if isinstance(item, dict)]
        if isinstance(info.get("chat"), dict):
            return [info["chat"]]
    return []


def _contact_entries(info) -> list[dict]:
    if not isinstance(info, dict):
        return []
    if isinstance(info.get("contacts"), list):
        return [item for item in info["contacts"] if isinstance(item, dict)]
    if isinstance(info.get("contact"), dict):
        return [info["contact"]]
    return []


def _strict_id(value) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if -(2 ** 63) <= value < 2 ** 63 else None
    if isinstance(value, str) and re.fullmatch(r"-?[0-9]{1,19}", value):
        parsed = int(value)
        return parsed if -(2 ** 63) <= parsed < 2 ** 63 else None
    return None


def _is_read_int(value, *, bits: int = 64) -> bool:
    return type(value) is int and -(2 ** (bits - 1)) <= value < 2 ** (bits - 1)


def _folder_from_data(raw) -> dict:
    """Validate known public yv fields without supplying optional defaults."""
    if (not isinstance(raw, dict) or not isinstance(raw.get("id"), str)
            or not isinstance(raw.get("title"), str)):
        raise ValueError("MAX вернул неподтверждённый формат папки")
    for key in ("emoji",):
        if key in raw and not isinstance(raw[key], str):
            raise ValueError("MAX вернул неверное текстовое поле папки")
    for key in ("include", "favorites"):
        if key in raw and (not isinstance(raw[key], list) or any(not _is_read_int(value) for value in raw[key])):
            raise ValueError("MAX вернул неверные идентификаторы чатов папки")
    for key, limit in (("filters", 14), ("options", 4)):
        if key in raw and (not isinstance(raw[key], list)
                or any(type(value) is not int or not 0 <= value <= limit for value in raw[key])):
            raise ValueError("MAX вернул неподтверждённые параметры папки")
    for key in ("updateTime", "sourceId"):
        if key in raw and not _is_read_int(raw[key]):
            raise ValueError("MAX вернул неверные метаданные папки")
    for key in ("elements", "widgets"):
        if key in raw and (not isinstance(raw[key], list) or any(not isinstance(value, dict) for value in raw[key])):
            raise ValueError("MAX вернул неверные элементы папки")
    if "filterSubjects" in raw and (not isinstance(raw["filterSubjects"], dict)
            or any(not isinstance(values, list) or any(not _is_read_int(value) for value in values)
                   for values in raw["filterSubjects"].values())):
        raise ValueError("MAX вернул неверные условия папки")
    return copy.deepcopy(raw)


def _story_from_data(raw, owner_id: int) -> dict:
    """Validate archive fields and own-owner association, retaining raw values."""
    if not isinstance(raw, dict):
        raise ValueError("MAX вернул неподтверждённый формат истории")
    for key in ("id", "updateTime", "time"):
        if not _is_read_int(raw.get(key)):
            raise ValueError("MAX вернул неверные идентификатор или время истории")
    owner = raw.get("owner")
    if (not isinstance(owner, dict) or type(owner.get("type")) is not int or owner["type"] != 0
            or not _is_read_int(owner.get("ownerId")) or owner["ownerId"] != owner_id):
        raise ValueError("MAX вернул историю другого владельца")
    for key in ("settings", "expiration", "viewsCount", "reactionsCount"):
        if not _is_read_int(raw.get(key), bits=32):
            raise ValueError("MAX вернул неверные параметры истории")
    media = raw.get("media")
    if not isinstance(media, dict) or media.get("_type") not in ("PHOTO", "VIDEO"):
        raise ValueError("MAX вернул неподтверждённый тип медиа истории")
    for key, bits in (("cid", 64), ("version", 32)):
        if key in raw and not _is_read_int(raw[key], bits=bits):
            raise ValueError("MAX вернул неверные метаданные истории")
    if "layers" in raw and (not isinstance(raw["layers"], list)
            or any(not isinstance(layer, dict) for layer in raw["layers"])):
        raise ValueError("MAX вернул неверные слои истории")
    if "reaction" in raw:
        reaction = raw["reaction"]
        if (not isinstance(reaction, dict) or type(reaction.get("reactionType")) is not int
                or reaction["reactionType"] not in (0, 1) or not isinstance(reaction.get("id"), str)):
            raise ValueError("MAX вернул неверную реакцию истории")
    return copy.deepcopy(raw)


def _server_avatar_url(data: dict) -> Optional[str]:
    """Use the server's direct contact URL; do not invent a photoId recipe."""
    value = data.get("baseUrl")
    if not isinstance(value, str):
        return None
    try:
        url = urlsplit(value)
        if url.scheme == "https" and url.hostname and url.username is None and url.password is None:
            return value
    except ValueError:
        pass
    return None


def _participant_ids(value) -> list[int]:
    """Participant-map keys are IDs; its values can be timestamps."""
    values = list(value) if isinstance(value, (dict, list)) else []
    identities = []
    for item in values:
        if isinstance(item, dict):
            item = item.get("id", item.get("userId"))
        identity = _strict_id(item)
        if identity is not None and identity not in identities:
            identities.append(identity)
    return identities


_CONTACT_FLAG_BITS = {"OFFICIAL": 1, "BOT": 2, "SERVICE_ACCOUNT": 8}


def _has_option(data, option: str) -> bool:
    if not isinstance(data, dict):
        return False
    options = data.get("options")
    if isinstance(options, dict):
        return options.get(option) is True
    return isinstance(options, list) and option in options


def _has_contact_flag(data, option: str) -> bool:
    if not isinstance(data, dict) or option not in _CONTACT_FLAG_BITS:
        return False
    # Current ContactDTO/model uses flags:int32. If supplied, this mask is
    # authoritative for the known contact flags; do not complete an incomplete
    # or malformed mask with legacy option strings. Old replies without flags
    # retain their explicit options compatibility.
    if "flags" in data:
        flags = data["flags"]
        return (type(flags) is int and 0 <= flags < 2 ** 31
                and bool(flags & _CONTACT_FLAG_BITS[option]))
    return _has_option(data, option)


def _verification_link(value) -> bool:
    if not isinstance(value, str):
        return False
    if value in {"verificationcodes_bot", "@verificationcodes_bot"}:
        return True
    try:
        url = urlsplit(value)
        return (url.scheme == "https" and url.netloc == "max.ru"
                and url.path.rstrip("/") == "/verificationcodes_bot"
                and not url.query and not url.fragment)
    except ValueError:
        return False


def _is_verification_contact(contact) -> bool:
    if not isinstance(contact, dict):
        return False
    if not all(_has_contact_flag(contact, flag) for flag in ("SERVICE_ACCOUNT", "OFFICIAL", "BOT")):
        return False
    # CUSTOM is an address-book alias, not the official service identity.
    names = contact.get("names")
    records = [item for item in names if isinstance(item, dict)] if isinstance(names, list) else []
    canonical = [item for item in records if item.get("type") == "ONEME"]
    unspecified = [item for item in records if "type" not in item]
    allowed_names = canonical or unspecified
    name = _person_name({"names": allowed_names}) if records else _person_name(contact)
    if name == "Безопасность":
        return False
    return (name == "Коды подтверждения" or _verification_link(contact.get("link"))
            or _verification_link(contact.get("username")))


def _own_read_mark(raw, owner) -> Optional[int | float]:
    """Own participant read marker of one chat DTO, for either key spelling."""
    participants = raw.get('participants') if isinstance(raw, dict) else None
    if type(owner) is not int or not isinstance(participants, dict):
        return None
    return _message_time({'time': participants.get(str(owner), participants.get(owner))})


def _message_time(value) -> Optional[int | float]:
    if not isinstance(value, dict):
        return None
    timestamp = value.get("time")
    if type(timestamp) is int and -(2 ** 63) <= timestamp < 2 ** 63:
        return timestamp
    if type(timestamp) is float and math.isfinite(timestamp):
        return timestamp
    return None


def _visible_message(value, *, include_clipboard=False) -> Optional[dict]:
    """Use literal server text and, only for the verified viewer, copy buttons."""
    if not isinstance(value, dict):
        return None
    text, timestamp = value.get("text", ''), _message_time(value)
    if (not isinstance(text, str) or timestamp is None
            or value.get("deleted") is True or value.get("removed") is True
            or value.get('status') == 'REMOVED'):
        return None
    if include_clipboard:
        from .message_presentation import visible_clipboard_text
        copied = visible_clipboard_text(value)
        if copied:
            text = text + '\n\n' + copied if text else copied
    if not text.strip():
        return None
    return {"id": _strict_id(value.get("id")), "time_ms": timestamp, "text": text}


def _extract_chats(info: dict, my_id: Optional[int] = None) -> list[Chat]:
    arr = _chat_entries(info)
    out: list[Chat] = []
    for m in arr:
        mm = {str(k): v for k, v in m.items()}
        cid = _strict_id(mm.get("id"))
        if cid is None:
            continue
        type_str = str(mm.get("type") or "").lower()
        members = _to_int(mm.get("membersCount"))
        if type_str in {"dialog", "chat", "channel"}:
            is_group = type_str != "dialog"
        else:
            is_group = "group" in type_str or "channel" in type_str or (members is not None and members > 2)
        supplied = set()
        if "type" in mm:
            supplied.update({"chat_type", "is_channel", "is_group"})
        elif members is not None:
            supplied.add("is_group")
        unread = mm.get("newMessages")
        if isinstance(unread, int) and not isinstance(unread, bool) and unread >= 0:
            supplied.add("unread")
        else:
            unread = 0
        participants = _participant_ids(mm.get("participants"))
        peer = None
        if (type_str == "dialog" and my_id is not None
                and isinstance(mm.get("participants"), (dict, list))):
            # Public uM.recipientId at DPMqYNCb.js:599317 chooses the first
            # non-viewer map key, falling back to the viewer. Service-dialog
            # maps can contain only their peer; requiring two entries lost it.
            others = [identity for identity in participants if identity != my_id]
            supplied.add("peer_user_id")
            # Match the native first-other/self branches for an unambiguous
            # dialog; multiple non-viewer IDs stay unresolved rather than route
            # a recipient from a malformed/ambiguous DTO.
            if len(others) <= 1:
                peer = others[0] if others else my_id
        last_message = _visible_message(mm.get("lastMessage"))
        last_time = _message_time(mm.get("lastMessage"))
        last_preview = last_message['text'] if last_message is not None else None
        if (last_time is not None and isinstance(mm.get('lastMessage'), dict)
                and isinstance(mm['lastMessage'].get('text', ''), str)
                and ('updateTime' not in mm['lastMessage']
                     or _message_time({'time': mm['lastMessage']['updateTime']}) is not None)
                and mm['lastMessage'].get('deleted') is not True
                and mm['lastMessage'].get('removed') is not True
                and mm['lastMessage'].get('status') != 'REMOVED'):
            parsed_last = Message.from_server(cid, mm['lastMessage'], my_id)
            localized = parsed_last.control_display_text
            if localized is not None:
                last_preview = localized
            elif last_preview is None:
                # A photo, file, sticker, forward or service message has no
                # literal text. Use the same label as the 128 delivery path, so
                # LOGIN/CHAT_INFO/135 snapshots cannot blank the row preview.
                last_preview = parsed_last.preview or None
        if last_time is None:
            last_time = _message_time({"time": mm.get("lastEventTime")})
        if last_time is not None:
            supplied.add("last_time_ms")
        if "lastMessage" in mm:
            supplied.add("last_preview")
        out.append(
            Chat(
                id=cid,
                title=_str(mm.get("title") or mm.get("name")),
                avatar_url=_str(mm.get("avatar") or mm.get("photo")),
                is_group=is_group,
                peer_user_id=peer,
                chat_type=type_str.upper() if type_str else None,
                is_channel=type_str == "channel",
                unread=unread,
                last_time_ms=last_time,
                last_preview=last_preview,
                server_fields=frozenset(supplied),
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
    if not isinstance(v, str) or not v:
        return None
    s = "".join(ch for ch in v if ch == " " or ch.isprintable())
    s = re.sub(r"\s+", " ", s).strip()
    return s or None
