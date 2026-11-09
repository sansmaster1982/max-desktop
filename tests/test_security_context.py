"""Security menu requests stay on the selected session; only synthetic data."""
import unittest
from unittest.mock import Mock, patch

import test_history_reconnect_review as fixture
from maxclient.protocol import client as protocol
from maxclient.protocol.client import MaxClient, MaxFrame, MaxWriteNotStarted, MaxWriteOutcomeUnknown


class SecurityContextTests(unittest.TestCase):
    def setUp(self):
        self.case = fixture.HistoryReconnectReviewTests()
        self.case.setUp()
        self.addCleanup(self.case.tearDown)
        self.service, self.client, self.store = self.case.service, self.case.client, self.case.store
        self.context = self.service._history_read_context()
        self.store.merge_config(101, {'hash': 'synthetic-before', 'user': {'HIDDEN': False}})
        self.client.update_user_settings = Mock(return_value={'hash': 'synthetic-after', 'user': {'HIDDEN': True}})
        self.client.auth_2fa_details = Mock(return_value=MaxFrame(1, 1, 104, b'', {
            'password': {'enabled': True, 'hint': 'Synthetic hint'}}))

    def change_session(self):
        self.client._auth_session = self.case.context_for(self.client)

    def test_confirmed_read_is_local_and_does_not_invent_missing_fields(self):
        self.assertEqual(self.service.get_privacy_settings(context=self.context), {'HIDDEN': False})
        self.client.update_user_settings.assert_not_called()
        self.client.auth_2fa_details.assert_not_called()

    def test_context_is_passed_unchanged_to_both_ordinary_protocol_operations(self):
        result = self.service.update_privacy_settings({'HIDDEN': True}, context=self.context)
        self.assertEqual(result, {'accepted': True, 'pending': False, 'settings': {'HIDDEN': True}})
        self.client.update_user_settings.assert_called_once_with({'HIDDEN': True}, expected_session=self.context[2])
        status = self.service.get_2fa_status(context=self.context)
        self.assertTrue(status['enabled'])
        self.client.auth_2fa_details.assert_called_once_with(expected_session=self.context[2])

    def test_same_owner_reauthenticated_before_queued_work_rejects_all_old_work(self):
        self.change_session()
        for call in (lambda: self.service.get_privacy_settings(context=self.context),
                     lambda: self.service.update_privacy_settings({'HIDDEN': True}, context=self.context),
                     lambda: self.service.get_2fa_status(context=self.context)):
            with self.assertRaises(ValueError):
                call()
        self.client.update_user_settings.assert_not_called()
        self.client.auth_2fa_details.assert_not_called()
        self.assertFalse(self.store.load_config(101)['user']['HIDDEN'])

    def test_owner_switch_before_worker_cannot_read_or_change_the_new_owner(self):
        self.case.change_account()
        self.store.merge_config(404, {'user': {'HIDDEN': False}})
        for call in (lambda: self.service.get_privacy_settings(context=self.context),
                     lambda: self.service.update_privacy_settings({'HIDDEN': True}, context=self.context),
                     lambda: self.service.get_2fa_status(context=self.context)):
            with self.assertRaises(ValueError):
                call()
        self.client.update_user_settings.assert_not_called()
        self.client.auth_2fa_details.assert_not_called()
        self.assertFalse(self.store.load_config(404)['user']['HIDDEN'])

    def test_context_is_rechecked_between_local_settings_read_and_rpc(self):
        actual = self.service.get_privacy_settings
        def read_then_switch(*, context):
            result = actual(context=context)
            self.change_session()
            return result
        with patch.object(self.service, 'get_privacy_settings', side_effect=read_then_switch):
            with self.assertRaises(ValueError):
                self.service.update_privacy_settings({'HIDDEN': True}, context=self.context)
        self.client.update_user_settings.assert_not_called()

    def test_same_owner_late_write_ack_is_unknown_without_config_merge_or_retry(self):
        def reply(*args, **kwargs):
            self.change_session()
            return {'hash': 'synthetic-late', 'user': {'HIDDEN': True}}
        self.client.update_user_settings.side_effect = reply
        with self.assertRaises(MaxWriteOutcomeUnknown):
            self.service.update_privacy_settings({'HIDDEN': True}, context=self.context)
        self.assertEqual(self.client.update_user_settings.call_count, 1)
        self.assertEqual(self.store.load_config(101)['hash'], 'synthetic-before')
        self.assertFalse(self.store.load_config(101)['user']['HIDDEN'])

    def test_late_2fa_reply_does_not_become_current_account_status(self):
        def reply(**kwargs):
            self.change_session()
            return MaxFrame(1, 1, 104, b'', {'password': {'enabled': False}})
        self.client.auth_2fa_details.side_effect = reply
        with self.assertRaises(ValueError):
            self.service.get_2fa_status(context=self.context)
        self.assertEqual(self.client.auth_2fa_details.call_count, 1)

    def test_pending_ack_keeps_confirmed_value_in_bound_cache(self):
        self.client.update_user_settings.return_value = None
        result = self.service.update_privacy_settings({'HIDDEN': True}, context=self.context)
        self.assertTrue(result['pending'])
        self.assertFalse(result['settings']['HIDDEN'])
        self.assertFalse(self.store.load_config(101)['user']['HIDDEN'])

    def test_safe_mode_guard_still_runs_before_bound_rpc(self):
        self.store.merge_config(101, {'user': {'SAFE_MODE': True}})
        for key, value in (('CONTENT_LEVEL_ACCESS', True), ('SEARCH_BY_PHONE', 'CONTACTS'),
                           ('INCOMING_CALL', 'CONTACTS'), ('CHATS_INVITE', 'CONTACTS')):
            with self.assertRaises(ValueError):
                self.service.update_privacy_settings({key: value}, context=self.context)
        self.client.update_user_settings.assert_not_called()

    def test_legacy_callers_keep_original_method_signatures_and_payload(self):
        self.service.get_privacy_settings()
        self.service.update_privacy_settings({'HIDDEN': True})
        self.service.get_2fa_status()
        self.client.update_user_settings.assert_called_once_with({'HIDDEN': True})
        self.client.auth_2fa_details.assert_called_once_with()


