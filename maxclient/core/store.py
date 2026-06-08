"""Локальный кэш SQLite: чаты, контакты, история сообщений.

История подтягивается с сервера, но кэшируется локально, чтобы чат открывался
мгновенно и работал офлайн. Контакты переживают перезапуск (важно для импорта
отдельной кнопкой). Доступ потокобезопасен через единый lock.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Optional

from .models import Attach, Chat, Contact, Message

_SCHEMA = """
CREATE TABLE IF NOT EXISTS chats (
    id            INTEGER PRIMARY KEY,
    title         TEXT,
    is_group      INTEGER DEFAULT 0,
    last_time_ms  INTEGER,
    last_preview  TEXT,
    unread        INTEGER DEFAULT 0,
    avatar_url    TEXT,
    title_locked  INTEGER DEFAULT 0,
    peer_user_id  INTEGER
);
CREATE TABLE IF NOT EXISTS contacts (
    id     INTEGER PRIMARY KEY,
    name   TEXT,
    phone  TEXT
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
    attaches   TEXT
);
CREATE INDEX IF NOT EXISTS idx_msg_chat ON messages(chat_id, time_ms);
CREATE UNIQUE INDEX IF NOT EXISTS idx_msg_server ON messages(chat_id, server_id)
    WHERE server_id IS NOT NULL;
"""


class Store:
    def __init__(self, db_path: Path) -> None:
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(db_path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.executescript(_SCHEMA)
            self._db.commit()
        self._migrate()

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

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # ───────────────────────── chats ─────────────────────────

    def upsert_chat(self, chat: Chat) -> None:
        with self._lock:
            self._db.execute(
                """INSERT INTO chats(id, title, is_group, last_time_ms, last_preview, unread, avatar_url)
                   VALUES(?,?,?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET
                     title=CASE WHEN chats.title_locked=1 THEN chats.title
                                ELSE COALESCE(excluded.title, chats.title) END,
                     is_group=excluded.is_group,
                     avatar_url=COALESCE(excluded.avatar_url, chats.avatar_url)""",
                (
                    chat.id, chat.title, int(chat.is_group), chat.last_time_ms,
                    chat.last_preview, chat.unread, chat.avatar_url,
                ),
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
                "SELECT * FROM chats ORDER BY COALESCE(last_time_ms,0) DESC, id DESC"
            ).fetchall()
        return [self._row_to_chat(r) for r in rows]

    def get_chat(self, chat_id: int) -> Optional[Chat]:
        with self._lock:
            r = self._db.execute("SELECT * FROM chats WHERE id=?", (chat_id,)).fetchone()
        return self._row_to_chat(r) if r else None

    def update_chat_preview(
        self, chat_id: int, time_ms: int, preview: str, inc_unread: int = 0
    ) -> None:
        with self._lock:
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
        )

    # ───────────────────────── contacts ─────────────────────────

    def upsert_contact(self, c: Contact) -> None:
        with self._lock:
            self._db.execute(
                """INSERT INTO contacts(id, name, phone) VALUES(?,?,?)
                   ON CONFLICT(id) DO UPDATE SET
                     name=COALESCE(excluded.name, contacts.name),
                     phone=COALESCE(excluded.phone, contacts.phone)""",
                (c.id, c.name, c.phone),
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
        return [Contact(id=r["id"], name=r["name"], phone=r["phone"]) for r in rows]

    def delete_contact(self, contact_id: int) -> None:
        with self._lock:
            self._db.execute("DELETE FROM contacts WHERE id=?", (contact_id,))
            self._db.commit()

    # ───────────────────────── messages ─────────────────────────

    def insert_message(self, m: Message) -> None:
        with self._lock:
            if self._insert_one(m):
                self._db.commit()

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
                                    outgoing, status, local_id, attaches)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (
                m.chat_id, m.id, m.sender, m.text, m.time_ms,
                int(m.outgoing), m.status, m.local_id, attaches_json,
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

    @staticmethod
    def _row_to_msg(r: sqlite3.Row) -> Message:
        attaches = []
        if r["attaches"]:
            try:
                attaches = [Attach(**a) for a in json.loads(r["attaches"])]
            except (ValueError, TypeError):
                attaches = []
        return Message(
            chat_id=r["chat_id"], id=r["server_id"], sender=r["sender"],
            text=r["text"] or "", time_ms=r["time_ms"] or 0,
            outgoing=bool(r["outgoing"]), status=r["status"] or "sent",
            local_id=r["local_id"], attaches=attaches,
        )
