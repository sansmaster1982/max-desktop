"""Synthetic report contracts and explicit single-send service transitions."""
import copy
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

import test_conversation_lifecycle as fixtures
from maxclient.core.models import Chat
from maxclient.core.store import Store
from maxclient.protocol.client import (MaxClient, MaxFrame, AuthenticatedSession,
    ConnectionState, MaxError, MaxTimeout, MaxWriteOutcomeUnknown, MaxWriteNotStarted)
from maxclient.protocol.conversation_report_contracts import (
    build_report_reasons, parse_report_reasons, build_conversation_report, parse_report_ack,
)


def catalog(sync=10, title='Synthetic reason', reason=129, type_id=2):
    return {'complainSync': sync, 'complains': [
        {'typeId': type_id, 'reasons': [{'reasonId': reason, 'reasonTitle': title}]}]}


class ConversationReportContractsTests(unittest.TestCase):
    def test_exact_requests_and_actual_unbounded_integer_alias(self):
        self.assertEqual(build_report_reasons(10.25), {'complainSync': 10.25})
        self.assertEqual(build_conversation_report(2, -303, 129),
                         {'reasonId': 129, 'typeId': 2, 'ids': [-303]})
        self.assertEqual(build_conversation_report(7, 202, -129)['reasonId'], -129)
        for invalid in (True, 0, 1.2, '1', 2**53):
            with self.subTest(reason=invalid), self.assertRaises(ValueError):
                build_conversation_report(2, -303, invalid)
        for kind in (True, 1, 3, 6, 9, '2'):
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                build_conversation_report(kind, -303, 1)
        for invalid in (True, -1, float('nan'), float('inf'), None):
            with self.assertRaises(ValueError): build_report_reasons(invalid)

    def test_catalog_required_fields_and_ambiguity(self):
        value = catalog(reason=-129, title='')
        result = parse_report_reasons(value)
        value['complains'][0]['reasons'][0]['reasonTitle'] = 'changed'
        self.assertEqual(result['complains'][0]['reasons'][0]['reasonTitle'], '')
        duplicated_reason = catalog()
        duplicated_reason['complains'][0]['reasons'] *= 2
        duplicated_type = catalog()
        duplicated_type['complains'] *= 2
        for invalid in (None, {}, {'complainSync': 1}, {'complains': []},
                        catalog(type_id=0), catalog(reason=True), duplicated_reason, duplicated_type):
            with self.subTest(data=invalid), self.assertRaises(ValueError): parse_report_reasons(invalid)

    def test_ack_boolean_false_is_preserved_not_truthy_success(self):
        self.assertEqual(parse_report_ack({'success': True, 'untrusted': 'ignored'}), {'success': True})
        self.assertEqual(parse_report_ack({'success': False}), {'success': False})
        for invalid in (None, {}, {'success': 1}, {'success': 'true'}, {'success': None}):
            with self.assertRaises(ValueError): parse_report_ack(invalid)


class ConversationReportWireTests(unittest.TestCase):
    def setUp(self):
        self.c = c = MaxClient(auto_reconnect=False)
        c._sock = object(); c._state = ConnectionState.CONNECTED; c._device_type = 'WEB'
        c._auth_session = AuthenticatedSession(101, c.device_id, 'WEB', c._sock)
        c.request = Mock(return_value=MaxFrame(1, 1, 161, b'', {'success': True}))

    def test_single_explicit161_and162_with_exact_session(self):
        session = self.c.authenticated_session
        self.assertEqual(self.c.report_conversation(7, 202, 129, expected_session=session), {'success': True})
        self.c.request.assert_called_once_with(161, {'reasonId': 129, 'typeId': 7, 'ids': [202]},
                                               expected_session=session)
        self.c.request.reset_mock(); self.c.request.return_value = MaxFrame(1, 2, 162, b'', catalog())
        self.assertEqual(self.c.conversation_report_reasons(0, expected_session=session), catalog())
        self.c.request.assert_called_once_with(162, {'complainSync': 0}, expected_session=session)

    def test_absent_or_stale_session_never_sends(self):
        for session in (None, AuthenticatedSession(101, self.c.device_id, 'WEB', self.c._sock)):
            with self.assertRaises(MaxWriteNotStarted):
                self.c.report_conversation(2, -303, 1, expected_session=session)
            with self.assertRaises(MaxWriteNotStarted):
                self.c.conversation_report_reasons(0, expected_session=session)
        self.c.request.assert_not_called()

    def test_false_refusal_malformed_unknown_timeout_single_attempt(self):
        session = self.c.authenticated_session
        self.c.request.return_value = MaxFrame(1, 1, 161, b'', {'success': False})
        with self.assertRaises(MaxError) as caught:
            self.c.report_conversation(2, -303, 1, expected_session=session)
        self.assertNotIsInstance(caught.exception, MaxWriteOutcomeUnknown)
        for response in (None, {}, {'success': 1}):
            self.c.request.reset_mock(); self.c.request.return_value = MaxFrame(1, 1, 161, b'', response)
            with self.assertRaises(MaxWriteOutcomeUnknown):
                self.c.report_conversation(2, -303, 1, expected_session=session)
            self.c.request.assert_called_once()
        self.c.request.reset_mock(); self.c.request.side_effect = MaxTimeout('synthetic')
        with self.assertRaises(MaxWriteOutcomeUnknown):
            self.c.report_conversation(2, -303, 1, expected_session=session)
        self.c.request.assert_called_once()


class ConversationReportCoreTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ConversationLifecycleTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.s = self.fixture.service
        self.fixture.remote = fixtures.chat(type='CHANNEL', owner=202, joinTime=1, adminParticipants={})
        self.s._server_chats[303] = copy.deepcopy(self.fixture.remote)
        self.s.store.upsert_chat(Chat(id=303, title='Synthetic channel', chat_type='CHANNEL', is_channel=True))
        self.context = self.s.conversation_context(303, activate_files=False)
        self.s.client.conversation_report_reasons = Mock(return_value=catalog())
        self.s.client.report_conversation = Mock(return_value={'success': True})

    def tearDown(self):
        self.s.client.request.assert_not_called()
        self.s.client.change_conversation_lifecycle.assert_not_called()

    def selection(self):
        return self.s.get_conversation_report_reasons(self.context)['ticket']

    def test_local_status_and_open_reasons_never_submit(self):
        state = self.s.conversation_report_status(self.context)
        self.assertEqual((state['available'], state['status']), (True, 'ready'))
        self.s.client.chat_info.assert_not_called()
        self.s.client.conversation_report_reasons.assert_not_called()
        data = self.s.get_conversation_report_reasons(self.context)
        self.assertEqual(data['reasons'], [{'reason_id': 129, 'title': 'Synthetic reason'}])
        self.s.client.report_conversation.assert_not_called()

    def test_channel_success_persists_duplicate_guard_without_history_change(self):
        ticket = self.selection()
        result = self.s.submit_conversation_report(self.context, 129, ticket=ticket)
        self.assertEqual((result['type_id'], result['target_id'], result['confirmed'], result['pending']),
                         (2, 303, True, False))
        self.s.client.report_conversation.assert_called_once_with(2, 303, 129,
            expected_session=self.context.session_context)
        self.assertIsNotNone(self.s.store.message(303, 9))
        self.assertIsNone(self.s.store.history_boundary(303))
        self.assertFalse(self.s.conversation_lifecycle_status(self.context)['pending'])
        self.assertTrue(self.s.conversation_report_status(self.context)['already_reported'])
        with self.assertRaises(ValueError): self.s.submit_conversation_report(self.context, 129, ticket=ticket)

    def test_empty_delta_preserves_both_prior_sync_and_reasons(self):
        ticket = self.selection()
        self.s.client.conversation_report_reasons.return_value = {'complainSync': 30, 'complains': []}
        self.s.submit_conversation_report(self.context, 129, ticket=ticket)
        self.assertEqual(self.s._conversation_report_catalog[2]['complainSync'], 10)
        self.assertEqual(self.s.client.conversation_report_reasons.call_args.args, (10,))

    def test_no_initial_reasons_never_invents_reason_or_advances_sync(self):
        self.s.client.conversation_report_reasons.return_value = {'complainSync': 30, 'complains': []}
        ticket = self.selection()
        self.assertEqual(ticket.reasons, ())
        self.assertEqual(ticket.sync, 0)
        with self.assertRaises(ValueError): self.s.submit_conversation_report(self.context, 129, ticket=ticket)
        self.s.client.report_conversation.assert_not_called()

    def test_reason_label_changed_or_target_group_removed_blocks_before_write(self):
        ticket = self.selection()
        for response in (catalog(11, 'different'), catalog(11, type_id=7)):
            self.s.client.conversation_report_reasons.return_value = response
            with self.assertRaises(ValueError): self.s.submit_conversation_report(self.context, 129, ticket=ticket)
        self.s.client.report_conversation.assert_not_called()
        self.assertIsNone(self.s.store.conversation_report_journal(101, 2, 303))

    def test_copied_or_replaced_ticket_not_accepted(self):
        old = self.selection()
        with self.assertRaises(ValueError):
            self.s.submit_conversation_report(self.context, 129, ticket=replace(old))
        self.selection()
        with self.assertRaises(ValueError): self.s.submit_conversation_report(self.context, 129, ticket=old)
        self.s.client.report_conversation.assert_not_called()

    def test_false_is_refusal_and_unknown_remains_pending_without_lifecycle_effects(self):
        ticket = self.selection()
        self.s.client.report_conversation.return_value = {'success': False}
        with self.assertRaises(MaxError): self.s.submit_conversation_report(self.context, 129, ticket=ticket)
        self.assertFalse(self.s.conversation_report_status(self.context)['pending'])
        self.s.client.report_conversation.return_value = {'success': 1}
        with self.assertRaises(MaxWriteOutcomeUnknown):
            self.s.submit_conversation_report(self.context, 129, ticket=ticket)
        self.assertTrue(self.s.conversation_report_status(self.context)['pending'])
        self.assertFalse(self.s.conversation_lifecycle_status(self.context)['pending'])
        status = self.s.get_conversation_report_reasons(self.context)
        self.assertTrue(status['pending'])
        self.assertIsNone(status['ticket'])
        self.assertEqual(self.s.client.report_conversation.call_count, 2)

    def test_changed_rights_on_final_refresh_and_after_reserve_block_send(self):
        ticket = self.selection()
        self.fixture.remote['restrictions'] = 256
        with self.assertRaises(ValueError): self.s.submit_conversation_report(self.context, 129, ticket=ticket)
        self.fixture.remote['restrictions'] = 0
        self.s._server_chats[303]['restrictions'] = 0
        original = self.s.store.reserve_conversation_report
        def reserve(*args):
            original(*args)
            self.s._server_chats[303]['owner'] = 101
        self.s.store.reserve_conversation_report = reserve
        with self.assertRaises(ValueError): self.s.submit_conversation_report(self.context, 129, ticket=ticket)
        self.s.client.report_conversation.assert_not_called()
        self.assertIsNone(self.s.store.conversation_report_journal(101, 2, 303))

    def test_reconnect_cannot_reuse_selection_or_resend_unknown(self):
        ticket = self.selection()
        self.s.client.report_conversation.side_effect = MaxWriteOutcomeUnknown('synthetic')
        with self.assertRaises(MaxWriteOutcomeUnknown):
            self.s.submit_conversation_report(self.context, 129, ticket=ticket)
        c = self.s.client
        c._auth_session = AuthenticatedSession(101, c.device_id, 'WEB', c._sock)
        new_context = self.s.conversation_context(303, activate_files=False)
        self.assertTrue(self.s.conversation_report_status(new_context)['pending'])
        with self.assertRaises(ValueError):
            self.s.submit_conversation_report(new_context, 129, ticket=ticket)
        self.s.client.report_conversation.assert_called_once()

    def test_catalog_does_not_cross_confirmed_session(self):
        self.selection()
        c = self.s.client
        c._auth_session = AuthenticatedSession(101, c.device_id, 'WEB', c._sock)
        self.context = self.s.conversation_context(303, activate_files=False)
        c.conversation_report_reasons.return_value = {'complainSync': 50, 'complains': []}
        data = self.s.get_conversation_report_reasons(self.context)
        self.assertEqual(data['reasons'], [])
        self.assertEqual(c.conversation_report_reasons.call_args.args, (0,))


