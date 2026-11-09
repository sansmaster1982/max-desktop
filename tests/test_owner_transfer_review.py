"""Independent owner transfer regressions: synthetic state, no live RPC."""
import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

import test_owner_transfer_core as core_fixture
from maxclient.core.store import Store
from maxclient.protocol.client import AuthenticatedSession, MaxError, MaxWriteOutcomeUnknown


class OwnerTransferReviewTests(unittest.TestCase):
    def new_case(self):
        case = core_fixture.OwnerTransferCoreTests()
        case.setUp()
        self.addCleanup(case.doCleanups)
        self.addCleanup(case.tearDown)
        return case

    def setUp(self):
        self.case = self.new_case()
        self.s = self.case.s
        self.context = self.case.context

    def rejoin(self, *, owner=202):
        self.case.fixture.remote.update(status='ACTIVE', joinTime=2, modified=200, owner=owner)
        self.s._remember_chat_info({'chats': [copy.deepcopy(self.case.fixture.remote)]}, my_id=101)

    def test_historical_complete_does_not_confirm_leave_after_rejoin(self):
        self.assertTrue(self.s.transfer_conversation_owner_and_leave(self.context, self.case.ticket())['confirmed'])
        self.rejoin()
        self.assertIsNone(self.s.store.conversation_departure(303))
        result = self.s.refresh_conversation_lifecycle(self.context)
        self.assertFalse(result['confirmed'])
        self.assertFalse(result['pending'])
        self.s.client.leave_conversation.assert_called_once()

    def test_new_owner_lifecycle_available_after_complete_and_rejoin(self):
        self.assertTrue(self.s.transfer_conversation_owner_and_leave(self.context, self.case.ticket())['confirmed'])
        self.rejoin(owner=101)
        self.assertTrue(self.s.owner_transfer_available(self.context))
        self.assertEqual(self.case.ticket().successor_id, 202)

    def test_same_chat_second_context_cannot_send_inside_first55(self):
        first = self.case.ticket()
        other = self.s.conversation_context(303, activate_files=False)
        second = self.s.get_owner_transfer_candidates(other)['candidates'][0]['ticket']

        def transfer(*args, **kwargs):
            with self.assertRaises(ValueError):
                self.s.transfer_conversation_owner_and_leave(other, second)
            return self.case.transfer()

        self.s.client.transfer_conversation_owner.side_effect = transfer
        self.assertTrue(self.s.transfer_conversation_owner_and_leave(self.context, first)['confirmed'])
        self.s.client.transfer_conversation_owner.assert_called_once()
        self.s.client.leave_conversation.assert_called_once()

    def test_same_owner_session_replacement_after55_prevents58(self):
        client = self.s.client

        def transfer(*args, **kwargs):
            result = self.case.transfer()
            client._auth_session = AuthenticatedSession(101, client.device_id, 'WEB', client._sock)
            return result

        client.transfer_conversation_owner.side_effect = transfer
        with self.assertRaises((ValueError, MaxWriteOutcomeUnknown)):
            self.s.transfer_conversation_owner_and_leave(self.context, self.case.ticket())
        client.leave_conversation.assert_not_called()
        fresh = self.s.conversation_context(303, activate_files=False)
        result = self.s.refresh_conversation_lifecycle(fresh)
        self.assertTrue(result['transfer_confirmed'])
        self.assertTrue(result['can_continue_leave'])
        client.transfer_conversation_owner.assert_called_once()
        client.leave_conversation.assert_not_called()

    def test_cas_replacement_before55_is_not_erased(self):
        reserve = self.s._reserve_lifecycle
        replacement = None

        def reserve_and_replace(context, record):
            nonlocal replacement
            reserve(context, record)
            replacement = {**record, 'operation_id': 'replacement-operation'}
            self.assertTrue(self.s.store.compare_conversation_action(101, 303, record, replacement))

        self.s._reserve_lifecycle = reserve_and_replace
        with self.assertRaises(ValueError):
            self.s.transfer_conversation_owner_and_leave(self.context, self.case.ticket())
        self.assertEqual(self.s.store.conversation_action_journal(101, 303), replacement)
        self.s.client.transfer_conversation_owner.assert_not_called()
        self.s.client.leave_conversation.assert_not_called()

    def test_cas_replacement_after55_never_advances_replacement(self):
        replacement = None

        def transfer(*args, **kwargs):
            nonlocal replacement
            result = self.case.transfer()
            old = self.s.store.conversation_action_journal(101, 303)
            replacement = {**old, 'operation_id': 'newer-operation'}
            self.assertTrue(self.s.store.compare_conversation_action(101, 303, old, replacement))
            return result

        self.s.client.transfer_conversation_owner.side_effect = transfer
        with self.assertRaises(MaxWriteOutcomeUnknown):
            self.s.transfer_conversation_owner_and_leave(self.context, self.case.ticket())
        self.assertEqual(self.s.store.conversation_action_journal(101, 303), replacement)
        self.s.client.leave_conversation.assert_not_called()

    def test_third_party_owner_after55_is_not_success_or_permission_to_leave(self):
        def transfer(*args, **kwargs):
            ack = self.case.transfer()
            self.case.fixture.remote.update(owner=999, modified=120)
            return ack

        self.s.client.transfer_conversation_owner.side_effect = transfer
        result = self.s.transfer_conversation_owner_and_leave(self.context, self.case.ticket())
        self.assertTrue(result['transfer_acknowledged'])
        self.assertFalse(result['transfer_confirmed'])
        self.assertFalse(result['can_continue_leave'])
        self.s.client.leave_conversation.assert_not_called()
        self.assertFalse(self.s.refresh_conversation_lifecycle(self.context)['confirmed'])
        self.s.client.transfer_conversation_owner.assert_called_once()

    def test_newer_push_during_first_owner_read_never_overwritten(self):
        original = self.s.client.chat_info.side_effect

        def read(*args, **kwargs):
            result = original(*args, **kwargs)
            if self.s.client.transfer_conversation_owner.called:
                self.s._server_chats[303].update(owner=999, modified=900)
            return result

        self.s.client.chat_info.side_effect = read
        result = self.s.transfer_conversation_owner_and_leave(self.context, self.case.ticket())
        self.assertFalse(result['transfer_confirmed'])
        self.assertEqual(self.s._server_chats[303]['owner'], 999)
        self.s.client.leave_conversation.assert_not_called()

    def test_false58_ack_unknown_never_replayed(self):
        self.s.client.leave_conversation.side_effect = None
        self.s.client.leave_conversation.return_value = False
        with self.assertRaises(MaxWriteOutcomeUnknown):
            self.s.transfer_conversation_owner_and_leave(self.context, self.case.ticket())
        result = self.s.refresh_conversation_lifecycle(self.context)
        self.assertEqual(result['stage'], 'leave_pending')
        self.assertFalse(result['leave_acknowledged'])
        with self.assertRaises(ValueError):
            self.s.continue_conversation_owner_leave(self.context)
        self.s.client.leave_conversation.assert_called_once()

    def test_rejected58_then_owner_changes_blocks_continuation(self):
        self.s.client.leave_conversation.side_effect = MaxError('synthetic')
        result = self.s.transfer_conversation_owner_and_leave(self.context, self.case.ticket())
        self.assertTrue(result['can_continue_leave'])
        self.case.fixture.remote.update(owner=999, modified=150)
        result = self.s.continue_conversation_owner_leave(self.context)
        self.assertFalse(result['can_continue_leave'])
        self.assertTrue(result['partial'])
        self.s.client.leave_conversation.assert_called_once()
        self.s.client.transfer_conversation_owner.assert_called_once()

    def test_unknown58_survives_store_reopen_and_readonly_recovery(self):
        directory = self.enterContext(tempfile.TemporaryDirectory(prefix='owner-review-', dir=Path.cwd()))
        path = Path(directory) / 'synthetic.sqlite'
        first = Store(path)
        self.addCleanup(first.close)
        self.s.store._db.backup(first._db)
        self.s.store = first
        self.s.client.leave_conversation.side_effect = MaxWriteOutcomeUnknown('synthetic')
        with self.assertRaises(MaxWriteOutcomeUnknown):
            self.s.transfer_conversation_owner_and_leave(self.context, self.case.ticket())
        restored = self.new_case()
        reopened = Store(path)
        self.addCleanup(reopened.close)
        restored.s.store = reopened
        restored.fixture.remote = copy.deepcopy(self.case.fixture.remote)
        restored.s._server_chats[303] = copy.deepcopy(restored.fixture.remote)
        result = restored.s.refresh_conversation_lifecycle(restored.context)
        self.assertEqual(result['stage'], 'leave_pending')
        self.assertFalse(result['can_continue_leave'])
        with self.assertRaises(ValueError):
            restored.s.continue_conversation_owner_leave(restored.context)
        restored.leave()
        self.assertTrue(restored.s.refresh_conversation_lifecycle(restored.context)['confirmed'])
        restored.s.client.transfer_conversation_owner.assert_not_called()
        restored.s.client.leave_conversation.assert_not_called()

    def test_failure_of_final_atomic_commit_never_hides_chat(self):
        original = self.s.store.compare_conversation_action

        def fail_final(*args, **kwargs):
            if kwargs.get('departure_cutoff') is not None:
                raise RuntimeError('Synthetic disk failure')
            return original(*args, **kwargs)

        self.s.store.compare_conversation_action = fail_final
        result = self.s.transfer_conversation_owner_and_leave(self.context, self.case.ticket())
        self.assertFalse(result['confirmed'])
        self.assertEqual(result['stage'], 'leave_pending')
        self.assertIsNone(self.s.store.conversation_departure(303))
        self.s.store.compare_conversation_action = original
        self.assertTrue(self.s.refresh_conversation_lifecycle(self.context)['confirmed'])
        self.s.client.transfer_conversation_owner.assert_called_once()
        self.s.client.leave_conversation.assert_called_once()

    def test_candidate_name_change_requires_new_explicit_selection(self):
        ticket = self.case.ticket()
        self.case.fixture.contact['names'][0]['name'] = 'Changed person label'
        with self.assertRaises(ValueError):
            self.s.transfer_conversation_owner_and_leave(self.context, ticket)
        self.s.client.transfer_conversation_owner.assert_not_called()

    def test_self_member_is_not_candidate_or_implicit_default(self):
        self.case.fixture.contact['id'] = 101
        self.case.fixture.remote['adminParticipants']['101'] = {'id': 101}
        result = self.s.get_owner_transfer_candidates(self.context)
        self.assertEqual(result['candidates'], [])
        self.s.client.transfer_conversation_owner.assert_not_called()

    def test_account_replacement_after55_cannot_send_or_confirm_other_owner(self):
        client = self.s.client

        def transfer(*args, **kwargs):
            result = self.case.transfer()
            client._auth_session = AuthenticatedSession(999, client.device_id, 'WEB', client._sock)
            self.s.session.my_user_id = 999
            return result

        client.transfer_conversation_owner.side_effect = transfer
        with self.assertRaises((ValueError, MaxWriteOutcomeUnknown)):
            self.s.transfer_conversation_owner_and_leave(self.context, self.case.ticket())
        client.leave_conversation.assert_not_called()
        self.assertEqual(self.s.store.conversation_action_journal(101, 303)['stage'], 'transfer_pending')
        self.assertIsNone(self.s.store.conversation_departure(303))

    def test_malformed_complete_cannot_release_unknown55_reservation(self):
        self.s.client.transfer_conversation_owner.side_effect = MaxWriteOutcomeUnknown('synthetic')
        with self.assertRaises(MaxWriteOutcomeUnknown):
            self.s.transfer_conversation_owner_and_leave(self.context, self.case.ticket())
        record = self.s.store.conversation_action_journal(101, 303)
        damaged = {**record, 'stage': 'complete'}  # Inconsistent phase flags after damaged persistence.
        self.assertTrue(self.s.store.compare_conversation_action(101, 303, record, damaged))
        with self.assertRaises(ValueError):
            self.s.get_owner_transfer_candidates(self.context)
        self.s.client.transfer_conversation_owner.assert_called_once()
        self.s.client.leave_conversation.assert_not_called()


if __name__ == '__main__':
    unittest.main()
