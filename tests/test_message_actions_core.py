"""No-network regressions for acknowledged writes and read-only call logs."""

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
from maxclient.protocol.client import MaxWriteOutcomeUnknown
from maxclient.protocol.errors import MaxError
from maxclient.protocol.uploader import AttachKind
from test_core_parity import CoreClient, STAMP
from test_token_service import confirm_fake_transport, login_frame


def ack(identity=101, chat_id=42, text="Confirmed server text", **extra):
    return {"chatId": chat_id, "message": {"id": identity, "sender": 123, "time": STAMP + identity,
                                            "text": text, **extra}}


class ActionsClient(CoreClient):
    def __init__(self, **options):
        super().__init__(**options)
        self.write_results = []
        self.peers = {}
        self.call_result = {"callHistoryItems": []}
        self.call_error = None

    def set_peer(self, chat_id, peer_id):
        self.peers[chat_id] = peer_id

    def peer_for(self, chat_id):
        return self.peers.get(chat_id)

    def next_write(self, call):
        self.calls.append(call)
        value = self.write_results.pop(0)
        if isinstance(value, Exception):
            raise value
        if callable(value):
            value = value()
        return copy.deepcopy(value)

    def send_message(self, chat_id, text, **kwargs):
        return self.next_write(("send", chat_id, text, kwargs))

    def reply_to_message(self, chat_id, message_id, text, **kwargs):
        return self.next_write(("reply", chat_id, message_id, text))

    def edit_message(self, chat_id, message_id, text, **kwargs):
        return self.next_write(("edit", chat_id, message_id, text))

    def delete_messages(self, chat_id, message_ids, **kwargs):
        return self.next_write(("delete", chat_id, list(message_ids), kwargs))

    def forward_message(self, source_chat_id, message_id, target_chat_id, **kwargs):
        return self.next_write(("forward", source_chat_id, message_id, target_chat_id))

    def call_history(self, **kwargs):
        self.calls.append((163, kwargs))
        if self.call_error:
            raise self.call_error
        return copy.deepcopy(self.call_result)


class MessageActionsCoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="max-message-actions-")
        self.environment = patch.dict(os.environ, {"MAX_DESKTOP_DATA_DIR": self.temporary.name})
        self.environment.start()
        self.paths = AppPaths()
        with patch("maxclient.core.service.MaxClient", ActionsClient):
            self.service = MaxService(self.paths, Settings.load(self.paths), Session.load(self.paths))
        chats = [{"id": 42, "type": "DIALOG", "participants": {123: STAMP, 77: STAMP},
                  "lastMessage": {"id": 100, "sender": 123, "time": STAMP, "text": "Original"}},
                 {"id": 43, "type": "DIALOG", "participants": {123: STAMP, 88: STAMP}}]
        self.service._finish_login(login_frame(chats=chats), None)
        confirm_fake_transport(self.service)
        self.context = self.service.client.authenticated_session
        self.service.store.insert_message(Message(chat_id=42, id=100, sender=123, text="Original",
                                                   time_ms=STAMP, outgoing=True))

    def tearDown(self):
        self.assertFalse(self.service.client.login_calls)
        self.assertFalse(self.service.client.connect_calls)
        self.service.shutdown()
        self.environment.stop()
        self.temporary.cleanup()

    def test_send_timeout_is_unknown_and_is_not_retried(self):
        self.service.client.write_results = [MaxWriteOutcomeUnknown("Synthetic unknown outcome")]
        result = self.service.send_text(42, "Requested text")
        self.assertEqual(result.status, "unknown")
        self.assertEqual(self.service.local_history(42)[-1].status, "unknown")
        self.assertEqual(len(self.service.client.calls), 1)

    def test_explicit_send_rejection_is_failed_without_a_second_attempt(self):
        self.service.client.write_results = [MaxError("Synthetic refusal")]
        result = self.service.send_text(42, "Requested text")
        self.assertEqual(result.status, "failed")
        self.assertEqual(len(self.service.client.calls), 1)

    def test_reply_persists_actual_server_body_and_reference(self):
        link = {"type": "REPLY", "messageId": 100}
        self.service.client.write_results = [ack(text="Server reply", link=link)]
        message = self.service.reply_to_message(42, 100, "Requested reply")
        self.assertEqual((message.id, message.text, message.status), (101, "Server reply", "sent"))
        self.assertEqual(message.link, link)
        self.assertEqual(self.service.local_history(42)[-1].link, link)
        self.assertEqual(self.service.client.calls, [("reply", 42, 100, "Requested reply")])

    def test_unknown_reply_stays_unknown_without_copying_an_unconfirmed_id(self):
        self.service.client.write_results = [MaxWriteOutcomeUnknown("Synthetic unknown outcome")]
        message = self.service.reply_to_message(42, 100, "Requested reply")
        self.assertEqual(message.status, "unknown")
        self.assertIsNone(message.id)
        self.assertEqual(len(self.service.client.calls), 1)

    def test_push_before_ack_does_not_create_duplicate_rows_or_a_failed_send(self):
        def push_then_ack():
            self.service.store.insert_message(Message(chat_id=42, id=101, sender=123,
                                                       text="Push body", time_ms=STAMP + 101, outgoing=True))
            return ack()
        self.service.client.write_results = [push_then_ack]
        message = self.service.send_text(42, "Requested text")
        self.assertEqual(message.status, "sent")
        messages = [item for item in self.service.local_history(42) if item.id == 101]
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].text, "Confirmed server text")

    def test_edit_rejection_leaves_cached_content_unchanged(self):
        self.service.client.write_results = [MaxError("Synthetic refusal")]
        with self.assertRaises(MaxError):
            self.service.edit_message(42, 100, "Requested edit", context=self.context)
        self.assertEqual(self.service.local_history(42)[0].text, "Original")

    def test_edit_uses_server_text_instead_of_an_optimistic_local_value(self):
        self.service.client.write_results = [ack(identity=100, text="Authoritative edit")]
        self.service.edit_message(42, 100, "Requested edit", context=self.context)
        self.assertEqual(self.service.local_history(42)[0].text, "Authoritative edit")

    def test_edit_ack_without_text_preserves_known_text_until_a_refresh(self):
        self.service.client.write_results = [{"message": {"id": 100}}]
        self.service.edit_message(42, 100, "Unconfirmed text", context=self.context)
        self.assertEqual(self.service.local_history(42)[0].text, "Original")

    def test_wrong_edit_id_is_unknown_and_does_not_mutate_cache(self):
        self.service.client.write_results = [ack(identity=999)]
        with self.assertRaises(MaxWriteOutcomeUnknown):
            self.service.edit_message(42, 100, "Requested edit", context=self.context)
        self.assertEqual([item.id for item in self.service.local_history(42)], [100])

    def test_delete_applies_only_ids_in_the_authoritative_partial_ack(self):
        self.service.store.insert_message(Message(chat_id=42, id=200, sender=123, text="Keep me", time_ms=STAMP))
        self.service.client.write_results = [{"chatId": 42, "messageIds": [100]}]
        self.service.delete_messages(42, [100, 200], for_all=True, context=self.context)
        self.assertEqual([item.id for item in self.service.local_history(42)], [200])
        self.assertNotIn("lastMessage", self.service._server_chats[42])
        self.assertEqual(self.service.client.calls, [("delete", 42, [100, 200],
                         {"for_all": True, "expected_session": self.context})])

    def test_delete_ack_with_unrequested_ids_does_not_delete_anything(self):
        self.service.client.write_results = [{"chatId": 42, "messageIds": [100, 999]}]
        with self.assertRaises(MaxWriteOutcomeUnknown):
            self.service.delete_messages(42, [100], context=self.context)
        self.assertEqual([item.id for item in self.service.local_history(42)], [100])

    def test_forward_stops_on_unknown_and_retains_only_confirmed_prefix(self):
        self.service.client.write_results = [ack(identity=201, chat_id=43, text="Forwarded first"),
                                               MaxWriteOutcomeUnknown("Synthetic unknown outcome"),
                                               ack(identity=203, chat_id=43)]
        with self.assertRaises(MaxWriteOutcomeUnknown):
            self.service.forward_messages(42, [100, 101, 102], 43, context=self.context)
        self.assertEqual(self.service.client.calls, [("forward", 42, 100, 43), ("forward", 42, 101, 43)])
        self.assertEqual([item.id for item in self.service.local_history(43)], [201])
        self.assertEqual(len(self.service.client.write_results), 1)

    def test_successful_forward_preserves_the_server_reference(self):
        link = {"type": "FORWARD", "chatId": 42, "messageId": 100}
        self.service.client.write_results = [ack(identity=201, chat_id=43, link=link)]
        result = self.service.forward_messages(42, [100], 43, context=self.context)
        self.assertEqual(result["messageIds"], [201])
        self.assertEqual(self.service.local_history(43)[0].link, link)

    def test_unknown_media_send_is_not_reported_as_failed_or_sent(self):
        self.service.client.write_results = [MaxWriteOutcomeUnknown("Synthetic unknown outcome")]
        with patch("maxclient.core.service.upload_and_build_attach", return_value={"_type": "PHOTO"}):
            message = self.service.send_media(42, "synthetic-local-file", AttachKind.PHOTO)
        self.assertEqual(message.status, "unknown")
        self.assertEqual(len(self.service.client.calls), 1)

    def test_structured_message_text_is_preserved_literally(self):
        text = "Literal attaches reactionInfo <b>text</b> & symbols"
        message = Message.from_server(42, {"id": 5, "text": text, "time": STAMP}, 123)
        self.assertEqual(message.text, text)

    def test_structured_timestamp_is_not_rescaled_to_fit_a_guessed_date_range(self):
        timestamp = 2_000_000_000_001
        message = Message.from_server(42, {'id': 5, 'text': 'Synthetic', 'time': timestamp}, 123)
        self.assertEqual(message.time_ms, timestamp)

    def test_read_only_call_history_preserves_ids_time_and_missing_fields(self):
        item = {"historyId": 7, "callId": "synthetic-call", "callerId": 77,
                "chatId": 42, "callType": "AUDIO", "time": STAMP}
        self.service.client.call_result = {"callHistoryItems": [item], "callHistorySync": STAMP, "reset": False}
        result = self.service.list_call_history(sync_cursor=STAMP - 1)
        self.assertEqual(result, self.service.client.call_result)
        self.assertNotIn("durationMs", result["callHistoryItems"][0])
        self.assertNotIn("direction", result["callHistoryItems"][0])
        self.assertEqual(self.service.client.calls, [(163, {"sync_cursor": STAMP - 1})])

    def test_bad_call_timestamp_is_not_replaced_with_the_local_clock(self):
        self.service.client.call_result = {"callHistoryItems": [{"historyId": 7, "callId": "synthetic-call",
                 "callerId": 77, "chatId": 42, "callType": "AUDIO", "time": None}]}
        with self.assertRaises(ValueError):
            self.service.list_call_history()
        self.assertEqual(len(self.service.client.calls), 1)

    def test_call_history_server_failure_propagates_without_auth_or_call_actions(self):
        self.service.client.call_error = ConnectionError("Synthetic call-log read failure")
        with self.assertRaises(ConnectionError):
            self.service.list_call_history()
        self.assertEqual(self.service.client.calls, [(163, {"sync_cursor": None})])


if __name__ == "__main__":
    unittest.main()
