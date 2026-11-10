"""Локальный кэш SQLite: чаты, контакты, история сообщений.

История подтягивается с сервера, но кэшируется локально, чтобы чат открывался
мгновенно и работал офлайн. Контакты переживают перезапуск (важно для импорта
отдельной кнопкой). Доступ потокобезопасен через единый lock.
"""
from __future__ import annotations

import json
import copy
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Optional

from .models import Attach, Chat, Contact, Message, Profile, parse_reaction_info, parse_message_link, _sanitize_time, server_attaches, _finite_file_number, file_message_schedule
from .models import ForwardedMessage, parse_forwarded_message, forwarded_to_data, forwarded_from_data

_SCHEMA = """
CREATE TABLE IF NOT EXISTS cache_metadata (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS conversation_creation_journal (
    owner_id INTEGER PRIMARY KEY,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS conversation_action_journal (
    owner_id INTEGER NOT NULL, chat_id INTEGER NOT NULL, payload TEXT NOT NULL,
    PRIMARY KEY(owner_id,chat_id)
);
CREATE TABLE IF NOT EXISTS bot_delete_peer_reservation (
    owner_id INTEGER NOT NULL, peer_id INTEGER NOT NULL, chat_id INTEGER NOT NULL,
    operation_id TEXT NOT NULL, PRIMARY KEY(owner_id,peer_id)
);
CREATE TABLE IF NOT EXISTS conversation_history_boundary (
    owner_id INTEGER NOT NULL, chat_id INTEGER NOT NULL, cutoff REAL NOT NULL,
    deleted INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(owner_id,chat_id)
);
CREATE TABLE IF NOT EXISTS confirmed_message_deletions (
    owner_id INTEGER NOT NULL, chat_id INTEGER NOT NULL, message_id INTEGER NOT NULL,
    PRIMARY KEY(owner_id,chat_id,message_id)
);
CREATE TABLE IF NOT EXISTS conversation_departures (
    owner_id INTEGER NOT NULL, chat_id INTEGER NOT NULL, cutoff REAL NOT NULL,
    chat_type TEXT NOT NULL, PRIMARY KEY(owner_id,chat_id)
);
CREATE TABLE IF NOT EXISTS conversation_report_journal (
    owner_id INTEGER NOT NULL, type_id INTEGER NOT NULL, target_id INTEGER NOT NULL,
    payload TEXT NOT NULL, PRIMARY KEY(owner_id,type_id,target_id)
);
CREATE TABLE IF NOT EXISTS chats (
    id            INTEGER PRIMARY KEY,
    title         TEXT,
    is_group      INTEGER DEFAULT 0,
    last_time_ms  INTEGER,
    last_preview  TEXT,
    unread        INTEGER DEFAULT 0,
    avatar_url    TEXT,
    title_locked  INTEGER DEFAULT 0,
    peer_user_id  INTEGER,
    chat_type     TEXT,
    is_channel    INTEGER DEFAULT 0,
    is_pinned     INTEGER DEFAULT 0,
    is_muted      INTEGER DEFAULT 0,
    is_verified   INTEGER DEFAULT 0,
    pin_order     INTEGER
);
CREATE TABLE IF NOT EXISTS contacts (
    id     INTEGER PRIMARY KEY,
    name   TEXT,
    phone  TEXT,
    avatar_url TEXT,
    is_verified INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS local_contact_visibility (
    owner_id INTEGER NOT NULL, contact_id INTEGER NOT NULL,
    PRIMARY KEY(owner_id,contact_id)
);
CREATE TABLE IF NOT EXISTS messages (
    row        INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id    INTEGER NOT NULL,
    server_id  INTEGER,
    sender     INTEGER,
    text       TEXT,
    time_ms    INTEGER,
    outgoing   INTEGER DEFAULT 0,
    status     TEXT DEFAULT 'sent',
    local_id   TEXT,
    attaches   TEXT,
    reaction_info TEXT,
    link_json TEXT,
    update_time_ms INTEGER,
    forwarded_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_msg_chat ON messages(chat_id, time_ms);
CREATE UNIQUE INDEX IF NOT EXISTS idx_msg_server ON messages(chat_id, server_id)
    WHERE server_id IS NOT NULL;
"""


_OWNER_UNKNOWN = object()


