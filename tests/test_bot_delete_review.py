"""Independent destructive-flow regressions; synthetic state and mocked RPC only."""
import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from maxclient.core.models import Chat
from maxclient.core.store import Store
from maxclient.protocol.client import AuthenticatedSession, MaxError, MaxWriteOutcomeUnknown
import test_conversation_lifecycle as lifecycle_fixture


class BotDeleteReviewTests(unittest.TestCase):
    def new_case(self):
        case = lifecycle_fixture.ConversationLifecycleTests()
        case.setUp()
        self.addCleanup(case.doCleanups)
        self.addCleanup(case.service.client.request.assert_not_called)
        case.remote.update(hasBots=True, isSuspended=False)
        case.service._server_chats[303] = copy.deepcopy(case.remote)

        def stop(*_args, **_kwargs):
            case.remote.update(isSuspended=True, modified=110)

        case.service.client.stop_bot = Mock(side_effect=stop)
        return case

    def setUp(self):
        self.case = self.new_case()
        self.service = self.case.service
        self.context = self.case.context

    def alias(self):
        s = self.service
        s.store.upsert_chat(Chat(id=304, title='Alias', chat_type='DIALOG', peer_user_id=202))
        s._server_chats[304] = {**copy.deepcopy(self.case.remote), 'id': 304}
        return s.conversation_context(304, activate_files=False)

    def test_alias_cannot_start_second_composite_inside_first_stop(self):
        alias = self.alias()
        original = self.service.client.stop_bot.side_effect

        def stop(*args, **kwargs):
            with self.assertRaises(ValueError):
                self.service.delete_conversation(alias)
            original(*args, **kwargs)

        self.service.client.stop_bot.side_effect = stop
        self.assertTrue(self.service.delete_conversation(self.context)['confirmed'])
        self.service.client.stop_bot.assert_called_once()
        self.service.client.change_conversation_lifecycle.assert_called_once()

    def test_unknown_standalone_stop_blocks_composite_alias(self):
        self.service.client.stop_bot.side_effect = MaxWriteOutcomeUnknown('synthetic')
        with self.assertRaises(MaxWriteOutcomeUnknown):
            self.service.stop_conversation_bot(self.context)
        alias = self.alias()
        with self.assertRaises(ValueError):
            self.service.delete_conversation(alias)
        self.service.client.stop_bot.assert_called_once()
        self.service.client.change_conversation_lifecycle.assert_not_called()

    def test_unknown_composite_blocks_standalone_alias(self):
        self.service.client.stop_bot.side_effect = MaxWriteOutcomeUnknown('synthetic')
        with self.assertRaises(MaxWriteOutcomeUnknown):
            self.service.delete_conversation(self.context)
        alias = self.alias()
        with self.assertRaises(ValueError):
            self.service.stop_conversation_bot(alias)
        self.service.client.stop_bot.assert_called_once()
        self.service.client.change_conversation_lifecycle.assert_not_called()

    def test_model_derived_zero_event_cannot_release_unknown_standalone_stop(self):
        self.service.client.stop_bot.side_effect = MaxWriteOutcomeUnknown('synthetic')
        with self.assertRaises(MaxWriteOutcomeUnknown):
            self.service.stop_conversation_bot(self.context)
        self.case.remote.update(lastEventTime=0, modified=110)
        self.case.remote.pop('isSuspended', None)
        self.service._remember_chat_info({'chats': [copy.deepcopy(self.case.remote)]}, my_id=101)
        state = self.service.get_conversation_action_state(self.context)
        self.assertTrue(state['pending_actions']['stop_bot'])
        with self.assertRaises(ValueError):
            self.service.delete_conversation(self.context)
        self.service.client.stop_bot.assert_called_once()

    def test_same_owner_session_replacement_after119_never_sends52(self):
        client = self.service.client

        def stop(*_args, **_kwargs):
            self.case.remote.update(isSuspended=True, modified=110)
            client._auth_session = AuthenticatedSession(101, client.device_id, 'WEB', client._sock)

        client.stop_bot.side_effect = stop
        with self.assertRaises((ValueError, MaxWriteOutcomeUnknown)):
            self.service.delete_conversation(self.context)
        client.change_conversation_lifecycle.assert_not_called()
        current = self.service.conversation_context(303, activate_files=False)
        result = self.service.refresh_conversation_lifecycle(current)
        self.assertTrue(result['stop_confirmed'])
        self.assertFalse(result['delete_confirmed'])
        client.stop_bot.assert_called_once()
        client.change_conversation_lifecycle.assert_not_called()

    def test_newer_restricted_push_during_final_contact_read_prevents52(self):
        original = self.service.client.contact_info.side_effect
        reads = 0

        def contact_info(*args, **kwargs):
            nonlocal reads
            reads += 1
            if reads == 2:
                newer = {**copy.deepcopy(self.case.remote), 'modified': 200, 'restrictions': 128}
                self.service._remember_chat_info({'chats': [newer]}, my_id=101)
            return original(*args, **kwargs)

        self.service.client.contact_info.side_effect = contact_info
        result = self.service.delete_conversation(self.context)
        self.assertTrue(result['stop_confirmed'])
        self.assertFalse(result['confirmed'])
        self.assertFalse(result['can_continue_delete'])
        self.assertEqual(self.service._server_chats[303]['modified'], 200)
        self.assertEqual(self.service._server_chats[303]['restrictions'], 128)
        self.service.client.change_conversation_lifecycle.assert_not_called()

    def test_missing_and_nonbool_suspended_never_confirm_stop(self):
        missing = object()
        for value in (missing, None, 0, 1, 'true', False):
            with self.subTest(value='missing' if value is missing else value):
                case = self.new_case()

                def stop(*_args, **_kwargs):
                    case.remote['modified'] = 110
                    if value is missing:
                        case.remote.pop('isSuspended', None)
                    else:
                        case.remote['isSuspended'] = value

                case.service.client.stop_bot.side_effect = stop
                state = case.service.delete_conversation(case.context)
                self.assertFalse(state['stop_confirmed'])
                self.assertEqual(state['stage'], 'stop_pending')
                case.service.client.change_conversation_lifecycle.assert_not_called()

    def test_zero_event_model_default_is_not_stop_readback(self):
        def stop(*_args, **_kwargs):
            self.case.remote.update(lastEventTime=0, modified=110)
            self.case.remote.pop('isSuspended', None)

        self.service.client.stop_bot.side_effect = stop
        result = self.service.delete_conversation(self.context)
        self.assertFalse(result['stop_confirmed'])
        self.assertTrue(result['pending'])
        self.service.client.change_conversation_lifecycle.assert_not_called()

    def test_scope_expansion_rejected_before_any_write(self):
        self.service.store.merge_config(101, {'server': {'delete-for-all': True}})
        for value in (True, 1, 0, None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.service.delete_conversation(self.context, for_all=value)
        self.service.client.stop_bot.assert_not_called()
        self.service.client.change_conversation_lifecycle.assert_not_called()

    def test_false_or_object_stop_ack_stays_unknown_without52(self):
        for invalid in (False, {}, {'success': False}, {'success': True}):
            with self.subTest(ack=invalid):
                case = self.new_case()
                case.service.client.stop_bot.side_effect = None
                case.service.client.stop_bot.return_value = invalid
                with self.assertRaises(MaxWriteOutcomeUnknown):
                    case.service.delete_conversation(case.context)
                state = case.service.conversation_lifecycle_status(case.context)
                self.assertFalse(state['stop_acknowledged'])
                self.assertFalse(state['confirmed'])
                case.service.client.change_conversation_lifecycle.assert_not_called()

    def test_invalid52_ack_and_get_recovery_never_repeat_writes(self):
        self.service.client.change_conversation_lifecycle.side_effect = None
        self.service.client.change_conversation_lifecycle.return_value = False
        with self.assertRaises(MaxWriteOutcomeUnknown):
            self.service.delete_conversation(self.context)
        self.assertTrue(self.service.conversation_lifecycle_status(self.context)['stop_confirmed'])
        for operation in (self.service.delete_conversation,
                          self.service.continue_bot_conversation_delete):
            with self.assertRaises(ValueError):
                operation(self.context)
        result = self.service.refresh_conversation_lifecycle(self.context)
        self.assertFalse(result['confirmed'])
        self.case.remote.update(status='REMOVED', modified=120)
        self.case.remote.pop('lastMessage', None)
        result = self.service.refresh_conversation_lifecycle(self.context)
        self.assertTrue(result['confirmed'])
        self.assertFalse(result['delete_acknowledged'])
        self.service.client.stop_bot.assert_called_once()
        self.service.client.change_conversation_lifecycle.assert_called_once()

    def test_deleted_unknown_survives_store_reopen_without_retry(self):
        directory = self.enterContext(tempfile.TemporaryDirectory(prefix='bot-review-', dir=Path.cwd()))
        path = Path(directory) / 'synthetic.sqlite'
        first = Store(path)
        self.addCleanup(first.close)
        self.service.store._db.backup(first._db)
        self.service.store = first
        self.service.client.change_conversation_lifecycle.side_effect = MaxWriteOutcomeUnknown('synthetic')
        with self.assertRaises(MaxWriteOutcomeUnknown):
            self.service.delete_conversation(self.context)
        restored = self.new_case()
        reopened = Store(path)
        self.addCleanup(reopened.close)
        restored.service.store = reopened
        restored.remote.update(isSuspended=True, modified=110)
        restored.service._server_chats[303] = copy.deepcopy(restored.remote)
        result = restored.service.refresh_conversation_lifecycle(restored.context)
        self.assertEqual(result['stage'], 'delete_pending')
        self.assertFalse(result['can_continue_delete'])
        for operation in (restored.service.delete_conversation,
                          restored.service.continue_bot_conversation_delete):
            with self.assertRaises(ValueError):
                operation(restored.context)
        restored.service.client.stop_bot.assert_not_called()
        restored.service.client.change_conversation_lifecycle.assert_not_called()

    def test_completed_journal_allows_new_operation_but_old_cas_cannot_release_it(self):
        self.assertTrue(self.service.delete_conversation(self.context)['confirmed'])
        old = self.service.store.conversation_action_journal(101, 303)
        self.case.remote.update(status='ACTIVE', isSuspended=False, modified=140,
                                lastMessage={'id': 12, 'time': 140, 'type': 'USER', 'text': 'new'})
        self.service._remember_chat_info({'chats': [copy.deepcopy(self.case.remote)]}, my_id=101)
        self.service.client.stop_bot.side_effect = MaxWriteOutcomeUnknown('synthetic')
        with self.assertRaises(MaxWriteOutcomeUnknown):
            self.service.delete_conversation(self.context)
        new = self.service.store.conversation_action_journal(101, 303)
        self.assertNotEqual(old['operation_id'], new['operation_id'])
        self.assertFalse(self.service.store.compare_conversation_action(101, 303, old, None))
        self.assertEqual(self.service.store.bot_delete_for_peer(101, 202), new)
        self.assertEqual(self.service.client.stop_bot.call_count, 2)
        self.service.client.change_conversation_lifecycle.assert_called_once()

    def test_completed_old_delete_not_returned_as_new_effect_by_controls(self):
        self.assertTrue(self.service.delete_conversation(self.context)['confirmed'])
        self.case.remote.update(status='ACTIVE', isSuspended=False, modified=140,
                                lastMessage={'id': 12, 'time': 140, 'type': 'USER', 'text': 'new'})
        self.service._remember_chat_info({'chats': [copy.deepcopy(self.case.remote)]}, my_id=101)
        self.service._pending_contact_write = {'owner': 101, 'id': 909, 'blocked': True}
        with patch.object(self.service, 'refresh_conversation_action_state', return_value={}):
            result = self.service.refresh_conversation_controls(self.context)
        self.assertNotIn('lifecycle_result', result)
        self.assertTrue(result['pending'])
        self.assertFalse(result['lifecycle_pending'])

    def test_explicit52_rejection_retains_stop_for_separate_continuation(self):
        self.service.client.change_conversation_lifecycle.side_effect = MaxError('synthetic rejected')
        result = self.service.delete_conversation(self.context)
        self.assertTrue(result['stop_confirmed'])
        self.assertFalse(result['delete_confirmed'])
        self.assertTrue(self.service.conversation_lifecycle_status(self.context)['can_continue_delete'])
        self.service.refresh_conversation_lifecycle(self.context)
        self.service.client.change_conversation_lifecycle.assert_called_once()
        self.service.client.change_conversation_lifecycle.side_effect = self.case.accept
        self.assertTrue(self.service.continue_bot_conversation_delete(self.context)['confirmed'])
        self.service.client.stop_bot.assert_called_once()
        self.assertEqual(self.service.client.change_conversation_lifecycle.call_count, 2)


if __name__ == '__main__':
    unittest.main()
