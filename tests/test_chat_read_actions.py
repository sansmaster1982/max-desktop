"""Explicit row receipts with synthetic transport and isolated SQLite only."""
import copy
import unittest
from unittest.mock import Mock

import test_history_reconnect_review as fixture
from maxclient.protocol.client import MaxClient, MaxFrame, MaxError, MaxWriteOutcomeUnknown, MaxWriteNotStarted


class ChatReadActionsTests(unittest.TestCase):
    def setUp(self):
        case = self.case = fixture.HistoryReconnectReviewTests()
        case.setUp()
        self.addCleanup(case.tearDown)
        self.service, self.client, self.store = case.service, case.client, case.store
        self.raw = self.service._server_chats[303]
        self.raw.update(status='ACTIVE', owner=101, created=fixture.STAMP - 50,
                        lastEventTime=fixture.STAMP, markedAsUnread=False)
        self.raw['participants'][101] = fixture.STAMP - 10
        self.client.mark_read = Mock(return_value={'mark': fixture.STAMP, 'unread': 0})
        self.client.mark_unread = Mock(return_value={'mark': fixture.STAMP-1, 'unread': 1})
        self.client.request = Mock(side_effect=AssertionError('No real RPC'))
        self.addCleanup(self.client.request.assert_not_called)
        self.context = self.capture()

    def capture(self):
        return self.service.capture_conversation_read(self.service.conversation_context(303, activate_files=False))

    def make_read(self):
        self.raw.update(newMessages=0, markedAsUnread=False)
        self.raw['participants'][101] = fixture.STAMP
        self.store.set_unread(303, 0)
        self.context = self.capture()

    def act(self, read=True):
        return self.service.set_conversation_read(self.context, read)

    def state(self):
        return self.service.get_conversation_read_state(self.context)

    def test_capture_is_read_only_and_does_not_fetch_messages(self):
        self.assertTrue(self.state()['available'])
        self.client.mark_read.assert_not_called()
        self.client.mark_unread.assert_not_called()
        self.client.chat_history_window.assert_not_called()
        self.client.chat_info.assert_not_called()

    def test_read_uses_captured_latest_message_and_server_counter(self):
        self.assertTrue(self.act()['confirmed'])
        self.assertEqual(self.store.get_chat(303).unread, 0)
        self.client.mark_read.assert_called_once_with(303, message_id=7, mark=fixture.STAMP,
                                                     expected_session=self.case.context, explicit=True)

    def test_unread_uses_time_minus_one_not_message_id(self):
        self.make_read()
        self.assertTrue(self.act(False)['confirmed'])
        self.client.mark_unread.assert_called_once_with(303, mark=fixture.STAMP-1,
                                                       expected_session=self.case.context)
        self.assertTrue(self.raw['markedAsUnread'])
        self.assertEqual(self.store.get_chat(303).unread, 1)

    def test_noop_does_not_write(self):
        self.assertTrue(self.act(False)['unchanged'])
        self.client.mark_unread.assert_not_called()

    def test_new_message_before_dispatch_requires_reopening_menu(self):
        self.raw['lastMessage'] = self.case.raw(identity=8, timestamp=fixture.STAMP+1)
        self.assertFalse(self.state()['available'])
        with self.assertRaises(ValueError): self.act()
        self.client.mark_read.assert_not_called()

    def test_blocked_or_missing_status_is_not_actionable(self):
        for status in ('BLOCKED', None, 'UNKNOWN'):
            self.raw['status'] = status
            with self.subTest(status=status), self.assertRaises(ValueError): self.act()
        self.client.mark_read.assert_not_called()

    def test_wrong_session_rejected_before_rpc(self):
        self.client._auth_session = self.case.context_for(self.client)
        with self.assertRaises(ValueError): self.act()
        self.client.mark_read.assert_not_called()

    def test_session_replaced_during_ack_is_unknown_and_cache_untouched(self):
        def reply(*args, **kwargs):
            self.client._auth_session = self.case.context_for(self.client)
            return {'mark': fixture.STAMP, 'unread': 0}
        self.client.mark_read.side_effect = reply
        with self.assertRaises(MaxWriteOutcomeUnknown): self.act()
        self.assertEqual(self.store.get_chat(303).unread, 5)
        self.service.on_chat_changed.assert_not_called()

    def test_success_only_ack_is_pending_and_blocks_repeat_and_auto_read(self):
        self.client.mark_read.return_value = {'success': True}
        with self.assertRaises(MaxWriteOutcomeUnknown): self.act()
        self.assertTrue(self.state()['pending'])
        with self.assertRaises(ValueError): self.act()
        with self.assertRaises(ValueError): self.service.mark_chat_read(303, 7, context=self.case.context)
        self.assertEqual(self.client.mark_read.call_count, 1)
        self.assertEqual(self.store.get_chat(303).unread, 5)

    def test_ack_before_captured_message_cannot_confirm_read(self):
        self.client.mark_read.return_value = {'mark': fixture.STAMP - 1, 'unread': 0}
        with self.assertRaises(MaxWriteOutcomeUnknown): self.act()
        self.assertTrue(self.state()['pending'])
        self.assertEqual(self.store.get_chat(303).unread, 5)

    def test_structured_rejection_or_not_started_allows_new_explicit_attempt(self):
        for error in (MaxError('rejected'), MaxWriteNotStarted('not sent')):
            self.client.mark_read.side_effect = error
            with self.assertRaises(type(error)): self.act()
            self.assertFalse(self.state()['pending'])
            self.assertTrue(self.state()['available'])

    def test_generic_network_failure_is_unknown_without_resend(self):
        self.client.mark_read.side_effect = ConnectionError('synthetic')
        with self.assertRaises(MaxWriteOutcomeUnknown): self.act()
        self.assertTrue(self.state()['pending'])
        self.assertEqual(self.client.mark_read.call_count, 1)

    def test_new_incoming_during_ack_keeps_new_unread_counter(self):
        def reply(*args, **kwargs):
            self.service._on_push(self.case.push(self.case.raw(identity=8, timestamp=fixture.STAMP+1)))
            return {'mark': fixture.STAMP, 'unread': 0}
        self.client.mark_read.side_effect = reply
        self.act()
        self.assertEqual(self.store.get_chat(303).unread, 6)
        self.assertEqual(self.raw['participants']['101'], fixture.STAMP)

    def test_explicit_refresh_can_resolve_unknown_without_resending(self):
        self.client.mark_read.return_value = {}
        with self.assertRaises(MaxWriteOutcomeUnknown): self.act()
        observed = copy.deepcopy(self.raw)
        observed.update(newMessages=0, markedAsUnread=False)
        observed['participants'] = {'101': fixture.STAMP, '202': fixture.STAMP}
        self.client.chat_info.return_value = {'chats': [observed]}
        result = self.service.refresh_conversation_read_state(self.context)
        self.assertTrue(result['confirmed'])
        self.assertEqual(self.client.mark_read.call_count, 1)
        self.client.chat_info.assert_called_once_with([303], expected_session=self.case.context)
        self.assertEqual(self.store.get_chat(303).unread, 0)

    def test_readback_of_other_chat_cannot_resolve_pending(self):
        self.client.mark_read.return_value = {}
        with self.assertRaises(MaxWriteOutcomeUnknown): self.act()
        observed = copy.deepcopy(self.raw); observed['id'] = 999
        self.client.chat_info.return_value = {'chats': [observed]}
        with self.assertRaises(ValueError): self.service.refresh_conversation_read_state(self.context)
        self.assertTrue(self.state()['pending'])

    def test_bool_target_rejected(self):
        for invalid in (0, 1, 'read', None):
            with self.subTest(value=invalid), self.assertRaises(ValueError):
                self.service.set_conversation_read(self.context, invalid)
        self.client.mark_read.assert_not_called()


