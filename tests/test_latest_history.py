"""Current public timestamp history contract, tested without a live account."""

import copy
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from maxclient.core.config import AppPaths, Session, Settings
from maxclient.core.models import Message
from maxclient.core.service import MaxService
from test_core_parity import CoreClient, STAMP
from test_token_service import confirm_fake_transport, login_frame


class HistoryClient(CoreClient):
    def __init__(self, **options):
        super().__init__(**options)
        self.info_result = {"chats": []}
        self.info_error = None

    def chat_info(self, ids, *, expected_session=None):
        assert expected_session is not None and expected_session is self.authenticated_session
        self.calls.append((48, list(ids)))
        if self.info_error:
            raise self.info_error
        return copy.deepcopy(self.info_result)

    def chat_history(self, *args, **kwargs):
        raise AssertionError("Latest history must not fall back to forward-from-zero")


class LatestHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="max-latest-history-")
        self.environment = patch.dict(os.environ, {"MAX_DESKTOP_DATA_DIR": self.temporary.name})
        self.environment.start()
        paths = AppPaths()
        with patch("maxclient.core.service.MaxClient", HistoryClient):
            self.service = MaxService(paths, Settings.load(paths), Session.load(paths))
        self.chat = {"id": 42, "type": "DIALOG", "participants": {123: STAMP, 77: STAMP},
                     "lastEventTime": STAMP + 10000,
                     "lastMessage": {"id": 999, "time": STAMP, "sender": 77, "text": "Latest"}}
        self.service._finish_login(login_frame(chats=[self.chat], contacts=[
            {"id": 77, "names": [{"type": "ONEME", "name": "Example peer"}]}]), None)
        confirm_fake_transport(self.service)
        self.service.client.info_result = {"chats": [self.chat]}
        self.service.client.window_result = {"messages": [self.chat["lastMessage"]]}

    def tearDown(self):
        self.assertFalse(self.service.client.login_calls)
        self.assertFalse(self.service.client.connect_calls)
        self.service.shutdown()
        self.environment.stop()
        self.temporary.cleanup()

    def expected_window(self, anchor=STAMP, count=50):
        return (49, 42, {"from_cursor": anchor, "forward": 0, "backward": count, "get_chat": True})

    def forget_anchor(self):
        self.service._server_chats.clear()

    def test_cached_last_message_time_is_used_not_message_id_event_time_or_clock(self):
        with patch("maxclient.core.service.time.time", return_value=9_999_999):
            messages = self.service.open_chat(42, count=17)
        self.assertEqual(self.service.client.calls, [self.expected_window(count=17)])
        self.assertEqual(messages[-1].id, 999)

    def test_missing_cached_anchor_gets_one_metadata_request_before_history(self):
        self.forget_anchor()
        self.service.open_chat(42)
        self.assertEqual(self.service.client.calls, [(48, [42]), self.expected_window()])

    def test_invalid_cached_time_is_refreshed_once_with_actual_server_time(self):
        self.service._server_chats[42]["lastMessage"]["time"] = None
        self.service.open_chat(42)
        self.assertEqual(self.service.client.calls, [(48, [42]), self.expected_window()])

    def test_single_chat_info_response_is_accepted(self):
        self.forget_anchor()
        self.service.client.info_result = {"chat": self.chat}
        self.service.open_chat(42)
        self.assertEqual(self.service.client.calls, [(48, [42]), self.expected_window()])

    def test_refreshed_empty_chat_does_not_request_history_or_display_old_cache(self):
        self.forget_anchor()
        self.service.client.info_result = {"chats": [{"id": 42, "type": "DIALOG"}]}
        self.service.store.insert_message(Message(chat_id=42, id=1, text="Old cache", time_ms=STAMP - 1))
        self.assertEqual(self.service.open_chat(42), [])
        self.assertEqual(self.service.client.calls, [(48, [42])])
        self.assertEqual(len(self.service.local_history(42)), 1)

    def test_last_message_without_time_is_an_error_without_a_substituted_cursor(self):
        self.forget_anchor()
        self.service.client.info_result = {"chats": [{"id": 42, "type": "DIALOG",
                                                       "lastMessage": {"id": 999, "text": "Visible"}}]}
        with self.assertRaises(ValueError):
            self.service.open_chat(42)
        self.assertEqual(self.service.client.calls, [(48, [42])])
        self.assertNotIn(42, self.service._chat_fetch_ts)

    def test_null_last_message_is_not_an_ordinary_optional_message_object(self):
        self.forget_anchor()
        self.service.client.info_result = {"chats": [{"id": 42, "lastMessage": None}]}
        with self.assertRaises(ValueError):
            self.service.open_chat(42)
        self.assertEqual(self.service.client.calls, [(48, [42])])

    def test_nonmatching_metadata_is_not_reported_as_an_empty_selected_chat(self):
        self.forget_anchor()
        self.service.client.info_result = {"chats": [{"id": 43, "type": "DIALOG"}]}
        with self.assertRaises(ValueError):
            self.service.open_chat(42)
        self.assertEqual(self.service.client.calls, [(48, [42])])

    def test_metadata_error_propagates_without_history_or_auth_fallback(self):
        self.forget_anchor()
        self.service.client.info_error = ConnectionError("Synthetic metadata failure")
        with self.assertRaises(ConnectionError):
            self.service.open_chat(42)
        self.assertEqual(self.service.client.calls, [(48, [42])])
        self.assertNotIn(42, self.service._chat_fetch_ts)

    def test_history_error_keeps_cache_but_does_not_present_it_as_refreshed(self):
        self.service.store.insert_message(Message(chat_id=42, id=1, text="Owned cache", time_ms=STAMP - 1))
        self.service.client.history_error = ConnectionError("Synthetic history failure")
        for _ in range(2):
            with self.assertRaises(ConnectionError):
                self.service.open_chat(42)
        self.assertEqual(self.service.client.calls, [self.expected_window(), self.expected_window()])
        self.assertEqual(len(self.service.local_history(42)), 1)
        self.assertNotIn(42, self.service._chat_fetch_ts)
        self.assertNotIn(42, self.service._chat_fetch_anchor)

    def test_newer_actual_anchor_bypasses_recent_history_throttle(self):
        self.service.open_chat(42)
        self.service._server_chats[42]["lastMessage"]["time"] = STAMP + 1
        self.service.open_chat(42)
        self.assertEqual(self.service.client.calls,
                         [self.expected_window(), self.expected_window(anchor=STAMP + 1)])

    def test_same_actual_anchor_can_use_recent_owned_cache(self):
        self.service.open_chat(42)
        self.service.open_chat(42)
        self.assertEqual(self.service.client.calls, [self.expected_window()])


if __name__ == "__main__":
    unittest.main()
