"""Offline QR contracts from the official current web producer/consumer.

No request is sent; all replies, tracks and credentials below are synthetic.
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from maxclient.protocol import codec
from maxclient.protocol.client import (
    MaxClient, MaxError, MaxFrame, MaxLoginFailed, MaxTimeout,
    MaxWriteOutcomeUnknown, _redact,
)


class QrProtocolContracts(unittest.TestCase):
    def client(self, opcode, response):
        client = MaxClient(auto_reconnect=False)
        client._device_type = 'WEB'
        client.request = Mock(return_value=MaxFrame(1, 7, opcode, b'', response))
        return client

    def challenge(self):
        return {'trackId': 'synthetic-track', 'qrLink': 'https://max.ru/synthetic-qr',
                'expiresAt': 1793459600000, 'pollingInterval': 5000, 'ttl': 60000}

    def test_get_qr_sends_no_payload_and_preserves_server_timers(self):
        response = self.challenge()
        client = self.client(288, response)
        self.assertEqual(client.qr_create(), response)
        client.request.assert_called_once_with(288, None)
        self.assertIsNone(client._token)

    def test_zero_body_frame_matches_official_undefined_encoder(self):
        packet = codec.build_frame(7, 288, None)
        self.assertEqual(len(packet), codec.HEADER_LEN)
        self.assertEqual(codec.parse_header(packet), (10, 0, 7, 288, 0, 0))
        self.assertEqual(codec.build_frame(7, 288, {})[codec.HEADER_LEN:], b'\x80')

    def test_poll_preserves_explicit_server_status_and_schema_defaults(self):
        status = {'loginAvailable': True, 'expiresAt': 1793459600000, 'ttl': 30000}
        client = self.client(289, {'status': status})
        self.assertEqual(client.qr_poll('synthetic-track'), {'status': status})
        client.request.assert_called_once_with(289, {'trackId': 'synthetic-track'})
        client.request.return_value = MaxFrame(1, 8, 289, b'', {'status': {}})
        self.assertEqual(client.qr_poll('synthetic-track'),
                         {'status': {'loginAvailable': False, 'expiresAt': 0, 'ttl': 0}})

    def test_finish_requires_both_login_attribute_and_profile_without_installing_token(self):
        response = {'tokenAttrs': {'LOGIN': {'token': 'synthetic-login-token', 'ttl': 3600}},
                    'profile': {'contact': {'id': 42}}}
        client = self.client(291, response)
        self.assertEqual(client.qr_finish('synthetic-track'), response)
        client.request.assert_called_once_with(291, {'trackId': 'synthetic-track'})
        self.assertIsNone(client._token)

    def test_password_challenge_is_returned_and_never_submitted_or_bypassed(self):
        response = {'tokenAttrs': {}, 'passwordChallenge': {
            'trackId': 'synthetic-password-track', 'hint': 'synthetic hint',
            'config': {'passMinLen': 1, 'passMaxLen': 100}}}
        client = self.client(291, response)
        self.assertEqual(client.qr_finish('synthetic-track'), response)
        client.request.assert_called_once_with(291, {'trackId': 'synthetic-track'})
        self.assertIsNone(client._token)

    def test_explicit_password_uses_same_challenge_115_and_keeps_typed_login_result(self):
        response = {'tokenAttrs': {'LOGIN': {'token': 'synthetic-login-token'}},
                    'profile': {'contact': {'id': 42}}}
        client = self.client(115, response)
        self.assertEqual(client.qr_check_password('synthetic-password-track', 'synthetic-explicit-password'), response)
        client.request.assert_called_once_with(115, {'trackId': 'synthetic-password-track',
                                                     'password': 'synthetic-explicit-password'})
        self.assertIsNone(client._token)

    def test_password_result_cannot_be_register_only_or_another_password_challenge(self):
        for response in [
            {'tokenAttrs': {'REGISTER': {'token': 'synthetic-register-token'}},
             'profile': {'contact': {'id': 42}}},
            {'tokenAttrs': {}, 'passwordChallenge': {'trackId': 'synthetic-second-track'}},
        ]:
            client = self.client(115, response)
            with self.assertRaises(MaxWriteOutcomeUnknown):
                client.qr_check_password('synthetic-password-track', 'synthetic-explicit-password')
            self.assertEqual(client.request.call_count, 1)
            self.assertIsNone(client._token)

    def test_register_only_and_incomplete_login_are_not_reported_as_signed_in(self):
        for response in [
            {'tokenAttrs': {'REGISTER': {'token': 'synthetic-register-token'}}},
            {'tokenAttrs': {'LOGIN': {'token': 'synthetic-login-token'}}},
            {'tokenAttrs': {}, 'profile': {'contact': {'id': 42}}},
            {'tokenAttrs': {'LOGIN': {'token': 'synthetic-login-token'}},
             'profile': {'contact': {'id': True}}},
            {'tokenAttrs': {}, 'passwordChallenge': {'hint': 'missing track'}},
            {'tokenAttrs': {}, 'passwordChallenge': None},
            {'tokenAttrs': {'LOGIN': {'token': 'synthetic-login-token', 'ttl': True}},
             'profile': {'contact': {'id': 42}}},
            None,
        ]:
            client = self.client(291, response)
            with self.assertRaises(MaxWriteOutcomeUnknown):
                client.qr_finish('synthetic-track')
            self.assertIsNone(client._token)
            self.assertEqual(client.request.call_count, 1)

    def test_qr_receiver_cannot_replace_an_already_authenticated_session(self):
        for logged_in, device in [(True, 'WEB'), (False, 'ANDROID')]:
            client = self.client(288, self.challenge())
            client._token = 'synthetic-existing-token' if logged_in else None
            client._device_type = device
            for call in [client.qr_create, lambda: client.qr_poll('synthetic-track'),
                         lambda: client.qr_finish('synthetic-track'),
                         lambda: client.qr_check_password('synthetic-track', 'synthetic-password')]:
                with self.assertRaises(MaxError):
                    call()
            client.request.assert_not_called()

    def test_invalid_timers_and_status_types_are_not_coerced(self):
        for key, value in [('ttl', True), ('ttl', -1), ('pollingInterval', 0),
                           ('pollingInterval', float('nan')), ('expiresAt', float('inf')),
                           ('trackId', ''), ('qrLink', 123)]:
            response = self.challenge()
            response[key] = value
            with self.assertRaises(MaxWriteOutcomeUnknown):
                self.client(288, response).qr_create()
        for status in [None, {'loginAvailable': 1}, {'ttl': True}, {'expiresAt': -1}]:
            with self.assertRaises(MaxError):
                self.client(289, {'status': status}).qr_poll('synthetic-track')

    def test_create_and_finish_unknown_outcome_are_never_retried(self):
        for call in [lambda c: c.qr_create(), lambda c: c.qr_finish('synthetic-track'),
                     lambda c: c.qr_check_password('synthetic-track', 'synthetic-password')]:
            client = self.client(288, self.challenge())
            client.request.side_effect = MaxTimeout('private synthetic server detail')
            with self.assertRaises(MaxWriteOutcomeUnknown) as caught:
                call(client)
            self.assertNotIn('private synthetic server detail', str(caught.exception))
            self.assertEqual(client.request.call_count, 1)

    def test_nack_and_invalid_track_do_not_leak_qr_link_or_send_other_commands(self):
        client = self.client(291, {})
        client.request.return_value = MaxFrame(3, 7, 291, b'synthetic private data',
                                              {'error': 'synthetic.denied', 'message': 'private synthetic detail'})
        with self.assertRaisesRegex(MaxLoginFailed, 'synthetic.denied') as caught:
            client.qr_finish('synthetic-track')
        self.assertNotIn('private synthetic detail', str(caught.exception))
        client.request.reset_mock()
        for track in ['', None, 1, 'x' * 4097]:
            with self.assertRaises(ValueError):
                client.qr_poll(track)
        client.request.assert_not_called()
        self.assertEqual(_redact({'qrLink': 'synthetic-link', 'trackId': 'synthetic-track'}),
                         {'qrLink': '***', 'trackId': '***'})


if __name__ == '__main__':
    unittest.main()
