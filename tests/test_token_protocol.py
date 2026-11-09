"""Offline regressions for token auth, binary IDs, and the WEB transport."""
import ssl
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import msgpack

from maxclient.protocol import codec, opcodes
from maxclient.protocol.client import (
    MaxClient, MaxFrame, MaxLoginFailed, MaxNotConnected, MaxWriteOutcomeUnknown,
    _Pending, _WebSocketTransport,
)


class TokenCodecTests(unittest.TestCase):
    def test_extended_integers_in_values_and_map_keys(self):
        wrapped = lambda value: msgpack.ExtType(1, msgpack.packb(value))
        payload = {'profile': {'contact': {'id': wrapped(-42), 'phone': wrapped(79990000000)}},
                   'participants': {wrapped(-42): wrapped(1793459502608)}}
        packed = msgpack.packb(payload, use_bin_type=True)
        decoded = codec.unpack_payload(packed)
        self.assertEqual(decoded['profile']['contact'], {'id': -42, 'phone': 79990000000})
        self.assertEqual(decoded['participants'], {-42: 1793459502608})

    def test_unknown_or_non_integer_extensions_are_not_coerced(self):
        for extension in [msgpack.ExtType(3, b'x'), msgpack.ExtType(1, msgpack.packb(True)),
                          msgpack.ExtType(1, msgpack.packb('text')), msgpack.ExtType(1, b'\xc1')]:
            self.assertEqual(codec.unpack_payload(msgpack.packb(extension)), extension)

    def test_packet_and_decompression_size_limits(self):
        with patch.object(codec, 'MAX_PAYLOAD_BYTES', 32):
            with self.assertRaises(ValueError):
                codec.build_frame(0, 19, {'token': 'x' * 40})
            header = bytes([10, 1, 0, 0, 0, 19, 0, 0, 0, 33])
            with self.assertRaises(ValueError):
                codec.parse_header(header)
            self.assertIsNone(codec.unpack_payload(b'x' * 33))
            self.assertEqual(codec.decompress_body(b'\x30abc', 1), b'abc')
            with self.assertRaises(ValueError):
                codec.decompress_body(b'\x50ab', 1)
            with self.assertRaises(ValueError):
                codec.decompress_body(b'\x1fA\x01\x00\x40', 1)


class FakeSocket:
    def __init__(self):
        self.sent = []

    def sendall(self, payload):
        self.sent.append(bytes(payload))

    def shutdown(self, _how=None):
        pass

    def close(self):
        pass


