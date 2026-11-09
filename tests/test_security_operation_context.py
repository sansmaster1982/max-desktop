"""Synthetic security-operation races. No sockets or real credentials are used."""
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from maxclient.core.config import AppPaths, Session, Settings
from maxclient.core.service import MaxService
from maxclient.protocol.client import (AuthenticatedSession, ConnectionState, MaxClient, MaxFrame,
    MaxNotConnected, MaxTimeout, MaxWriteNotStarted, MaxWriteOutcomeUnknown)
from test_token_service import FakeClient, confirm_fake_transport, login_frame


class SecurityClient(FakeClient):
    def __init__(self, **options):
        super().__init__(**options)
        self.calls = []
        self.after_call = lambda _opcode: None
        self.rejection = None

    def step(self, opcode, expected_session):
        if expected_session is not self.authenticated_session:
            raise AssertionError('A security RPC lost the captured transport')
        self.calls.append((opcode, expected_session))
        self.after_call(opcode)
        if self.rejection == opcode:
            return MaxFrame(3, 1, opcode, b'', {'error': 'synthetic.refusal'})
        return MaxFrame(1, 1, opcode, b'', {'trackId': 'synthetic-track'})

    def auth_create_track(self, kind, *, expected_session):
        self.step(112, expected_session)
        return 'synthetic-initial-track'

    def auth_check_password(self, track, password, *, expected_session):
        return self.step(113, expected_session)

    def auth_set_2fa(self, track, password, hint, *, capability, expected_session):
        return self.step(111, expected_session)

    def auth_verify_email(self, track, email, *, expected_session):
        return self.step(109, expected_session)

    def auth_check_email(self, track, code, *, expected_session):
        return self.step(110, expected_session)


class SecurityOperationContextTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='maxim-security-context-')
        self.env = patch.dict(os.environ, {'MAX_DESKTOP_DATA_DIR': self.temporary.name})
        self.env.start()
        paths = AppPaths()
        with patch('maxclient.core.service.MaxClient', SecurityClient):
            self.service = MaxService(paths, Settings.load(paths), Session.load(paths))
        self.service._finish_login(login_frame(), None)
        confirm_fake_transport(self.service)
        self.context = self.service._history_read_context()
        self.client = self.service.client

    def tearDown(self):
        self.assertEqual(self.client.login_calls, [])
        self.assertEqual(self.client.connect_calls, [])
        self.service.shutdown()
        self.env.stop()
        self.temporary.cleanup()

    def invoke(self, operation, context):
        self.service._twofa_cooldown_until = 0
        if operation == 'enable':
            return self.service.enable_2fa('synthetic-new-password', context=context)
        if operation == 'change':
            return self.service.change_2fa('synthetic-new-password',
                current_password='synthetic-old-password', context=context)
        if operation == 'email':
            return self.service.start_set_recovery_email('example@example.test',
                current_password='synthetic-old-password', context=context)
        return self.service.confirm_recovery_email('synthetic-track', 'synthetic-code', context=context)

    def test_every_security_step_uses_the_original_session(self):
        for operation, opcodes in (('enable', [112, 111]), ('change', [112, 113, 111]),
                                   ('email', [112, 113, 109]), ('confirm', [110])):
            with self.subTest(operation=operation):
                self.client.calls.clear()
                self.invoke(operation, self.context)
                self.assertEqual([op for op, _ in self.client.calls], opcodes)
                self.assertTrue(all(ctx is self.context[2] for _, ctx in self.client.calls))

    def test_default_context_is_captured_without_authentication(self):
        self.invoke('change', None)
        self.assertEqual([op for op, _ in self.client.calls], [112, 113, 111])
        self.assertTrue(all(ctx is self.context[2] for _, ctx in self.client.calls))

    def test_reconnected_session_rejects_the_queued_old_operation(self):
        confirm_fake_transport(self.service)
        for operation in ('enable', 'change', 'email', 'confirm'):
            with self.subTest(operation=operation), self.assertRaises(ValueError):
                self.invoke(operation, self.context)
        self.assertEqual(self.client.calls, [])

    def test_account_change_rejects_the_queued_operation(self):
        self.service.session.my_user_id = 456
        with self.assertRaises(ValueError):
            self.invoke('change', self.context)
        self.assertEqual(self.client.calls, [])

    def test_device_change_rejects_the_queued_operation(self):
        self.service.session.device_id = 'synthetic-other-device'
        with self.assertRaises(ValueError):
            self.invoke('email', self.context)
        self.assertEqual(self.client.calls, [])

    def test_reconnect_between_each_stage_never_continues_or_reports_success(self):
        for operation, sequence in (('enable', [112, 111]), ('change', [112, 113, 111]),
                                    ('email', [112, 113, 109]), ('confirm', [110])):
            for stage, opcode in enumerate(sequence):
                with self.subTest(operation=operation, opcode=opcode):
                    confirm_fake_transport(self.service)
                    context = self.service._history_read_context()
                    self.client.calls.clear()
                    self.client.after_call = lambda op, expected=opcode: (
                        confirm_fake_transport(self.service) if op == expected else None)
                    with self.assertRaises(MaxWriteOutcomeUnknown):
                        self.invoke(operation, context)
                    self.assertEqual([op for op, _ in self.client.calls], sequence[:stage + 1])
                    self.assertTrue(all(ctx is context[2] for _, ctx in self.client.calls))

    def test_rejected_password_never_reaches_commit_or_email_send(self):
        self.client.rejection = 113
        for operation in ('change', 'email'):
            self.client.calls.clear()
            with self.subTest(operation=operation), self.assertRaises(ValueError):
                self.invoke(operation, self.context)
            self.assertEqual([op for op, _ in self.client.calls], [112, 113])

    def test_cooldown_blocks_a_second_request_even_on_same_session(self):
        self.invoke('enable', self.context)
        with self.assertRaises(ValueError):
            self.service.enable_2fa('synthetic-new-password', context=self.context)
        self.assertEqual([op for op, _ in self.client.calls], [112, 111])

    def test_timeout_and_transport_loss_remain_unknown_without_retry(self):
        for operation, sequence in (('enable', [112, 111]), ('change', [112, 113, 111]),
                                    ('email', [112, 113, 109]), ('confirm', [110])):
            for stage, opcode in enumerate(sequence):
                for exception in (MaxTimeout, MaxNotConnected):
                    with self.subTest(operation=operation, opcode=opcode, exception=exception):
                        self.client.calls.clear()
                        def fail_on(op, target=opcode, kind=exception):
                            if op == target:
                                raise kind('synthetic transport failure')
                        self.client.after_call = fail_on
                        with self.assertRaises(MaxWriteOutcomeUnknown):
                            self.invoke(operation, self.context)
                        self.assertEqual([op for op, _ in self.client.calls], sequence[:stage + 1])

    def test_missing_password_fails_before_any_rpc(self):
        for call in (
            lambda: self.service.change_2fa('synthetic-new', context=self.context),
            lambda: self.service.start_set_recovery_email('example@example.test', context=self.context),
        ):
            with self.assertRaises(ValueError):
                call()
        self.assertEqual(self.client.calls, [])


