"""Offline calls contracts; fake backend only, no browser or capture device."""
import unittest
import json
import base64
from maxclient.core.call_runtime import CallConfig, CallRuntime, CallRuntimeError
from maxclient.protocol.client import MaxWriteOutcomeUnknown


class Backend:
    def __init__(self):
        self.calls = []
        self.response = {}
        self.error = None

    def rpc(self, opcode, payload):
        self.calls.append((opcode, payload))
        if self.error:
            raise self.error
        return self.response


class CallRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.backend = Backend()
        self.authorized = True
        self.runtime = CallRuntime(self.backend, is_authorized=lambda: self.authorized)

    def tearDown(self):
        self.runtime.close()

    def outgoing(self):
        self.intent = self.runtime.arm_outgoing(77, is_video=True)
        return self.intent['generation']

    def start_payload(self):
        return {'conversationId': 'synthetic-call', 'internalParams': 'synthetic-sdk-params', 'isVideo': True}

    def test_disarmed_operations_never_reach_backend(self):
        with self.assertRaises(CallRuntimeError):
            self.runtime.execute('call_token', {}, 0)
        self.assertEqual(self.backend.calls, [])

    def test_stale_generation_and_account_replacement_never_reach_backend(self):
        generation = self.outgoing()
        with self.assertRaises(CallRuntimeError):
            self.runtime.execute('call_token', {}, generation - 1)
        self.authorized = False
        with self.assertRaises(CallRuntimeError):
            self.runtime.execute('fast_start', self.start_payload(), generation)
        self.assertEqual(self.backend.calls, [])

    def test_only_call_token_from158_is_returned(self):
        generation = self.outgoing()
        self.backend.response = {'token': 'synthetic-calls-only', 'token_lifetime_ts': 2000, 'token_refresh_ts': 1500,
                                 'loginToken': 'synthetic-forbidden-role'}
        result = self.runtime.execute('call_token', {}, generation)
        self.assertEqual(self.backend.calls, [(158, None)])
        self.assertEqual(result, {'token': 'synthetic-calls-only', 'lifetimeTs': 2000, 'refreshTs': 1500})

    def test_target_is_pinned_in_python_and_peer_injection_is_rejected(self):
        generation = self.outgoing()
        injected = {**self.start_payload(), 'calleeIds': [999]}
        with self.assertRaises(CallRuntimeError):
            self.runtime.execute('fast_start', injected, generation)
        self.backend.response = {'internalCallerParams': '{"endpoint":"wss://signal.invalid/call?token=synthetic"}', 'rejectedParticipants': []}
        self.runtime.execute('fast_start', self.start_payload(), generation)
        self.assertEqual(self.backend.calls, [(78, {**self.start_payload(), 'calleeIds': [77]})])

    def test_single_start_and_bound_hangup(self):
        generation = self.outgoing()
        self.backend.response = {'internalCallerParams': '{"endpoint":"wss://signal.invalid/call"}', 'rejectedParticipants': []}
        self.runtime.execute('fast_start', self.start_payload(), generation)
        with self.assertRaises(CallRuntimeError):
            self.runtime.execute('fast_start', self.start_payload(), generation)
        with self.assertRaises(CallRuntimeError):
            self.runtime.execute('fast_hangup', {'conversationId': 'wrong', 'reason': 'ENDED'}, generation)
        self.backend.response = {'success': True}
        self.assertEqual(self.runtime.execute('fast_hangup', {'conversationId': 'synthetic-call', 'reason': 'ENDED'}, generation), {'success': True})
        self.assertEqual(self.backend.calls[-1], (167, {'conversationId': 'synthetic-call', 'reason': 'ENDED'}))

    def test_unknown_write_stops_further_requests_without_leaking_error(self):
        generation = self.outgoing()
        self.backend.error = MaxWriteOutcomeUnknown(78)
        with self.assertRaises(CallRuntimeError) as caught:
            self.runtime.execute('fast_start', self.start_payload(), generation)
        self.assertIn('неизвестен', str(caught.exception))
        with self.assertRaises(CallRuntimeError):
            self.runtime.execute('call_token', {}, generation)
        self.assertEqual(len(self.backend.calls), 1)

    def test_backend_error_and_malformed_ack_are_sanitized(self):
        generation = self.outgoing()
        self.backend.error = ValueError('synthetic-secret-that-must-not-leak')
        with self.assertRaises(CallRuntimeError) as caught:
            self.runtime.execute('call_token', {}, generation)
        self.assertNotIn('synthetic-secret', str(caught.exception))

    def test_join_and_incoming_bind_normal_conversation(self):
        generation = self.runtime.arm_join('synthetic-join')['generation']
        params = '{"endpoint":"wss://signal.invalid/call"}'
        self.backend.response = {'conversationId': 'joined-call', 'internalParams': params}
        result = self.runtime.execute('fast_join', {'joinLink': 'synthetic-join', 'isVideo': False, 'internalParams': 'sdk'}, generation)
        self.assertEqual(result, {'conversationId': 'joined-call', 'internalCallerParams': params})
        self.assertEqual(self.backend.calls[0], (166, {'joinLink': 'synthetic-join', 'isVideo': False, 'internalParams': 'sdk'}))

    def test_config_fails_closed_missing_or_untrusted_scheme(self):
        for config in (CallConfig('', 'https://calls.invalid', 'public'), CallConfig('26.10.11', 'http://calls.invalid', 'public'), CallConfig('26.10.11', 'https://calls.invalid', '')):
            with self.assertRaises(CallRuntimeError):
                config.public_dict()

    def test_cancel_during_start_blocks_new_start_but_keeps_bound_hangup(self):
        generation = self.outgoing()
        self.runtime.cancel_start()
        with self.assertRaises(CallRuntimeError):
            self.runtime.execute('fast_start', self.start_payload(), generation)
        self.assertEqual(self.backend.calls, [])

    def test_incoming_origins_are_decoded_from_bounded_server_vcp(self):
        raw = json.dumps({'wse': 'wss://incoming.invalid/call?token=synthetic', 'wte': 'https://wt.invalid'}).encode()
        n = len(raw)
        compressed = bytes([0xF0]) + bytes([n - 15]) + raw  # valid literal-only LZ4 block
        vcp = str(n) + ':' + base64.b64encode(compressed).decode()
        captured = []
        self.runtime.origins.connect(captured.append)
        self.runtime.arm_incoming('incoming-call', vcp)
        self.assertEqual(captured, [{'wss://incoming.invalid', 'https://wt.invalid'}])

    def test_invalid_or_oversized_vcp_never_arms_a_call(self):
        for vcp in ('not-vcp', '2000000:AAAA', '30:AAAA'):
            with self.assertRaises(CallRuntimeError):
                self.runtime.arm_incoming('incoming-call', vcp)
        self.assertEqual(self.backend.calls, [])


if __name__ == '__main__':
    unittest.main()
