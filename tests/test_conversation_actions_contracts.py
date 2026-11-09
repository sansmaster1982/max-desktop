"""Info Sound wire contracts, all synthetic and without transport."""
import math
import unittest
from unittest.mock import Mock

from maxclient.protocol.client import (
    AuthenticatedSession, ConnectionState, MaxClient, MaxError, MaxFrame,
    MaxNotConnected, MaxTimeout, MaxWriteOutcomeUnknown,
)
from maxclient.protocol.conversation_actions_contracts import (
    build_chat_mute, mute_deadline, mute_is_active, parse_chat_mute_ack, supplied_mute,
)
from maxclient.protocol.settings_contracts import chat_notification_values


class ConversationActionsContractsTests(unittest.TestCase):
    def test_exact_payload_durations_and_no_rounding(self):
        now = 1_790_000_000_000.25
        for duration in (0, -1, 3_600_000, 14_400_000, 86_400_000):
            expected = duration if duration <= 0 else now + duration
            self.assertEqual(mute_deadline(duration, now), expected)
            self.assertEqual(build_chat_mute(303, expected),
                {'settings': {'chats': {'303': {'dontDisturbUntil': expected}}}})
        for duration in (True, '3600000', 3_600_000.0, 1, -2):
            with self.subTest(duration=duration), self.assertRaises(ValueError):
                mute_deadline(duration, now)

    def test_invalid_identity_and_times_are_rejected(self):
        for identity in (True, '303', 2**63, None):
            with self.subTest(identity=identity), self.assertRaises(ValueError):
                build_chat_mute(identity, 0)
        for value in (True, '0', None, math.nan, math.inf, -math.inf, -2):
            with self.subTest(value=value), self.assertRaises(ValueError):
                build_chat_mute(303, value)
            with self.assertRaises(ValueError):
                chat_notification_values({'303': {'dontDisturbUntil': value}})
        with self.assertRaises(ValueError): build_chat_mute(303, -.5)

    def test_partial_ack_never_echoes_requested_value(self):
        self.assertIsNone(parse_chat_mute_ack(None))
        self.assertIsNone(parse_chat_mute_ack({}))
        self.assertEqual(parse_chat_mute_ack({'hash': 'synthetic', 'user': {'HIDDEN': True}}),
                         {'hash': 'synthetic', 'user': {'HIDDEN': True}})
        self.assertEqual(parse_chat_mute_ack({'hash': 'synthetic', 'chats': {
            '404': {'dontDisturbUntil': 99.5}, '303': {'other': 'ignored'}}}),
            {'hash': 'synthetic', 'chats': {'404': {'dontDisturbUntil': 99.5}}})
        for raw in ([], True, {'chats': {}}, {'hash': 1},
                    {'hash': 'synthetic', 'chats': {'303': {'dontDisturbUntil': True}}}):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                parse_chat_mute_ack(raw)

    def test_actual_supplied_state_and_expiry_without_default(self):
        for raw in ({}, {'chats': {}}, {'chats': {'303': {}}}):
            self.assertIsNone(supplied_mute(raw, 303))
        self.assertIsNone(mute_is_active(None, 100))
        for value, active in ((-1, True), (0, False), (99.5, False), (100, False), (100.25, True)):
            self.assertIs(mute_is_active(value, 100), active)
            self.assertEqual(supplied_mute({'chats': {'303': {'dontDisturbUntil': value}}}, 303), value)

    def make_client(self):
        client = MaxClient(auto_reconnect=False)
        client._sock = object()
        client._state = ConnectionState.CONNECTED
        client._device_type = 'WEB'
        client._auth_session = AuthenticatedSession(101, client.device_id, 'WEB', client._sock)
        client.request = Mock(return_value=MaxFrame(1, 1, 22, b'', None))
        return client

    def test_wrapper_one_request_and_optional_ack(self):
        client = self.make_client()
        context = client.authenticated_session
        self.assertIsNone(client.update_chat_mute(303, -1, expected_session=context))
        client.request.assert_called_once_with(22, {'settings': {'chats': {'303': {'dontDisturbUntil': -1}}}},
                                               expected_session=context)
        client.request.reset_mock()
        client.request.return_value.decoded = {}
        self.assertIsNone(client.update_chat_mute(303, 0))
        client.request.assert_called_once()

    def test_wrapper_unknown_outcomes_never_retry_and_negative_ack_is_distinct(self):
        client = self.make_client()
        for failure in (MaxTimeout('synthetic'), MaxNotConnected('synthetic')):
            client.request.reset_mock()
            client.request.side_effect = failure
            with self.assertRaises(MaxWriteOutcomeUnknown): client.update_chat_mute(303, 0)
            client.request.assert_called_once()
        client.request.side_effect = None
        client.request.return_value = MaxFrame(3, 1, 22, b'', {'error': 'synthetic'})
        with self.assertRaises(MaxError) as result: client.update_chat_mute(303, 0)
        self.assertNotIsInstance(result.exception, MaxWriteOutcomeUnknown)
        client.request.return_value = MaxFrame(1, 1, 22, b'', ['invalid'])
        with self.assertRaises(MaxWriteOutcomeUnknown): client.update_chat_mute(303, 0)

    def test_wrapper_rejects_replaced_session_before_transport(self):
        client = self.make_client()
        old = client.authenticated_session
        client._auth_session = AuthenticatedSession(101, client.device_id, 'WEB', client._sock)
        with self.assertRaises(ValueError): client.update_chat_mute(303, 0, expected_session=old)
        client.request.assert_not_called()


if __name__ == '__main__':
    unittest.main()
