"""Notification settings bind queued work to its owner/session; synthetic only."""
import unittest
from unittest.mock import Mock, patch

import test_history_reconnect_review as fixture
from maxclient.core.notification_settings import NotificationSettings
from maxclient.protocol import client as protocol
from maxclient.protocol.client import MaxError, MaxFrame, MaxWriteNotStarted, MaxWriteOutcomeUnknown


class NotificationContextTests(unittest.TestCase):
    def setUp(self):
        self.case = fixture.HistoryReconnectReviewTests()
        self.case.setUp()
        self.addCleanup(self.case.tearDown)
        self.service, self.client, self.store = self.case.service, self.case.client, self.case.store
        self.context = self.service._history_read_context()
        self.store.merge_config(101, {'hash': 'synthetic-before', 'user': {'PUSH_DETAILS': True}})
        self.client.update_user_settings = Mock(return_value={
            'hash': 'synthetic-after', 'user': {'PUSH_DETAILS': False}})
        self.adapter = NotificationSettings(self.service)

    def reauthenticate(self):
        self.client._auth_session = self.case.context_for(self.client)
        return self.service._history_read_context()

    def test_bound_read_is_local_and_keeps_unknown_custom_sound(self):
        self.store.merge_config(101, {'user': {'PUSH_SOUND': 'custom-synthetic'}})
        result = self.adapter.read(context=self.context)
        self.assertTrue(result['available'])
        self.assertEqual(result['settings']['PUSH_SOUND'], 'custom-synthetic')
        self.assertFalse(result['pending'])
        self.client.update_user_settings.assert_not_called()

    def test_bound_write_passes_exact_context_and_merges_confirmed_ack(self):
        result = self.adapter.update({'PUSH_DETAILS': False}, context=self.context)
        self.client.update_user_settings.assert_called_once_with(
            {'PUSH_DETAILS': False}, expected_session=self.context[2])
        self.assertFalse(result['pending'])
        self.assertFalse(result['settings']['PUSH_DETAILS'])
        self.assertEqual(self.store.load_config(101)['hash'], 'synthetic-after')

    def test_same_owner_reauthentication_before_worker_rejects_old_read_and_write(self):
        self.reauthenticate()
        for call in (lambda: self.adapter.read(context=self.context),
                     lambda: self.adapter.update({'PUSH_DETAILS': False}, context=self.context)):
            with self.assertRaises(ValueError):
                call()
        self.client.update_user_settings.assert_not_called()
        self.assertEqual(self.adapter._pending, {})
        self.assertTrue(self.store.load_config(101)['user']['PUSH_DETAILS'])

    def test_different_owner_before_worker_cannot_read_or_write_new_account(self):
        self.case.change_account()
        self.store.merge_config(404, {'user': {'PUSH_DETAILS': True}})
        for call in (lambda: self.adapter.read(context=self.context),
                     lambda: self.adapter.update({'PUSH_DETAILS': False}, context=self.context)):
            with self.assertRaises(ValueError):
                call()
        self.client.update_user_settings.assert_not_called()
        self.assertTrue(self.store.load_config(404)['user']['PUSH_DETAILS'])

    def test_context_rechecked_after_local_read_before_send(self):
        original = self.adapter.read
        def read_then_switch(*, context):
            result = original(context=context)
            self.reauthenticate()
            return result
        with patch.object(self.adapter, 'read', side_effect=read_then_switch):
            with self.assertRaises(ValueError):
                self.adapter.update({'PUSH_DETAILS': False}, context=self.context)
        self.client.update_user_settings.assert_not_called()
        self.assertEqual(self.adapter._pending, {})

    def test_session_changed_inside_config_read_does_not_resolve_pending(self):
        self.client.update_user_settings.return_value = None
        self.adapter.update({'PUSH_DETAILS': False}, context=self.context)
        original = self.store.load_config
        def load_then_switch(owner):
            result = original(owner)
            result['user']['PUSH_DETAILS'] = False
            self.reauthenticate()
            return result
        with patch.object(self.store, 'load_config', side_effect=load_then_switch):
            with self.assertRaises(ValueError):
                self.adapter.read(context=self.context)
        self.assertEqual(self.adapter._pending, {'PUSH_DETAILS': False})

    def test_same_owner_late_ack_is_unknown_and_cannot_merge_or_retry(self):
        def reply(*args, **kwargs):
            self.reauthenticate()
            return {'hash': 'synthetic-stale', 'user': {'PUSH_DETAILS': False}}
        self.client.update_user_settings.side_effect = reply
        with self.assertRaises(MaxWriteOutcomeUnknown):
            self.adapter.update({'PUSH_DETAILS': False}, context=self.context)
        self.assertEqual(self.store.load_config(101)['hash'], 'synthetic-before')
        self.assertTrue(self.store.load_config(101)['user']['PUSH_DETAILS'])
        context = self.service._history_read_context()
        self.assertTrue(self.adapter.read(context=context)['pending'])
        with self.assertRaises(ValueError):
            self.adapter.update({'PUSH_DETAILS': False}, context=context)
        self.assertEqual(self.client.update_user_settings.call_count, 1)
        self.store.merge_config(101, {'user': {'PUSH_DETAILS': False}})
        self.assertFalse(self.adapter.read(context=context)['pending'])

    def test_different_owner_late_ack_is_unknown_without_new_account_merge(self):
        def reply(*args, **kwargs):
            self.case.change_account()
            self.store.merge_config(404, {'hash': 'synthetic-new-owner', 'user': {'PUSH_DETAILS': True}})
            return {'hash': 'synthetic-stale', 'user': {'PUSH_DETAILS': False}}
        self.client.update_user_settings.side_effect = reply
        with self.assertRaises(MaxWriteOutcomeUnknown):
            self.adapter.update({'PUSH_DETAILS': False}, context=self.context)
        self.assertEqual(self.store.load_config(404)['hash'], 'synthetic-new-owner')
        self.assertTrue(self.store.load_config(404)['user']['PUSH_DETAILS'])
        self.assertEqual(self.client.update_user_settings.call_count, 1)

    def test_optional_ack_survives_same_owner_reconnect_until_all_fields_read_back(self):
        self.client.update_user_settings.return_value = None
        desired = {'DONT_DISTURB_UNTIL': -1, 'CHATS_PUSH_NOTIFICATION': 'ON'}
        result = self.adapter.update(desired, context=self.context)
        self.assertTrue(result['pending'])
        current = self.reauthenticate()
        self.store.merge_config(101, {'user': {'DONT_DISTURB_UNTIL': -1}})
        self.assertTrue(self.adapter.read(context=current)['pending'])
        self.store.merge_config(101, {'user': {'CHATS_PUSH_NOTIFICATION': 'ON'}})
        self.assertFalse(self.adapter.read(context=current)['pending'])
        self.assertEqual(self.client.update_user_settings.call_count, 1)

    def test_explicit_nack_and_positive_not_started_clear_pending(self):
        for error in (MaxError('synthetic-denied'), MaxWriteNotStarted('synthetic-before-send')):
            with self.subTest(error=type(error).__name__):
                self.client.update_user_settings.side_effect = error
                with self.assertRaises(type(error)):
                    self.adapter.update({'PUSH_DETAILS': False}, context=self.context)
                self.assertFalse(self.adapter.read(context=self.context)['pending'])

    def test_unknown_transport_exception_keeps_pending_without_private_error(self):
        self.client.update_user_settings.side_effect = RuntimeError('synthetic-private-details')
        with self.assertRaises(MaxWriteOutcomeUnknown) as caught:
            self.adapter.update({'PUSH_DETAILS': False}, context=self.context)
        self.assertNotIn('synthetic-private-details', str(caught.exception))
        self.assertTrue(self.adapter.read(context=self.context)['pending'])
        with self.assertRaises(ValueError):
            self.adapter.update({'PUSH_DETAILS': False}, context=self.context)
        self.assertEqual(self.client.update_user_settings.call_count, 1)

    def test_legacy_call_without_context_retains_no_kwargs_signature(self):
        self.adapter.read()
        self.adapter.update({'PUSH_DETAILS': False})
        self.client.update_user_settings.assert_called_once_with({'PUSH_DETAILS': False})

    def test_bound_wire_payload_has_no_context_fields(self):
        del self.client.update_user_settings
        with patch.object(self.client, 'request', return_value=MaxFrame(1, 1, 22, b'', None)) as request:
            self.adapter.update({'PUSH_DETAILS': False}, context=self.context)
        request.assert_called_once_with(22, {'settings': {'user': {'PUSH_DETAILS': False}}},
                                        expected_session=self.context[2])

    def test_reauth_during_real_frame_build_rejected_before_bytes_and_releases_pending(self):
        del self.client.update_user_settings
        socket = self.client._sock
        original = protocol.build_frame
        def build_then_reauthenticate(*args, **kwargs):
            result = original(*args, **kwargs)
            self.reauthenticate()
            return result
        with patch.object(socket, 'sendall') as send, patch.object(protocol, 'build_frame', side_effect=build_then_reauthenticate):
            with self.assertRaises(MaxWriteNotStarted):
                self.adapter.update({'PUSH_DETAILS': False}, context=self.context)
            send.assert_not_called()
        self.assertEqual(self.client._pending, {})
        self.assertFalse(self.adapter.read(context=self.service._history_read_context())['pending'])


if __name__ == '__main__':
    unittest.main()