class UnreadPacketTests(unittest.TestCase):
    def test_explicit_read_uses_write_outcome_classification(self):
        client = MaxClient(auto_reconnect=False)
        context = object()
        client._write_object = Mock(return_value={'mark': 100, 'unread': 0})
        client._request_object = Mock(side_effect=AssertionError('explicit read is a write'))
        client.mark_read(303, message_id=7, mark=100, expected_session=context, explicit=True)
        client._write_object.assert_called_once_with(50,
            {'chatId': 303, 'type': 'READ_MESSAGE', 'messageId': 7, 'mark': 100},
            expected_session=context)
        client._request_object.assert_not_called()

    def test_exact_unread_packet_has_no_message_id(self):
        client = MaxClient(auto_reconnect=False)
        context = object()
        client._write_object = Mock(return_value={'mark': 99.5, 'unread': 1})
        client.mark_unread(-(2**63), mark=99.5, expected_session=context)
        client._write_object.assert_called_once_with(50,
            {'chatId': -(2**63), 'type': 'SET_AS_UNREAD', 'mark': 99.5}, expected_session=context)

    def test_invalid_unread_packet_never_writes(self):
        client = MaxClient(auto_reconnect=False)
        client._write_object = Mock()
        for chat, mark in ((True, 1), (2**63, 1), (1, True), (1, float('nan')), (1, float('inf'))):
            with self.subTest(chat=chat, mark=mark), self.assertRaises(ValueError):
                client.mark_unread(chat, mark=mark)
        client._write_object.assert_not_called()