class SecurityProtocolContextTests(unittest.TestCase):
    def setUp(self):
        self.client = MaxClient(device_id=fixture.DEVICE, auto_reconnect=False)
        self.socket = Mock()
        self.client._sock = self.socket
        self.context = protocol.AuthenticatedSession(101, fixture.DEVICE, 'WEB', self.socket)
        self.client._auth_session = self.context

    def invoke(self, kind):
        if kind == 'privacy':
            return self.client.update_user_settings({'HIDDEN': False}, expected_session=self.context)
        return self.client.auth_2fa_details(expected_session=self.context)

    def test_expected_session_is_non_wire_argument_and_payload_is_unchanged(self):
        for kind, opcode, payload in (('privacy', 22, {'settings': {'user': {'HIDDEN': False}}}),
                                      ('2fa', 104, {})):
            with self.subTest(kind=kind), patch.object(self.client, 'request', return_value=MaxFrame(
                    1, 1, opcode, b'', None)) as request:
                self.invoke(kind)
                request.assert_called_once_with(opcode, payload, expected_session=self.context)
        self.socket.sendall.assert_not_called()

    def test_stale_session_before_request_sends_no_bytes(self):
        self.client._auth_session = protocol.AuthenticatedSession(101, fixture.DEVICE, 'WEB', self.socket)
        for kind in ('privacy', '2fa'):
            with self.subTest(kind=kind), self.assertRaises(MaxWriteNotStarted):
                self.invoke(kind)
        self.socket.sendall.assert_not_called()
        self.assertEqual(self.client._pending, {})

    def test_session_switch_during_frame_build_is_rejected_at_last_send_gate(self):
        original = protocol.build_frame
        def build_then_switch(*args, **kwargs):
            frame = original(*args, **kwargs)
            self.client._auth_session = protocol.AuthenticatedSession(101, fixture.DEVICE, 'WEB', self.socket)
            return frame
        for kind in ('privacy', '2fa'):
            self.client._auth_session = self.context
            with self.subTest(kind=kind), patch.object(protocol, 'build_frame', side_effect=build_then_switch):
                with self.assertRaises(MaxWriteNotStarted):
                    self.invoke(kind)
            self.assertEqual(self.client._pending, {})
        self.socket.sendall.assert_not_called()


if __name__ == '__main__':
    unittest.main()
