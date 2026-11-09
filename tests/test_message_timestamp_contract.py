"""Finite source timestamps survive parsing/cache without rounding or epoch guesses."""
import unittest

from maxclient.core.models import Message
from maxclient.core.service import _message_time
import test_history_reconnect_review as history_fixture

STAMP = history_fixture.STAMP


class MessageTimestampContractTests(unittest.TestCase):
    setUp = history_fixture.HistoryReconnectReviewTests.setUp
    tearDown = history_fixture.HistoryReconnectReviewTests.tearDown
    raw = history_fixture.HistoryReconnectReviewTests.raw
    chat = history_fixture.HistoryReconnectReviewTests.chat
    context_for = history_fixture.HistoryReconnectReviewTests.context_for
    push = history_fixture.HistoryReconnectReviewTests.push

    def test_fraction_zero_and_negative_source_times_survive_optional_sender_push_and_sql(self):
        expected = [STAMP + 0.25, 0, -1.5]
        for identity, timestamp in enumerate(expected, 1):
            raw = self.raw(identity, timestamp)
            raw.pop('sender')
            frame = self.push(raw)
            parsed = self.service._parse_push(frame)
            self.assertIsNotNone(parsed)
            self.assertEqual(parsed.time_ms, timestamp)
            self.assertIsNone(parsed.sender)
            self.service._on_push(frame)
        rows = {row.id: row for row in self.store.messages(303)}
        self.assertEqual([rows[index].time_ms for index in (1, 2, 3)], expected)
        self.assertEqual(self.service.on_message.call_count, 3)

    def test_invalid_timestamp_does_not_become_a_guessed_time_or_reach_sql(self):
        for value in (True, False, float('nan'), float('inf'), float('-inf'), None, '123', 2**63):
            with self.subTest(kind=type(value).__name__):
                raw = self.raw(timestamp=value)
                self.assertIsNone(self.service._parse_push(self.push(raw)))
                self.assertIsNone(_message_time(raw))
                with self.assertRaises(ValueError):
                    Message.from_server(303, raw, 101)
                self.service._on_push(self.push(raw))
        self.assertEqual(self.store.messages(303), [])
        self.service.on_message.assert_not_called()

    def test_required_message_type_and_optional_sender_do_not_relax_id_types(self):
        for field, value in (('id', True), ('id', '7'), ('sender', False),
                             ('sender', '202'), ('type', 'other'), ('type', None)):
            raw = self.raw()
            raw[field] = value
            self.assertIsNone(self.service._parse_push(self.push(raw)))
        raw = self.raw()
        raw.pop('type')
        self.assertIsNone(self.service._parse_push(self.push(raw)))
        raw = self.raw()
        raw.pop('sender')
        self.assertIsNotNone(self.service._parse_push(self.push(raw)))

    def test_message_model_and_existing_ack_keep_fraction_and_zero_exactly(self):
        first = self.raw(timestamp=STAMP + 0.25)
        message = Message.from_server(303, first, 101)
        self.assertEqual(message.time_ms, STAMP + 0.25)
        self.store.insert_message(message)
        for timestamp in (STAMP + 0.75, 0, -1.25):
            updated = self.store.apply_message_ack(303, self.raw(timestamp=timestamp), my_id=101)
            self.assertEqual(updated.time_ms, timestamp)
            self.assertEqual(self.store.messages(303)[0].time_ms, timestamp)
        with self.assertRaises(ValueError):
            self.store.apply_message_ack(303, self.raw(timestamp=float('nan')), my_id=101)
        self.assertEqual(self.store.messages(303)[0].time_ms, -1.25)

    def test_zero_and_fraction_are_valid_actual_history_anchors_without_metadata_fallback(self):
        self.service._resolve_chat_contacts = lambda *_args, **_kwargs: None
        for timestamp in (0, STAMP + 0.5):
            self.service._server_chats[303]['lastMessage'] = self.raw(timestamp=timestamp)
            self.client.chat_history_window.reset_mock()
            self.client.chat_history_window.return_value = {'messages': [self.raw(timestamp=timestamp)]}
            self.service.open_chat(303, force=True)
            self.client.chat_history_window.assert_called_once_with(
                303, from_cursor=timestamp, forward=0, backward=50, get_chat=True)
        self.client.chat_info.assert_not_called()


if __name__ == '__main__':
    unittest.main()
