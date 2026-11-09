"""Authenticated transport identity regressions using in-memory fake I/O only."""
import unittest
from unittest.mock import Mock

from maxclient.protocol import opcodes
from maxclient.protocol.client import (
    AuthenticatedSession, ConnectionState, MaxClient, MaxFrame, MaxLoginFailed,
    MaxWriteOutcomeUnknown, _Pending,
)


class _Socket:
    def __init__(self):
        self.sent = []
    def sendall(self, data):
        self.sent.append(data)
    def shutdown(self, _how=None):
        pass
    def close(self):
        pass


class CallSessionIdentityTests(unittest.TestCase):
    def setUp(self):
        self.received = []
        self.client = MaxClient(device_id='00112233-4455-6677-8899-aabbccddeeff',
                                on_push=self.received.append, auto_reconnect=False)
        self.client._device_type = 'WEB'
        self.client._sock = _Socket()
        self.client._state = ConnectionState.CONNECTED
        self.client._closed = False

    def response(self, owner=101):
        return MaxFrame(1, 9, opcodes.LOGIN, b'', {'profile': {'contact': {'id': owner}}})

    def login_fake(self, owner=101):
        self.client._auth_frame = Mock(return_value=self.response(owner))
        self.client.login('synthetic-login-credential')
        context = self.client.authenticated_session
        self.assertIsInstance(context, AuthenticatedSession)
        return context

    def test_only_confirmed_login_creates_context_and_each_login_renews_identity(self):
        self.assertIsNone(self.client.authenticated_session)
        first = self.login_fake()
        self.assertEqual((first.owner_id, first.device_id, first.device_type),
                         (101, self.client.device_id, 'WEB'))
        self.assertIs(first.transport, self.client._sock)
        second = self.login_fake()
        self.assertIsNot(first, second)
        self.assertNotEqual(first, second)
        self.assertNotIn('synthetic-login-credential', repr(second))
        self.assertNotIn('_Socket', repr(second))

    def test_login_begin_invalidates_previous_identity_before_rpc(self):
        self.login_fake()
        observed = []
        def response(*_args):
            observed.append(self.client.authenticated_session)
            return self.response()
        self.client._auth_frame = Mock(side_effect=response)
        self.client.login('synthetic-next-credential')
        self.assertEqual(observed, [None])

    def test_rejected_or_malformed_login_does_not_leave_authenticated_context(self):
        for result in (self.response(True), self.response('101'),
                       MaxFrame(1, 9, 19, b'', {}), MaxFrame(1, 9, 16, b'', {'profile': {'contact': {'id': 101}}})):
            self.login_fake()
            self.client._auth_frame = Mock(return_value=result)
            with self.assertRaises(MaxWriteOutcomeUnknown):
                self.client.login('synthetic-next-credential')
            self.assertIsNone(self.client.authenticated_session)
        self.login_fake()
        self.client._auth_frame = Mock(side_effect=MaxLoginFailed('synthetic_rejection'))
        with self.assertRaises(MaxLoginFailed):
            self.client.login('synthetic-rejected')
        self.assertIsNone(self.client.authenticated_session)

    def test_transport_closed_or_device_changed_while_login_waits_cannot_confirm_old_session(self):
        for change in ('socket', 'closed', 'device', 'mode'):
            self.setUp()
            def response(*_args):
                if change == 'socket':
                    self.client._sock = _Socket()
                elif change == 'closed':
                    self.client._closed = True
                elif change == 'device':
                    self.client.device_id = 'different-device'
                else:
                    self.client._device_type = 'ANDROID'
                return self.response()
            self.client._auth_frame = Mock(side_effect=response)
            self.client._token = 'synthetic-prior-credential'
            with self.subTest(change=change):
                with self.assertRaises(MaxWriteOutcomeUnknown):
                    self.client.login('synthetic-login-credential')
            self.assertEqual(self.client._token, 'synthetic-prior-credential')
            self.assertIsNone(self.client.authenticated_session)

    def test_property_rejects_changed_device_mode_state_and_transport(self):
        context = self.login_fake()
        for attribute, bad in (('device_id', 'other-device'), ('_device_type', 'ANDROID'),
                               ('_state', ConnectionState.DISCONNECTED), ('_closed', True),
                               ('_sock', _Socket())):
            with self.subTest(attribute=attribute):
                old = getattr(self.client, attribute)
                setattr(self.client, attribute, bad)
                self.assertIsNone(self.client.authenticated_session)
                setattr(self.client, attribute, old)
        self.assertIs(self.client.authenticated_session, context)

    def test_close_drop_and_logout_clear_authenticated_context(self):
        for action in ('close', 'drop', 'logout'):
            self.setUp()
            self.login_fake()
            self.client.request = Mock(return_value=MaxFrame(1, 4, opcodes.LOGOUT, b'', {}))
            if action == 'close':
                self.client.close()
            elif action == 'drop':
                self.client._handle_drop(self.client._sock)
            else:
                self.client.logout()
            self.assertIsNone(self.client.authenticated_session)
            self.assertIsNone(self.client._auth_session)

    def test_stale_reader_drop_does_not_invalidate_current_authenticated_session(self):
        context = self.login_fake()
        self.client._handle_drop(_Socket())
        self.assertIs(self.client.authenticated_session, context)
        self.assertIs(self.client._auth_session, context)

    def test_dispatch_captures_current_context_overwrites_supplied_and_does_not_ack137(self):
        context = self.login_fake()
        supplied = AuthenticatedSession(999, 'other-device', 'WEB', _Socket())
        frame = MaxFrame(0, 7, 137, b'', {'conversationId': 'synthetic'}, auth_context=supplied)
        self.client._dispatch(frame, source=self.client._sock)
        self.assertEqual(self.received, [frame])
        self.assertIs(frame.auth_context, context)
        self.assertEqual(self.client._sock.sent, [])

    def test_stale_socket_and_rpc_commands_cannot_become_incoming_notice(self):
        self.login_fake()
        stale = _Socket()
        self.client._dispatch(MaxFrame(0, 7, 137, b'', {}), source=stale)
        for cmd in (1, 2, 3):
            self.client._dispatch(MaxFrame(cmd, 7, 137, b'', {}), source=self.client._sock)
        self.assertEqual(self.received, [])
        pending = _Pending(opcodes.LOGIN, self.client._sock)
        self.client._pending[7] = pending
        self.client._dispatch(MaxFrame(0, 7, 137, b'', {}), source=self.client._sock)
        self.assertFalse(pending.event.is_set())
        self.assertEqual(len(self.received), 1)
        self.assertEqual(self.client._sock.sent, [])

    def test_receipt_context_survives_renewal_before_callback_without_rebinding(self):
        context = self.login_fake()
        def receive(frame):
            self.client._auth_session = AuthenticatedSession(101, self.client.device_id, 'WEB', self.client._sock)
            self.received.append(frame)
        self.client.on_push = receive
        frame = MaxFrame(0, 7, 137, b'', {})
        self.client._dispatch(frame, source=self.client._sock)
        self.assertIs(frame.auth_context, context)
        self.assertIsNot(frame.auth_context, self.client.authenticated_session)


if __name__ == '__main__':
    unittest.main()
