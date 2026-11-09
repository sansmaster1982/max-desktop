"""Synthetic current22/96/97 contracts; no account or network operations."""
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from maxclient.protocol.client import (MaxClient, MaxError, MaxFrame, MaxTimeout,
                                       MaxNotConnected, MaxWriteOutcomeUnknown, _redact)


class PrivacySessionsProtocolTests(unittest.TestCase):
    def client(self, opcode=22, data=None):
        client = MaxClient(auto_reconnect=False)
        client.request = Mock(return_value=MaxFrame(1, 7, opcode, b"", data))
        return client

    def test_privacy_wire_keys_and_false_values_are_preserved_without_extra_fields(self):
        client = self.client(data={"hash": "synthetic-hash", "user": {"HIDDEN": False}})
        result = client.update_user_settings({"HIDDEN": False, "PHONE_NUMBER_PRIVACY": "NOBODY"})
        client.request.assert_called_once_with(22, {"settings": {"user": {
            "HIDDEN": False, "PHONE_NUMBER_PRIVACY": "NOBODY"}}})
        self.assertEqual(result, {"hash": "synthetic-hash", "user": {"HIDDEN": False}})

    def test_optional_positive_settings_ack_has_no_fabricated_config(self):
        client = self.client()
        self.assertIsNone(client.update_user_settings({"SEARCH_BY_PHONE": "CONTACTS"}))
        self.assertEqual(client.request.call_count, 1)

    def test_family_safemode_unknown_keys_and_bad_enums_make_zero_rpc(self):
        client = self.client()
        for patch in ({}, {"FAMILY_PROTECTION": "OFF"}, {"SAFE_MODE": False},
                      {"HIDDEN": "NOBODY"}, {"CONTENT_LEVEL_ACCESS": 0},
                      {"SEARCH_BY_PHONE": "NOBODY"}, {"unknown": True}):
            with self.assertRaises(ValueError):
                client.update_user_settings(patch)
        client.request.assert_not_called()

    def test_incomplete_settings_ack_and_network_loss_are_unknown_without_retry(self):
        for data in ({}, [], {"hash": True}, {"hash": "x", "user": {"HIDDEN": 1}},
                     {"hash": "x", "chats": []}):
            client = self.client(data=data)
            with self.assertRaises(MaxWriteOutcomeUnknown):
                client.update_user_settings({"HIDDEN": True})
            self.assertEqual(client.request.call_count, 1)
        for error in (MaxTimeout("Synthetic private text"), MaxNotConnected("Synthetic private text")):
            client = self.client()
            client.request.side_effect = error
            with self.assertRaises(MaxWriteOutcomeUnknown) as caught:
                client.update_user_settings({"HIDDEN": True})
            self.assertNotIn("Synthetic private text", str(caught.exception))
            self.assertEqual(client.request.call_count, 1)

    def test_explicit_nack_is_an_error_and_does_not_retry(self):
        client = self.client()
        client.request.return_value = MaxFrame(3, 7, 22, b"", {"error": "synthetic.denied"})
        with self.assertRaisesRegex(MaxError, "synthetic.denied"):
            client.update_user_settings({"HIDDEN": True})
        self.assertEqual(client.request.call_count, 1)

    def test_sessions_read_has_string_marker_and_no_guessed_ids(self):
        client = self.client(96, {"sessions": [], "marker": "synthetic-next"})
        result = client.sessions_info(page_size=30, marker="synthetic-start")
        client.request.assert_called_once_with(96, {"pageSize": 30, "marker": "synthetic-start"})
        self.assertEqual(result.decoded["marker"], "synthetic-next")
        client.request.reset_mock()
        for kwargs in ({"marker": 1}, {"page_size": True}, {"page_size": 0}):
            with self.assertRaises(ValueError):
                client.sessions_info(**kwargs)
        client.request.assert_not_called()

    def test_close_all_others_is_exact97_and_does_not_replace_login_token(self):
        client = self.client(97, {"token": "synthetic-opaque-ack"})
        client._token = "synthetic-original-login"
        self.assertEqual(client.close_other_sessions(), {"token": "synthetic-opaque-ack"})
        client.request.assert_called_once_with(97, {})
        self.assertEqual(client.token, "synthetic-original-login")
        self.assertEqual(_redact({"token": "synthetic-opaque-ack"}), {"token": "***"})

    def test_selected_time_as_id_path_is_disabled_before_rpc(self):
        client = self.client(97, {"token": "synthetic"})
        with self.assertRaises(NotImplementedError):
            client.sessions_close([1_700_000_000_000])
        client.request.assert_not_called()

    def test_close_timeout_or_bad_ack_is_unknown_and_never_retried(self):
        for data in ({}, {"token": ""}, {"token": True}, None):
            client = self.client(97, data)
            with self.assertRaises(MaxWriteOutcomeUnknown):
                client.close_other_sessions()
            self.assertEqual(client.request.call_count, 1)
        client = self.client(97, {"token": "synthetic"})
        client.request.side_effect = MaxTimeout("Synthetic timeout")
        with self.assertRaises(MaxWriteOutcomeUnknown):
            client.close_other_sessions()
        self.assertEqual(client.request.call_count, 1)


if __name__ == "__main__":
    unittest.main()