class TokenDispatchTests(unittest.TestCase):
    def setUp(self):
        self.push = []
        self.client = MaxClient(on_push=self.push.append, auto_reconnect=False)
        self.client._sock = FakeSocket()

    def test_push_same_seq_wrong_opcode_and_wrong_seq_cannot_complete_login(self):
        pending = _Pending(opcodes.LOGIN)
        self.client._pending[7] = pending
        self.client._dispatch(MaxFrame(0, 7, 19, b'', {'profile': {'id': 1}}))
        self.client._dispatch(MaxFrame(1, 7, 16, b'', {'profile': {'id': 1}}))
        self.client._dispatch(MaxFrame(1, 8, 19, b'', {'profile': {'id': 1}}))
        self.assertFalse(pending.event.is_set())
        self.assertIs(self.client._pending[7], pending)
        self.assertEqual(len(self.push), 1)
        answer = MaxFrame(1, 7, 19, b'', {'profile': {'id': 1}})
        self.client._dispatch(answer)
        self.assertTrue(pending.event.is_set())
        self.assertIs(pending.frame, answer)
        self.assertNotIn(7, self.client._pending)

    def test_matching_rejection_completes_pending_request(self):
        pending = _Pending(opcodes.LOGIN)
        self.client._pending[7] = pending
        answer = MaxFrame(3, 7, 19, b'', {'error': 'login.token'})
        self.client._dispatch(answer)
        self.assertIs(pending.frame, answer)
        self.assertTrue(pending.event.is_set())

    def test_stale_socket_reply_cannot_complete_replacement_connection_request(self):
        current = self.client._sock
        stale = FakeSocket()
        pending = _Pending(opcodes.LOGIN, current)
        self.client._pending[7] = pending
        answer = MaxFrame(1, 7, 19, b'', {'profile': {'id': 1}})
        self.client._dispatch(answer, source=stale)
        self.assertFalse(pending.event.is_set())
        self.client._dispatch(answer, source=current)
        self.assertTrue(pending.event.is_set())

    def test_server_ping_ack_preserves_sequence_and_pending_login(self):
        pending = _Pending(opcodes.LOGIN)
        self.client._pending[7] = pending
        self.client._seq = 88
        self.client._dispatch(MaxFrame(0, 7, 1, b'', None))
        ack = self.client._sock.sent[0]
        self.assertEqual(len(ack), 10)
        self.assertEqual(codec.parse_header(ack), (10, 1, 7, 1, 0, 0))
        self.assertEqual(self.client._seq, 88)
        self.assertFalse(pending.event.is_set())
        self.assertEqual(self.push, [])

    def test_message_delivery_ack_is_separate_from_read_mark_and_login_reply(self):
        pending = _Pending(opcodes.LOGIN, self.client._sock)
        self.client._pending[7] = pending
        self.client._seq = 88
        incoming = MaxFrame(0, 7, 128, b'', {'chatId': 42, 'message': {'id': 81, 'text': 'synthetic'}})
        self.client._dispatch(incoming)
        ack = self.client._sock.sent[0]
        self.assertEqual(codec.parse_header(ack)[:4], (10, 1, 7, 128))
        self.assertEqual(codec.unpack_payload(ack[10:]), {'chatId': 42, 'messageId': 81})
        self.assertEqual(self.client._seq, 88)
        self.assertFalse(pending.event.is_set())
        self.assertEqual(self.push, [incoming])

    def test_delivery_ack_has_no_loop_and_ignores_stale_or_malformed_events(self):
        payload = {'chatId': 42, 'message': {'id': 81}}
        self.client._dispatch(MaxFrame(1, 7, 128, b'', payload))
        self.client._dispatch(MaxFrame(0, 7, 128, b'', payload), source=FakeSocket())
        self.client._dispatch(MaxFrame(0, 7, 128, b'', {'chatId': True, 'message': {'id': 81}}))
        self.client._dispatch(MaxFrame(0, 7, 128, b'', {'chatId': 42, 'message': {}}))
        self.assertEqual(self.client._sock.sent, [])

    def test_login_requires_typed_profile_and_retains_input_token(self):
        invalid = [MaxFrame(1, 0, 19, b'', {}), MaxFrame(1, 0, 19, b'', {'profile': {}}),
                   MaxFrame(0, 0, 19, b'', {'profile': {'id': 1}}),
                   MaxFrame(1, 0, 128, b'', {'profile': {'id': 1}})]
        for answer in invalid:
            with self.subTest(command=answer.cmd, opcode=answer.opcode, decoded=answer.decoded):
                client = MaxClient(auto_reconnect=False)
                client._sock = FakeSocket()
                client.request = Mock(return_value=answer)
                with self.assertRaises(MaxWriteOutcomeUnknown):
                    client.login('original-token')
                self.assertIsNone(client.token)
        client = MaxClient(auto_reconnect=False)
        client._sock = FakeSocket()
        answer = MaxFrame(1, 0, 19, b'', {'profile': {'contact': {'id': 1}},
                                             'token': 'different-response-token'})
        client.request = Mock(return_value=answer)
        self.assertIs(client.login('original-token'), answer)
        self.assertEqual(client.token, 'original-token')

    def test_error_text_never_dumps_server_messages_or_raw_credentials(self):
        secret = 'private-credential-not-for-logs'
        frame = MaxFrame(3, 0, 19, secret.encode(), {'message': secret, 'token': secret})
        self.assertEqual(frame.error_text(), 'server_rejected_request')
        frame.decoded['error'] = 'login.token'
        self.assertEqual(frame.error_text(), 'login.token')
        frame.decoded['error'] = 'unsafe error ' + secret
        self.assertEqual(frame.error_text(), 'server_rejected_request')


class WebTransportTests(unittest.TestCase):
    def test_secure_pinned_websocket_stream_adapter(self):
        ws = Mock()
        ws.recv.return_value = b'abcdef'
        factory = Mock(return_value=ws)
        with patch.dict(sys.modules, {'websocket': types.SimpleNamespace(create_connection=factory)}):
            stream = _WebSocketTransport(12.0)
        args, kwargs = factory.call_args
        self.assertEqual(args, ('wss://api.oneme.ru/websocket',))
        self.assertEqual(kwargs['origin'], 'https://web.max.ru')
        self.assertEqual(kwargs['sslopt'], {'cert_reqs': ssl.CERT_REQUIRED, 'check_hostname': True})
        self.assertTrue(kwargs['enable_multithread'])
        self.assertEqual(stream.recv(2), b'ab')
        self.assertEqual(stream.recv(10), b'cdef')
        stream.sendall(b'payload')
        ws.send_binary.assert_called_once_with(b'payload')
        stream.shutdown()
        ws.abort.assert_called_once()
        stream.close()
        ws.shutdown.assert_called_once()

    def test_web_connection_failure_never_falls_back_to_raw_tls(self):
        client = MaxClient(auto_reconnect=False)
        client._open_raw_socket = Mock(side_effect=AssertionError('must not use TLS fallback'))
        with patch('maxclient.protocol.client._WebSocketTransport', side_effect=MaxNotConnected('unavailable')):
            with self.assertRaises(MaxNotConnected):
                client.connect(device_type='WEB')
        client._open_raw_socket.assert_not_called()

    def test_web_init_keeps_imported_device_and_web_version(self):
        client = MaxClient(device_id='00112233-4455-6677-8899-aabbccddeeff', app_version='26.10.7')
        client._device_type = 'WEB'
        client.request = Mock(return_value=MaxFrame(1, 0, 6, b'', {}))
        client._init_session()
        payload = client.request.call_args.args[1]
        self.assertEqual(payload['deviceId'], '00112233-4455-6677-8899-aabbccddeeff')
        self.assertEqual(payload['userAgent'], {'deviceType': 'WEB', 'pushDeviceType': 'WEBPUSH',
                                               'locale': 'ru', 'appVersion': '26.10.7'})


if __name__ == '__main__':
    unittest.main()
