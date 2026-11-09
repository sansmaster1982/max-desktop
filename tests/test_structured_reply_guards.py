"""Synthetic-only regressions for authoritative auth/identity reply fields.

No credentials, account files, sockets, UI or network calls are used.
"""
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock

import msgpack

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from maxclient.protocol import codec, opcodes
from maxclient.protocol.client import (
    MaxClient, MaxError, MaxFrame, MaxLoginFailed, MaxTimeout,
    MaxWriteOutcomeUnknown, _peer_from_finder_error,
)


EXISTING = 'synthetic-existing-credential'
LONG_UNRELATED = 'synthetic-unrelated-' + 'x' * 140


class StructuredReplyGuards(unittest.TestCase):
    def client(self, opcode, response, *, body=None, command=1):
        client = MaxClient(auto_reconnect=False)
        client._token = EXISTING
        if body is None:
            body = msgpack.packb(response, use_bin_type=True)
        client.request = Mock(return_value=MaxFrame(command, 7, opcode, body, response))
        return client

    def auth_result(self, token_kind='LOGIN'):
        return {'tokenAttrs': {token_kind: {'token': 'synthetic-typed-credential'}},
                'profile': {'contact': {'id': 42}}}

    def assert_unchanged(self, client):
        self.assertEqual(client.token, EXISTING)
        self.assertEqual(client.request.call_count, 1)

    def test_sms_start_uses_only_the_named_verification_token(self):
        client = self.client(17, {'token': 'synthetic-verification', 'other': LONG_UNRELATED})
        self.assertEqual(client.start_auth_sms('+79990000000'), 'synthetic-verification')
        self.assert_unchanged(client)
        for reply in [None, {}, {'other': LONG_UNRELATED}, {'tokenAttrs': self.auth_result()['tokenAttrs']},
                      {'token': True}, {'token': ''}]:
            client = self.client(17, reply, body=LONG_UNRELATED.encode())
            with self.assertRaises(MaxWriteOutcomeUnknown):
                client.start_auth_sms('+79990000000')
            self.assert_unchanged(client)

    def test_sms_confirm_requires_typed_login_with_profile_and_does_not_install_it(self):
        reply = self.auth_result()
        reply['other'] = LONG_UNRELATED
        client = self.client(18, reply)
        self.assertEqual(client.confirm_sms('synthetic-verification', '123456'),
                         ('synthetic-typed-credential', None))
        self.assert_unchanged(client)

    def test_register_top_level_token_and_unrelated_long_strings_cannot_become_login(self):
        for reply in [None, {'token': LONG_UNRELATED}, self.auth_result('REGISTER'),
                      {'tokenAttrs': {'LOGIN': {'token': LONG_UNRELATED}}},
                      {'tokenAttrs': {}, 'profile': {'contact': {'id': 42}}},
                      {'tokenAttrs': {'LOGIN': {'token': LONG_UNRELATED}},
                       'profile': {'contact': {'id': True}}}]:
            for opcode, call in [(18, lambda c: c.confirm_sms('synthetic-verification', '123456')),
                                 (115, lambda c: c.confirm_2fa('synthetic-track', 'synthetic-password'))]:
                with self.subTest(opcode=opcode, response=reply):
                    client = self.client(opcode, reply, body=LONG_UNRELATED.encode())
                    with self.assertRaises(MaxWriteOutcomeUnknown):
                        call(client)
                    self.assert_unchanged(client)

    def test_password_challenge_uses_its_named_track_and_is_never_skipped(self):
        reply = {'tokenAttrs': {'REGISTER': {'token': LONG_UNRELATED}},
                 'unrelated': '11111111-2222-3333-4444-555555555555',
                 'passwordChallenge': {'trackId': 'synthetic-challenge', 'hint': 'Synthetic'}}
        client = self.client(18, reply)
        self.assertEqual(client.confirm_sms('synthetic-verification', '123456'),
                         (None, 'synthetic-challenge'))
        self.assert_unchanged(client)
        client = self.client(115, reply)
        with self.assertRaises(MaxWriteOutcomeUnknown):
            client.confirm_2fa('synthetic-challenge', 'synthetic-password')
        self.assert_unchanged(client)

    def test_explicit_2fa_returns_only_verified_login_attribute(self):
        client = self.client(115, self.auth_result())
        self.assertEqual(client.confirm_2fa('synthetic-track', 'synthetic-password'),
                         'synthetic-typed-credential')
        self.assert_unchanged(client)

    def test_auth_timeouts_are_unknown_without_retry_or_secret_disclosure(self):
        for opcode, call in [(17, lambda c: c.start_auth_sms('+79990000000')),
                             (18, lambda c: c.confirm_sms('synthetic-verification', '123456')),
                             (115, lambda c: c.confirm_2fa('synthetic-track', 'synthetic-password')),
                             (112, lambda c: c.auth_create_track())]:
            client = self.client(opcode, {})
            client.request.side_effect = MaxTimeout('synthetic-private-error-detail')
            with self.assertRaises(MaxWriteOutcomeUnknown) as caught:
                call(client)
            self.assertNotIn('synthetic-private-error-detail', str(caught.exception))
            self.assert_unchanged(client)

    def test_auth_nack_is_distinct_from_malformed_ack_and_never_retried(self):
        client = self.client(18, {'error': 'synthetic.denied', 'message': LONG_UNRELATED}, command=3)
        with self.assertRaisesRegex(MaxLoginFailed, 'synthetic.denied') as caught:
            client.confirm_sms('synthetic-verification', '123456')
        self.assertNotIn(LONG_UNRELATED, str(caught.exception))
        self.assert_unchanged(client)
        client = self.client(115, self.auth_result())
        with self.assertRaises(MaxWriteOutcomeUnknown):
            client.confirm_sms('synthetic-verification', '123456')
        self.assert_unchanged(client)

    def test_login_ignores_top_level_response_token_and_requires_canonical_profile_id(self):
        reply = {'profile': {'contact': {'id': 42}}, 'token': 'synthetic-opaque-response'}
        client = self.client(19, reply)
        client.login('synthetic-explicit-login')
        self.assertEqual(client.token, 'synthetic-explicit-login')
        for profile in [{}, {'id': 42}, {'contact': {'id': True}},
                        {'contact': {'id': '42'}}, {'contact': {'id': 2**63}}]:
            client = self.client(19, {'profile': profile, 'token': LONG_UNRELATED})
            with self.assertRaises(MaxWriteOutcomeUnknown):
                client.login('synthetic-candidate')
            self.assert_unchanged(client)

    def test_unconfirmed_login_does_not_close_or_overwrite_the_current_client_session(self):
        for response, error in [(None, None), ({'profile': {'id': 42}}, None),
                                ({}, MaxTimeout('synthetic-private-timeout'))]:
            client = self.client(19, response)
            current_socket = object()
            client._sock = current_socket
            client._teardown_socket = Mock()
            if error is not None:
                client.request.side_effect = error
            with self.assertRaises(MaxWriteOutcomeUnknown):
                client.login('synthetic-candidate')
            self.assert_unchanged(client)
            self.assertIs(client._sock, current_socket)
            client._teardown_socket.assert_not_called()

    def test_malformed_one_object_payload_cannot_recover_raw_auth_or_profile_fields(self):
        for opcode, call in [(17, lambda c: c.start_auth_sms('+79990000000')),
                             (18, lambda c: c.confirm_sms('synthetic-verification', '123456')),
                             (115, lambda c: c.confirm_2fa('synthetic-track', 'synthetic-password')),
                             (19, lambda c: c.login('synthetic-candidate')),
                             (16, lambda c: c.current_profile())]:
            body = msgpack.packb({'token': LONG_UNRELATED, **self.auth_result()}, use_bin_type=True) + b'\xc1'
            decoded = codec.unpack_payload(body)
            self.assertIsNone(decoded)
            client = self.client(opcode, decoded, body=body)
            with self.assertRaises(MaxError):
                call(client)
            self.assert_unchanged(client)

    def test_profile_read_requires_canonical_identity_not_an_unscoped_raw_id(self):
        reply = {'profile': {'contact': {'id': 42}}}
        self.assertEqual(self.client(16, reply).current_profile(), reply)
        for reply in [None, {}, {'id': 42}, {'profile': {'id': 42}},
                      {'profile': {'contact': {'id': True}}}]:
            client = self.client(16, reply, body=msgpack.packb({'id': 42, 'name': 'Synthetic'}))
            with self.assertRaises(MaxError):
                client.current_profile()
            self.assert_unchanged(client)

    def test_track_creation_uses_only_exact_track_id_field(self):
        client = self.client(112, {'trackId': 'synthetic-track'})
        self.assertEqual(client.auth_create_track(), 'synthetic-track')
        self.assert_unchanged(client)
        for reply in [None, {}, {'id': 'synthetic-alias-track'}, {'track_id': 'synthetic-alias-track'}]:
            client = self.client(112, reply, body=b'11111111-2222-3333-4444-555555555555')
            with self.assertRaises(MaxWriteOutcomeUnknown):
                client.auth_create_track()
            self.assert_unchanged(client)

    def test_contact_by_phone_never_extracts_identity_from_invalid_payload_or_aliases(self):
        reply = {'contact': {'id': 42, 'name': 'Synthetic', 'phone': 79990000000}}
        self.assertEqual(self.client(46, reply).contact_by_phone('+79990000000'),
                         {'id': 42, 'name': 'Synthetic', 'phone': '79990000000'})
        for reply in [None, {}, {'contacts': [{'id': 42}]}, {'user': {'id': 42}},
                      {'contact': {'id': '42'}}, {'contact': {'id': True}}]:
            client = self.client(46, reply, body=msgpack.packb({'id': 42, 'name': 'Synthetic'}))
            with self.assertRaises(MaxError):
                client.contact_by_phone('+79990000000')
            self.assert_unchanged(client)

    def test_legacy_history_never_fabricates_messages_from_raw_markers(self):
        self.assertEqual(self.client(49, {'messages': []}).chat_history(42)[0], [])
        for reply in [None, {}, {'messages': None}, {'messages': [{'id': 81}, 'invalid']}]:
            client = self.client(49, reply, body=msgpack.packb({'chatId': 42, 'id': 81, 'text': 'Synthetic'}))
            with self.assertRaises(MaxError):
                client.chat_history(42)
            self.assert_unchanged(client)

    def test_raw_finder_text_cannot_trigger_a_second_message_to_an_inferred_target(self):
        raw = b'common.finder[User,999]'
        for reply in [None, {}, {'error': 'not.found'}, {'message': 'User,999'},
                      {'error': 'rate.limit', 'message': 'User,999'}]:
            client = self.client(64, reply, body=raw, command=3)
            with self.assertRaises(MaxError):
                client.send_message(42, 'Synthetic')
            self.assertEqual(client.request.call_count, 1)
        # The existing explicit, structured finder rejection remains distinct.
        self.assertEqual(_peer_from_finder_error(MaxFrame(3, 7, 64, raw,
                         {'error': 'not.found', 'message': 'User,999'})), 999)

    def test_qr_finish_rejects_malformed_raw_credentials_without_installing_them(self):
        body = msgpack.packb(self.auth_result(), use_bin_type=True) + b'\xc1'
        client = self.client(291, codec.unpack_payload(body), body=body)
        client._device_type = 'WEB'
        client._token = None  # A QR receiver must be a separate unauthenticated client.
        with self.assertRaises(MaxWriteOutcomeUnknown):
            client.qr_finish('synthetic-track')
        self.assertIsNone(client.token)
        self.assertEqual(client.request.call_count, 1)

    def test_reconnect_unknown_login_preserves_credential_and_stops_without_retry(self):
        client = MaxClient(auto_reconnect=False, on_auth_invalid=Mock())
        client._token = EXISTING
        client._closed = False
        client._reconnect_running = True
        client._reconnect_stop = threading.Event()
        client._reconnect_delay = Mock(return_value=0)
        client.connect = Mock()
        client.login = Mock(side_effect=MaxWriteOutcomeUnknown('synthetic unknown ACK'))
        client._teardown_socket = Mock()
        client._reconnect_loop()
        self.assertEqual(client.token, EXISTING)
        client.connect.assert_called_once()
        client.login.assert_called_once_with(EXISTING)
        self.assertFalse(client._reconnect_running)
        client.on_auth_invalid.assert_not_called()


if __name__ == '__main__':
    unittest.main()