class Store:
    def __init__(self, db_path: Path) -> None:
        self._lock = threading.RLock()
        # cache_owner is read by nearly every UI guard; it changes only through
        # clear_cache() and bind_owner() of this Store, which reset the copy.
        self._owner_cache = _OWNER_UNKNOWN
        self._db = sqlite3.connect(str(db_path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.executescript(_SCHEMA)
            self._db.commit()
        self._migrate()

    @contextmanager
    def account_scope(self, owner_id: int):
        """Keep a checked cache owner stable for a bounded group of local writes."""
        with self._lock:
            if self.cache_owner != owner_id:
                raise ValueError("The cache owner changed before its update")
            yield

    # Бамп инвалидирует кэш. v2: сброс «грязных» сообщений, закэшированных до
    # фикса распаковки кадров (LZ4) — иначе дедуп по (chat_id, server_id) не даст
    # перезаписать их чистыми при повторной загрузке истории.
    _CACHE_VERSION = 2

    def _migrate(self) -> None:
        with self._lock:
            ver = self._db.execute("PRAGMA user_version").fetchone()[0]
            if ver < self._CACHE_VERSION:
                self._db.execute("DELETE FROM messages")
                self._db.execute("UPDATE chats SET last_preview=NULL")
                self._db.execute(f"PRAGMA user_version={self._CACHE_VERSION}")
                self._db.commit()
            # Аддитивная колонка title_locked: локальное переименование чата
            # не должно затираться серверным sync (upsert_chat). Старые БД — ALTER.
            cols = {r[1] for r in self._db.execute("PRAGMA table_info(chats)")}
            if "title_locked" not in cols:
                self._db.execute(
                    "ALTER TABLE chats ADD COLUMN title_locked INTEGER DEFAULT 0"
                )
                self._db.commit()
            # peer_user_id: для 1:1 диалога — id собеседника (SEND_MESSAGE по
            # userId, не chatId). Узнаётся из ошибки сервера и кэшируется.
            if "peer_user_id" not in cols:
                self._db.execute("ALTER TABLE chats ADD COLUMN peer_user_id INTEGER")
                self._db.commit()
            for name, definition in {
                "chat_type": "TEXT", "is_channel": "INTEGER DEFAULT 0",
                "is_pinned": "INTEGER DEFAULT 0", "is_muted": "INTEGER DEFAULT 0",
                "is_verified": "INTEGER DEFAULT 0", "pin_order": "INTEGER",
            }.items():
                if name not in cols:
                    self._db.execute(f"ALTER TABLE chats ADD COLUMN {name} {definition}")
            contact_cols = {r[1] for r in self._db.execute("PRAGMA table_info(contacts)")}
            if "avatar_url" not in contact_cols:
                self._db.execute("ALTER TABLE contacts ADD COLUMN avatar_url TEXT")
            if "is_verified" not in contact_cols:
                self._db.execute("ALTER TABLE contacts ADD COLUMN is_verified INTEGER DEFAULT 0")
            message_cols = {r[1] for r in self._db.execute("PRAGMA table_info(messages)")}
            if "reaction_info" not in message_cols:
                self._db.execute("ALTER TABLE messages ADD COLUMN reaction_info TEXT")
            if "link_json" not in message_cols:
                self._db.execute("ALTER TABLE messages ADD COLUMN link_json TEXT")
            if 'update_time_ms' not in message_cols:
                self._db.execute('ALTER TABLE messages ADD COLUMN update_time_ms INTEGER')
            if 'forwarded_json' not in message_cols:
                self._db.execute('ALTER TABLE messages ADD COLUMN forwarded_json TEXT')
            self._db.commit()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def clear_cache(self) -> None:
        """Полностью очистить кэш аккаунта (чаты/контакты/сообщения). Нужно при
        входе под ДРУГИМ аккаунтом — иначе старые данные «протекают» в новый."""
        with self._lock:
            self._owner_cache = _OWNER_UNKNOWN
            try:
                self._db.execute("DELETE FROM messages")
                self._db.execute("DELETE FROM contacts")
                self._db.execute("DELETE FROM chats")
                self._db.execute("DELETE FROM cache_metadata")
                self._db.commit()
            finally:
                self._owner_cache = _OWNER_UNKNOWN

    @property
    def cache_owner(self) -> Optional[int]:
        with self._lock:
            if self._owner_cache is not _OWNER_UNKNOWN:
                return self._owner_cache
            row = self._db.execute("SELECT value FROM cache_metadata WHERE key='owner_id'").fetchone()
            value = None
            if row is not None:
                try:
                    value = int(row["value"])
                except (TypeError, ValueError):
                    value = None
            self._owner_cache = value
            return value

    def bind_owner(self, owner_id: int) -> bool:
        """Atomically fail closed for an unknown/different account cache owner."""
        if type(owner_id) is not int:
            raise ValueError("A confirmed integer account ID is required")
        with self._lock:
            try:
                with self._db:
                    if self.cache_owner == owner_id:
                        return False
                    self._owner_cache = _OWNER_UNKNOWN
                    self._db.execute("DELETE FROM messages")
                    self._db.execute("DELETE FROM contacts")
                    self._db.execute("DELETE FROM chats")
                    self._db.execute("DELETE FROM cache_metadata")
                    self._db.execute("INSERT INTO cache_metadata(key,value) VALUES('owner_id',?) "
                                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(owner_id),))
            finally:
                # Committed or rolled back, the next read takes the stored value.
                self._owner_cache = _OWNER_UNKNOWN
        return True

    def save_profile(self, profile: Profile) -> None:
        if profile.id is None or self.cache_owner != profile.id:
            raise ValueError("The profile must match the confirmed cache owner")
        payload = json.dumps(asdict(profile), ensure_ascii=False)
        with self._lock:
            self._db.execute("INSERT INTO cache_metadata(key,value) VALUES('own_profile',?) "
                             "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (payload,))
            self._db.commit()

    def load_profile(self) -> Optional[Profile]:
        with self._lock:
            row = self._db.execute("SELECT value FROM cache_metadata WHERE key='own_profile'").fetchone()
        if row is None:
            return None
        try:
            data = json.loads(row["value"])
            allowed = Profile.__dataclass_fields__
            profile = Profile(**{key: value for key, value in data.items() if key in allowed})
        except (TypeError, ValueError, AttributeError):
            return None
        return profile if profile.id == self.cache_owner else None

    def load_config(self, owner_id: int) -> dict:
        """Read supplied config only for the confirmed persistent owner."""
        with self._lock:
            if type(owner_id) is not int or self.cache_owner != owner_id:
                return {}
            row = self._db.execute("SELECT value FROM cache_metadata WHERE key='account_config'").fetchone()
        try:
            value = json.loads(row["value"]) if row is not None else {}
            if not isinstance(value, dict) or any(key in value and not isinstance(value[key], dict)
                                                    for key in ("user", "server", "chats", "experiments")):
                return {}
            return value
        except (TypeError, ValueError):
            return {}

    def merge_config(self, owner_id: int, supplied: dict) -> dict:
        """Persist supplied owner-scoped user/server maps, never local defaults."""
        incoming = json.loads(json.dumps(supplied, ensure_ascii=False, allow_nan=False))
        if not isinstance(incoming, dict):
            raise ValueError("Config must be an object")
        with self._lock, self._db:
            if type(owner_id) is not int or self.cache_owner != owner_id:
                raise ValueError("Config must match the confirmed cache owner")
            merged = self.load_config(owner_id)
            for key in ("user", "server", "chats"):
                if key in incoming:
                    if not isinstance(incoming[key], dict):
                        raise ValueError("Config maps must be structured")
                    merged[key] = {**merged.get(key, {}), **incoming[key]}
            if incoming.get('experiments') is not None:
                if not isinstance(incoming['experiments'], dict):
                    raise ValueError('Config experiments must be structured')
                # Shipped experiments replace the previous map, unlike server
                # merges. Retain only the two evidenced text-edit limits.
                merged['experiments'] = {key: value for key, value in incoming['experiments'].items()
                    if key in ('max-cname-length', 'max-description-length')}
            if "hash" in incoming:
                if not isinstance(incoming["hash"], str):
                    raise ValueError("Config hash must be a string")
                merged["hash"] = incoming["hash"]
            self._db.execute("INSERT INTO cache_metadata(key,value) VALUES('account_config',?) "
                             "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                             (json.dumps(merged, ensure_ascii=False, allow_nan=False),))
        return merged

    # ───────────────────────── chats ─────────────────────────

    def load_conversation_creation(self, owner_id: int):
        """Pending write journal is separate from disposable account caches."""
        with self.account_scope(owner_id):
            row = self._db.execute('SELECT payload FROM conversation_creation_journal WHERE owner_id=?',
                                   (owner_id,)).fetchone()
            if row is None:
                return None
            try:
                value = json.loads(row['payload'])
            except (TypeError, ValueError):
                raise ValueError('Журнал создания повреждён; повторная отправка заблокирована') from None
            if not isinstance(value, dict):
                raise ValueError('Журнал создания повреждён; повторная отправка заблокирована')
            return value

    def save_conversation_creation(self, owner_id: int, pending: dict):
        with self.account_scope(owner_id), self._db:
            changed = self._db.execute('UPDATE conversation_creation_journal SET payload=? WHERE owner_id=?',
                (json.dumps(pending, ensure_ascii=False, allow_nan=False), owner_id))
            if changed.rowcount != 1:
                raise ValueError('Нет исходной записи создания')

    def reserve_conversation_creation(self, owner_id: int, pending: dict):
        # UNIQUE owner_id also excludes competing service/process instances.
        with self.account_scope(owner_id), self._db:
            try:
                self._db.execute('INSERT INTO conversation_creation_journal(owner_id,payload) VALUES(?,?)',
                    (owner_id, json.dumps(pending, ensure_ascii=False, allow_nan=False)))
            except sqlite3.IntegrityError:
                raise ValueError('Предыдущее создание ещё не подтверждено') from None

    def clear_conversation_creation(self, owner_id: int):
        with self.account_scope(owner_id), self._db:
            self._db.execute('DELETE FROM conversation_creation_journal WHERE owner_id=?', (owner_id,))

    def upsert_chat(self, chat: Chat) -> None:
        values = {
            "id": chat.id, "title": chat.title, "is_group": int(chat.is_group),
            "last_time_ms": chat.last_time_ms, "last_preview": chat.last_preview,
            "unread": chat.unread, "avatar_url": chat.avatar_url,
            "peer_user_id": chat.peer_user_id, "chat_type": chat.chat_type,
            "is_channel": int(chat.is_channel), "is_pinned": int(chat.is_pinned),
            "is_muted": int(chat.is_muted), "is_verified": int(chat.is_verified),
            "pin_order": chat.pin_order,
        }
        supplied = chat.server_fields
        if supplied is None:
            # Legacy/local callers do not carry server-presence information.
            # Preserve unknown counters/flags rather than turn defaults into DTOs.
            supplied = {"is_group"}
            supplied.update(key for key in ("last_time_ms", "last_preview", "avatar_url",
                                            "peer_user_id", "chat_type", "pin_order")
                            if values[key] is not None)
            supplied.update(key for key in ("unread", "is_channel", "is_pinned", "is_muted", "is_verified")
                            if values[key])
        if self.history_was_cleared(chat.id, chat.last_time_ms):
            supplied = set(supplied) - {'last_time_ms', 'last_preview', 'unread'}
            values.update(last_time_ms=None, last_preview=None, unread=0)
        updates = ["title=CASE WHEN chats.title_locked=1 THEN chats.title "
                   "ELSE COALESCE(excluded.title, chats.title) END"]
        updates.extend(f"{key}=excluded.{key}" for key in values
                       if key not in {"id", "title"} and key in supplied)
        columns = ",".join(values)
        placeholders = ",".join("?" for _ in values)
        with self._lock:
            self._db.execute(
                f"INSERT INTO chats({columns}) VALUES({placeholders}) "
                "ON CONFLICT(id) DO UPDATE SET " + ",".join(updates),
                tuple(values.values()),
            )
            self._db.commit()

    def ensure_chat(self, chat_id: int, title: Optional[str] = None) -> None:
        with self._lock:
            row = self._db.execute("SELECT id, title FROM chats WHERE id=?", (chat_id,)).fetchone()
            if row is None:
                self._db.execute(
                    "INSERT INTO chats(id, title) VALUES(?,?)",
                    (chat_id, title or f"Чат {chat_id}"),
                )
            elif title and not row["title"]:
                self._db.execute("UPDATE chats SET title=? WHERE id=?", (title, chat_id))
            self._db.commit()

    def rename_chat(self, chat_id: int, title: str) -> None:
        """Локальное переименование: ПЕРЕЗАПИСЫВАЕТ заголовок (в отличие от
        ensure_chat, который ставит имя только если его ещё не было)."""
        with self._lock:
            self._db.execute(
                "INSERT INTO chats(id, title, title_locked) VALUES(?,?,1) "
                "ON CONFLICT(id) DO UPDATE SET title=excluded.title, title_locked=1",
                (chat_id, title),
            )
            self._db.commit()

    def list_chats(self) -> list[Chat]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM chats ORDER BY is_pinned DESC, "
                "CASE WHEN is_pinned=1 AND pin_order IS NOT NULL THEN 0 ELSE 1 END, "
                "CASE WHEN is_pinned=1 THEN pin_order ELSE NULL END ASC, "
                "COALESCE(last_time_ms,0) DESC, id DESC"
            ).fetchall()
        return [self._row_to_chat(r) for r in rows]

    def set_confirmed_contact_chat_title(self, chat_id: int, title: str) -> None:
        """Install a confirmed server alias on an existing mapped dialog only.

        The explicit server rename supersedes its old local alias; subsequent
        server CUSTOM/ONEME changes remain visible. Never creates a chat row.
        """
        with self._lock:
            self._db.execute('UPDATE chats SET title=?, title_locked=0 WHERE id=?',
                             (title, chat_id))
            self._db.commit()

    def get_chat(self, chat_id: int) -> Optional[Chat]:
        with self._lock:
            r = self._db.execute("SELECT * FROM chats WHERE id=?", (chat_id,)).fetchone()
        return self._row_to_chat(r) if r else None

    def update_chat_preview(
        self, chat_id: int, time_ms: int, preview: str, inc_unread: int = 0, *,
        monotonic: bool = False,
    ) -> None:
        with self._lock:
            if self.history_was_cleared(chat_id, time_ms):
                return
            if monotonic:
                self._db.execute(
                    """UPDATE chats SET
                       last_preview=CASE WHEN last_time_ms IS NULL OR ? >= last_time_ms
                                         THEN ? ELSE last_preview END,
                       last_time_ms=CASE WHEN last_time_ms IS NULL OR ? >= last_time_ms
                                        THEN ? ELSE last_time_ms END,
                       unread=unread+? WHERE id=?""",
                    (time_ms, preview, time_ms, time_ms, inc_unread, chat_id),
                )
            else:
                self._db.execute(
                    """UPDATE chats SET last_time_ms=?, last_preview=?, unread=unread+?
                       WHERE id=?""",
                    (time_ms, preview, inc_unread, chat_id),
                )
            self._db.commit()

    def reset_unread(self, chat_id: int) -> None:
        with self._lock:
            self._db.execute("UPDATE chats SET unread=0 WHERE id=?", (chat_id,))
            self._db.commit()

    def set_unread(self, chat_id: int, unread: int) -> None:
        if isinstance(unread, bool) or not isinstance(unread, int) or unread < 0:
            raise ValueError("Unread count must be a nonnegative integer")
        with self._lock:
            self._db.execute("UPDATE chats SET unread=? WHERE id=?", (unread, chat_id))
            self._db.commit()

    def set_chat_peer(self, chat_id: int, peer_user_id: int) -> None:
        """Запомнить peer userId диалога (1:1). Используется для отправки по
        userId и для подстановки имени собеседника из контактов."""
        if not peer_user_id:
            return
        with self._lock:
            self._db.execute(
                "INSERT INTO chats(id, peer_user_id) VALUES(?,?) "
                "ON CONFLICT(id) DO UPDATE SET peer_user_id=excluded.peer_user_id",
                (chat_id, peer_user_id),
            )
            self._db.commit()


    @staticmethod
    def _row_to_chat(r: sqlite3.Row) -> Chat:
        keys = r.keys()
        return Chat(
            id=r["id"], title=r["title"], is_group=bool(r["is_group"]),
            last_time_ms=r["last_time_ms"], last_preview=r["last_preview"],
            unread=r["unread"] or 0, avatar_url=r["avatar_url"],
            title_locked=bool(r["title_locked"]) if "title_locked" in keys else False,
            peer_user_id=r["peer_user_id"] if "peer_user_id" in keys else None,
            chat_type=r["chat_type"], is_channel=bool(r["is_channel"]),
            is_pinned=bool(r["is_pinned"]), is_muted=bool(r["is_muted"]),
            is_verified=bool(r["is_verified"]), pin_order=r["pin_order"],
        )

    # ───────────────────────── contacts ─────────────────────────

    def upsert_contact(self, c: Contact) -> None:
        updates = ["name=COALESCE(excluded.name, contacts.name)",
                   "phone=COALESCE(excluded.phone, contacts.phone)"]
        if c.avatar_url is not None or (c.server_fields and "avatar_url" in c.server_fields):
            updates.append("avatar_url=excluded.avatar_url")
        if c.is_verified or (c.server_fields and "is_verified" in c.server_fields):
            updates.append("is_verified=excluded.is_verified")
        with self._lock:
            self._db.execute(
                "INSERT INTO contacts(id,name,phone,avatar_url,is_verified) VALUES(?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET " + ",".join(updates),
                (c.id, c.name, c.phone, c.avatar_url, int(c.is_verified)),
            )
            self._db.commit()

    def list_contacts(self, query: str = "") -> list[Contact]:
        with self._lock:
            if query:
                like = f"%{query}%"
                rows = self._db.execute(
                    "SELECT * FROM contacts WHERE name LIKE ? OR phone LIKE ? ORDER BY name",
                    (like, like),
                ).fetchall()
            else:
                rows = self._db.execute(
                    "SELECT * FROM contacts ORDER BY name COLLATE NOCASE"
                ).fetchall()
        return [Contact(id=r["id"], name=r["name"], phone=r["phone"],
                        avatar_url=r["avatar_url"], is_verified=bool(r["is_verified"])) for r in rows]

    def get_contact(self, contact_id: int) -> Optional[Contact]:
        """One stored contact by ID, the same row list_contacts() would return."""
        with self._lock:
            r = self._db.execute("SELECT * FROM contacts WHERE id=?", (contact_id,)).fetchone()
        if r is None:
            return None
        return Contact(id=r["id"], name=r["name"], phone=r["phone"],
                       avatar_url=r["avatar_url"], is_verified=bool(r["is_verified"]))

    def hidden_contact_ids(self, owner_id: int) -> set[int]:
        """Local address-list preferences; raw profile metadata stays readable."""
        if type(owner_id) is not int or not -(2**63) <= owner_id < 2**63:
            raise ValueError("A confirmed integer account ID is required")
        with self.account_scope(owner_id):
            rows = self._db.execute(
                "SELECT contact_id FROM local_contact_visibility WHERE owner_id=?", (owner_id,)
            ).fetchall()
        return {row["contact_id"] for row in rows}

    def set_contact_hidden(self, owner_id: int, contact_id: int, hidden: bool) -> None:
        """Persist an owner's explicit visibility choice, never server membership.

        These preferences survive cache clears/account switches; the compound
        owner key prevents a same-ID contact in another account being hidden.
        Ordinary profile upserts never change them.
        """
        if (type(owner_id) is not int or not -(2**63) <= owner_id < 2**63
                or type(contact_id) is not int or not -(2**63) <= contact_id < 2**63
                or type(hidden) is not bool):
            raise ValueError("Valid owner/contact IDs and a boolean visibility are required")
        with self.account_scope(owner_id), self._db:
            if hidden:
                self._db.execute(
                    "INSERT OR IGNORE INTO local_contact_visibility(owner_id,contact_id) VALUES(?,?)",
                    (owner_id, contact_id))
            else:
                self._db.execute(
                    "DELETE FROM local_contact_visibility WHERE owner_id=? AND contact_id=?",
                    (owner_id, contact_id))

    def delete_contact(self, contact_id: int) -> None:
        with self._lock:
            self._db.execute("DELETE FROM contacts WHERE id=?", (contact_id,))
            self._db.commit()

    # ───────────────────────── messages ─────────────────────────

    def insert_message(self, m: Message) -> bool:
        with self._lock:
            inserted = self._insert_one(m)
            if inserted:
                self._db.commit()
            return inserted

    def insert_messages(self, msgs: list[Message]) -> int:
        """Пакетная вставка одной транзакцией (для синхронизации истории)."""
        if not msgs:
            return 0
        added = 0
        with self._lock:
            for m in msgs:
                if self._insert_one(m):
                    added += 1
            if added:
                self._db.commit()
        return added

    def _insert_one(self, m: Message) -> bool:
        """Вставка без commit (вызывать под self._lock). True — строка добавлена."""
        if m.id is not None and (self.history_was_cleared(m.chat_id, m.time_ms)
                                or self.message_was_deleted(m.chat_id, m.id)):
            return False
        if m.id is not None:
            exists = self._db.execute(
                "SELECT row FROM messages WHERE chat_id=? AND server_id=?",
                (m.chat_id, m.id),
            ).fetchone()
            if exists:
                return False
        attaches_json = json.dumps(
            [a.__dict__ for a in m.attaches], ensure_ascii=False
        ) if m.attaches else None
        self._db.execute(
            """INSERT INTO messages(chat_id, server_id, sender, text, time_ms,
                                    outgoing, status, local_id, attaches, reaction_info, link_json, update_time_ms, forwarded_json)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                m.chat_id, m.id, m.sender, m.text, m.time_ms,
                int(m.outgoing), m.status, m.local_id, attaches_json,
                json.dumps(parse_reaction_info(m.reaction_info), ensure_ascii=False),
                json.dumps(parse_message_link(m.link), ensure_ascii=False),
                m.update_time_ms,
                json.dumps(forwarded_to_data(m.forwarded), ensure_ascii=False),
            ),
        )
        return True

    def update_message_by_local_id(
        self, local_id: str, *, server_id: Optional[int] = None, status: Optional[str] = None
    ) -> None:
        with self._lock:
            if server_id is not None and status is not None:
                self._db.execute(
                    "UPDATE messages SET server_id=?, status=? WHERE local_id=?",
                    (server_id, status, local_id),
                )
            elif status is not None:
                self._db.execute(
                    "UPDATE messages SET status=? WHERE local_id=?", (status, local_id)
                )
            self._db.commit()

    def settle_unsent_message(self, chat_id: int, local_id: str, status: str) -> bool:
        """Finish only a still-pending local row; an acknowledged row is never downgraded."""
        if (status not in ('failed', 'unknown') or type(chat_id) is not int
                or not isinstance(local_id, str) or not local_id):
            raise ValueError('An unsent local message and a final local status are required')
        with self._lock, self._db:
            cursor = self._db.execute(
                "UPDATE messages SET status=? WHERE chat_id=? AND local_id=? "
                "AND server_id IS NULL AND status='pending'", (status, chat_id, local_id))
        return cursor.rowcount == 1

    def apply_message_ack(self, chat_id: int, raw: dict, *, local_id: Optional[str] = None,
                          my_id: Optional[int] = None, outgoing: Optional[bool] = None,
                          replay: bool = False) -> Message:
        """Upsert only supplied server fields after an authoritative write ACK.

        A server push may have inserted the same ID before its ACK arrives.
        Merge that row with the local pending row instead of violating the
        unique index or keeping two visible copies.
        """
        identity = raw.get("id") if isinstance(raw, dict) else None
        if type(identity) is not int:
            raise ValueError("A confirmed server message ID is required")
        supplied_time = _sanitize_time(raw["time"]) if "time" in raw else None
        supplied_version = _sanitize_time(raw['updateTime']) if 'updateTime' in raw else None
        with self._lock, self._db:
            if self.message_was_deleted(chat_id, identity):
                # A late edit/send ACK cannot recreate an ID already deleted by
                # a matching66 reply or142 push. Unsent rows remain independent.
                self._db.execute('DELETE FROM messages WHERE chat_id=? AND server_id=?', (chat_id, identity))
                if local_id is not None:
                    self._db.execute('DELETE FROM messages WHERE chat_id=? AND local_id=? AND server_id IS NULL',
                                     (chat_id, local_id))
                return Message(chat_id=chat_id, id=identity, time_ms=supplied_time,
                               status='removed', local_id=local_id)
            if self.history_was_cleared(chat_id, supplied_time):
                # A late push/ACK cannot reinsert history already cleared on the
                # server, nor turn a pending row into a resurrected old message.
                newer = self._db.execute('SELECT * FROM messages WHERE chat_id=? AND server_id=?',
                                         (chat_id, identity)).fetchone()
                if newer is not None and not self.history_was_cleared(chat_id, newer['time_ms']):
                    return self._row_to_msg(newer)
                self._db.execute('DELETE FROM messages WHERE chat_id=? AND server_id=?', (chat_id, identity))
                if local_id is not None:
                    self._db.execute('DELETE FROM messages WHERE chat_id=? AND local_id=? AND server_id IS NULL',
                                     (chat_id, local_id))
                return Message(chat_id=chat_id, id=identity, time_ms=supplied_time,
                               status='removed', local_id=local_id)
            existing = self._db.execute("SELECT * FROM messages WHERE chat_id=? AND server_id=?",
                                        (chat_id, identity)).fetchone()
            pending = (self._db.execute("SELECT * FROM messages WHERE local_id=?", (local_id,)).fetchone()
                       if local_id is not None else None)
            if existing is not None and pending is not None and existing["row"] != pending["row"]:
                self._db.execute("DELETE FROM messages WHERE row=?", (pending["row"],))
                pending = None
            target = existing or pending
            if target is None:
                message = Message.from_server(chat_id, raw, my_id)
                if outgoing is not None:
                    message.outgoing = outgoing
                message.local_id = local_id
                self._insert_one(message)
                target = self._db.execute("SELECT * FROM messages WHERE chat_id=? AND server_id=?",
                                          (chat_id, identity)).fetchone()
            changes = {"chat_id": chat_id, "server_id": identity,
                       "status": 'removed' if raw.get('status') == 'REMOVED' or target['status'] == 'removed' else 'sent'}
            known_version = target['update_time_ms']
            allow_content = not (known_version is not None and (
                supplied_version is not None and supplied_version < known_version
                or replay and supplied_version is None))
            if supplied_version is not None and (known_version is None or supplied_version >= known_version):
                changes['update_time_ms'] = supplied_version
            if local_id is not None:
                changes["local_id"] = local_id
            if allow_content and isinstance(raw.get("text"), str):
                changes["text"] = raw["text"]
            if allow_content and type(raw.get("sender")) is int:
                changes["sender"] = raw["sender"]
            if allow_content and "time" in raw:
                changes["time_ms"] = supplied_time
            if allow_content and outgoing is not None:
                changes["outgoing"] = int(outgoing)
            elif allow_content and type(raw.get('sender')) is int and type(my_id) is int:
                changes['outgoing'] = int(raw['sender'] == my_id)
            attaches = raw.get("attaches", raw.get("attachments"))
            if raw.get('status') == 'REMOVED':
                previous_attaches = self._row_to_msg(target).attaches
                for attach in previous_attaches:
                    if attach.type == 'FILE':
                        attach.deleted, attach.file_metadata_confirmed = True, False
                        if _finite_file_number(raw.get('updateTime')):
                            attach.file_update_time = (max(attach.file_update_time, raw['updateTime'])
                                if _finite_file_number(attach.file_update_time) else raw['updateTime'])
                changes['attaches'] = json.dumps([attach.__dict__ for attach in previous_attaches], ensure_ascii=False)
            elif (not allow_content and not replay and isinstance(attaches, list)
                    and all(isinstance(item, dict) for item in attaches)):
                # An explicit ACK may conservatively disable the same FILE
                # without rolling back stale content or another attachment.
                previous_attaches = self._row_to_msg(target).attaches
                candidate = server_attaches(raw, previous=previous_attaches)
                restricted = False
                for index, (previous, actual) in enumerate(zip(previous_attaches, candidate)):
                    if (attaches[index].get('_type') == 'FILE' and previous.type == actual.type == 'FILE'
                            and _finite_file_number(previous.file_id) and previous.file_id == actual.file_id
                            and (actual.deleted is True or actual.file_metadata_confirmed is not True)):
                        previous.deleted = previous.deleted or actual.deleted
                        previous.file_metadata_confirmed = (previous.file_metadata_confirmed is True
                                                            and actual.file_metadata_confirmed is True)
                        versions = [value for value in (previous.file_update_time, supplied_version, known_version)
                                    if _finite_file_number(value)]
                        if versions:
                            previous.file_update_time = max(versions)
                        restricted = True
                if restricted:
                    changes['attaches'] = json.dumps([attach.__dict__ for attach in previous_attaches], ensure_ascii=False)
            elif allow_content and isinstance(attaches, list) and all(isinstance(item, dict) for item in attaches):
                previous_attaches = self._row_to_msg(target).attaches
                candidate = server_attaches(raw, previous=previous_attaches)
                known = [attach.file_update_time for attach in previous_attaches if attach.type == 'FILE'
                         and _finite_file_number(attach.file_update_time)]
                incoming = raw.get('updateTime') if _finite_file_number(raw.get('updateTime')) else None
                if known and (incoming is None or incoming <= max(known)):
                    # Preserve a known newer resource. A restrictive invalidation
                    # may disable it, but an old/unversioned ACK cannot revive it.
                    if not any(attach.type == 'FILE' and (attach.deleted or not attach.file_metadata_confirmed)
                               for attach in candidate):
                        if incoming is None and 'delayedAttributes' in raw:
                            schedule, valid_schedule, supplied_schedule = file_message_schedule(raw)
                            if valid_schedule and supplied_schedule:
                                for old_attach, actual in zip(previous_attaches, candidate):
                                    if (old_attach.type == actual.type == 'FILE'
                                            and old_attach.file_metadata_confirmed is True
                                            and actual.file_metadata_confirmed is True
                                            and old_attach.file_id == actual.file_id):
                                        old_attach.file_item_type = actual.file_item_type
                        candidate = previous_attaches
                if known:
                    for attach in candidate:
                        if attach.type == 'FILE' and (not _finite_file_number(attach.file_update_time)
                                                      or attach.file_update_time < max(known)):
                            attach.file_update_time = max(known)
                changes['attaches'] = json.dumps([attach.__dict__ for attach in candidate], ensure_ascii=False)
            elif allow_content and 'delayedAttributes' in raw:
                schedule, schedule_valid, supplied = file_message_schedule(raw)
                if not schedule_valid:
                    raise ValueError('Invalid actual file message schedule')
                previous_attaches = self._row_to_msg(target).attaches
                for attach in previous_attaches:
                    if supplied and attach.type == 'FILE' and attach.file_metadata_confirmed is True:
                        incoming = raw.get('updateTime') if _finite_file_number(raw.get('updateTime')) else None
                        if (_finite_file_number(attach.file_update_time) and incoming is not None
                                and incoming <= attach.file_update_time):
                            continue
                        attach.file_item_type = 'DELAYED' if schedule > 0 else 'REGULAR'
                        if incoming is not None:
                            attach.file_update_time = incoming
                changes['attaches'] = json.dumps([attach.__dict__ for attach in previous_attaches], ensure_ascii=False)
            if allow_content and isinstance(raw.get("reactionInfo"), dict):
                changes["reaction_info"] = json.dumps(parse_reaction_info(raw["reactionInfo"]), ensure_ascii=False)
            link = parse_message_link(raw.get("link"))
            if allow_content and link is not None:
                changes["link_json"] = json.dumps(link, ensure_ascii=False)
            previous_forward = self._row_to_msg(target).forwarded
            if allow_content and 'link' in raw:
                forward = parse_forwarded_message(raw, previous=previous_forward)
                # Incoming snapshots never persist as an outgoing reference.
                changes['link_json'] = json.dumps(link, ensure_ascii=False)
            else:
                forward = previous_forward
            if (forward is not None and previous_forward is not None and previous_forward.available
                    and forward.available
                    and (forward.source_chat_id, forward.source_message_id) ==
                        (previous_forward.source_chat_id, previous_forward.source_message_id)):
                versions = [a.file_update_time for a in previous_forward.attaches
                            if a.type == 'FILE' and _finite_file_number(a.file_update_time)]
                if supplied_version is None or versions and supplied_version <= max(versions):
                    # Equal/undated snapshots do not undo a known restriction or
                    # grant a different FILE at the same displayed index.
                    for index, attach in enumerate(forward.attaches):
                        if attach.type != 'FILE':
                            continue
                        previous = (previous_forward.attaches[index]
                                    if index < len(previous_forward.attaches) else None)
                        if previous is None or previous.type != 'FILE' or previous.file_id != attach.file_id:
                            attach.file_metadata_confirmed = False
                        else:
                            attach.deleted = previous.deleted or attach.deleted
                            attach.file_metadata_confirmed = (previous.file_metadata_confirmed is True
                                                              and attach.file_metadata_confirmed is True)
                        if versions:
                            attach.file_update_time = max(versions)
                if not allow_content and not replay and 'link' in raw:
                    restrictive = parse_forwarded_message(raw)
                    if (restrictive is not None and restrictive.available
                            and (restrictive.source_chat_id, restrictive.source_message_id) ==
                                (forward.source_chat_id, forward.source_message_id)):
                        forward = copy.deepcopy(forward)
                        for old_attach, candidate in zip(forward.attaches, restrictive.attaches):
                            if (old_attach.type == candidate.type == 'FILE'
                                    and old_attach.file_id == candidate.file_id and candidate.deleted):
                                old_attach.deleted = True
            if forward is not None:
                # Keep outer scheduling/restriction state on its selected FILEs
                # even when an ordinary partial ACK omits the nested snapshot.
                if raw.get('status') == 'REMOVED' or target['status'] == 'removed':
                    for attach in forward.attaches:
                        if attach.type == 'FILE':
                            attach.deleted, attach.file_metadata_confirmed = True, False
                elif allow_content and 'delayedAttributes' in raw:
                    schedule, valid, supplied = file_message_schedule(raw)
                    if not valid:
                        raise ValueError('Invalid forwarded file message schedule')
                    if supplied:
                        for attach in forward.attaches:
                            if attach.type == 'FILE':
                                attach.file_item_type = 'DELAYED' if schedule > 0 else 'REGULAR'
                if known_version is not None:
                    for attach in forward.attaches:
                        if attach.type == 'FILE' and (not _finite_file_number(attach.file_update_time)
                                or attach.file_update_time < known_version):
                            attach.file_update_time = known_version
            if (allow_content and 'link' in raw) or forward is not None:
                changes['forwarded_json'] = json.dumps(forwarded_to_data(forward), ensure_ascii=False)
            self._db.execute("UPDATE messages SET " + ",".join(f"{key}=?" for key in changes) + " WHERE row=?",
                             (*changes.values(), target["row"]))
            row = self._db.execute("SELECT * FROM messages WHERE row=?", (target["row"],)).fetchone()
        return self._row_to_msg(row)

    def clear_chat_preview(self, chat_id: int) -> None:
        with self._lock:
            self._db.execute("UPDATE chats SET last_time_ms=NULL,last_preview=NULL WHERE id=?", (chat_id,))
            self._db.commit()

    def sole_message_at_time(self, chat_id: int, time_ms: int | float) -> Optional[Message]:
        """Return an unambiguous preview anchor, including pending/tombstone collisions."""
        if type(chat_id) is not int or not _finite_file_number(time_ms):
            return None
        with self._lock:
            rows = self._db.execute(
                'SELECT * FROM messages WHERE chat_id=? AND time_ms=? LIMIT 2',
                (chat_id, time_ms),
            ).fetchall()
            return self._row_to_msg(rows[0]) if len(rows) == 1 else None

    def message(self, chat_id: int, message_id: int) -> Optional[Message]:
        """Read one exact current cached server message, never a guessed window."""
        if any(type(value) is not int or not -(2**63) <= value < 2**63 for value in (chat_id, message_id)):
            raise ValueError('Actual message/chat IDs are required')
        with self._lock:
            rows = self._db.execute('SELECT * FROM messages WHERE chat_id=? AND server_id=? LIMIT 2',
                                    (chat_id, message_id)).fetchall()
        return self._row_to_msg(rows[0]) if len(rows) == 1 else None

    def refresh_message_forward_metadata(self, chat_id: int, raw: dict) -> None:
        """Enrich legacy forward rows; never roll newer nested content backward."""
        if (type(chat_id) is not int or not isinstance(raw, dict) or type(raw.get('id')) is not int
                or not -(2**63) <= raw['id'] < 2**63 or 'link' not in raw):
            return
        candidate = parse_forwarded_message(raw)
        if candidate is None or not candidate.available:
            return
        with self._lock, self._db:
            old = self.message(chat_id, raw['id'])
            if old is None or old.status != 'sent':
                return
            incoming = raw.get('updateTime')
            known = old.update_time_ms
            if old.forwarded is not None:
                if not (_finite_file_number(incoming) and _finite_file_number(known) and incoming > known):
                    return
            else:
                # Old source references are not proof by themselves. Upgrade only
                # a matching actual outer row and a fully parsed received snapshot.
                if (old.time_ms != raw.get('time') or old.sender != raw.get('sender')
                        or old.text != raw.get('text', '')
                        or known is not None and (not _finite_file_number(incoming) or incoming < known)):
                    return
                if old.link and (old.link.get('type') != 'FORWARD'
                        or old.link.get('chatId') != candidate.source_chat_id
                        or old.link.get('messageId') != candidate.source_message_id):
                    return
            self.apply_message_ack(chat_id, raw, replay=True)

    def refresh_message_file_metadata(self, chat_id: int, raw: dict) -> None:
        """Refresh only existing FILE attachment metadata from a full server row."""
        if (not isinstance(raw, dict) or type(raw.get('id')) is not int
                or raw.get('type') not in ('USER', 'CHANNEL', 'CHANNEL_ADMIN')
                or not _finite_file_number(raw.get('time'))):
            return
        with self._lock, self._db:
            row = self._db.execute('SELECT * FROM messages WHERE chat_id=? AND server_id=?',
                                   (chat_id, raw['id'])).fetchone()
            if row is None:
                return
            attaches = server_attaches(raw)
            old = self._row_to_msg(row)
            if old.status == 'removed':
                return
            if not any(attach.type == 'FILE' for attach in attaches + old.attaches):
                return
            previous_files = [attach for attach in old.attaches if attach.type == 'FILE']
            versions = [attach.file_update_time for attach in previous_files
                        if _finite_file_number(attach.file_update_time)]
            old_version = max(versions) if versions else None
            new_version = raw.get('updateTime') if _finite_file_number(raw.get('updateTime')) else None
            if previous_files:
                if old_version is not None and (new_version is None or new_version <= old_version):
                    return
                if old_version is None and any(attach.deleted is True or attach.file_metadata_confirmed is True
                                               for attach in previous_files):
                    return  # No ordering proof: a late49 cannot revoke newer128/ACK metadata.
            if (len(old.attaches) != len(attaches)
                    or any(previous.type != actual.type for previous, actual in zip(old.attaches, attaches))):
                # A confirmed newer layout invalidates the old FILE capability,
                # without guessing new indices or replacing other attachments.
                for attach in previous_files:
                    attach.file_metadata_confirmed = False
                    if new_version is not None:
                        attach.file_update_time = new_version
                if previous_files:
                    encoded = json.dumps([attach.__dict__ for attach in old.attaches], ensure_ascii=False)
                    self._db.execute('UPDATE messages SET attaches=? WHERE row=?', (encoded, row['row']))
                return
            if any(attach.type == 'CONTROL' for attach in old.attaches):
                incoming = Message.from_server(chat_id, raw, None)
                if (old.status != 'sent' or old.text != incoming.text
                        or old.time_ms != incoming.time_ms or old.sender != incoming.sender):
                    return
            # This helper owns FILE metadata only. In particular, an accepted
            # newer FILE version cannot overwrite a CONTROL that the separate
            # provenance guard rejected, nor another attachment's cached state.
            merged = [actual if previous.type == 'FILE' else previous
                      for previous, actual in zip(old.attaches, attaches)]
            encoded = json.dumps([attach.__dict__ for attach in merged], ensure_ascii=False) if merged else None
            if encoded != row['attaches']:
                self._db.execute('UPDATE messages SET attaches=? WHERE row=?', (encoded, row['row']))

    def refresh_message_photo_metadata(self, chat_id: int, raw: dict) -> None:
        """Propagate an actual PHOTO deletion onto an unchanged legacy row.

        The old PHOTO parser discarded ``deleted``. This repair is monotonic:
        it can revoke a preview, never restore one or replace a supplied URL.
        """
        if (type(chat_id) is not int or not -(2**63) <= chat_id < 2**63
                or not isinstance(raw, dict) or type(raw.get('id')) is not int
                or not -(2**63) <= raw['id'] < 2**63
                or raw.get('type') not in ('USER', 'CHANNEL', 'CHANNEL_ADMIN')
                or not _finite_file_number(raw.get('time'))
                or ('text' in raw and not isinstance(raw['text'], str))
                or ('sender' in raw and (type(raw['sender']) is not int
                                        or not -(2**63) <= raw['sender'] < 2**63))
                or ('updateTime' in raw and not _finite_file_number(raw['updateTime']))
                or ('status' in raw and raw['status'] not in ('EDITED', 'SPAM', 'DELAYED_FIRE_ERROR'))
                or not isinstance(raw.get('attaches'), list)
                or not all(isinstance(item, dict) for item in raw['attaches'])
                or not any(item.get('_type') == 'PHOTO' and item.get('deleted') is True
                           for item in raw['attaches'])):
            return
        try:
            incoming = Message.from_server(chat_id, raw, None)
        except (ValueError, TypeError, OverflowError):
            return
        with self._lock, self._db:
            row = self._db.execute('SELECT * FROM messages WHERE chat_id=? AND server_id=?',
                                   (chat_id, raw['id'])).fetchone()
            if row is None:
                return
            old = self._row_to_msg(row)
            if (old.status != 'sent' or old.text != incoming.text or old.time_ms != incoming.time_ms
                    or old.sender != incoming.sender or len(old.attaches) != len(incoming.attaches)
                    or old.forwarded is not None or incoming.forwarded is not None
                    or old.update_time_ms is not None and (incoming.update_time_ms is None
                        or incoming.update_time_ms < old.update_time_ms)):
                return
            fields = ('type', 'token', 'file_id', 'url', 'base_url', 'file_name', 'mime_type',
                      'size', 'width', 'height', 'duration_ms', 'local_path')
            if any(any(getattr(previous, field) != getattr(actual, field) for field in fields)
                   for previous, actual in zip(old.attaches, incoming.attaches)):
                return
            changed = False
            for index, (previous, actual) in enumerate(zip(old.attaches, incoming.attaches)):
                if (previous.type == actual.type == 'PHOTO' and previous.deleted is False
                        and raw['attaches'][index].get('deleted') is True):
                    previous.deleted = True
                    changed = True
            if changed:
                encoded = json.dumps([attach.__dict__ for attach in old.attaches], ensure_ascii=False)
                self._db.execute('UPDATE messages SET attaches=? WHERE row=?', (encoded, row['row']))

    def refresh_message_display_metadata(self, chat_id: int, raw: dict) -> None:
        """Enrich stripped legacy keyboard/share fields from a matching full row.

        Same-version history insert is deliberately a no-op elsewhere. This
        one-way projection cannot replace a proven button, roll back an edit,
        revive a removal or modify neighbouring media resources.
        """
        if (type(chat_id) is not int or not isinstance(raw, dict)
                or type(raw.get('id')) is not int or not -(2**63) <= raw['id'] < 2**63
                or raw.get('type') not in ('USER', 'CHANNEL', 'CHANNEL_ADMIN')
                or not _finite_file_number(raw.get('time'))
                or ('text' in raw and not isinstance(raw['text'], str))
                or ('sender' in raw and type(raw['sender']) is not int)
                or ('updateTime' in raw and not _finite_file_number(raw['updateTime']))
                or ('status' in raw and raw['status'] not in ('EDITED', 'SPAM', 'DELAYED_FIRE_ERROR'))
                or raw.get('deleted') is True or raw.get('removed') is True
                or not isinstance(raw.get('attaches'), list)
                or not all(isinstance(value, dict) for value in raw['attaches'])):
            return
        incoming = Message.from_server(chat_id, raw, None)
        with self._lock, self._db:
            row = self._db.execute('SELECT * FROM messages WHERE chat_id=? AND server_id=?',
                                   (chat_id, raw['id'])).fetchone()
            if row is None:
                return
            old = self._row_to_msg(row)
            if (old.status != 'sent' or old.display_source != 'outer'
                    or old.text != incoming.text or old.sender != incoming.sender or old.time_ms != incoming.time_ms
                    or len(old.attaches) != len(incoming.attaches)
                    or any(a.type != b.type for a, b in zip(old.attaches, incoming.attaches))):
                return
            versions = [value for value in [old.update_time_ms] + [
                value for attach in old.attaches for value in (attach.file_update_time, attach.control_update_time)]
                if value is not None]
            if (any(not _finite_file_number(value) for value in versions)
                    or versions and (not _finite_file_number(raw.get('updateTime'))
                                     or raw['updateTime'] < max(versions))):
                return
            changed = False
            for previous, actual in zip(old.attaches, incoming.attaches):
                if (previous.type == 'INLINE_KEYBOARD' and not previous.inline_metadata_confirmed
                        and not previous.inline_buttons and actual.inline_metadata_confirmed):
                    previous.inline_buttons = actual.inline_buttons
                    previous.inline_metadata_confirmed = True
                    previous.inline_open_app_metadata_confirmed = actual.inline_open_app_metadata_confirmed
                    changed = True
                elif (previous.type == 'INLINE_KEYBOARD' and previous.inline_metadata_confirmed is True
                        and previous.inline_open_app_metadata_confirmed is not True
                        and actual.inline_open_app_metadata_confirmed is True):
                    # The preceding release retained OPEN_APP labels, but not
                    # its optional contactId/startParam. Require all old display
                    # fields to agree before adding newly proven action fields.
                    from .message_presentation import normalized_button_rows
                    old_rows = normalized_button_rows(previous.inline_buttons)
                    new_rows = normalized_button_rows(actual.inline_buttons)
                    if old_rows is None or new_rows is None:
                        continue
                    if any(button['type'] == 'OPEN_APP' and ('contactId' in button or 'payload' in button)
                           for row in old_rows for button in row):
                        continue
                    labels = [[{key: value for key, value in button.items()
                                if button['type'] != 'OPEN_APP' or key not in ('contactId', 'payload')}
                               for button in row] for row in new_rows]
                    if old_rows == labels:
                        previous.inline_buttons = actual.inline_buttons
                        previous.inline_open_app_metadata_confirmed = True
                        changed = True
                elif (previous.type == 'SHARE' and not previous.share_metadata_confirmed
                        and all(getattr(previous, key) is None for key in
                                ('share_title', 'share_description', 'share_url'))
                        and previous.url == actual.url and actual.share_metadata_confirmed):
                    for key in ('share_metadata_confirmed', 'share_title', 'share_description', 'share_url'):
                        setattr(previous, key, getattr(actual, key))
                    changed = True
            if changed:
                encoded = json.dumps([attach.__dict__ for attach in old.attaches], ensure_ascii=False)
                self._db.execute('UPDATE messages SET attaches=? WHERE row=?', (encoded, row['row']))

    def refresh_message_control_metadata(self, chat_id: int, raw: dict) -> None:
        """Enrich an unchanged legacy row from actual full CONTROL metadata.

        This is a one-way provenance upgrade, not history/message replacement.
        It cannot overwrite a proven control, revive removed messages, change
        text/indices or replace another attachment's current resource metadata.
        """
        if (type(chat_id) is not int or not -(2**63) <= chat_id < 2**63
                or not isinstance(raw, dict) or type(raw.get('id')) is not int
                or not -(2**63) <= raw['id'] < 2**63
                or raw.get('type') not in ('USER', 'CHANNEL', 'CHANNEL_ADMIN')
                or not _finite_file_number(raw.get('time'))
                or ('text' in raw and not isinstance(raw['text'], str))
                or ('sender' in raw and (type(raw['sender']) is not int
                                        or not -(2**63) <= raw['sender'] < 2**63))
                or ('updateTime' in raw and not _finite_file_number(raw['updateTime']))
                or ('status' in raw and raw['status'] not in ('EDITED', 'SPAM', 'DELAYED_FIRE_ERROR'))
                or not isinstance(raw.get('attaches'), list)
                or not all(isinstance(attach, dict) for attach in raw['attaches'])):
            return
        eligible = {index for index, attach in enumerate(raw['attaches'])
                    if attach.get('_type') == 'CONTROL' and isinstance(attach.get('event'), str)
                    and all(key not in attach or isinstance(attach[key], str)
                            for key in ('message', 'shortMessage'))}
        if not eligible:
            return
        try:
            incoming = Message.from_server(chat_id, raw, None)
        except (ValueError, TypeError, OverflowError):
            return
        with self._lock, self._db:
            row = self._db.execute('SELECT * FROM messages WHERE chat_id=? AND server_id=?',
                                   (chat_id, raw['id'])).fetchone()
            if row is None:
                return
            old = self._row_to_msg(row)
            if (old.status != 'sent' or old.text != incoming.text
                    or old.time_ms != incoming.time_ms or old.sender != incoming.sender
                    or len(old.attaches) != len(incoming.attaches)):
                return
            resource_fields = ('type', 'token', 'file_id', 'url', 'base_url', 'file_name',
                               'mime_type', 'size', 'width', 'height', 'duration_ms', 'local_path',
                               'deleted', 'file_item_type')
            versions = [getattr(attach, field) for attach in old.attaches
                        for field in ('control_update_time', 'file_update_time')
                        if getattr(attach, field) is not None]
            if any(not _finite_file_number(value) for value in versions):
                return
            watermark = max(versions) if versions else None
            incoming_version = raw.get('updateTime')
            if watermark is not None and (incoming_version is None or incoming_version < watermark):
                return
            for previous, actual in zip(old.attaches, incoming.attaches):
                if previous.type != actual.type:
                    return
                if previous.type != 'CONTROL' and any(
                        getattr(previous, field) != getattr(actual, field) for field in resource_fields):
                    return
            changed = False
            for index, (previous, actual) in enumerate(zip(old.attaches, incoming.attaches)):
                if (previous.type != 'CONTROL' or previous.control_metadata_confirmed is True
                        or index not in eligible):
                    continue
                known, newer = previous.control_update_time, actual.control_update_time
                if known is not None and (not _finite_file_number(known)
                        or newer is None or newer < known):
                    continue
                fields = ('control_event', 'control_message', 'control_short_message')
                if any(getattr(previous, field) is not None
                       and getattr(previous, field) != getattr(actual, field) for field in fields):
                    continue  # Existing conflicting source metadata is not a legacy omission.
                for field in (*fields, 'control_metadata_confirmed', 'control_update_time'):
                    value = getattr(actual, field)
                    if getattr(previous, field) != value:
                        setattr(previous, field, value)
                        changed = True
            if changed:
                encoded = json.dumps([attach.__dict__ for attach in old.attaches], ensure_ascii=False)
                self._db.execute('UPDATE messages SET attaches=? WHERE row=?', (encoded, row['row']))

    def messages(self, chat_id: int, limit: int = 300) -> list[Message]:
        with self._lock:
            rows = self._db.execute(
                """SELECT * FROM (
                       SELECT * FROM messages WHERE chat_id=?
                       ORDER BY time_ms DESC, row DESC LIMIT ?
                   ) ORDER BY time_ms ASC, row ASC""",
                (chat_id, limit),
            ).fetchall()
        return [self._row_to_msg(r) for r in rows]

    def update_message_reactions(self, chat_id: int, message_id: int, info: dict, *, replace: bool = False) -> None:
        supplied = parse_reaction_info(info)
        if not supplied and not replace:
            return
        with self._lock:
            row = self._db.execute(
                "SELECT reaction_info FROM messages WHERE chat_id=? AND server_id=?",
                (chat_id, message_id),
            ).fetchone()
            if row is None:
                return
            previous = {}
            if not replace:
                try:
                    previous = parse_reaction_info(json.loads(row["reaction_info"] or "{}"))
                except (ValueError, TypeError):
                    previous = {}
            previous.update(supplied)
            self._db.execute("UPDATE messages SET reaction_info=? WHERE chat_id=? AND server_id=?",
                             (json.dumps(previous, ensure_ascii=False), chat_id, message_id))
            self._db.commit()

    def delete_messages(self, chat_id: int, message_ids: list[int]) -> None:
        if not message_ids:
            return
        with self._lock:
            self._db.executemany("DELETE FROM messages WHERE chat_id=? AND server_id=?",
                                 ((chat_id, identity) for identity in message_ids))
            self._db.commit()

    def message_was_deleted(self, chat_id: int, message_id: int) -> bool:
        """Only the current confirmed owner's exact message ID is tombstoned."""
        if any(type(value) is not int or not -(2**63) <= value < 2**63
               for value in (chat_id, message_id)):
            return False
        with self._lock:
            owner = self.cache_owner
            return owner is not None and self._db.execute(
                'SELECT 1 FROM confirmed_message_deletions WHERE owner_id=? AND chat_id=? AND message_id=?',
                (owner, chat_id, message_id)).fetchone() is not None

    def confirm_message_deletions(self, owner_id: int, chat_id: int, message_ids,
                                  *, deleted_last_time=None) -> None:
        """Atomically retain confirmed IDs, remove rows and repair their preview."""
        identities = tuple(message_ids)
        if any(type(value) is not int or not -(2**63) <= value < 2**63
               for value in (owner_id, chat_id, *identities)):
            raise ValueError('Confirmed owner/chat/message IDs are required')
        if not identities:
            return
        identities = tuple(dict.fromkeys(identities))
        with self.account_scope(owner_id), self._db:
            current = self.get_chat(chat_id)
            affected_times = {row['time_ms'] for identity in identities for row in self._db.execute(
                'SELECT time_ms FROM messages WHERE chat_id=? AND server_id=?', (chat_id, identity))}
            if _finite_file_number(deleted_last_time):
                affected_times.add(deleted_last_time)
            repair_preview = (current is not None and current.last_time_ms is not None
                              and current.last_time_ms in affected_times)
            self._db.executemany('INSERT OR IGNORE INTO confirmed_message_deletions VALUES(?,?,?)',
                                 ((owner_id, chat_id, identity) for identity in identities))
            self._db.executemany('DELETE FROM messages WHERE chat_id=? AND server_id=?',
                                 ((chat_id, identity) for identity in identities))
            if repair_preview:
                row = self._db.execute("SELECT * FROM messages WHERE chat_id=? AND status!='removed' "
                                       'ORDER BY time_ms DESC,row DESC LIMIT 1', (chat_id,)).fetchone()
                candidate = self._row_to_msg(row) if row is not None else None
                self._db.execute('UPDATE chats SET last_time_ms=?,last_preview=? WHERE id=?',
                    (candidate.time_ms if candidate else None, candidate.preview if candidate else None, chat_id))

    def repair_preview_after_removal(self, chat_id: int, removed_time) -> bool:
        """Move a chat preview off a server-removed message to the latest visible row."""
        if type(chat_id) is not int or not _finite_file_number(removed_time):
            return False
        with self._lock, self._db:
            current = self.get_chat(chat_id)
            if current is None or current.last_time_ms != removed_time:
                return False
            row = self._db.execute("SELECT * FROM messages WHERE chat_id=? AND status!='removed' "
                                   'ORDER BY time_ms DESC,row DESC LIMIT 1', (chat_id,)).fetchone()
            candidate = self._row_to_msg(row) if row is not None else None
            self._db.execute('UPDATE chats SET last_time_ms=?,last_preview=? WHERE id=?',
                (candidate.time_ms if candidate else None, candidate.preview if candidate else None, chat_id))
            return True

    def conversation_action_journal(self, owner_id, chat_id):
        with self.account_scope(owner_id):
            row = self._db.execute('SELECT payload FROM conversation_action_journal WHERE owner_id=? AND chat_id=?',
                                   (owner_id, chat_id)).fetchone()
            if row is None:
                return None
            value = json.loads(row['payload'])
            if not isinstance(value, dict):
                raise ValueError('Журнал действия повреждён. Повтор не выполнен.')
            return value

    def reserve_conversation_action(self, owner_id, chat_id, record):
        with self.account_scope(owner_id), self._db:
            try:
                old = self.conversation_action_journal(owner_id, chat_id)
                if old and old.get('action') in ('bot_delete', 'owner_transfer') and old.get('stage') == 'complete':
                    self._db.execute('DELETE FROM conversation_action_journal WHERE owner_id=? AND chat_id=?',
                                     (owner_id, chat_id))
                if record.get('action') == 'bot_delete':
                    self._db.execute('INSERT INTO bot_delete_peer_reservation VALUES(?,?,?,?)',
                                     (owner_id, record['peer_id'], chat_id, record['operation_id']))
                self._db.execute('INSERT INTO conversation_action_journal VALUES(?,?,?)',
                    (owner_id, chat_id, json.dumps(record, allow_nan=False)))
            except sqlite3.IntegrityError:
                raise ValueError('Предыдущее действие с чатом ещё не подтверждено') from None

    def finish_conversation_action(self, owner_id, chat_id):
        with self.account_scope(owner_id), self._db:
            self._db.execute('DELETE FROM bot_delete_peer_reservation WHERE owner_id=? AND chat_id=?',
                             (owner_id, chat_id))
            self._db.execute('DELETE FROM conversation_action_journal WHERE owner_id=? AND chat_id=?',
                             (owner_id, chat_id))

    def compare_conversation_action(self, owner_id, chat_id, expected, replacement, *, delete_cutoff=None,
                                    departure_cutoff=None, edit_title=None):
        """CAS one durable operation; never acknowledge or clear a replacement."""
        with self.account_scope(owner_id), self._db:
            if self.conversation_action_journal(owner_id, chat_id) != expected:
                return False
            if edit_title is not None:
                if (replacement is not None or expected.get('action') != 'conversation_edit'
                        or not isinstance(edit_title, str) or not edit_title
                        or expected.get('patch', {}).get('theme') != edit_title
                        or expected.get('chat_type') not in ('CHAT', 'CHANNEL')):
                    raise ValueError('Серверное название не подтверждено исходным изменением')
                self._db.execute('UPDATE chats SET title=?,title_locked=0 WHERE id=? AND chat_type=?',
                                 (edit_title, chat_id, expected['chat_type']))
            if delete_cutoff is not None:
                from math import isfinite
                ordinary_delete = (replacement is None and expected.get('action') == 'delete'
                    and expected.get('acknowledged') is True
                    and type(delete_cutoff) in (int, float) and 0 <= delete_cutoff < 2**63
                    and isfinite(delete_cutoff) and type(expected.get('cutoff')) in (int, float)
                    and delete_cutoff == expected['cutoff'])
                bot_delete = (bool(replacement) and expected.get('action') == 'bot_delete'
                    and replacement.get('stage') == 'complete'
                    and replacement.get('stop_confirmed') is True
                    and replacement.get('delete_confirmed') is True)
                if not (ordinary_delete or bot_delete):
                    raise ValueError('Удаление не подтверждено исходной операцией')
                self._clear_confirmed_history_rows(owner_id, chat_id, delete_cutoff, True)
            if departure_cutoff is not None:
                if (not replacement or expected.get('action') != 'owner_transfer'
                        or replacement.get('stage') != 'complete'
                        or replacement.get('transfer_confirmed') is not True
                        or replacement.get('leave_confirmed') is not True
                        or expected.get('chat_type') not in ('CHAT', 'CHANNEL')):
                    raise ValueError('Выход не подтверждён исходной операцией')
                self._db.execute('INSERT INTO conversation_departures VALUES(?,?,?,?) '
                    'ON CONFLICT(owner_id,chat_id) DO UPDATE SET cutoff=MAX(cutoff,excluded.cutoff),chat_type=excluded.chat_type',
                    (owner_id, chat_id, departure_cutoff, expected['chat_type']))
            if replacement is None:
                self._db.execute('DELETE FROM conversation_action_journal WHERE owner_id=? AND chat_id=?',
                                 (owner_id, chat_id))
            else:
                self._db.execute('UPDATE conversation_action_journal SET payload=? WHERE owner_id=? AND chat_id=?',
                    (json.dumps(replacement, allow_nan=False), owner_id, chat_id))
            if replacement is None or replacement.get('stage') == 'complete':
                self._db.execute('DELETE FROM bot_delete_peer_reservation '
                    'WHERE owner_id=? AND chat_id=? AND operation_id=?',
                    (owner_id, chat_id, expected.get('operation_id')))
            return True

    def bot_delete_for_peer(self, owner_id, peer_id):
        with self.account_scope(owner_id):
            row = self._db.execute('SELECT chat_id,operation_id FROM bot_delete_peer_reservation '
                                  'WHERE owner_id=? AND peer_id=?', (owner_id, peer_id)).fetchone()
            if row is None:
                return None
            record = self.conversation_action_journal(owner_id, row['chat_id'])
            if (not record or record.get('action') != 'bot_delete'
                    or record.get('operation_id') != row['operation_id'] or record.get('peer_id') != peer_id):
                raise ValueError('Журнал удаления бота повреждён. Повтор не выполнен.')
            return record

    def acknowledge_conversation_action(self, owner_id, chat_id):
        with self.account_scope(owner_id), self._db:
            record = self.conversation_action_journal(owner_id, chat_id)
            if record is None:
                raise ValueError('Нет исходной записи действия')
            record['acknowledged'] = True
            self._db.execute('UPDATE conversation_action_journal SET payload=? WHERE owner_id=? AND chat_id=?',
                (json.dumps(record, allow_nan=False), owner_id, chat_id))

    def conversation_report_journal(self, owner_id, type_id, target_id):
        with self.account_scope(owner_id):
            row = self._db.execute('SELECT payload FROM conversation_report_journal '
                'WHERE owner_id=? AND type_id=? AND target_id=?', (owner_id, type_id, target_id)).fetchone()
            if row is None:
                return None
            value = json.loads(row['payload'])
            if (not isinstance(value, dict) or value.get('status') not in ('pending', 'confirmed')
                    or not isinstance(value.get('operation_id'), str) or not value['operation_id']):
                raise ValueError('Журнал жалобы повреждён. Повтор не выполнен.')
            return value

    def reserve_conversation_report(self, owner_id, type_id, target_id, record):
        with self.account_scope(owner_id), self._db:
            try:
                self._db.execute('INSERT INTO conversation_report_journal VALUES(?,?,?,?)',
                    (owner_id, type_id, target_id, json.dumps(record, allow_nan=False)))
            except sqlite3.IntegrityError:
                raise ValueError('Жалоба уже отправлена или её результат не подтверждён.') from None

    def settle_conversation_report(self, owner_id, type_id, target_id, operation_id, *, confirmed):
        if type(confirmed) is not bool:
            raise ValueError('Не подтверждён результат жалобы')
        with self.account_scope(owner_id), self._db:
            record = self.conversation_report_journal(owner_id, type_id, target_id)
            if record is None or record['operation_id'] != operation_id or record['status'] != 'pending':
                return False
            if confirmed:
                record['status'] = 'confirmed'
                self._db.execute('UPDATE conversation_report_journal SET payload=? '
                    'WHERE owner_id=? AND type_id=? AND target_id=?',
                    (json.dumps(record, allow_nan=False), owner_id, type_id, target_id))
            else:
                self._db.execute('DELETE FROM conversation_report_journal '
                    'WHERE owner_id=? AND type_id=? AND target_id=?', (owner_id, type_id, target_id))
            return True

    def conversation_departure(self, chat_id):
        with self._lock:
            row = self._db.execute('SELECT cutoff,chat_type FROM conversation_departures WHERE owner_id=? AND chat_id=?',
                                   (self.cache_owner, chat_id)).fetchone()
            return dict(row) if row is not None else None

    def confirm_conversation_departure(self, owner_id, chat_id, cutoff, chat_type):
        """Hide confirmed departed membership, retaining history and drafts."""
        with self.account_scope(owner_id), self._db:
            self._db.execute('INSERT INTO conversation_departures VALUES(?,?,?,?) '
                'ON CONFLICT(owner_id,chat_id) DO UPDATE SET cutoff=MAX(cutoff,excluded.cutoff),chat_type=excluded.chat_type',
                (owner_id, chat_id, cutoff, chat_type))

    def clear_conversation_departure(self, owner_id, chat_id):
        with self.account_scope(owner_id), self._db:
            self._db.execute('DELETE FROM conversation_departures WHERE owner_id=? AND chat_id=?',
                             (owner_id, chat_id))

    def history_boundary(self, chat_id):
        with self._lock:
            row = self._db.execute('SELECT cutoff,deleted FROM conversation_history_boundary WHERE owner_id=? AND chat_id=?',
                                   (self.cache_owner, chat_id)).fetchone()
            return dict(row) if row is not None else None

    def history_was_cleared(self, chat_id, timestamp):
        boundary = self.history_boundary(chat_id)
        return bool(boundary and type(timestamp) in (int, float) and timestamp <= boundary['cutoff'])

    def clear_confirmed_history(self, owner_id, chat_id, cutoff, *, deleted=False):
        """Apply a confirmed server boundary; preserve newer/unsent local messages."""
        with self.account_scope(owner_id), self._db:
            self._clear_confirmed_history_rows(owner_id, chat_id, cutoff, deleted)

    def _clear_confirmed_history_rows(self, owner_id, chat_id, cutoff, deleted):
        """Transaction body also used by the final bot journal CAS."""
        self._db.execute('INSERT INTO conversation_history_boundary VALUES(?,?,?,?) '
            'ON CONFLICT(owner_id,chat_id) DO UPDATE SET cutoff=MAX(cutoff,excluded.cutoff),deleted=excluded.deleted',
            (owner_id, chat_id, cutoff, int(deleted)))
        self._db.execute('DELETE FROM messages WHERE chat_id=? AND server_id IS NOT NULL AND time_ms<=?',
                         (chat_id, cutoff))
        self._db.execute('UPDATE chats SET last_preview=NULL,last_time_ms=NULL,unread=0 '
                         'WHERE id=? AND (last_time_ms IS NULL OR last_time_ms<=?)', (chat_id, cutoff))

    @staticmethod
    def _row_to_msg(r: sqlite3.Row) -> Message:
        attaches = []
        if r["attaches"]:
            try:
                attaches = [Attach(**a) for a in json.loads(r["attaches"])]
            except (ValueError, TypeError):
                attaches = []
        try:
            reaction_info = parse_reaction_info(json.loads(r["reaction_info"] or "{}"))
        except (ValueError, TypeError):
            reaction_info = {}
        try:
            link = parse_message_link(json.loads(r["link_json"] or "null"))
        except (ValueError, TypeError):
            link = None
        forward = None
        try:
            data = json.loads(r['forwarded_json'] or 'null')
            if data is not None:
                forward = forwarded_from_data(data) or ForwardedMessage()
        except (ValueError, TypeError, KeyError):
            forward = ForwardedMessage()
        return Message(
            chat_id=r["chat_id"], id=r["server_id"], sender=r["sender"],
            text=r["text"] or "", time_ms=r["time_ms"] or 0,
            outgoing=bool(r["outgoing"]), status=r["status"] or "sent",
            local_id=r["local_id"], attaches=attaches,
            reaction_info=reaction_info,
            link=link,
            update_time_ms=r['update_time_ms'],
            forwarded=forward,
        )
