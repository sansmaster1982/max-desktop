"""Owner-wide pending writes are read back only for their actual target.

All contexts are real authenticated-context objects over an assertion-only
synthetic transport. No account requests, writes or media are performed.
"""
import copy
import unittest
from unittest.mock import Mock

import test_conversation_folder_contact_actions as actions_fixture
import test_conversation_info_service as info_fixture
from maxclient.core.conversation_info_service import ConversationUnavailable
from maxclient.core.models import Chat
from maxclient.protocol.client import AuthenticatedSession


class ConversationControlPendingScopeTests(unittest.TestCase):
    def setUp(self):
        fixture = actions_fixture.ConversationFolderContactActionsTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.service = fixture.service
        self.client = fixture.client
        self.state = fixture.state
        self.context_a = fixture.context
        self.service.store.upsert_chat(Chat(id=404, title='Other synthetic chat',
                                           chat_type='DIALOG', peer_user_id=222))
        self.service._server_chats[404] = info_fixture.chat_dto(
            404, participants={'101': 0, '222': 0})
        self.service._server_contacts[222] = {'id': 222, 'updateTime': info_fixture.STAMP}
        self.context_b = self.service.conversation_context(404)
        self.pending = {'owner': 101, 'id': 202, 'blocked': True}
        self.service._pending_contact_write = self.pending
        self.service.get_conversation_action_state = Mock(
            side_effect=lambda context: {'context': context, 'pending': False})
        self.service.refresh_conversation_action_state = Mock()

    def tearDown(self):
        self.client.request.assert_not_called()
        self.assertEqual(self.state.writes, [])

    def test_other_card_preserves_owner_wide_block_and_reports_actual_required_action(self):
        before = self.service.store._db.total_changes
        result = self.service.conversation_action_status(self.context_b)
        self.assertTrue(result['pending'])
        self.assertTrue(result['menu_pending']['block'])
        self.assertTrue(result['pending_elsewhere'])
        self.assertEqual(result['action_status'],
                         'Откройте карточку контакта, для которого ожидается подтверждение.')
        self.assertIs(self.service._pending_contact_write, self.pending)
        self.assertEqual(self.service.store._db.total_changes, before)
        self.assertEqual(self.state.reads, [])

    def test_refresh_other_card_does_not_request_wrong_contact_or_clear_original_pending(self):
        self.client.contact_info = Mock(side_effect=AssertionError('Wrong-peer read'))
        result = self.service.refresh_conversation_controls(self.context_b)
        self.client.contact_info.assert_not_called()
        self.assertTrue(result['pending_elsewhere'])
        self.assertTrue(result['pending'])
        self.assertIs(self.service._pending_contact_write, self.pending)

    def test_actual_target_bound_readback_confirms_and_unlocks_owner(self):
        self.state.contact['status'] = 'BLOCKED'
        result = self.service.refresh_conversation_controls(self.context_a)
        self.assertEqual(self.state.reads, [('contact', self.context_a.session_context)])
        self.assertFalse(result['pending'])
        self.assertFalse(result['pending_elsewhere'])
        self.assertIsNone(self.service._pending_contact_write)
        self.assertFalse(self.service.conversation_action_status(self.context_b)['pending'])

    def test_target_readback_that_does_not_confirm_keeps_pending_without_elsewhere_message(self):
        result = self.service.refresh_conversation_controls(self.context_a)
        self.assertEqual(self.state.reads, [('contact', self.context_a.session_context)])
        self.assertTrue(result['pending'])
        self.assertFalse(result['pending_elsewhere'])
        self.assertNotIn('action_status', result)
        self.assertIs(self.service._pending_contact_write, self.pending)

    def test_other_owner_record_does_not_block_or_request_current_contacts(self):
        self.service._pending_contact_write = {**self.pending, 'owner': 999}
        result = self.service.refresh_conversation_controls(self.context_b)
        self.assertFalse(result['pending'])
        self.assertFalse(result['pending_elsewhere'])
        self.assertFalse(result['menu_pending']['block'])
        self.assertEqual(self.state.reads, [])

    def test_owner_wide_folder_read_can_settle_on_other_card_without_contact_read(self):
        self.service._pending_folder_write = {
            'owner': 101, 'kind': 'upsert', 'expected': copy.deepcopy(self.state.folders[1])}
        self.client.contact_info = Mock(side_effect=AssertionError('Wrong-peer read'))
        result = self.service.refresh_conversation_controls(self.context_b)
        self.assertEqual(self.state.reads, [('folders', self.context_b.session_context)])
        self.assertFalse(result['menu_pending']['folders'])
        self.assertTrue(result['pending_elsewhere'])
        self.assertTrue(result['pending'])
        self.client.contact_info.assert_not_called()

    def test_contact_target_is_rechecked_after_other_action_readback(self):
        def replace_target(_context):
            self.service._pending_contact_write = {**self.pending, 'id': 222}
        self.service.refresh_conversation_action_state.side_effect = replace_target
        self.client.contact_info = Mock(side_effect=AssertionError('Changed-target read'))
        result = self.service.refresh_conversation_controls(self.context_a)
        self.assertTrue(result['pending_elsewhere'])
        self.client.contact_info.assert_not_called()
        self.assertEqual(self.service._pending_contact_write['id'], 222)

    def test_session_changed_before_refresh_rejects_without_any_readback(self):
        self.client._auth_session = AuthenticatedSession(
            101, self.client.device_id, 'WEB', self.client._sock)
        with self.assertRaises(ConversationUnavailable):
            self.service.refresh_conversation_controls(self.context_b)
        self.service.refresh_conversation_action_state.assert_not_called()
        self.assertEqual(self.state.reads, [])


if __name__ == '__main__':
    unittest.main()
