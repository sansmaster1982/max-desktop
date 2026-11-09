"""Explicit contact navigation after delete52, with in-memory synthetic state."""
import copy
import unittest

import test_contact_dialog_core as fixtures
from maxclient.protocol.client import MaxFrame

dialog = fixtures.dialog


class ContactDialogReopenTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ContactDialogCoreTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.s, self.client, self.store = self.fixture.service, self.fixture.client, self.fixture.store
        self.context, self.route = self.fixture.context, self.fixture.route
        old = dialog(lastEventTime=100, lastMessage=self.message(7, 100))
        self.s._remember_chat_info({'chats': [old]}, my_id=101)
        self.s._cache_history_messages(self.route, [self.message(7, 100)])
        self.store.clear_confirmed_history(101, self.route, 100, deleted=True)
        self.s._server_chats[self.route].pop('lastMessage', None)
        self.s._server_chats[self.route]['status'] = 'REMOVED'
        self.boundary = self.store.history_boundary(self.route)

    @staticmethod
    def message(identity, moment):
        return {'id': identity, 'time': moment, 'type': 'USER', 'sender': 202, 'text': 'Synthetic'}

    def assert_navigation_only(self):
        self.assertEqual(self.store.history_boundary(self.route), self.boundary)
        self.client._write_frame.assert_not_called()
        self.client.request.assert_not_called()
        self.client.contact_by_phone.assert_not_called()

    def test_matching_active_dto_reopens_without_unhiding_or_erasing_cutoff(self):
        self.client.chat_info.return_value = {'chats': [dialog(lastEventTime=100)]}
        result = self.s.open_contact_dialog(self.context)
        self.assertEqual((result['chat'].id, result['chat'].peer_user_id), (self.route, 202))
        self.assertFalse(result['local_draft'])
        self.assertEqual(self.s.list_chats(), [])
        self.assertEqual(self.store.messages(self.route), [])
        self.assert_navigation_only()

    def test_hidden_and_removed_dto_are_still_valid_explicit_navigation(self):
        for status in ('HIDDEN', 'REMOVED'):
            with self.subTest(status=status):
                self.client.chat_info.return_value = {'chats': [dialog(status=status, lastEventTime=100)]}
                result = self.s.open_contact_dialog(self.context)
                self.assertEqual(result['chat'].id, self.route)
                self.assertFalse(result['local_draft'])
                self.assertEqual(self.s.list_chats(), [])
                self.assertEqual(self.s._server_chats[self.route]['status'], status)
                self.assert_navigation_only()

    def test_old_last_message_readback_cannot_restore_deleted_history(self):
        self.client.chat_info.return_value = {'chats': [dialog(lastEventTime=100,
                                                             lastMessage=self.message(7, 100))]}
        result = self.s.open_contact_dialog(self.context)
        self.assertEqual(result['chat'].id, self.route)
        self.s._cache_history_messages(self.route, [self.message(7, 100)])
        self.assertEqual(self.store.messages(self.route), [])
        self.assertNotIn('lastMessage', self.s._server_chats[self.route])
        self.assertIsNone(self.store.get_chat(self.route).last_preview)
        self.assertEqual(self.s.list_chats(), [])
        self.assert_navigation_only()

    def test_empty48_replaces_only_old_metadata_with_session_bound_draft(self):
        self.client.chat_info.return_value = {'chats': []}
        result = self.s.open_contact_dialog(self.context)
        self.assertTrue(result['local_draft'])
        self.assertNotIn(self.route, self.s._server_chats)
        self.assertIs(self.s._contact_dialog_draft(self.route, self.s._history_read_context()), self.context)
        self.assertEqual(self.s.open_chat(self.route), [])
        self.client.chat_info.assert_called_once_with([self.route], expected_session=self.context.session_context)
        self.assertEqual(self.s.list_chats(), [])
        self.assert_navigation_only()

    def test_newer_push_during_empty48_wins_without_losing_its_message(self):
        fresh = dialog(lastEventTime=101, lastMessage=self.message(8, 101))
        def read(*args, **kwargs):
            self.s._remember_chat_info({'chats': [fresh]}, my_id=101)
            self.s._cache_history_messages(self.route, [self.message(8, 101)])
            return {'chats': []}
        self.client.chat_info.side_effect = read
        result = self.s.open_contact_dialog(self.context)
        self.assertFalse(result['local_draft'])
        self.assertEqual(self.s._server_chats[self.route]['lastMessage']['id'], 8)
        self.assertEqual([m.id for m in self.store.messages(self.route)], [8])
        self.assertEqual([c.id for c in self.s.list_chats()], [self.route])
        self.assert_navigation_only()

    def test_hidden_typed_push_during_empty48_is_not_discarded_as_old_metadata(self):
        fresh = dialog(lastEventTime=100, status='HIDDEN', title='Fresh metadata')
        def read(*args, **kwargs):
            self.s._remember_chat_info({'chats': [fresh]}, my_id=101)
            return {'chats': []}
        self.client.chat_info.side_effect = read
        result = self.s.open_contact_dialog(self.context)
        self.assertFalse(result['local_draft'])
        self.assertEqual(self.s._server_chats[self.route], fresh)
        self.assertEqual(self.s.list_chats(), [])
        self.assert_navigation_only()

    def test_foreign_participant_readback_cannot_override_deleted_route(self):
        original = copy.deepcopy(self.s._server_chats[self.route])
        self.client.chat_info.return_value = {'chats': [dialog(peer=404, lastEventTime=100)]}
        with self.assertRaises(ValueError):
            self.s.open_contact_dialog(self.context)
        self.assertEqual(self.s._server_chats[self.route], original)
        self.assert_navigation_only()

    def test_incomplete_concurrent_metadata_is_not_promoted_or_discarded(self):
        observed = {'id': self.route, 'type': 'DIALOG', 'status': 'HIDDEN'}
        def read(*args, **kwargs):
            self.s._server_chats[self.route] = copy.deepcopy(observed)
            return {'chats': []}
        self.client.chat_info.side_effect = read
        with self.assertRaises(ValueError):
            self.s.open_contact_dialog(self.context)
        self.assertEqual(self.s._server_chats[self.route], observed)
        self.assertEqual(self.s.list_chats(), [])
        self.assert_navigation_only()

    def test_empty48_proof_cannot_survive_reconnect(self):
        self.client.chat_info.return_value = {'chats': []}
        self.s.open_contact_dialog(self.context)
        self.fixture.fixture.switch(same_owner=True)
        with self.assertRaises(ValueError):
            self.s.open_chat(self.route)
        self.client.chat_info.assert_called_once()
        self.assert_navigation_only()

    def test_reconnect_during48_cannot_install_old_session_navigation(self):
        original = copy.deepcopy(self.s._server_chats[self.route])
        def read(*args, **kwargs):
            self.fixture.fixture.switch(same_owner=True)
            return {'chats': [dialog(lastEventTime=100)]}
        self.client.chat_info.side_effect = read
        with self.assertRaises(ValueError):
            self.s.open_contact_dialog(self.context)
        self.assertEqual(self.s._server_chats[self.route], original)
        self.assert_navigation_only()

    def test_explicit_send_after_reopened_draft_uses_exact_peer_once(self):
        self.client.chat_info.return_value = {'chats': []}
        self.s.open_contact_dialog(self.context)
        self.client._write_frame.side_effect = None
        self.client._write_frame.return_value = MaxFrame(1, 1, 64, b'', self.fixture._send_reply())
        result = self.s.send_text(self.route, 'Synthetic explicit send', context=self.context.session_context)
        self.assertEqual(result.status, 'sent')
        self.client._write_frame.assert_called_once()
        args, kwargs = self.client._write_frame.call_args
        self.assertEqual((args[0], args[1]['userId']), (64, 202))
        self.assertNotIn('chatId', args[1])
        self.assertIs(kwargs['expected_session'], self.context.session_context)
        self.assertEqual(self.store.history_boundary(self.route), self.boundary)
        self.assertIsNone(self.store.message(self.route, 7))

    def test_explicit_send_after_hidden_dto_reopen_uses_exact_peer_once(self):
        self.client.chat_info.return_value = {'chats': [dialog(status='HIDDEN', lastEventTime=100)]}
        self.assertFalse(self.s.open_contact_dialog(self.context)['local_draft'])
        self.client._write_frame.side_effect = None
        self.client._write_frame.return_value = MaxFrame(1, 1, 64, b'', self.fixture._send_reply())
        result = self.s.send_text(self.route, 'Synthetic explicit send', context=self.context.session_context)
        self.assertEqual(result.status, 'sent')
        self.client._write_frame.assert_called_once()
        args, kwargs = self.client._write_frame.call_args
        self.assertEqual((args[0], args[1]['userId']), (64, 202))
        self.assertIs(kwargs['expected_session'], self.context.session_context)
        self.assertEqual(self.store.history_boundary(self.route), self.boundary)
        self.assertIsNone(self.store.message(self.route, 7))

    def stale_contact(self, **changes):
        raw = dict(fixtures.record(), updateTime=19, **changes)
        self.client.contact_info.side_effect = None
        self.client.contact_info.return_value = {'contacts': [raw]}

    def test_older_valid32_allows_typed48_navigation_without_contact_downgrade_or_freshness(self):
        observation = object()
        self.s._contact_profile_observations = {(101, 202): observation}
        before = copy.deepcopy(self.s._server_contacts)
        local_before = copy.deepcopy(self.store.list_contacts())
        self.stale_contact(names=[{'type': 'ONEME', 'firstName': 'Older name'}])
        self.client.chat_info.return_value = {'chats': [dialog(lastEventTime=100)]}
        result = self.s.open_contact_dialog(self.context)
        self.assertEqual(result['chat'].peer_user_id, 202)
        self.assertFalse(result['local_draft'])
        self.assertEqual(result['chat'].title, 'Synthetic Peer')
        self.assertEqual(self.s._server_contacts, before)
        self.assertEqual(self.store.list_contacts(), local_before)
        self.assertIs(self.s._contact_profile_observations[(101, 202)], observation)
        self.assertFalse(self.s.contact_snapshot(self.context)['fresh'])
        self.assert_navigation_only()

    def test_older_valid32_empty48_draft_uses_newer_cached_contact_name(self):
        newer = dict(fixtures.record(), names=[{'type': 'ONEME', 'firstName': 'Newer', 'lastName': 'Profile'}])
        self.s._remember_contact_info({'contacts': [newer]})
        self.stale_contact(names=[{'type': 'ONEME', 'firstName': 'Older name'}])
        self.client.chat_info.return_value = {'chats': []}
        result = self.s.open_contact_dialog(self.context)
        self.assertTrue(result['local_draft'])
        self.assertEqual(result['chat'].title, 'Newer Profile')
        self.assertFalse(self.s.contact_snapshot(self.context)['fresh'])
        self.assertEqual(self.s._server_contacts[202], newer)
        self.assert_navigation_only()

    def test_older_removed32_cannot_remove_cached_contact_during_open(self):
        before = copy.deepcopy(self.store.list_contacts())
        self.stale_contact(status='REMOVED', flags=0)
        self.client.chat_info.return_value = {'chats': []}
        self.assertTrue(self.s.open_contact_dialog(self.context)['local_draft'])
        self.assertEqual(self.store.list_contacts(), before)
        self.assertNotIn('status', self.s._server_contacts[202])
        self.assert_navigation_only()

    def test_older32_during_open_cannot_confirm_pending_add_or_restore_hidden_contact(self):
        pending = {'owner': 101, 'id': 202, 'action': 'ADD',
                   'payload': {'contactId': 202, 'action': 'ADD'}, 'inflight': False}
        self.s._pending_contact_profile_write = pending
        self.store.set_contact_hidden(101, 202, True)
        self.stale_contact(flags=512)
        self.client.chat_info.return_value = {'chats': []}
        self.assertTrue(self.s.open_contact_dialog(self.context)['local_draft'])
        self.assertIs(self.s._pending_contact_profile_write, pending)
        self.assertIn(202, self.store.hidden_contact_ids(101))
        self.assertFalse(self.s.contact_snapshot(self.context)['fresh'])
        self.assert_navigation_only()

    def test_older32_during_open_cannot_confirm_pending_block_state(self):
        pending = {'owner': 101, 'id': 202, 'blocked': False}
        self.s._pending_contact_write = pending
        self.stale_contact()
        self.client.chat_info.return_value = {'chats': []}
        self.assertTrue(self.s.open_contact_dialog(self.context)['local_draft'])
        self.assertIs(self.s._pending_contact_write, pending)
        self.assert_navigation_only()

    def test_older32_remains_rejected_for_refresh_and_action_preflight(self):
        self.stale_contact(flags=512)
        for action in (lambda: self.s.refresh_contact_profile(self.context),
                       lambda: self.s.add_contact_profile(self.context)):
            with self.subTest(action=action), self.assertRaises(ValueError):
                action()
        self.client.chat_info.assert_not_called()
        self.assert_navigation_only()

    def test_older_but_malformed32_is_not_navigation_identity_proof(self):
        self.stale_contact(phone='Invalid phone')
        with self.assertRaises(ValueError):
            self.s.open_contact_dialog(self.context)
        self.client.chat_info.assert_not_called()
        self.assert_navigation_only()


if __name__ == '__main__':
    unittest.main()