class ConversationReportStoreTests(unittest.TestCase):
    def test_pending_and_confirmed_survive_reopen_and_actor_switch(self):
        with tempfile.TemporaryDirectory(prefix='max-report-') as folder:
            path = Path(folder) / 'synthetic.sqlite'
            store = Store(path)
            store.bind_owner(101)
            store.reserve_conversation_report(101, 7, 202, {'status': 'pending', 'operation_id': 'synthetic'})
            store.close()
            store = Store(path)
            try:
                self.assertEqual(store.conversation_report_journal(101, 7, 202)['status'], 'pending')
                self.assertFalse(store.settle_conversation_report(101, 7, 202, 'replacement', confirmed=True))
                store.bind_owner(404)
                self.assertIsNone(store.conversation_report_journal(404, 7, 202))
                with self.assertRaises(ValueError): store.conversation_report_journal(101, 7, 202)
                store.bind_owner(101)
                self.assertTrue(store.settle_conversation_report(101, 7, 202, 'synthetic', confirmed=True))
            finally:
                store.close()
            store = Store(path)
            try:
                self.assertEqual(store.conversation_report_journal(101, 7, 202)['status'], 'confirmed')
                with self.assertRaises(ValueError):
                    store.reserve_conversation_report(101, 7, 202, {'status': 'pending', 'operation_id': 'duplicate'})
            finally:
                store.close()
