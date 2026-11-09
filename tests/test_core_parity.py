"""Synthetic offline checks for metadata, account isolation and ordinary actions."""

import copy
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from maxclient.core.config import AppPaths, Session, Settings
from maxclient.core.models import Chat, Contact, Message
from maxclient.core.service import MaxService, _extract_chats
from maxclient.core.store import Store
from maxclient.protocol.client import MaxFrame
from test_token_service import FakeClient, confirm_fake_transport, login_frame


STAMP = 1_700_000_000_000


def response(opcode, decoded=None, cmd=1, *, auth_context=None):
    return MaxFrame(cmd=cmd, seq=1, opcode=opcode, body=b"", decoded=decoded, auth_context=auth_context)


class CoreClient(FakeClient):
    def __init__(self, **options):
        super().__init__(**options)
        self.calls = []
        self.action_result = {}
        self.action_error = None
        self.history_error = None
        self.history_messages = []
        self.window_result = {"messages": []}
        self.page_result = {"chats": [], "marker": 0}
        self.password_result = response(113, {"trackId": "checked-track"})
        self.status_result = response(104, {"password": {"enabled": True}})

    def set_peer(self, *args):
        pass

    def mark_read(self, chat_id, **kwargs):
        self.calls.append((50, chat_id, kwargs))
        if self.action_error:
            raise self.action_error
        return copy.deepcopy(self.action_result)

    def set_reaction(self, *args, **kwargs):
        self.calls.append((178, args, kwargs))
        if self.action_error:
            raise self.action_error
        return copy.deepcopy(self.action_result)

    def remove_reaction(self, *args, **kwargs):
        self.calls.append((179, args, kwargs))
        return copy.deepcopy(self.action_result)

    def list_chats(self, **kwargs):
        self.calls.append((53, kwargs))
        return copy.deepcopy(self.page_result)

    def chat_history(self, chat_id, **kwargs):
        self.calls.append((49, chat_id, kwargs))
        if self.history_error:
            raise self.history_error
        return copy.deepcopy(self.history_messages), b""

    def chat_history_window(self, chat_id, **kwargs):
        self.calls.append((49, chat_id, kwargs))
        if self.history_error:
            raise self.history_error
        return copy.deepcopy(self.window_result)

    def auth_create_track(self, kind, *, expected_session=None):
        self.calls.append((112, kind))
        return "initial-track"

    def auth_check_password(self, track_id, password, *, expected_session=None):
        self.calls.append((113, track_id))
        return self.password_result

    def auth_set_2fa(self, track_id, new_password, hint, *, capability, expected_session=None):
        self.calls.append((111, track_id, capability))
        if self.action_error:
            raise self.action_error
        return response(111)

    def auth_verify_email(self, track_id, email, *, expected_session=None):
        self.calls.append((109, track_id))
        return response(109, {"trackId": "email-track", "blockingDuration": 0})

    def auth_2fa_details(self):
        self.calls.append((104,))
        return self.status_result

    def close_other_sessions(self):
        self.calls.append((97, {}))
        return {"token": "synthetic-opaque-ack"}


class CoreParityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="max-core-parity-")
        self.environment = patch.dict(os.environ, {"MAX_DESKTOP_DATA_DIR": self.temporary.name})
        self.environment.start()
        self.paths = AppPaths()
        with patch("maxclient.core.service.MaxClient", CoreClient):
            self.service = MaxService(self.paths, Settings.load(self.paths), Session.load(self.paths))
        self.chat = {"id": 42, "type": "DIALOG", "participants": {123: STAMP, 77: STAMP},
                     "newMessages": 5, "lastMessage": {"id": 100, "time": STAMP, "text": "Latest"}}
        contact = {"id": 77, "names": [{"name": "Example peer"}],
                   "baseUrl": "https://cdn.example.test/direct-avatar", "options": ["OFFICIAL"]}
        self.service._finish_login(login_frame(chats=[self.chat], contacts=[contact]), None)
        confirm_fake_transport(self.service)

    def tearDown(self):
        self.assertFalse(self.service.client.login_calls)
        self.assertFalse(self.service.client.connect_calls)
        self.service.shutdown()
        self.environment.stop()
        self.temporary.cleanup()

    def test_actual_chat_type_and_new_messages_drive_sections(self):
        channel = {"id": 43, "type": "CHANNEL", "newMessages": 0, "title": "Example channel"}
        self.service._remember_chat_info({"chats": [channel]}, my_id=123)
        self.assertEqual([chat.id for chat in self.service.list_chats("new")], [42])
        self.assertEqual([chat.id for chat in self.service.list_chats("channels")], [43])
        self.assertEqual(self.service.chat_section_counts(), {"all": 2, "new": 1, "channels": 1})
        peer = next(chat for chat in self.service.list_chats() if chat.id == 42)
        self.assertEqual(peer.display, "Example peer")
        self.assertEqual(peer.avatar_url, "https://cdn.example.test/direct-avatar")
        self.assertTrue(peer.is_verified)

    def test_malformed_binary_or_other_event_cannot_manufacture_a_message(self):
        hidden_keys = b'\xa6chatId\x2a\xa4text\xa6hidden\xa6sender\x4d\xa2id\x64\xa4time\x01'
        self.service._on_push(MaxFrame(cmd=0, seq=7, opcode=128, body=hidden_keys, decoded=None))
        payload = {'chatId': 42, 'message': {'id': 101, 'sender': 77, 'time': STAMP, 'text': 'Synthetic'}}
        self.service._on_push(response(999, payload, cmd=0, auth_context=self.service.client.authenticated_session))
        self.assertEqual(self.service.store.messages(42), [])

    def test_message_push_requires_typed_identity_and_actual_server_time(self):
        payload = {'chatId': 42, 'message': {'id': 101, 'sender': 77, 'text': 'Synthetic', 'type': 'USER'}}
        self.service._on_push(response(128, payload, cmd=0, auth_context=self.service.client.authenticated_session))
        payload['message']['time'] = True
        self.service._on_push(response(128, payload, cmd=0, auth_context=self.service.client.authenticated_session))
        payload['message']['time'] = STAMP
        payload['message']['sender'] = '77'
        self.service._on_push(response(128, payload, cmd=0, auth_context=self.service.client.authenticated_session))
        self.assertEqual(self.service.store.messages(42), [])

    def test_confirmed_message_push_keeps_server_time_and_emits_once(self):
        received = []
        self.service.on_message = received.append
        literal_text = 'Термин attaches сохраняется буквально; reactionInfo — тоже.'
        link = {'type': 'REPLY', 'messageId': 99}
        reactions = {'totalCount': 1, 'counters': [{'reaction': '❤', 'count': 1}]}
        payload = {'chatId': 42, 'message': {'id': 101, 'sender': 77, 'time': STAMP,
                                            'text': literal_text, 'link': link, 'reactionInfo': reactions, 'type': 'USER'}}
        self.service._on_push(response(128, payload, cmd=0, auth_context=self.service.client.authenticated_session))
        self.service._on_push(response(128, payload, cmd=0, auth_context=self.service.client.authenticated_session))
        self.assertEqual(len(received), 1)
        self.assertTrue(self.service.is_current_message_event(received[0]))
        self.assertEqual(received[0].message.time_ms, STAMP)
        self.assertEqual(self.service.store.messages(42)[0].text, literal_text)
        self.assertEqual(self.service.store.messages(42)[0].link, link)
        self.assertEqual(self.service.store.messages(42)[0].reaction_info, reactions)

    def test_partial_metadata_preserves_unread_and_explicit_zero_clears_it(self):
        self.service._remember_chat_info({"chats": [{"id": 42, "type": "DIALOG"}]}, my_id=123)
        self.assertEqual(self.service.store.get_chat(42).unread, 5)
        self.service._remember_chat_info({"chats": [{"id": 42, "newMessages": 0}]}, my_id=123)
        self.assertEqual(self.service.store.get_chat(42).unread, 0)

    def test_unverified_aliases_do_not_create_badges_or_counters(self):
        chat = _extract_chats({"chats": [{"id": 45, "type": "DIALOG", "title": "Channel",
                                          "unreadCount": 99, "pinned": True, "muted": True,
                                          "verified": True}]}, my_id=123)[0]
        self.assertEqual(chat.unread, 0)
        self.assertFalse(chat.is_channel or chat.is_pinned or chat.is_muted or chat.is_verified)

    def test_metadata_and_manual_title_survive_store_restart(self):
        self.service.store.rename_chat(42, "Local alias")
        self.service._remember_chat_info({"chats": [{**self.chat, "title": "Server title"}]}, my_id=123)
        self.service.store.close()
        self.service.store = Store(self.paths.db_file)
        chat = self.service.list_chats()[0]
        self.assertEqual(chat.display, "Local alias")
        self.assertEqual(chat.unread, 5)
        self.assertEqual(chat.peer_user_id, 77)
        self.assertTrue(chat.is_verified)
        self.assertEqual(self.service.store.cache_owner, 123)

    def test_explicit_badge_fields_persist_without_guessing_partial_dto_values(self):
        supplied = frozenset({"chat_type", "is_channel", "is_pinned", "pin_order", "is_muted", "is_verified"})
        self.service.store.upsert_chat(Chat(id=90, chat_type="CHANNEL", is_channel=True,
                                             is_pinned=True, pin_order=2, is_muted=True,
                                             is_verified=True, server_fields=supplied))
        self.service._remember_chat_info({"chats": [{"id": 90, "type": "CHANNEL"}]}, my_id=123)
        self.service.store.close()
        self.service.store = Store(self.paths.db_file)
        chat = self.service.store.list_chats()[0]
        self.assertEqual(chat.id, 90)
        self.assertTrue(chat.is_channel and chat.is_pinned and chat.is_muted and chat.is_verified)
        self.assertEqual(chat.pin_order, 2)
        self.service.store.upsert_chat(Chat(id=90, is_pinned=False, server_fields=frozenset({"is_pinned"})))
        self.assertEqual(self.service.store.list_chats()[0].id, 42)

    def test_unknown_cache_owner_is_cleared_before_a_confirmed_login(self):
        self.service.store.clear_cache()
        self.service.store.upsert_chat(Chat(id=999, title="Other account cache"))
        self.service.store.upsert_contact(Contact(id=888, name="Other account contact"))
        self.service.store.insert_message(Message(chat_id=999, id=9, text="Other account message"))
        self.service.session.my_user_id = None
        self.service._contacts_map()
        self.service._finish_login(login_frame(chats=[self.chat], contacts=[]), None)
        self.assertEqual([chat.id for chat in self.service.list_chats()], [42])
        self.assertFalse(self.service.list_contacts())
        self.assertFalse(self.service.store.messages(999))
        self.assertEqual(self.service.store.cache_owner, 123)

    def test_matching_persisted_cache_owner_survives_missing_session_profile_metadata(self):
        self.service.store.insert_message(Message(chat_id=42, id=100, text="Owned cached message"))
        self.service.session.my_user_id = None
        self.service._finish_login(login_frame(chats=[self.chat], contacts=[]), None)
        self.assertEqual(len(self.service.store.messages(42)), 1)

    def test_read_state_updates_only_from_acknowledged_count(self):
        self.service._server_chats[42]['participants'][123] = STAMP - 1
        self.service.client.action_result = {"success": False, "unread": 0}
        with self.assertRaises(ValueError):
            self.service.mark_chat_read(42, message_id=100)
        self.assertEqual(self.service.store.get_chat(42).unread, 5)
        self.service.client.action_result = {"success": True}
        self.service.mark_chat_read(42, message_id=100)
        self.assertEqual(self.service.store.get_chat(42).unread, 5)
        self.service.client.action_result = {"success": True, "unread": 0, "mark": STAMP}
        self.service.mark_chat_read(42, message_id=100)
        self.assertEqual(self.service.store.get_chat(42).unread, 0)

    def test_reaction_failures_do_not_optimistically_change_message(self):
        self.service.store.insert_message(Message(chat_id=42, id=100, text="Visible message"))
        self.service.client.action_error = ConnectionError("Synthetic server failure")
        with self.assertRaises(ConnectionError):
            self.service.set_message_reaction(42, 100, "sample-emoji", context=self.service.search_context())
        self.assertEqual(self.service.local_history(42)[0].reaction_info, {})
        self.service.client.action_error = None
        info = {"counters": [{"reaction": "sample-emoji", "count": 4}],
                "totalCount": 4, "yourReaction": "sample-emoji"}
        self.service.client.action_result = {"reactionInfo": info}
        self.service.set_message_reaction(42, 100, "sample-emoji", context=self.service.search_context())
        self.assertEqual(self.service.local_history(42)[0].reaction_info, info)
        self.service.client.action_result = {"reactionInfo": {"counters": [], "totalCount": 0,
                                                                "yourReaction": ""}}
        self.service.remove_message_reaction(42, 100, context=self.service.search_context())
        self.assertEqual(self.service.local_history(42)[0].reaction_info["totalCount"], 0)

    def test_explicit_reaction_snapshot_clears_absent_selection_but_omitted_snapshot_does_not(self):
        initial = {"counters": [{"reaction": "sample-emoji", "count": 1}], "yourReaction": "sample-emoji"}
        self.service.store.insert_message(Message(chat_id=42, id=100, text="Visible", reaction_info=initial))
        self.service.client.action_result = {}
        self.service.remove_message_reaction(42, 100, context=self.service.search_context())
        self.assertEqual(self.service.local_history(42)[0].reaction_info, initial)
        self.service.client.action_result = {"reactionInfo": {"counters": [{"reaction": "other", "count": 2}]}}
        self.service.remove_message_reaction(42, 100, context=self.service.search_context())
        self.assertNotIn("yourReaction", self.service.local_history(42)[0].reaction_info)
        self.service.client.action_result = {"reactionInfo": {}}
        self.service.remove_message_reaction(42, 100, context=self.service.search_context())
        self.assertEqual(self.service.local_history(42)[0].reaction_info, {})

    def test_history_errors_propagate_without_throttling_a_retry(self):
        self.service.store.insert_message(Message(chat_id=42, id=100, text="Cached"))
        self.service.client.history_error = ConnectionError("Synthetic refresh failure")
        for _ in range(2):
            with self.assertRaises(ConnectionError):
                self.service.open_chat(42)
        self.assertNotIn(42, self.service._chat_fetch_ts)
        self.assertEqual(len(self.service.local_history(42)), 1)
        self.assertEqual(self.service.store.get_chat(42).unread, 5)
        self.assertEqual(len(self.service.client.calls), 2)

    def test_older_history_cannot_regress_latest_chat_preview(self):
        self.service.client.window_result = {"messages": [{"id": 90, "sender": 77, "time": STAMP - 1000,
                                                              "text": "Older message"}]}
        self.service.open_chat(42, force=True)
        chat = self.service.store.get_chat(42)
        self.assertEqual((chat.last_time_ms, chat.last_preview, chat.unread), (STAMP, "Latest", 5))

    def test_explicit_history_window_keeps_the_callers_cursor_and_never_falls_back(self):
        self.service.client.window_result = {"messages": [{"id": 100, "sender": 77,
                                                            "time": STAMP, "text": "Latest"}]}
        self.service.open_chat_window(42, from_cursor=STAMP, backward=17)
        self.assertEqual(self.service.client.calls[-1], (49, 42, {"from_cursor": STAMP,
                                                                  "forward": 0, "backward": 17,
                                                                  "get_chat": True}))
        self.service.client.history_error = ConnectionError("Synthetic window failure")
        with self.assertRaises(ConnectionError):
            self.service.open_chat_window(42, from_cursor=STAMP, backward=17)
        self.assertEqual(len(self.service.client.calls), 2)

    def test_chat_page_preserves_server_marker_and_channels_filter(self):
        self.service.client.page_result = {"chats": [{"id": 43, "type": "CHANNEL", "newMessages": 2}],
                                            "marker": 777}
        result = self.service.load_chat_page(marker=25, count=12, section="channels")
        self.assertEqual(result["marker"], 777)
        self.assertEqual([chat.id for chat in result["chats"]], [43])
        self.assertEqual(self.service.client.calls, [(53, {"marker": 25, "count": 12,
                                                           "chat_types": ["CHANNEL"]})])

    def test_password_change_and_recovery_email_require_current_password_before_rpc(self):
        with self.assertRaises(ValueError):
            self.service.change_2fa("synthetic-new-password")
        with self.assertRaises(ValueError):
            self.service.start_set_recovery_email("example@example.test")
        self.assertFalse(self.service.client.calls)

    def test_bad_current_password_prevents_password_commit(self):
        self.service.client.password_result = response(113, {"error": "synthetic-refusal"}, cmd=3)
        with self.assertRaises(ValueError):
            self.service.change_2fa("synthetic-new-password", current_password="synthetic-current-password")
        self.assertEqual([call[0] for call in self.service.client.calls], [112, 113])

    def test_bad_current_password_prevents_recovery_email_request(self):
        self.service.client.password_result = response(113, {"error": "synthetic-refusal"}, cmd=3)
        with self.assertRaises(ValueError):
            self.service.start_set_recovery_email("example@example.test",
                                                    current_password="synthetic-current-password")
        self.assertEqual([call[0] for call in self.service.client.calls], [112, 113])

    def test_confirmed_password_track_is_used_for_one_commit(self):
        self.service.change_2fa("synthetic-new-password", current_password="synthetic-current-password")
        self.assertEqual(self.service.client.calls, [(112, 0), (113, "initial-track"),
                                                       (111, "checked-track", 1)])

    def test_recovery_email_uses_checked_track_and_returns_server_email_track(self):
        result = self.service.start_set_recovery_email("example@example.test",
                                                        current_password="synthetic-current-password")
        self.assertEqual(result, "email-track")
        self.assertEqual(self.service.client.calls, [(112, 0), (113, "initial-track"),
                                                       (109, "checked-track")])

    def test_unknown_security_state_is_not_presented_as_disabled(self):
        self.service.client.status_result = response(104, {})
        with self.assertRaises(ValueError):
            self.service.get_2fa_status()
        self.service.client.status_result = response(104, {"enabled": False})
        self.assertIs(self.service.get_2fa_status()["enabled"], False)

    def test_session_close_uses_current_producer_without_guessing_identifiers(self):
        self.assertTrue(self.service.terminate_other_sessions())
        self.assertEqual(self.service.client.calls, [(97, {})])

    def test_read_and_chat_update_pushes_apply_real_metadata(self):
        self.service._on_push(response(130, {"chatId": 42, "userId": 77, "unread": 0, "mark": STAMP}, cmd=0,
                                       auth_context=self.service.client.authenticated_session))
        self.assertEqual(self.service.store.get_chat(42).unread, 5)
        self.service._on_push(response(130, {"chatId": 42, "userId": 123, "unread": 2, "mark": STAMP + 1}, cmd=0,
                                       auth_context=self.service.client.authenticated_session))
        self.assertEqual(self.service.store.get_chat(42).unread, 2)
        self.service._on_push(response(135, {"chat": {"id": 43, "type": "CHANNEL", "newMessages": 3}}, cmd=0,
                                       auth_context=self.service.client.authenticated_session))
        self.assertEqual([chat.id for chat in self.service.list_chats("channels")], [43])


