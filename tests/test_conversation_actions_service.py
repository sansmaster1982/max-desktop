"""Owner/session races and pending Sound actions with synthetic memory storage."""
import copy
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from maxclient.core.conversation_actions_service import ConversationActionsServiceMixin
from maxclient.core.conversation_info_service import ConversationInfoServiceMixin, ConversationUnavailable
from maxclient.core.models import Chat
from maxclient.core.service import MaxService
from maxclient.core.store import Store
from maxclient.protocol.client import (
    AuthenticatedSession, ConnectionState, MaxClient, MaxError, MaxWriteOutcomeUnknown,
)


DEVICE = '00112233-4455-6677-8899-aabbccddeeff'
NOW = 1_790_000_000_000


class _Service(ConversationActionsServiceMixin, ConversationInfoServiceMixin):
    _history_read_context = MaxService._history_read_context
    _require_history_context = MaxService._require_history_context
    _confirmed_profile_id = MaxService._confirmed_profile_id
    _remember_config = MaxService._remember_config

    @property
    def my_id(self):
        return self.session.my_user_id


class ConversationActionsServiceTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(':memory:')
        self.addCleanup(self.store.close)
        self.store.bind_owner(101)
        self.store.upsert_chat(Chat(id=303, chat_type='DIALOG', peer_user_id=202))
        self.service = _Service()
        self.service.store = self.store
        self.service.session = SimpleNamespace(my_user_id=101, device_id=DEVICE)
        self.service._metadata_lock = threading.RLock()
        self.service._server_metadata_owner = 101
        self.service._server_chats = {303: {'id': 303, 'type': 'DIALOG', 'status': 'ACTIVE',
            'participants': {'101': 0, '202': 0}, 'restrictions': 0}}
        self.service._server_contacts = {202: {'id': 202}}
        self.client = MaxClient(device_id=DEVICE, auto_reconnect=False)
        self.client._sock = object()
        self.client._device_type = 'WEB'
        self.client._state = ConnectionState.CONNECTED
        self.client._auth_session = AuthenticatedSession(101, DEVICE, 'WEB', self.client._sock)
        self.client.update_chat_mute = Mock(return_value=None)
        self.client.request = Mock(side_effect=AssertionError('No transport allowed'))
        self.service.client = self.client
        self.service.on_config_changed = Mock()
        self.service.log = Mock()
        self.context = self.service.conversation_context(303)
        self.capabilities = self.enterContext(patch('maxclient.core.conversation_actions_service._capabilities',
                                                   return_value={'mute': True}))
        self.enterContext(patch('maxclient.core.conversation_actions_service._now_ms', return_value=NOW))

    def tearDown(self):
        self.client.request.assert_not_called()

    def read(self):
        return self.service.get_conversation_action_state(self.context)

    def supplied(self, value, chat=303):
        self.service._remember_config({'chats': {str(chat): {'dontDisturbUntil': value}}}, owner_id=101)

    def test_read_unknown_state_has_no_default_or_request_and_expiry_is_actual(self):
        state = self.read()
        self.assertTrue(state['available'])
        self.assertIsNone(state['muted'])
        self.assertIsNone(state['dont_disturb_until'])
        self.assertFalse(state['pending'])
        self.client.update_chat_mute.assert_not_called()
        for value, muted in ((-1, True), (0, False), (NOW, False), (NOW + .25, True)):
            self.supplied(value)
            self.assertIs(self.read()['muted'], muted)

    def test_actual_noop_is_zero_rpc_and_durations_become_absolute(self):
        self.supplied(0)
        state = self.service.set_conversation_mute(self.context, 0)
        self.assertEqual((state['accepted'], state['unchanged'], state['pending']), (False, True, False))
        self.client.update_chat_mute.assert_not_called()
        state = self.service.set_conversation_mute(self.context, 3_600_000)
        self.client.update_chat_mute.assert_called_once_with(303, NOW + 3_600_000,
                                                           expected_session=self.context.session_context)
        self.assertTrue(state['pending'])
        self.assertEqual(state['dont_disturb_until'], 0)
        self.assertFalse(state['muted'])

    def test_empty_ack_remains_pending_and_blocks_all_repeat_until_matching_config(self):
        self.supplied(0)
        self.assertTrue(self.service.set_conversation_mute(self.context, -1)['pending'])
        for duration in (-1, 0, 14_400_000):
            with self.assertRaises(ValueError): self.service.set_conversation_mute(self.context, duration)
        self.supplied(-1, chat=404)
        self.assertTrue(self.read()['pending'])
        self.supplied(NOW + 3_600_000)
        self.assertTrue(self.read()['pending'])
        self.supplied(-1)
        self.assertFalse(self.read()['pending'])
        self.assertTrue(self.read()['muted'])
        self.client.update_chat_mute.assert_called_once()

    def test_actual_ack_clears_pending_and_preserves_unrelated_config(self):
        self.service._remember_config({'user': {'HIDDEN': True}, 'chats': {'404': {'dontDisturbUntil': 0}},
                                      'server': {'p2p-calls-enabled': True}}, owner_id=101)
        self.client.update_chat_mute.return_value = {'hash': 'synthetic', 'chats': {'303': {'dontDisturbUntil': -1}}}
        state = self.service.set_conversation_mute(self.context, -1)
        self.assertFalse(state['pending'])
        self.assertTrue(state['muted'])
        actual = self.store.load_config(101)
        self.assertEqual(actual['user'], {'HIDDEN': True})
        self.assertEqual(actual['server'], {'p2p-calls-enabled': True})
        self.assertEqual(actual['chats']['404'], {'dontDisturbUntil': 0})

    def test_partial_or_other_chat_ack_is_not_confirmation(self):
        self.client.update_chat_mute.return_value = {'hash': 'synthetic', 'chats': {'404': {'dontDisturbUntil': -1}}}
        state = self.service.set_conversation_mute(self.context, -1)
        self.assertTrue(state['pending'])
        self.assertIsNone(state['muted'])

    def test_unknown_write_stays_pending_after_reauthentication_no_retry(self):
        self.client.update_chat_mute.side_effect = MaxWriteOutcomeUnknown('synthetic')
        with self.assertRaises(MaxWriteOutcomeUnknown): self.service.set_conversation_mute(self.context, -1)
        self.assertTrue(self.read()['pending'])
        self.client._auth_session = AuthenticatedSession(101, DEVICE, 'WEB', self.client._sock)
        self.context = self.service.conversation_context(303)
        with self.assertRaises(ValueError): self.service.set_conversation_mute(self.context, -1)
        self.supplied(-1)
        self.assertFalse(self.read()['pending'])
        self.client.update_chat_mute.assert_called_once()

    def test_explicit_rejection_unlocks_without_config_mutation(self):
        self.client.update_chat_mute.side_effect = MaxError('synthetic rejected')
        before = self.store.load_config(101)
        with self.assertRaises(MaxError): self.service.set_conversation_mute(self.context, -1)
        self.assertFalse(self.read()['pending'])
        self.assertEqual(self.store.load_config(101), before)

    def test_malformed_ack_is_unknown_and_never_persisted(self):
        self.client.update_chat_mute.return_value = {'chats': {'303': {'dontDisturbUntil': -1}}}
        before = self.store.load_config(101)
        with self.assertRaises(MaxWriteOutcomeUnknown): self.service.set_conversation_mute(self.context, -1)
        self.assertEqual(self.store.load_config(101), before)
        self.assertTrue(self.read()['pending'])

    def test_owner_client_socket_device_and_context_change_reject_before_rpc(self):
        changes = [lambda: setattr(self.service.session, 'my_user_id', 999),
                   lambda: setattr(self.service, 'client', object()),
                   lambda: setattr(self.client, '_sock', object()),
                   lambda: setattr(self.client, 'device_id', 'changed'),
                   lambda: setattr(self.client, '_auth_session', AuthenticatedSession(101, DEVICE, 'WEB', self.client._sock)),
                   lambda: self.store.bind_owner(999)]
        for change in changes:
            with self.subTest(change=changes.index(change)):
                old_client, old_socket, old_device = self.service.client, self.client._sock, self.client.device_id
                old_context = self.client._auth_session
                change()
                with self.assertRaises(ValueError): self.service.set_conversation_mute(self.context, -1)
                self.client.update_chat_mute.assert_not_called()
                self.service.client, self.client._sock, self.client.device_id = old_client, old_socket, old_device
                self.client._auth_session = old_context
                self.service.session.my_user_id = 101
                self.store.bind_owner(101)
                self.store.upsert_chat(Chat(id=303, chat_type='DIALOG', peer_user_id=202))

    def test_late_owner_change_does_not_write_config_into_other_owner(self):
        def replace(*args, **kwargs):
            self.service.session.my_user_id = 999
            self.store.bind_owner(999)
            return {'hash': 'synthetic', 'chats': {'303': {'dontDisturbUntil': -1}}}
        self.client.update_chat_mute.side_effect = replace
        with self.assertRaises(MaxWriteOutcomeUnknown): self.service.set_conversation_mute(self.context, -1)
        self.assertEqual(self.store.load_config(999), {})
        self.service.on_config_changed.assert_not_called()

    def test_late_same_owner_session_change_does_not_merge_ack(self):
        def replace(*args, **kwargs):
            self.client._auth_session = AuthenticatedSession(101, DEVICE, 'WEB', self.client._sock)
            return {'hash': 'synthetic', 'chats': {'303': {'dontDisturbUntil': -1}}}
        self.client.update_chat_mute.side_effect = replace
        with self.assertRaises(MaxWriteOutcomeUnknown): self.service.set_conversation_mute(self.context, -1)
        self.assertEqual(self.store.load_config(101), {})
        self.context = self.service.conversation_context(303)
        self.assertTrue(self.read()['pending'])

    def test_inflight_matching_push_cannot_enable_second_write(self):
        def during(*args, **kwargs):
            self.supplied(-1)
            self.assertTrue(self.read()['pending'])
            with self.assertRaises(ValueError): self.service.set_conversation_mute(self.context, 0)
            return None
        self.client.update_chat_mute.side_effect = during
        self.assertFalse(self.service.set_conversation_mute(self.context, -1)['pending'])
        self.client.update_chat_mute.assert_called_once()

    def test_capability_recomputed_and_invalid_input_never_requests(self):
        self.capabilities.return_value = {'mute': False}
        self.assertFalse(self.read()['available'])
        with self.assertRaises(ValueError): self.service.set_conversation_mute(self.context, -1)
        self.capabilities.return_value = {'mute': True}
        for value in (True, '0', 60_000):
            with self.assertRaises(ValueError): self.service.set_conversation_mute(self.context, value)
        self.client.update_chat_mute.assert_not_called()
        self.assertFalse(self.read()['pending'])

    def use_actual_capabilities(self):
        from maxclient.core.conversation_action_capabilities import conversation_action_capabilities
        self.capabilities.side_effect = lambda raw, contact, owner, server, peer: conversation_action_capabilities(
            raw, contact, owner, server, peer_id=peer)

    def test_actual_capabilities_allow_normal_and_deny_blocked_or_suspended_bot(self):
        self.use_actual_capabilities()
        self.assertTrue(self.read()['available'])
        self.service._server_contacts[202]['status'] = 'BLOCKED'
        self.assertFalse(self.read()['available'])
        with self.assertRaises(ValueError): self.service.set_conversation_mute(self.context, -1)
        self.service._server_contacts[202].pop('status')
        self.service._server_chats[303].update(hasBots=True, isSuspended=True)
        self.assertFalse(self.read()['available'])
        with self.assertRaises(ValueError): self.service.set_conversation_mute(self.context, -1)
        self.client.update_chat_mute.assert_not_called()

    def test_capability_changed_between_reservation_and_send_is_zero_rpc(self):
        self.capabilities.side_effect = [{'mute': True}, {'mute': False}]
        with self.assertRaises(ValueError): self.service.set_conversation_mute(self.context, -1)
        self.client.update_chat_mute.assert_not_called()
        self.capabilities.side_effect = None
        self.assertFalse(self.read()['pending'])

    def test_concurrent_second_gesture_cannot_replay_inflight_write(self):
        entered, release = threading.Event(), threading.Event()
        results, errors = [], []
        def blocked(*args, **kwargs):
            entered.set()
            if not release.wait(2): raise AssertionError('Synthetic worker did not finish')
            return None
        def first():
            try: results.append(self.service.set_conversation_mute(self.context, -1))
            except Exception as error: errors.append(error)
        self.client.update_chat_mute.side_effect = blocked
        worker = threading.Thread(target=first)
        worker.start()
        try:
            self.assertTrue(entered.wait(2))
            with self.assertRaises(ValueError): self.service.set_conversation_mute(self.context, 0)
            self.assertTrue(self.read()['pending'])
        finally:
            release.set()
            worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertTrue(results[0]['pending'])
        self.client.update_chat_mute.assert_called_once()


if __name__ == '__main__':
    unittest.main()
