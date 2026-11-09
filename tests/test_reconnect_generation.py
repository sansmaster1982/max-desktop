"""Old reconnect completions cannot mutate a replacement generation; no I/O."""
import threading
import unittest
from unittest.mock import Mock

from maxclient.protocol.client import (
    AuthenticatedSession, ConnectionState, MaxClient, MaxFrame,
    MaxLoginFailed, MaxWriteOutcomeUnknown,
)


class _Socket:
    def sendall(self, _frame):
        raise AssertionError('No network writes allowed')
    def shutdown(self, _how=None):
        pass
    def close(self):
        pass


class ReconnectGenerationTests(unittest.TestCase):
    def setUp(self):
        self.client = MaxClient(device_id='synthetic-device', auto_reconnect=False)
        self.client._device_type = 'WEB'
        self.client._token = 'synthetic-old-credential'
        self.client._sock = _Socket()
        self.client._state = ConnectionState.RECONNECTING
        self.client._reconnect_running = True
        self.old_event = threading.Event()
        self.client._reconnect_stop = self.old_event
        self.client._reconnect_delay = lambda _attempt: 0
        self.client.on_auth_invalid = Mock()
        self.client._auth_frame = Mock(side_effect=AssertionError('No auth requests allowed'))
        def connect(**_kwargs):
            self.client._sock = _Socket()
            self.client._state = ConnectionState.CONNECTED
        self.client.connect = Mock(side_effect=connect)

    def replace_generation(self):
        self.old_event.set()
        self.replacement_event = threading.Event()
        self.replacement_socket = _Socket()
        self.replacement_context = AuthenticatedSession(
            404, self.client.device_id, 'WEB', self.replacement_socket)
        self.client._sock = self.replacement_socket
        self.client._auth_session = self.replacement_context
        self.client._token = 'synthetic-replacement-credential'
        self.client._state = ConnectionState.CONNECTED
        self.client._reconnect_stop = self.replacement_event
        self.client._reconnect_running = True
        self.replacement_thread = object()
        self.client._reconnect_thread = self.replacement_thread

    def assert_replacement_untouched(self):
        self.assertEqual(self.client.token, 'synthetic-replacement-credential')
        self.assertIs(self.client.authenticated_session, self.replacement_context)
        self.assertIs(self.client._sock, self.replacement_socket)
        self.assertIs(self.client._reconnect_stop, self.replacement_event)
        self.assertIs(self.client._reconnect_thread, self.replacement_thread)
        self.assertTrue(self.client._reconnect_running)
        self.assertFalse(self.replacement_event.is_set())
        self.client.on_auth_invalid.assert_not_called()
        self.client._auth_frame.assert_not_called()
        self.assertEqual(self.client._reconnect_ok_ts, [])

    def test_old_login_error_handlers_leave_new_credential_and_worker_untouched(self):
        for error in (MaxLoginFailed('synthetic rejection'),
                      MaxWriteOutcomeUnknown('synthetic unknown'), RuntimeError('synthetic failure')):
            with self.subTest(kind=type(error).__name__):
                self.setUp()
                def login(_token):
                    self.replace_generation()
                    raise error
                self.client.login = Mock(side_effect=login)
                self.client._reconnect_loop()
                self.client.login.assert_called_once_with('synthetic-old-credential')
                self.assert_replacement_untouched()

    def test_stale_success_cannot_commit_or_stop_replacement_worker(self):
        def login(_token):
            self.replace_generation()
            return MaxFrame(1, 8, 19, b'', {'profile': {'contact': {'id': 101}}})
        self.client.login = Mock(side_effect=login)
        self.client._reconnect_loop()
        self.assert_replacement_untouched()

    def test_stale_connect_result_does_not_login_or_teardown_current_socket(self):
        self.client.connect = Mock(side_effect=lambda **_kwargs: self.replace_generation())
        self.client.login = Mock(side_effect=AssertionError('Stale worker cannot authenticate'))
        self.client._reconnect_loop()
        self.client.login.assert_not_called()
        self.assert_replacement_untouched()

    def test_stale_callback_failure_cannot_stop_the_new_generation(self):
        def login(_token):
            self.client._auth_session = AuthenticatedSession(
                101, self.client.device_id, 'WEB', self.client._sock)
            return MaxFrame(1, 8, 19, b'', {'profile': {'contact': {'id': 101}}})
        def callback(_frame, _context):
            self.replace_generation()
            raise ValueError('Synthetic replaced session')
        self.client.login = Mock(side_effect=login)
        self.client.on_reauthenticated = callback
        self.client._reconnect_loop()
        self.assert_replacement_untouched()

    def test_active_generation_rejection_still_closes_and_reports_once(self):
        self.client.login = Mock(side_effect=MaxLoginFailed('synthetic rejection'))
        self.client._reconnect_loop()
        self.assertIsNone(self.client.token)
        self.assertFalse(self.client._reconnect_running)
        self.assertIsNone(self.client._sock)
        self.client.on_auth_invalid.assert_called_once_with()
        self.client._auth_frame.assert_not_called()


if __name__ == '__main__':
    unittest.main()
