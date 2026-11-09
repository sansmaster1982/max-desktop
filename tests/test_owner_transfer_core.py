"""Synthetic owner transfer: exact membership, two writes, honest partial state."""
import copy
import dataclasses
import unittest
from unittest.mock import Mock

import test_conversation_lifecycle as base
from maxclient.core.models import Chat
from maxclient.protocol.client import MaxError, MaxWriteOutcomeUnknown, MaxWriteNotStarted, MaxFrame
from maxclient.protocol.owner_transfer_contracts import (
    build_owner_transfer, parse_owner_transfer_ack, build_transfer_members, parse_transfer_members,
)
import test_conversation_leave_core as leave_tests


class OwnerTransferWireTests(unittest.TestCase):
    def test_55_exact_scope_and_typed_actual_owner_ack(self):
        client = leave_tests.ConversationLeaveWireTests().client()
        raw = base.chat(type='CHAT', owner=202)
        client.request.return_value = MaxFrame(1, 1, 55, b'', {'chat': raw})
        self.assertEqual(client.transfer_conversation_owner(303, 202,
            expected_session=client.authenticated_session)['owner'], 202)
        client.request.assert_called_once_with(55, {'chatId': 303, 'changeOwnerId': 202},
            expected_session=client.authenticated_session)
        for bad in ({}, None, {'chat': None}, {'chat': base.chat(type='CHAT', owner=101)}, {'chat': raw, 'success': True}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                parse_owner_transfer_ack(bad, 303, 202)

    def test_bad_ack_is_unknown_and_no_session_cannot_send(self):
        client = leave_tests.ConversationLeaveWireTests().client()
        client.request.return_value = MaxFrame(1, 1, 55, b'', {})
        with self.assertRaises(MaxWriteOutcomeUnknown):
            client.transfer_conversation_owner(303, 202, expected_session=client.authenticated_session)
        client.request.assert_called_once()
        with self.assertRaises(MaxWriteNotStarted):
            client.transfer_conversation_owner(303, 202, expected_session=None)
        client.request.assert_called_once()

    def test_59_numeric_cursor_search_bounds_no_invented_id_lookup(self):
        self.assertEqual(build_transfer_members(303, 'MEMBER', marker=2.5),
            {'chatId': 303, 'type': 'MEMBER', 'count': 50, 'marker': 2.5})
        self.assertEqual(build_transfer_members(303, 'ADMIN', query='Synthetic'),
            {'chatId': 303, 'type': 'ADMIN', 'query': 'Synthetic'})
        for args in ((True, 202), (303, True), (303, 2**63), (303, -1)):
            with self.assertRaises(ValueError):
                build_owner_transfer(*args)
        for marker in (True, 'next', float('nan')):
            with self.assertRaises(ValueError):
                build_transfer_members(303, 'MEMBER', marker=marker)
        with self.assertRaises(ValueError):
            build_transfer_members(303, 'ADMIN', marker=2)

    def test_member_rows_need_real_contact_duplicate_ids_fail(self):
        contact = {'id': 202, 'updateTime': 1, 'names': [{'name': 'Synthetic'}]}
        result = parse_transfer_members({'members': [{}, {'contact': contact}], 'marker': 3.5})
        self.assertEqual(result['contacts'], [contact])
        self.assertFalse(result['complete'])
        for data in ({'members': [{'contact': contact}, {'contact': contact}]},
                     {'members': [{'id': 202, 'contact': {}}]}, {'marker': None}):
            with self.assertRaises(ValueError):
                parse_transfer_members(data)


class OwnerTransferCoreTests(unittest.TestCase):
    def setUp(self):
        self.fixture = base.ConversationLifecycleTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.s = self.fixture.service
        self.fixture.remote = base.chat(type='CHAT', owner=101, joinTime=1,
            adminParticipants={'202': {'id': 202}}, title='Synthetic group', participantsCount=2)
        self.s._server_chats[303] = copy.deepcopy(self.fixture.remote)
        self.s.store.upsert_chat(Chat(id=303, title='Synthetic group', chat_type='CHAT'))
        self.context = self.s.conversation_context(303, activate_files=False)
        self.s.client.conversation_transfer_members = Mock(side_effect=lambda *a, **k:
            {'contacts': [copy.deepcopy(self.fixture.contact)], 'marker': None, 'complete': True})
        self.s.client.transfer_conversation_owner = Mock(side_effect=self.transfer)
        self.s.client.leave_conversation = Mock(side_effect=self.leave)

    def tearDown(self):
        self.s.client.request.assert_not_called()
        self.s.client.change_conversation_lifecycle.assert_not_called()

    def ticket(self, **kwargs):
        return self.s.get_owner_transfer_candidates(self.context, **kwargs)['candidates'][0]['ticket']

    def transfer(self, *_args, **_kwargs):
        self.fixture.remote.update(owner=202, modified=110)
        return copy.deepcopy(self.fixture.remote)

    def leave(self, *_args, **_kwargs):
        self.fixture.remote.update(status='LEFT', joinTime=0, modified=120)
        return {}

    def test_two_steps_confirmed_preserve_history_and_departure(self):
        result = self.s.transfer_conversation_owner_and_leave(self.context, self.ticket())
        self.assertTrue(result['confirmed'])
        self.assertTrue(result['transfer_acknowledged'])
        self.assertTrue(result['leave_acknowledged'])
        self.assertEqual(result['stage'], 'complete')
        self.s.client.transfer_conversation_owner.assert_called_once_with(303, 202, expected_session=self.context.session_context)
        self.s.client.leave_conversation.assert_called_once_with(303, expected_session=self.context.session_context)
        self.assertIsNotNone(self.s.store.message(303, 9))
        self.assertEqual(self.s.store.conversation_departure(303)['cutoff'], 120)

    def test_group_admin_only_channel_member_positive_page(self):
        self.fixture.remote['adminParticipants'] = {}
        self.assertEqual(self.s.get_owner_transfer_candidates(self.context)['candidates'], [])
        self.fixture.remote['type'] = 'CHANNEL'
        self.s._server_chats[303]['type'] = 'CHANNEL'
        self.s.store.upsert_chat(Chat(id=303, title='Channel', chat_type='CHANNEL', is_channel=True))
        self.s.client.conversation_transfer_members.side_effect = None
        self.s.client.conversation_transfer_members.return_value = {
            'contacts': [copy.deepcopy(self.fixture.contact)], 'marker': 42.5, 'complete': False}
        page = self.s.get_owner_transfer_candidates(self.context, marker=12.5)
        self.assertEqual(page['candidates'][0]['role'], 'MEMBER')
        self.assertFalse(page['complete'])
        ticket = page['candidates'][0]['ticket']
        self.assertTrue(self.s.transfer_conversation_owner_and_leave(self.context, ticket)['confirmed'])
        self.assertTrue(all(call.kwargs['marker'] == 12.5 for call in self.s.client.conversation_transfer_members.call_args_list[1:]))

    def test_ticket_is_actual_issued_object_bound_to_context(self):
        ticket = self.ticket()
        for invalid in (202, dataclasses.replace(ticket), dataclasses.replace(ticket, successor_id=999)):
            with self.assertRaises(ValueError):
                self.s.transfer_conversation_owner_and_leave(self.context, invalid)
        other = self.s.conversation_context(303, activate_files=False)
        with self.assertRaises(ValueError):
            self.s.transfer_conversation_owner_and_leave(other, ticket)
        self.s.client.transfer_conversation_owner.assert_not_called()

    def test_search_revalidation_same_query_missing_choice_blocks(self):
        ticket = self.ticket(query='Synthetic')
        self.s.client.conversation_transfer_members.side_effect = None
        self.s.client.conversation_transfer_members.return_value = {'contacts': [], 'marker': None, 'complete': True}
        with self.assertRaises(ValueError):
            self.s.transfer_conversation_owner_and_leave(self.context, ticket)
        self.assertEqual(self.s.client.conversation_transfer_members.call_args.kwargs['query'], 'Synthetic')
        self.s.client.transfer_conversation_owner.assert_not_called()

    def test_role_revocation_unknown_count_and_restriction_block_before55(self):
        ticket = self.ticket()
        for changes in ({'owner': 202}, {'participantsCount': None}, {'participantsCount': 1},
                        {'restrictions': 8}, {'adminParticipants': {}}, {'options': {'SERVICE_CHAT': True}}):
            saved = copy.deepcopy(self.fixture.remote)
            self.fixture.remote.update(changes)
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.s.transfer_conversation_owner_and_leave(self.context, ticket)
            self.fixture.remote = saved
            self.s._server_chats[303] = copy.deepcopy(saved)
        self.s.client.transfer_conversation_owner.assert_not_called()

    def test_push_after_reservation_revokes_transfer_without_journal_replay(self):
        reserve = self.s._reserve_lifecycle
        def push(context, record):
            reserve(context, record)
            self.s._server_chats[303]['restrictions'] = 8
        self.s._reserve_lifecycle = push
        with self.assertRaises(ValueError):
            self.s.transfer_conversation_owner_and_leave(self.context, self.ticket())
        self.s.client.transfer_conversation_owner.assert_not_called()
        self.assertIsNone(self.s.store.conversation_action_journal(101, 303))

    def test_unknown55_no58_and_readonly_recovery_needs_explicit_continue(self):
        ticket = self.ticket()
        def unknown(*a, **k):
            self.transfer()
            raise MaxWriteOutcomeUnknown('synthetic')
        self.s.client.transfer_conversation_owner.side_effect = unknown
        with self.assertRaises(MaxWriteOutcomeUnknown):
            self.s.transfer_conversation_owner_and_leave(self.context, ticket)
        self.s.client.leave_conversation.assert_not_called()
        result = self.s.refresh_conversation_lifecycle(self.context)
        self.assertEqual(result['stage'], 'transferred')
        self.assertFalse(result['transfer_acknowledged'])
        self.assertTrue(result['can_continue_leave'])
        self.s.client.leave_conversation.assert_not_called()
        self.assertTrue(self.s.continue_conversation_owner_leave(self.context)['confirmed'])
        self.s.client.transfer_conversation_owner.assert_called_once()

    def test_malformed55_ack_remains_unknown_even_with_remote_change(self):
        def malformed(*a, **k):
            self.transfer()
            return {}
        self.s.client.transfer_conversation_owner.side_effect = malformed
        with self.assertRaises(MaxWriteOutcomeUnknown):
            self.s.transfer_conversation_owner_and_leave(self.context, self.ticket())
        self.assertEqual(self.s.conversation_lifecycle_status(self.context)['stage'], 'transfer_pending')
        self.s.client.leave_conversation.assert_not_called()

    def test_ack55_alone_does_not_confirm_owner_or_send58(self):
        self.s.client.transfer_conversation_owner.side_effect = lambda *a, **k: {
            **copy.deepcopy(self.fixture.remote), 'owner': 202}
        result = self.s.transfer_conversation_owner_and_leave(self.context, self.ticket())
        self.assertTrue(result['transfer_acknowledged'])
        self.assertFalse(result['transfer_confirmed'])
        self.assertEqual(result['stage'], 'transfer_pending')
        self.s.client.leave_conversation.assert_not_called()

    def test_rejected58_preserves_transfer_allows_only_explicit58(self):
        self.s.client.leave_conversation.side_effect = MaxError('synthetic reject')
        result = self.s.transfer_conversation_owner_and_leave(self.context, self.ticket())
        self.assertTrue(result['partial'])
        self.assertTrue(result['can_continue_leave'])
        self.assertEqual(result['stage'], 'transferred')
        with self.assertRaises(ValueError):
            self.s.leave_conversation(self.context)
        self.s.client.leave_conversation.side_effect = self.leave
        self.assertTrue(self.s.continue_conversation_owner_leave(self.context)['confirmed'])
        self.s.client.transfer_conversation_owner.assert_called_once()

    def test_unknown58_never_retries_empty_read_is_not_departure(self):
        self.s.client.leave_conversation.side_effect = MaxWriteOutcomeUnknown('synthetic')
        with self.assertRaises(MaxWriteOutcomeUnknown):
            self.s.transfer_conversation_owner_and_leave(self.context, self.ticket())
        self.assertEqual(self.s.conversation_lifecycle_status(self.context)['stage'], 'leave_pending')
        with self.assertRaises(ValueError):
            self.s.continue_conversation_owner_leave(self.context)
        saved = self.fixture.remote
        self.fixture.remote = None
        with self.assertRaises(ValueError):
            self.s.refresh_conversation_lifecycle(self.context)
        self.assertIsNone(self.s.store.conversation_departure(303))
        self.fixture.remote = saved
        self.leave()
        self.assertTrue(self.s.refresh_conversation_lifecycle(self.context)['confirmed'])
        self.s.client.leave_conversation.assert_called_once()

    def test_pending_blocks_lifecycle_and_mute_before_write(self):
        self.s.client.transfer_conversation_owner.side_effect = MaxWriteOutcomeUnknown('synthetic')
        with self.assertRaises(MaxWriteOutcomeUnknown):
            self.s.transfer_conversation_owner_and_leave(self.context, self.ticket())
        self.s.client.update_chat_mute = Mock()
        for action in (lambda: self.s.clear_conversation_history(self.context),
                       lambda: self.s.delete_conversation(self.context, for_all=True),
                       lambda: self.s.set_conversation_mute(self.context, 0)):
            with self.assertRaises(ValueError):
                action()
        self.s.client.update_chat_mute.assert_not_called()

    def test_read_refresh_inflight_does_not_advance_or_duplicate(self):
        def transfer(*a, **k):
            raw = self.transfer()
            self.assertEqual(self.s.refresh_conversation_lifecycle(self.context)['stage'], 'transfer_pending')
            with self.assertRaises(ValueError):
                self.s.continue_conversation_owner_leave(self.context)
            return raw
        self.s.client.transfer_conversation_owner.side_effect = transfer
        self.assertTrue(self.s.transfer_conversation_owner_and_leave(self.context, self.ticket())['confirmed'])
        self.s.client.transfer_conversation_owner.assert_called_once()

    def test_newer_restriction_push_during_final48_stops58(self):
        original = self.s.client.chat_info.side_effect
        def read(*a, **k):
            result = original(*a, **k)
            if self.s.store.conversation_action_journal(101, 303) and self.s.store.conversation_action_journal(101, 303)['stage'] == 'transferred':
                self.s._server_chats[303].update(restrictions=8, modified=999)
            return result
        self.s.client.chat_info.side_effect = read
        result = self.s.transfer_conversation_owner_and_leave(self.context, self.ticket())
        self.assertTrue(result['partial'])
        self.assertFalse(result['can_continue_leave'])
        self.s.client.leave_conversation.assert_not_called()

    def test_final_status_projects_context_and_continuation(self):
        self.s.client.leave_conversation.side_effect = MaxError('synthetic')
        self.s.transfer_conversation_owner_and_leave(self.context, self.ticket())
        status = self.s.conversation_action_status(self.context)
        self.assertTrue(status['owner_transfer_state']['can_continue_leave'])
        self.assertIs(status['owner_transfer_state']['context'], self.context)
        snapshot = self.s._conversation_snapshot(self.context, self.s._require_conversation(self.context))
        self.assertTrue(any(x['action'] == 'continue_owner_leave' and x['available'] for x in snapshot['more_actions']))

    def test_changed_owner_status_does_not_offer_unavailable_continuation(self):
        self.s.client.leave_conversation.side_effect = MaxError('synthetic')
        self.s.transfer_conversation_owner_and_leave(self.context, self.ticket())
        self.fixture.remote.update(owner=404, modified=130)
        result = self.s.refresh_conversation_lifecycle(self.context)
        self.assertTrue(result['transfer_confirmed'])
        self.assertFalse(result['can_continue_leave'])
        self.assertIn('недоступно', result['action_status'])
        self.assertNotIn('можно продолжить', result['action_status'])

    def test_ambiguous_successor_names_are_marked_and_not_submitted(self):
        self.fixture.remote['adminParticipants']['404'] = {'id': 404}
        self.s.client.conversation_transfer_members.side_effect = None
        self.s.client.conversation_transfer_members.return_value = {'contacts': [self.fixture.contact,
            {**self.fixture.contact, 'id': 404}], 'marker': None, 'complete': True}
        page = self.s.get_owner_transfer_candidates(self.context)
        self.assertTrue(all(item['ambiguous'] for item in page['candidates']))
        with self.assertRaises(ValueError):
            self.s.transfer_conversation_owner_and_leave(self.context, page['candidates'][0]['ticket'])
        self.s.client.transfer_conversation_owner.assert_not_called()
