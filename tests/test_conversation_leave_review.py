"""Independent adversarial leave tests; synthetic sessions and in-memory store only."""
import copy
import unittest
from unittest.mock import Mock

import test_conversation_lifecycle as base
from maxclient.core.models import Chat, Message
from maxclient.protocol.client import AuthenticatedSession, MaxError, MaxWriteOutcomeUnknown


class ConversationLeaveIndependentTests(unittest.TestCase):
    def setUp(self):
        self.fixture = base.ConversationLifecycleTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.service = self.fixture.service
        self.client = self.service.client
        self.fixture.remote = base.chat(type='CHAT', owner=202, joinTime=1,
            adminParticipants={}, participantsCount=2, title='Synthetic group')
        self.service._server_chats[303] = copy.deepcopy(self.fixture.remote)
        self.service.store.upsert_chat(Chat(id=303, title='Synthetic group', chat_type='CHAT'))
        self.context = self.service.conversation_context(303, activate_files=False)
        self.client.leave_conversation = Mock(side_effect=self.accept)

    def tearDown(self):
        self.client.request.assert_not_called()
        self.client.change_conversation_lifecycle.assert_not_called()

    def left(self, **fields):
        result = copy.deepcopy(self.fixture.remote)
        result.update(status='LEFT', joinTime=0, modified=110,
                      participants={'202': 1}, adminParticipants={})
        result.update(fields)
        return result

    def accept(self, *_args, **_kwargs):
        self.fixture.remote = self.left()
        return {}

    def unknown(self):
        self.client.leave_conversation.side_effect = MaxWriteOutcomeUnknown('Synthetic unknown')
        with self.assertRaises(MaxWriteOutcomeUnknown):
            self.service.leave_conversation(self.context)

    def assert_history_preserved(self):
        self.assertEqual(self.service.store.message(303, 9).text, 'old')
        self.assertIsNone(self.service.store.history_boundary(303))

    def test_confirmed_leave_never_deletes_cached_history_or_pending_draft(self):
        self.service.store.insert_message(Message(chat_id=303, time_ms=100,
            text='unsent synthetic draft', local_id='draft', status='pending'))
        self.service.store.insert_message(Message(chat_id=404, id=9, time_ms=90, text='other'))
        result = self.service.leave_conversation(self.context)
        self.assertTrue(result['confirmed'])
        self.assertFalse(result['pending'])
        self.assert_history_preserved()
        self.assertTrue(any(m.local_id == 'draft' for m in self.service.store.messages(303)))
        self.assertEqual(self.service.store.message(404, 9).text, 'other')
        self.client.leave_conversation.assert_called_once_with(
            303, expected_session=self.context.session_context)

    def test_confirmed_departure_hides_list_row_but_not_stored_chat(self):
        self.assertTrue(self.service.leave_conversation(self.context)['confirmed'])
        self.assertIsNotNone(self.service.store.get_chat(303))
        self.assertIsNotNone(self.service.store.conversation_departure(303))
        self.assertNotIn(303, [chat.id for chat in self.service.list_chats()])
        self.assert_history_preserved()

    def test_older_snapshot_cannot_restore_departed_row(self):
        old = copy.deepcopy(self.fixture.remote)
        self.assertTrue(self.service.leave_conversation(self.context)['confirmed'])
        self.service._remember_chat_info({'chats': [old]}, my_id=101)
        self.assertIsNotNone(self.service.store.conversation_departure(303))
        self.assertNotIn(303, [chat.id for chat in self.service.list_chats()])
        self.assert_history_preserved()

    def test_newer_valid_rejoin_restores_row_and_keeps_history(self):
        rejoin = copy.deepcopy(self.fixture.remote)
        self.assertTrue(self.service.leave_conversation(self.context)['confirmed'])
        rejoin.update(status='ACTIVE', joinTime=190, modified=200)
        self.service._remember_chat_info({'chats': [rejoin]}, my_id=101)
        self.assertIsNone(self.service.store.conversation_departure(303))
        self.assertIn(303, [chat.id for chat in self.service.list_chats()])
        self.assert_history_preserved()

    def test_departure_visibility_is_account_scoped(self):
        self.assertTrue(self.service.leave_conversation(self.context)['confirmed'])
        self.service.store.bind_owner(404)
        self.assertIsNone(self.service.store.conversation_departure(303))
        self.service.store.bind_owner(101)
        self.assertIsNotNone(self.service.store.conversation_departure(303))

    def test_unknown_blocks_second_leave_and_other_lifecycle_writes(self):
        self.unknown()
        with self.assertRaises(ValueError):
            self.service.leave_conversation(self.context)
        with self.assertRaises(ValueError):
            self.service.clear_conversation_history(self.context)
        self.assertTrue(self.service.refresh_conversation_lifecycle(self.context)['pending'])
        self.assert_history_preserved()
        self.client.leave_conversation.assert_called_once()

    def test_unknown_can_be_settled_by_readback_without_repeating_write(self):
        self.unknown()
        self.fixture.remote = self.left()
        result = self.service.refresh_conversation_lifecycle(self.context)
        self.assertTrue(result['confirmed'])
        self.assertFalse(result['pending'])
        self.assert_history_preserved()
        self.client.leave_conversation.assert_called_once()

    def test_empty_readback_does_not_prove_leave(self):
        self.unknown()
        self.fixture.remote = None
        result = self.service.refresh_conversation_lifecycle(self.context)
        self.assertTrue(result['pending'])
        self.assertFalse(result['confirmed'])
        self.assert_history_preserved()

    def test_stale_left_snapshot_cannot_settle_unknown(self):
        self.unknown()
        self.fixture.remote = self.left(created=1, modified=1, lastEventTime=1)
        result = self.service.refresh_conversation_lifecycle(self.context)
        self.assertTrue(result['pending'])
        self.assert_history_preserved()

    def test_left_status_with_positive_membership_is_not_confirmed(self):
        self.unknown()
        self.fixture.remote = self.left(joinTime=1, participants={'101': 1, '202': 1})
        result = self.service.refresh_conversation_lifecycle(self.context)
        self.assertTrue(result['pending'])
        self.assertFalse(result['confirmed'])
        self.assert_history_preserved()

    def test_explicit_left_with_absent_optional_join_time_can_settle(self):
        self.unknown()
        self.fixture.remote = self.left()
        self.fixture.remote.pop('joinTime')
        result = self.service.refresh_conversation_lifecycle(self.context)
        self.assertTrue(result['confirmed'])
        self.assertFalse(result['pending'])
        self.assert_history_preserved()

    def test_active_without_join_time_is_not_a_departure_confirmation(self):
        self.unknown()
        self.fixture.remote = self.left(status='ACTIVE')
        self.fixture.remote.pop('joinTime')
        result = self.service.refresh_conversation_lifecycle(self.context)
        self.assertTrue(result['pending'])
        self.assert_history_preserved()

    def test_null_join_time_is_not_schema_valid_nonmembership(self):
        self.unknown()
        self.fixture.remote = self.left(joinTime=None)
        try:
            result = self.service.refresh_conversation_lifecycle(self.context)
        except ValueError:
            result = {'pending': True}
        self.assertTrue(result['pending'])
        self.assert_history_preserved()

    def test_changed_group_to_channel_type_cannot_settle_same_chat_id(self):
        self.unknown()
        self.fixture.remote = self.left(type='CHANNEL')
        try:
            result = self.service.refresh_conversation_lifecycle(self.context)
        except ValueError:
            result = {'pending': True}
        self.assertTrue(result['pending'])
        self.assertIsNotNone(self.service.store.conversation_action_journal(101, 303))
        self.assert_history_preserved()

    def test_newer_rejoin_push_is_not_overwritten_by_old_left_readback(self):
        self.unknown()
        returned = self.left()
        current = copy.deepcopy(self.fixture.remote)
        current.update(modified=200, joinTime=190, status='ACTIVE')
        self.service._server_chats[303] = copy.deepcopy(current)
        self.fixture.remote = returned
        result = self.service.refresh_conversation_lifecycle(self.context)
        self.assertTrue(result['pending'])
        self.assertEqual(self.service._server_chats[303], current)
        self.assert_history_preserved()

    def test_unknown_journal_survives_owner_switch_and_is_owner_scoped(self):
        self.unknown()
        before = copy.deepcopy(self.service.store.conversation_action_journal(101, 303))
        self.service.store.bind_owner(404)
        self.assertIsNone(self.service.store.conversation_action_journal(404, 303))
        self.service.store.bind_owner(101)
        self.assertEqual(self.service.store.conversation_action_journal(101, 303), before)

    def test_readback_cannot_release_replacement_operation(self):
        self.unknown()
        replacement = {'action': 'clear', 'cutoff': 300, 'for_all': False,
                       'peer_id': None, 'created': 700, 'acknowledged': False,
                       'operation_id': 'newer-synthetic-operation'}
        old = self.left()
        def overlapping_refresh(*_args, **_kwargs):
            self.service.store.finish_conversation_action(101, 303)
            self.service.store.reserve_conversation_action(101, 303, replacement)
            return {'chats': [old]}
        self.client.chat_info.side_effect = overlapping_refresh
        try:
            result = self.service.refresh_conversation_lifecycle(self.context)
        except ValueError:
            result = {'pending': True}
        self.assertTrue(result['pending'])
        self.assertEqual(self.service.store.conversation_action_journal(101, 303), replacement)
        self.assert_history_preserved()

    def test_session_replaced_during_metadata_read_prevents_write(self):
        def reconnect(*_args, **_kwargs):
            self.client._auth_session = AuthenticatedSession(101, self.client.device_id,
                                                            'WEB', self.client._sock)
            return {'chats': [copy.deepcopy(self.fixture.remote)]}
        self.client.chat_info.side_effect = reconnect
        with self.assertRaises(ValueError):
            self.service.leave_conversation(self.context)
        self.client.leave_conversation.assert_not_called()
        self.assertIsNone(self.service.store.conversation_action_journal(101, 303))
        self.assert_history_preserved()

    def test_permission_revoked_after_reservation_prevents_write(self):
        reserve = self.service._reserve_lifecycle
        def revoke(context, record):
            reserve(context, record)
            self.service._server_chats[303]['restrictions'] = 8
        self.service._reserve_lifecycle = revoke
        with self.assertRaises(ValueError):
            self.service.leave_conversation(self.context)
        self.client.leave_conversation.assert_not_called()
        self.assertIsNone(self.service.store.conversation_action_journal(101, 303))
        self.assert_history_preserved()

    def test_session_replaced_after_ack_preserves_unknown_journal_without_departure(self):
        def replaced_session(*_args, **_kwargs):
            self.client._auth_session = AuthenticatedSession(101, self.client.device_id,
                                                            'WEB', self.client._sock)
            return {}
        self.client.leave_conversation.side_effect = replaced_session
        with self.assertRaises((ValueError, MaxWriteOutcomeUnknown)):
            self.service.leave_conversation(self.context)
        self.assertIsNotNone(self.service.store.conversation_action_journal(101, 303))
        self.assertIsNone(self.service.store.conversation_departure(303))
        self.assert_history_preserved()
        self.client.leave_conversation.assert_called_once()

    def test_invalid_void_ack_is_unknown_even_with_left_remote(self):
        def invalid_ack(*_args, **_kwargs):
            self.fixture.remote = self.left()
            return None
        self.client.leave_conversation.side_effect = invalid_ack
        with self.assertRaises(MaxWriteOutcomeUnknown):
            self.service.leave_conversation(self.context)
        self.assertIsNotNone(self.service.store.conversation_action_journal(101, 303))
        self.assertIsNone(self.service.store.conversation_departure(303))
        self.assert_history_preserved()
        self.client.leave_conversation.assert_called_once()

    def test_server_refusal_releases_reservation_without_local_departure(self):
        self.client.leave_conversation.side_effect = MaxError('Synthetic denied')
        with self.assertRaises(MaxError):
            self.service.leave_conversation(self.context)
        self.assertFalse(self.service.conversation_lifecycle_status(self.context)['pending'])
        self.assertEqual(self.service._server_chats[303]['status'], 'ACTIVE')
        self.assert_history_preserved()

    def test_owner_even_with_single_participant_cannot_use_nonowner_route(self):
        self.fixture.remote.update(owner=101, participantsCount=1, participants={'101': 1})
        with self.assertRaises(ValueError):
            self.service.leave_conversation(self.context)
        self.client.leave_conversation.assert_not_called()
        self.assertIsNone(self.service.store.conversation_action_journal(101, 303))

    def test_fresh_restriction_or_nonmembership_revokes_cached_leave(self):
        for changes in ({'restrictions': 8}, {'joinTime': 0},
                        {'participants': {'202': 1}},
                        {'videoConversation': {'type': 0}}, {'postId': 99}):
            with self.subTest(changes=changes):
                original = copy.deepcopy(self.fixture.remote)
                self.fixture.remote.update(changes)
                try:
                    with self.assertRaises(ValueError):
                        self.service.leave_conversation(self.context)
                    self.client.leave_conversation.assert_not_called()
                    self.assertIsNone(self.service.store.conversation_action_journal(101, 303))
                finally:
                    self.fixture.remote = original


if __name__ == '__main__':
    unittest.main()
