"""Independent report safety review: fake sessions and an in-memory account only."""
import copy
import unittest
from unittest.mock import Mock

import test_conversation_lifecycle as base
from maxclient.core.models import Chat, Message
from maxclient.protocol.client import AuthenticatedSession, MaxError, MaxWriteOutcomeUnknown


def reasons_reply(*, sync=10, type_id=2, reason_id=1):
    return {'complainSync': sync, 'complains': [
        {'typeId': type_id, 'reasons': [
            {'reasonId': reason_id, 'reasonTitle': 'Synthetic reason'}]}]}


class ConversationReportIndependentTests(unittest.TestCase):
    def setUp(self):
        self.fixture = base.ConversationLifecycleTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.service = self.fixture.service
        self.client = self.service.client
        self.client.conversation_report_reasons = Mock(return_value=reasons_reply())
        self.client.report_conversation = Mock(return_value={'success': True})
        self.client.leave_conversation = Mock(side_effect=AssertionError('Leave forbidden'))
        self.make_channel()

    def tearDown(self):
        self.client.request.assert_not_called()
        self.client.leave_conversation.assert_not_called()
        self.client.change_conversation_lifecycle.assert_not_called()

    def make_channel(self, **fields):
        self.fixture.remote = base.chat(type='CHANNEL', owner=202, joinTime=1,
            adminParticipants={}, participantsCount=2, title='Synthetic channel', **fields)
        self.service._server_chats[303] = copy.deepcopy(self.fixture.remote)
        self.service.store.upsert_chat(Chat(id=303, title='Synthetic channel', chat_type='CHANNEL'))
        self.context = self.service.conversation_context(303, activate_files=False)

    def make_bot(self):
        self.fixture.remote = base.chat(hasBots=True)
        self.service._server_chats[303] = copy.deepcopy(self.fixture.remote)
        self.service.store.upsert_chat(Chat(id=303, title='Synthetic bot', chat_type='DIALOG', peer_user_id=202))
        self.service.store.merge_config(101, {'server': {'bot-complaint-enabled': True}})
        self.context = self.service.conversation_context(303, activate_files=False)
        self.client.conversation_report_reasons.return_value = reasons_reply(type_id=7)

    def ticket(self):
        result = self.service.get_conversation_report_reasons(self.context)
        self.assertIs(result['context'], self.context)
        return result['ticket']

    def submit(self, ticket, reason=1):
        return self.service.submit_conversation_report(self.context, reason, ticket=ticket)

    def journal(self, *, type_id=2, target_id=303, owner_id=101):
        return self.service.store.conversation_report_journal(owner_id, type_id, target_id)

    def assert_history_intact(self):
        self.assertEqual(self.service.store.message(303, 9).text, 'old')
        self.assertIsNone(self.service.store.history_boundary(303))
        self.assertIsNone(self.service.store.conversation_departure(303))

    def test_channel_uses_captured_chat_and_preserves_history(self):
        self.service.store.insert_message(Message(chat_id=303, time_ms=100,
            local_id='synthetic-draft', text='draft', status='pending'))
        result = self.submit(self.ticket())
        self.assertIs(result['context'], self.context)
        self.assertEqual((result['chat_id'], result['type_id'], result['target_id']), (303, 2, 303))
        self.assertIs(result['confirmed'], True)
        self.assertIs(result['pending'], False)
        self.client.report_conversation.assert_called_once_with(2, 303, 1,
            expected_session=self.context.session_context)
        self.assert_history_intact()
        self.assertTrue(any(m.local_id == 'synthetic-draft' for m in self.service.store.messages(303)))

    def test_bot_uses_contact_identity_not_conversation_identity(self):
        self.make_bot()
        result = self.submit(self.ticket())
        self.assertEqual((result['type_id'], result['target_id']), (7, 202))
        self.client.report_conversation.assert_called_once_with(7, 202, 1,
            expected_session=self.context.session_context)
        self.assert_history_intact()

    def test_report_reasons_are_read_only(self):
        self.ticket()
        self.client.report_conversation.assert_not_called()
        self.assertIsNone(self.journal())
        self.assert_history_intact()

    def test_success_prevents_same_target_duplicate(self):
        ticket = self.ticket()
        self.submit(ticket)
        with self.assertRaises(ValueError):
            self.submit(ticket)
        state = self.service.conversation_report_status(self.context)
        self.assertIs(state['pending'], False)
        self.assertIs(state['already_reported'], True)
        self.assertIs(state['available'], False)
        self.client.report_conversation.assert_called_once()

    def test_timeout_preserves_unknown_and_never_retries(self):
        ticket = self.ticket()
        self.client.report_conversation.side_effect = MaxWriteOutcomeUnknown('Synthetic timeout')
        with self.assertRaises(MaxWriteOutcomeUnknown):
            self.submit(ticket)
        before = copy.deepcopy(self.journal())
        self.assertIsNotNone(before)
        for _ in range(3):
            state = self.service.conversation_report_status(self.context)
            self.assertIs(state['pending'], True)
            self.assertIs(state['available'], False)
            with self.assertRaises(ValueError):
                self.submit(ticket)
        self.assertEqual(self.journal(), before)
        self.client.report_conversation.assert_called_once()
        self.assert_history_intact()

    def test_malformed_ack_is_unknown_not_success(self):
        ticket = self.ticket()
        self.client.report_conversation.return_value = {'success': 1}
        with self.assertRaises(MaxWriteOutcomeUnknown):
            self.submit(ticket)
        state = self.service.conversation_report_status(self.context)
        self.assertIs(state['pending'], True)
        self.assertIs(state['already_reported'], False)
        self.assertIsNotNone(self.journal())
        self.assert_history_intact()

    def test_arbitrary_reason_not_in_server_list_never_sent(self):
        ticket = self.ticket()
        for reason in (2, 0, True, '1', 1.0, None):
            with self.subTest(reason=reason):
                with self.assertRaises(ValueError):
                    self.submit(ticket, reason)
        self.client.report_conversation.assert_not_called()
        self.assertIsNone(self.journal())

    def test_mutating_returned_reason_list_cannot_authorize_new_reason(self):
        result = self.service.get_conversation_report_reasons(self.context)
        result['reasons'].append({'reason_id': 999, 'title': 'Injected client-side reason'})
        with self.assertRaises(ValueError):
            self.submit(result['ticket'], 999)
        self.client.report_conversation.assert_not_called()
        self.assertIsNone(self.journal())

    def test_same_numeric_reason_for_wrong_report_type_is_not_accepted(self):
        self.client.conversation_report_reasons.return_value = reasons_reply(type_id=7)
        try:
            result = self.service.get_conversation_report_reasons(self.context)
        except ValueError:
            result = None
        if result is not None:
            self.assertEqual(result['reasons'], [])
            with self.assertRaises(ValueError):
                self.submit(result.get('ticket'))
        self.client.report_conversation.assert_not_called()

    def test_first_empty_reasons_never_fabricate_authorization(self):
        self.client.conversation_report_reasons.return_value = {'complainSync': 10, 'complains': []}
        try:
            result = self.service.get_conversation_report_reasons(self.context)
        except ValueError:
            result = None
        if result is not None:
            self.assertEqual(result['reasons'], [])
            with self.assertRaises(ValueError):
                self.submit(result.get('ticket'))
        self.client.report_conversation.assert_not_called()

    def test_reconnect_invalidates_previously_selected_reason_ticket(self):
        ticket = self.ticket()
        self.client._auth_session = AuthenticatedSession(101, self.client.device_id, 'WEB', self.client._sock)
        old = self.context
        self.context = self.service.conversation_context(303, activate_files=False)
        self.assertIsNot(self.context.session_context, old.session_context)
        with self.assertRaises(ValueError):
            self.submit(ticket)
        self.client.report_conversation.assert_not_called()
        self.assertIsNone(self.journal())

    def test_session_replaced_while_loading_reasons_returns_no_ticket(self):
        def replace(*_args, **_kwargs):
            self.client._auth_session = AuthenticatedSession(101, self.client.device_id, 'WEB', self.client._sock)
            return reasons_reply()
        self.client.conversation_report_reasons.side_effect = replace
        with self.assertRaises(ValueError):
            self.ticket()
        self.client.report_conversation.assert_not_called()

    def test_channel_becoming_own_channel_revokes_old_ticket(self):
        ticket = self.ticket()
        self.fixture.remote['owner'] = 101
        with self.assertRaises(ValueError):
            self.submit(ticket)
        self.client.report_conversation.assert_not_called()
        self.assertIsNone(self.journal())

    def test_restriction_256_revokes_previously_selected_reason(self):
        ticket = self.ticket()
        self.fixture.remote['restrictions'] = 256
        with self.assertRaises(ValueError):
            self.submit(ticket)
        self.client.report_conversation.assert_not_called()
        self.assertIsNone(self.journal())

    def test_bot_feature_revocation_prevents_write(self):
        self.make_bot()
        ticket = self.ticket()
        self.service.store.merge_config(101, {'server': {'bot-complaint-enabled': False}})
        with self.assertRaises(ValueError):
            self.submit(ticket)
        self.client.report_conversation.assert_not_called()
        self.assertIsNone(self.journal(type_id=7, target_id=202))

    def test_changed_bot_peer_does_not_redirect_report(self):
        self.make_bot()
        ticket = self.ticket()
        self.fixture.remote['participants'] = {'101': 1, '404': 1}
        with self.assertRaises(ValueError):
            self.submit(ticket)
        self.client.report_conversation.assert_not_called()
        self.assertIsNone(self.journal(type_id=7, target_id=202))

    def test_inflight_second_submit_does_not_issue_second_write(self):
        ticket = self.ticket()
        def reenter(*_args, **_kwargs):
            state = self.service.conversation_report_status(self.context)
            self.assertIs(state['pending'], True)
            with self.assertRaises(ValueError):
                self.submit(ticket)
            return {'success': True}
        self.client.report_conversation.side_effect = reenter
        self.assertIs(self.submit(ticket)['confirmed'], True)
        self.client.report_conversation.assert_called_once()

    def test_confirmed_refusal_is_not_success_and_releases_only_own_intent(self):
        ticket = self.ticket()
        self.client.report_conversation.return_value = {'success': False}
        with self.assertRaises(MaxError):
            self.submit(ticket)
        self.assertIsNone(self.journal())
        state = self.service.conversation_report_status(self.context)
        self.assertIs(state['already_reported'], False)
        self.assertIs(state['pending'], False)
        self.client.report_conversation.assert_called_once()
        self.assert_history_intact()

    def test_success_after_reconnect_does_not_modify_new_session(self):
        ticket = self.ticket()
        def reconnect_after_write(*_args, **_kwargs):
            self.client._auth_session = AuthenticatedSession(101, self.client.device_id, 'WEB', self.client._sock)
            return {'success': True}
        self.client.report_conversation.side_effect = reconnect_after_write
        with self.assertRaises(MaxWriteOutcomeUnknown):
            self.submit(ticket)
        self.context = self.service.conversation_context(303, activate_files=False)
        self.assertIs(self.service.conversation_report_status(self.context)['pending'], True)
        self.client.report_conversation.assert_called_once()
        self.assert_history_intact()

    def test_new_modal_invalidates_old_selection_ticket(self):
        old_ticket = self.ticket()
        fresh_ticket = self.ticket()
        self.assertIsNot(old_ticket, fresh_ticket)
        with self.assertRaises(ValueError):
            self.submit(old_ticket)
        self.client.report_conversation.assert_not_called()
        self.assertIsNone(self.journal())

    def test_forged_equal_ticket_is_not_an_authorized_ticket(self):
        import dataclasses
        original = self.ticket()
        forged = dataclasses.replace(original)
        with self.assertRaises(ValueError):
            self.submit(forged)
        self.client.report_conversation.assert_not_called()

    def test_empty_delta_preserves_confirmed_reasons_and_sync(self):
        self.ticket()
        self.client.conversation_report_reasons.return_value = {'complainSync': 20, 'complains': []}
        result = self.service.get_conversation_report_reasons(self.context)
        self.assertEqual(result['reasons'], [{'reason_id': 1, 'title': 'Synthetic reason'}])
        self.client.conversation_report_reasons.assert_called_with(10,
            expected_session=self.context.session_context)
        self.service.get_conversation_report_reasons(self.context)
        self.client.conversation_report_reasons.assert_called_with(10,
            expected_session=self.context.session_context)
        self.client.report_conversation.assert_not_called()

    def test_older_reason_version_prevents_submit(self):
        ticket = self.ticket()
        self.client.conversation_report_reasons.return_value = reasons_reply(sync=9)
        with self.assertRaises(ValueError):
            self.submit(ticket)
        self.client.report_conversation.assert_not_called()
        self.assertIsNone(self.journal())

    def test_reason_label_change_requires_new_explicit_selection(self):
        ticket = self.ticket()
        updated = reasons_reply(sync=11)
        updated['complains'][0]['reasons'][0]['reasonTitle'] = 'A different meaning'
        self.client.conversation_report_reasons.return_value = updated
        with self.assertRaises(ValueError):
            self.submit(ticket)
        self.client.report_conversation.assert_not_called()
        self.assertIsNone(self.journal())

    def test_duplicate_reason_id_is_not_silently_accepted(self):
        reply = reasons_reply()
        reply['complains'][0]['reasons'].append({'reasonId': 1, 'reasonTitle': 'Conflicting reason'})
        self.client.conversation_report_reasons.return_value = reply
        with self.assertRaises(ValueError):
            self.ticket()
        self.client.report_conversation.assert_not_called()

    def test_post_reservation_permission_revocation_prevents_write(self):
        ticket = self.ticket()
        reserve = self.service.store.reserve_conversation_report
        def revoke(*args, **kwargs):
            reserve(*args, **kwargs)
            self.service._server_chats[303]['restrictions'] = 256
        self.service.store.reserve_conversation_report = revoke
        with self.assertRaises(ValueError):
            self.submit(ticket)
        self.client.report_conversation.assert_not_called()
        self.assertIsNone(self.journal())

    def test_pending_report_survives_new_conversation_alias_for_same_bot(self):
        self.make_bot()
        ticket = self.ticket()
        self.client.report_conversation.side_effect = MaxWriteOutcomeUnknown('Synthetic timeout')
        with self.assertRaises(MaxWriteOutcomeUnknown):
            self.submit(ticket)
        alias = base.chat(id=404, hasBots=True)
        self.service._server_chats[404] = alias
        self.service.store.upsert_chat(Chat(id=404, title='Other synthetic alias',
            chat_type='DIALOG', peer_user_id=202))
        other = self.service.conversation_context(404, activate_files=False)
        before = self.client.conversation_report_reasons.call_count
        state = self.service.get_conversation_report_reasons(other)
        self.assertIs(state['pending'], True)
        self.assertIs(state['available'], False)
        self.assertEqual(state['reasons'], [])
        self.assertIsNone(state['ticket'])
        self.assertEqual(self.client.conversation_report_reasons.call_count, before)
        self.client.report_conversation.assert_called_once()

    def test_unknown_journal_isolated_by_account_and_report_type(self):
        ticket = self.ticket()
        self.client.report_conversation.side_effect = MaxWriteOutcomeUnknown('Synthetic timeout')
        with self.assertRaises(MaxWriteOutcomeUnknown):
            self.submit(ticket)
        before = copy.deepcopy(self.journal())
        self.assertIsNone(self.journal(type_id=7))
        self.service.store.bind_owner(404)
        self.assertIsNone(self.journal(owner_id=404))
        self.service.store.bind_owner(101)
        self.assertEqual(self.journal(), before)

    def replace_journal(self):
        previous = self.journal()
        self.assertTrue(self.service.store.settle_conversation_report(
            101, 2, 303, previous['operation_id'], confirmed=False))
        replacement = {**previous, 'operation_id': 'replacement-independent-operation'}
        self.service.store.reserve_conversation_report(101, 2, 303, replacement)
        return replacement

    def test_late_success_cannot_confirm_replacement_operation(self):
        ticket = self.ticket()
        state = {}
        def replace_then_ack(*_args, **_kwargs):
            state['replacement'] = self.replace_journal()
            return {'success': True}
        self.client.report_conversation.side_effect = replace_then_ack
        with self.assertRaises(MaxWriteOutcomeUnknown):
            self.submit(ticket)
        self.assertEqual(self.journal(), state['replacement'])
        self.assertEqual(self.journal()['status'], 'pending')
        self.client.report_conversation.assert_called_once()

    def test_late_refusal_cannot_release_replacement_operation(self):
        ticket = self.ticket()
        state = {}
        def replace_then_refuse(*_args, **_kwargs):
            state['replacement'] = self.replace_journal()
            return {'success': False}
        self.client.report_conversation.side_effect = replace_then_refuse
        with self.assertRaises((MaxError, MaxWriteOutcomeUnknown)):
            self.submit(ticket)
        self.assertEqual(self.journal(), state['replacement'])
        self.client.report_conversation.assert_called_once()


if __name__ == '__main__':
    unittest.main()