class CacheMigrationTests(unittest.TestCase):
    def test_existing_cache_migrates_additively_and_unknown_owner_clears_only_when_bound(self):
        with tempfile.TemporaryDirectory(prefix="max-cache-migration-") as directory:
            path = Path(directory) / "legacy.db"
            with sqlite3.connect(path) as db:
                db.executescript("""
                    CREATE TABLE chats(id INTEGER PRIMARY KEY,title TEXT,is_group INTEGER DEFAULT 0,
                      last_time_ms INTEGER,last_preview TEXT,unread INTEGER DEFAULT 0,avatar_url TEXT,
                      title_locked INTEGER DEFAULT 0,peer_user_id INTEGER);
                    CREATE TABLE contacts(id INTEGER PRIMARY KEY,name TEXT,phone TEXT);
                    CREATE TABLE messages(row INTEGER PRIMARY KEY AUTOINCREMENT,chat_id INTEGER NOT NULL,
                      server_id INTEGER,sender INTEGER,text TEXT,time_ms INTEGER,outgoing INTEGER DEFAULT 0,
                      status TEXT DEFAULT 'sent',local_id TEXT,attaches TEXT);
                    INSERT INTO chats(id,title,unread) VALUES(99,'Legacy owned data',3);
                    INSERT INTO contacts(id,name) VALUES(88,'Legacy contact');
                    INSERT INTO messages(chat_id,server_id,text) VALUES(99,77,'Legacy message');
                    PRAGMA user_version=2;
                """)
            db.close()
            store = Store(path)
            try:
                self.assertEqual(store.get_chat(99).unread, 3)
                self.assertEqual(store.messages(99)[0].text, "Legacy message")
                self.assertIsNone(store.cache_owner)
                self.assertTrue(store.bind_owner(123))
                self.assertFalse(store.list_chats())
                self.assertFalse(store.list_contacts())
                self.assertFalse(store.messages(99))
                self.assertEqual(store.cache_owner, 123)
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
