"""Resolve dialog identity from server participants/contact DTOs, never text."""

import copy
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from maxclient.core.config import AppPaths, Session, Settings
from maxclient.core.service import MaxService
from test_core_parity import CoreClient, STAMP
from test_token_service import confirm_fake_transport, login_frame


class IdentityClient(CoreClient):
    def __init__(self, **options):
        super().__init__(**options)
        self.is_connected = True
        self.chat_response = {"chats": []}
        self.contact_response = {"contacts": []}
        self.contact_error = None

    def chat_info(self, ids, *, expected_session=None):
        if expected_session is not None:
            assert expected_session is self.authenticated_session
        self.calls.append((48, list(ids)))
        return copy.deepcopy(self.chat_response)

    def contact_info(self, ids, *, expected_session=None):
        if expected_session is not None:
            assert expected_session is self.authenticated_session
        self.calls.append((32, list(ids)))
        if self.contact_error:
            raise self.contact_error
        return copy.deepcopy(self.contact_response)


class ChatIdentityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="max-chat-identity-")
        self.environment = patch.dict(os.environ, {"MAX_DESKTOP_DATA_DIR": self.temporary.name})
        self.environment.start()
        paths = AppPaths()
        with patch("maxclient.core.service.MaxClient", IdentityClient):
            self.service = MaxService(paths, Settings.load(paths), Session.load(paths))
        self.chat = {"id": 42, "type": "DIALOG", "participants": {77: STAMP},
                     "options": {"SERVICE_CHAT": True}, "newMessages": 4,
                     "lastMessage": {"id": 101, "sender": 77, "time": STAMP, "text": "Server text"}}
        self.contact = {"id": 77, "names": [{"type": "ONEME", "name": "Безопасность"}],
                        "options": ["SERVICE_ACCOUNT", "OFFICIAL", "BOT"],
                        "baseUrl": "https://cdn.example.test/service-avatar"}
        self.service._finish_login(login_frame(chats=[self.chat], contacts=[]), None)
        confirm_fake_transport(self.service)
        self.service.client.chat_response = {"chats": [self.chat]}
        self.service.client.contact_response = {"contacts": [self.contact]}

    def tearDown(self):
        self.assertFalse(self.service.client.login_calls)
        self.assertFalse(self.service.client.connect_calls)
        self.service.shutdown()
        self.environment.stop()
        self.temporary.cleanup()

    def test_service_dialog_with_only_peer_in_map_resolves_title_avatar_badge(self):
        self.assertEqual(self.service.store.get_chat(42).peer_user_id, 77)
        chat = self.service.sync_chats()[0]
        self.assertEqual(self.service.client.calls, [(48, [42]), (32, [77])])
        self.assertEqual(chat.display, "Безопасность")
        self.assertEqual(chat.avatar_url, "https://cdn.example.test/service-avatar")
        self.assertTrue(chat.is_verified)
        self.assertEqual(chat.unread, 4)

    def test_opening_known_dialog_resolves_missing_contact_once_before_history(self):
        self.service.client.window_result = {"messages": [self.chat["lastMessage"]]}
        self.service.open_chat(42)
        self.assertEqual(self.service.client.calls[0], (32, [77]))
        self.assertEqual(self.service.client.calls[1][0], 49)
        self.assertEqual(self.service.list_chats()[0].display, "Безопасность")
        self.service.open_chat(42)
        self.assertEqual(len(self.service.client.calls), 2)

    def test_partial_chat_and_contact_dtos_do_not_erase_identity(self):
        self.service.sync_chats()
        self.service._remember_chat_info({"chats": [{"id": 42, "type": "DIALOG", "newMessages": 0}]}, my_id=123)
        self.service._remember_contact_info({"contacts": [{"id": 77, "options": ["OFFICIAL"]}]})
        chat = self.service.list_chats()[0]
        self.assertEqual(chat.peer_user_id, 77)
        self.assertEqual(chat.display, "Безопасность")
        self.assertTrue(chat.is_verified)
        self.assertEqual(chat.avatar_url, "https://cdn.example.test/service-avatar")

    def test_name_records_keep_source_order_instead_of_prioritizing_custom(self):
        self.service.client.contact_response["contacts"][0]["names"].append(
            {"type": "CUSTOM", "name": "My local alias"})
        chat = self.service.sync_chats()[0]
        self.assertEqual(chat.display, "Безопасность")
        self.assertTrue(chat.is_verified)
        self.assertEqual(self.service.verification_code_messages()["status"], "chat_not_available")

    def test_contact_refresh_failure_is_not_reported_as_synced(self):
        self.service.client.contact_error = ConnectionError("Synthetic contact read failure")
        with self.assertRaises(ConnectionError):
            self.service.sync_chats()
        self.assertEqual(self.service.client.calls, [(48, [42]), (32, [77])])

    def test_no_title_or_badge_is_inferred_from_service_message_text(self):
        self.service.client.contact_response = {"contacts": []}
        self.chat["lastMessage"]["text"] = "Безопасность: a synthetic message says it is official"
        chat = self.service.sync_chats()[0]
        self.assertEqual(chat.display, "Чат 42")
        self.assertFalse(chat.is_verified)
        self.assertIsNone(chat.avatar_url)

    def test_ambiguous_participants_do_not_route_to_an_arbitrary_person(self):
        self.service._remember_chat_info({"chats": [{"id": 42, "type": "DIALOG",
                                                      "participants": {77: STAMP, 88: STAMP}}]}, my_id=123)
        self.assertIsNone(self.service.store.get_chat(42).peer_user_id)


if __name__ == "__main__":
    unittest.main()
