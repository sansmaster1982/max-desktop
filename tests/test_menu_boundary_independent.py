"""Independent menu-mute and cleared-history integration, synthetic only."""
import copy
import unittest
from unittest.mock import Mock

import test_conversation_lifecycle as fixtures
from maxclient.core.models import Message
from maxclient.protocol.conversation_lifecycle_contracts import lifecycle_readback


class MenuBoundaryIndependentTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ConversationLifecycleTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.s = self.fixture.service
        self.ctx = self.fixture.context

    def test_conflicting_empty_chat_list_and_supplied_foreign_chat_not_absence(self):
        with self.assertRaises(ValueError):
            lifecycle_readback({'chats': [], 'chat': fixtures.chat(id=999)}, 303)

    def test_fresh_info_mute_available_even_when_cached_push_has_no_status(self):
        self.s._server_chats[303].pop('status')
        self.assertFalse(self.s.conversation_snapshot(self.ctx)['availability']['mute'])
        fresh = self.s.refresh_conversation(self.ctx)
        self.assertTrue(fresh['availability']['mute'])
        self.s.client.request.assert_not_called()
        self.s.client.change_conversation_lifecycle.assert_not_called()

    def test_detached_info_does_not_repair_row_menu_but_captured_metadata_read_does(self):
        self.s._server_chats[303].pop('status')
        self.s._server_chats[303]['participants'] = {101: 1, 202: 1}
        self.assertFalse(self.s.conversation_snapshot(self.ctx)['action_capabilities']['supported'])
        fresh_info = self.s.refresh_conversation(self.ctx)
        self.assertTrue(fresh_info['action_capabilities']['supported'])
        self.assertNotIn('status', self.s._server_chats[303])
        self.assertFalse(self.s.conversation_snapshot(self.ctx)['action_capabilities']['supported'])
        self.s.client.chat_info.reset_mock()
        self.s.client.contact_info.reset_mock()
        self.fixture.remote['participants'] = {101: 1, 202: 1}
        self.s.refresh_conversation_menu_metadata(self.ctx)
        recovered = self.s.conversation_snapshot(self.ctx)
        self.assertTrue(recovered['action_capabilities']['supported'])
        self.assertIn('share_contact', recovered['action_capabilities']['more'])
        self.s.client.chat_info.assert_called_once_with([303], expected_session=self.ctx.session_context)
        self.s.client.contact_info.assert_called_once_with([202], expected_session=self.ctx.session_context)
        self.s.client.request.assert_not_called()
        self.s.client.change_conversation_lifecycle.assert_not_called()

    def test_missing_contact_metadata_recovers_through_exact_read32_not_id_guess(self):
        self.s._server_contacts.pop(202)
        self.assertFalse(self.s.conversation_snapshot(self.ctx)['action_capabilities']['supported'])
        self.s.refresh_conversation_menu_metadata(self.ctx)
        self.assertTrue(self.s.conversation_snapshot(self.ctx)['action_capabilities']['supported'])
        self.s.client.contact_info.assert_called_once_with([202], expected_session=self.ctx.session_context)
        self.s.client.change_conversation_lifecycle.assert_not_called()

    def test_explicit_mute_refreshes_stale_capability_then_sends_once(self):
        self.s._server_chats[303].pop('status')
        self.s.client.update_chat_mute = Mock(return_value={'hash': 'synthetic', 'chats': {'303': {'dontDisturbUntil': -1}}})
        self.s.set_conversation_mute(self.ctx, -1)
        self.s.client.chat_info.assert_called_once_with([303], expected_session=self.ctx.session_context)
        self.s.client.update_chat_mute.assert_called_once_with(303, -1, expected_session=self.ctx.session_context)

    def test_late_ack_without_pending_does_not_restore_deleted_history_or_crash(self):
        self.s.clear_conversation_history(self.ctx)
        self.s.store.apply_message_ack(303, {'id': 9, 'time': 90, 'sender': 101, 'text': 'old ack'}, my_id=101)
        self.assertIsNone(self.s.store.message(303, 9))

    def test_late_ack_for_unsent_row_does_not_restore_message_below_boundary(self):
        self.s.store.insert_message(Message(chat_id=303, time_ms=80, text='pending',
            local_id='captured-pending', status='pending'))
        self.s.clear_conversation_history(self.ctx)
        self.s.store.apply_message_ack(303, {'id': 22, 'time': 80, 'sender': 101, 'text': 'old ack'},
            local_id='captured-pending', my_id=101, outgoing=True)
        self.assertIsNone(self.s.store.message(303, 22))

    def test_newer_ack_survives_clear_and_uses_actual_time(self):
        self.s.clear_conversation_history(self.ctx)
        message = self.s.store.apply_message_ack(303, {'id': 22, 'time': 120, 'sender': 101, 'text': 'new ack'}, my_id=101)
        self.assertEqual(message.text, 'new ack')
        self.assertEqual(self.s.store.message(303, 22).time_ms, 120)

    def test_late_server_chat_cannot_reintroduce_old_last_message_anchor(self):
        self.s.clear_conversation_history(self.ctx)
        self.s._remember_chat_info({'chats': [fixtures.chat()]}, my_id=101)
        self.assertNotIn('lastMessage', self.s._server_chats[303])
        self.assertIsNone(self.s.store.get_chat(303).last_time_ms)
        self.assertEqual(self.s.store.get_chat(303).unread, 0)

    def test_boundary_survives_same_owner_and_does_not_apply_to_other_owner(self):
        self.s.clear_conversation_history(self.ctx)
        self.s.store.bind_owner(101)
        self.assertTrue(self.s.store.history_was_cleared(303, 90))
        self.s.store.bind_owner(404)
        self.assertFalse(self.s.store.history_was_cleared(303, 90))


if __name__ == '__main__':
    unittest.main()
