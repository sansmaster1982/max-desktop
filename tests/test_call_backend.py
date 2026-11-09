"""Session/config boundary checks; fake transport only, no RTC or network."""
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from maxclient.core.call_backend import MaxCallBackend, PUBLIC_CALLS_APPLICATION_KEY
from maxclient.core.call_runtime import CallRuntimeError
from maxclient.protocol.client import ConnectionState, MaxFrame, MaxWriteOutcomeUnknown
import test_privacy_sessions_core as privacy_fixture


class CallBackendTests(unittest.TestCase):
    def setUp(self):
        self.owner = 123
        self.config = {'server': {'calls-endpoint': 'https://calls.example.invalid/api',
                                  'p2p-calls-enabled': True, 'calls-fast-hangup': True}}
        self.service = SimpleNamespace(
            _confirmed_profile_id=lambda: self.owner,
            settings=SimpleNamespace(web_app_version='26.10.11'),
            store=SimpleNamespace(load_config=lambda owner: self.config),
            client=SimpleNamespace(device_id='synthetic-device', state=ConnectionState.CONNECTED,
                                   _write_frame=Mock(return_value=MaxFrame(1, 1, 158, b'',
                                                {'token': 'synthetic-calls-only', 'token_lifetime_ts': 3,
                                                 'token_refresh_ts': 2}))))
        self.backend = MaxCallBackend(self.service)

    def test_config_comes_from_owner_bound_server_without_any_rpc(self):
        config = self.backend.config()
        self.assertEqual(config.api_base_url, self.config['server']['calls-endpoint'])
        self.assertEqual(config.api_key, PUBLIC_CALLS_APPLICATION_KEY)
        self.assertTrue(config.fast_hangup)
        self.service.client._write_frame.assert_not_called()

    def test_unavailable_or_non_https_config_is_not_guessed(self):
        for value in ('', 'http://calls.example.invalid', 'https://user:pass@calls.example.invalid',
                      'https://calls.example.invalid/?token=synthetic'):
            self.config['server']['calls-endpoint'] = value
            with self.assertRaises(CallRuntimeError):
                self.backend.config()
        self.service.client._write_frame.assert_not_called()

    def test_only_call_opcodes_allowed_no_login_token_forwarding(self):
        for opcode in (19, 111, 22, True):
            with self.assertRaises(CallRuntimeError):
                self.backend.rpc(opcode, {})
        with self.assertRaises(CallRuntimeError):
            self.backend.rpc(158, {'token': 'synthetic-login'})
        self.service.client._write_frame.assert_not_called()
        self.assertEqual(self.backend.rpc(158, None)['token'], 'synthetic-calls-only')
        self.service.client._write_frame.assert_called_once_with(158, None)

    def test_wrong_frame_or_unknown_write_never_retried(self):
        for frame in (MaxFrame(1, 1, 19, b'', {}), MaxFrame(1, 1, 158, b'', None)):
            self.service.client._write_frame.return_value = frame
            with self.assertRaises(MaxWriteOutcomeUnknown):
                self.backend.rpc(158, None)
        self.assertEqual(self.service.client._write_frame.call_count, 2)

    def test_owner_change_before_read_and_after_rpc_blocks_stale_token(self):
        self.owner = 456
        self.assertFalse(self.backend.is_authorized())
        with self.assertRaises(CallRuntimeError):
            self.backend.rpc(158, None)
        self.service.client._write_frame.assert_not_called()
        self.owner = 123
        def swapped(*_args):
            self.owner = 456
            return MaxFrame(1, 1, 158, b'', {'token': 'must-not-return'})
        self.service.client._write_frame.side_effect = swapped
        with self.assertRaises(MaxWriteOutcomeUnknown):
            self.backend.rpc(158, None)

    def test_device_or_connection_change_invalidates_backend(self):
        self.service.client.device_id = 'changed-device'
        self.assertFalse(self.backend.is_authorized())
        self.service.client.device_id = 'synthetic-device'
        self.service.client.state = ConnectionState.DISCONNECTED
        self.assertFalse(self.backend.is_authorized())

    def test_replaced_client_and_server_disabled_flag_block_call(self):
        self.config['server']['p2p-calls-enabled'] = False
        with self.assertRaises(CallRuntimeError):
            self.backend.config()
        self.config['server']['p2p-calls-enabled'] = True
        self.service.client = SimpleNamespace(device_id='synthetic-device', state=ConnectionState.CONNECTED)
        self.assertFalse(self.backend.is_authorized())


class ConfigExtensionTests(unittest.TestCase):
    setUp = privacy_fixture.PrivacySessionsCoreTests.setUp
    tearDown = privacy_fixture.PrivacySessionsCoreTests.tearDown

    def test_partial_user_and_chat_settings_preserve_prior_config(self):
        self.service._remember_config({'user': {'PUSH_DETAILS': False},
                                      'chats': {'40': {'dontDisturbUntil': -1}},
                                      'server': {'calls-endpoint': 'https://calls.example.invalid/api'}})
        self.service._remember_config({'chats': {'41': {'dontDisturbUntil': 0}},
                                      'user': {'PUSH_SOUND': '_NONE_'}})
        config = self.service.store.load_config(123)
        self.assertFalse(config['user']['PUSH_DETAILS'])
        self.assertEqual(config['user']['SEARCH_BY_PHONE'], 'ALL')
        self.assertEqual(config['chats']['40'], {'dontDisturbUntil': -1})
        self.assertEqual(config['chats']['41'], {'dontDisturbUntil': 0})
        self.assertEqual(config['server']['calls-endpoint'], 'https://calls.example.invalid/api')
        self.assertFalse(self.service.client.calls)

    def test_invalid_known_config_does_not_partially_commit(self):
        before = self.service.store.load_config(123)
        for raw in ({'user': {'PUSH_DETAILS': False}, 'chats': {'40': {'dontDisturbUntil': True}}},
                    {'server': {'calls-endpoint': []}}, {'chats': {'not-an-id': {}}}):
            with self.assertRaises(ValueError):
                self.service._remember_config(raw)
        self.assertEqual(self.service.store.load_config(123), before)


if __name__ == '__main__':
    unittest.main()
