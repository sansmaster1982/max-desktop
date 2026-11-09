"""Deterministic expected-session handoff races; synthetic sockets only."""
import unittest
from unittest.mock import Mock, patch

import test_conversation_actions_service as fixtures
from maxclient.protocol.client import (
    AuthenticatedSession, ConnectionState, MaxClient, MaxFrame,
    MaxWriteNotStarted, MaxWriteOutcomeUnknown,
)


class _SyntheticSocket:
    def __init__(self):
        self.sendall = Mock()


class _ReplacingLock:
    def __init__(self, callback):
        self.callback = callback
    def __enter__(self):
        self.callback()
    def __exit__(self, *args):
        return False


class ConversationActionTransportTests(unittest.TestCase):
    def make_client(self):
        client = MaxClient(auto_reconnect=False)
        client._sock = _SyntheticSocket()
        client._device_type = 'WEB'
        client._state = ConnectionState.CONNECTED
        client._auth_session = AuthenticatedSession(101, client.device_id, 'WEB', client._sock)
        return client

    def test_replacement_between_wrapper_and_request_sends_on_neither_socket(self):
        client = self.make_client()
        old, context = client._sock, client.authenticated_session
        replacement = _SyntheticSocket()
        original = client.request
        def replace(*args, **kwargs):
            client._sock = replacement
            client._auth_session = AuthenticatedSession(101, client.device_id, 'WEB', replacement)
            return original(*args, **kwargs)
        client.request = replace
        with self.assertRaises(MaxWriteNotStarted): client.update_chat_mute(303, -1, expected_session=context)
        old.sendall.assert_not_called()
        replacement.sendall.assert_not_called()
        self.assertEqual(client._pending, {})

    def test_same_owner_context_replaced_while_waiting_for_send_lock(self):
        client = self.make_client()
        context, sock = client.authenticated_session, client._sock
        client._send_lock = _ReplacingLock(lambda: setattr(client, '_auth_session',
            AuthenticatedSession(101, client.device_id, 'WEB', sock)))
        with self.assertRaises(MaxWriteNotStarted): client.stop_bot(202, expected_session=context)
        sock.sendall.assert_not_called()
        self.assertEqual(client._pending, {})

    def test_context_replacement_after_pending_registration_is_cleaned_before_send(self):
        client = self.make_client()
        context, sock = client.authenticated_session, client._sock
        def replace(*args, **kwargs):
            self.assertEqual(len(client._pending), 1)
            client._auth_session = AuthenticatedSession(101, client.device_id, 'WEB', sock)
            return b'synthetic-frame'
        with patch('maxclient.protocol.client.build_frame', side_effect=replace):
            with self.assertRaises(MaxWriteNotStarted):
                client.set_contact_stories_hidden(202, True, expected_session=context)
        sock.sendall.assert_not_called()
        self.assertEqual(client._pending, {})

    def test_success_uses_captured_transport_once_and_send_failure_is_unknown(self):
        client = self.make_client()
        context, sock = client.authenticated_session, client._sock
        def reply(_frame):
            seq, pending = next(iter(client._pending.items()))
            pending.frame = MaxFrame(1, seq, 22, b'', None)
            pending.event.set()
            client._pending.pop(seq)
        sock.sendall.side_effect = reply
        self.assertIsNone(client.update_chat_mute(303, -1, expected_session=context))
        sock.sendall.assert_called_once()
        self.assertEqual(client._pending, {})
        sock.sendall.reset_mock()
        sock.sendall.side_effect = OSError('synthetic send failure')
        with self.assertRaises(MaxWriteOutcomeUnknown): client.update_chat_mute(303, 0, expected_session=context)
        sock.sendall.assert_called_once()
        self.assertEqual(client._pending, {})

    def test_contact_and_chat_readbacks_reject_replaced_context_before_send(self):
        for method, ids in (('contact_info', [202]), ('chat_info', [303])):
            client = self.make_client()
            old = client.authenticated_session
            client._auth_session = AuthenticatedSession(101, client.device_id, 'WEB', client._sock)
            with self.assertRaises(MaxWriteNotStarted): getattr(client, method)(ids, expected_session=old)
            client._sock.sendall.assert_not_called()


class ConversationActionNotStartedTests(unittest.TestCase):
    setUp = fixtures.ConversationActionsServiceTests.setUp
    tearDown = fixtures.ConversationActionsServiceTests.tearDown

    def test_proven_unsent_action_unlocks_instead_of_becoming_unknown(self):
        self.client.update_chat_mute.side_effect = MaxWriteNotStarted('synthetic preflight change')
        with self.assertRaises(MaxWriteNotStarted): self.service.set_conversation_mute(self.context, -1)
        self.assertFalse(self.service.get_conversation_action_state(self.context)['pending'])
        self.assertEqual(self.store.load_config(101), {})
        self.client.update_chat_mute.assert_called_once()


if __name__ == '__main__':
    unittest.main()