class SecurityWireContextTests(unittest.TestCase):
    def test_replacement_inside_send_lock_sends_to_neither_transport(self):
        for method, args in (
            ('auth_create_track', (0,)), ('auth_check_password', ('t', 'p')),
            ('auth_set_2fa', ('t', 'p')), ('auth_verify_email', ('t', 'example@example.test')),
            ('auth_check_email', ('t', 'c')),
        ):
            with self.subTest(method=method):
                client = MaxClient(auto_reconnect=False)
                old, new = Mock(), Mock()
                client._sock = old
                client._device_type = 'WEB'
                client._state = ConnectionState.CONNECTED
                context = AuthenticatedSession(123, client.device_id, 'WEB', old)
                client._auth_session = context
                class ReplaceLock:
                    def __enter__(self):
                        client._sock = new
                        client._auth_session = AuthenticatedSession(123, client.device_id, 'WEB', new)
                    def __exit__(self, *_):
                        return False
                client._send_lock = ReplaceLock()
                with self.assertRaises(MaxWriteNotStarted):
                    getattr(client, method)(*args, expected_session=context)
                old.sendall.assert_not_called()
                new.sendall.assert_not_called()
                self.assertEqual(client._pending, {})

    def test_expected_session_reaches_request_without_payload_changes(self):
        client = MaxClient(auto_reconnect=False)
        session = object()
        operations = (
            (112, lambda: client.auth_create_track(0, expected_session=session), {'type': 0}),
            (113, lambda: client.auth_check_password('t', 'p', expected_session=session),
             {'trackId': 't', 'password': 'p'}),
            (111, lambda: client.auth_set_2fa('t', 'p', 'h', capability=1, expected_session=session),
             {'trackId': 't', 'password': 'p', 'hint': 'h', 'expectedCapabilities': [1]}),
            (109, lambda: client.auth_verify_email('t', 'example@example.test', expected_session=session),
             {'trackId': 't', 'email': 'example@example.test'}),
            (110, lambda: client.auth_check_email('t', 'c', expected_session=session),
             {'trackId': 't', 'verifyCode': 'c'}),
        )
        for opcode, call, payload in operations:
            with self.subTest(opcode=opcode):
                client.request = Mock(return_value=MaxFrame(1, 1, opcode, b'', {'trackId': 'synthetic-track'}))
                call()
                client.request.assert_called_once_with(opcode, payload, expected_session=session)


if __name__ == '__main__':
    unittest.main()
