"""Independent receipt-order regressions with in-memory data and no RPC.

Only synthetic transport callbacks run. Real account/session files are unused.
"""
import copy
import unittest
from unittest.mock import Mock

import test_history_reconnect_review as fixture
from maxclient.core.models import Chat
from maxclient.protocol.client import MaxError, MaxFrame, MaxTimeout, MaxWriteOutcomeUnknown


class ChatReadActionsReviewTests(unittest.TestCase):
    def setUp(self):
        self.case = fixture.HistoryReconnectReviewTests()
        self.case.setUp()
        self.addCleanup(self.case.tearDown)
        self.service, self.store, self.client = self.case.service, self.case.store, self.case.client
        self.context = self.case.context
        self.raw = self.service._server_chats[303]
        self.raw.update(status='ACTIVE', owner=101, created=0,
                        lastEventTime=fixture.STAMP, markedAsUnread=True)
        self.raw['participants'][101] = fixture.STAMP - 1
        self.client.mark_read = Mock(return_value=self.ack())
        self.client.mark_unread = Mock(return_value=self.ack(mark=fixture.STAMP - 1, unread=1))
        self.client.request = Mock(side_effect=AssertionError('No live request'))
        self.addCleanup(self.client.request.assert_not_called)

    def ack(self, mark=fixture.STAMP, unread=0):
        return {'mark': mark, 'unread': unread, 'success': True}

    def read(self):
        return self.service.mark_chat_read(303, message_id=7, context=self.context)

    def manual_context(self):
        return self.service.capture_conversation_read(
            self.service.conversation_context(303, activate_files=False))

    def set_initial_read(self):
        self.raw.update(markedAsUnread=False, newMessages=0)
        self.raw['participants'] = {'101': fixture.STAMP, '202': fixture.STAMP}
        self.store.set_unread(303, 0)

    def push_receipt(self, mark, unread, *, marked=False):
        self.service._on_push(MaxFrame(0, 9, 130, b'', {
            'chatId': 303, 'userId': 101, 'mark': mark,
            'unread': unread, 'setAsUnread': marked,
        }, auth_context=self.context))

    def test_aba_read_then_unread_push_cannot_be_overwritten_by_older_read_ack(self):
        def reply(*args, **kwargs):
            self.push_receipt(fixture.STAMP + 10, 0)
            self.push_receipt(fixture.STAMP - 1, 2, marked=True)
            return self.ack()
        self.client.mark_read.side_effect = reply
        self.read()
        self.assertEqual(self.service.read_receipt_state(303, context=self.context),
                         {'mark': fixture.STAMP - 1, 'unread': 2, 'markedAsUnread': True})

    def test_an_inflight_read_prevents_a_second_transport_request_for_same_chat(self):
        entered = False
        def reply(*args, **kwargs):
            nonlocal entered
            if not entered:
                entered = True
                try:
                    self.read()
                except (ValueError, MaxError):
                    pass
            return self.ack()
        self.client.mark_read.side_effect = reply
        self.read()
        self.assertEqual(self.client.mark_read.call_count, 1)

    def test_manual_read_timeout_is_pending_and_blocks_automatic_receipts(self):
        context = self.manual_context()
        self.client.mark_read.side_effect = MaxTimeout('Synthetic sent packet timeout')
        with self.assertRaises(MaxWriteOutcomeUnknown):
            self.service.set_conversation_read(context, True)
        self.assertTrue(self.service.get_conversation_read_state(context)['pending'])
        with self.assertRaises(ValueError):
            self.read()
        self.assertEqual(self.client.mark_read.call_count, 1)
        self.assertEqual(self.store.get_chat(303).unread, 5)

    def test_unknown_unread_blocks_auto_and_manual_retries_after_same_owner_reconnect(self):
        self.set_initial_read()
        context = self.manual_context()
        self.client.mark_unread.side_effect = MaxWriteOutcomeUnknown('Synthetic lost ACK')
        with self.assertRaises(MaxWriteOutcomeUnknown):
            self.service.set_conversation_read(context, False)
        self.client._auth_session = self.case.context_for(self.client)
        self.context = self.client._auth_session
        fresh = self.manual_context()
        self.assertTrue(self.service.get_conversation_read_state(fresh)['pending'])
        for write in (lambda: self.read(), lambda: self.service.set_conversation_read(fresh, False)):
            with self.assertRaises(ValueError):
                write()
        self.client.mark_unread.assert_called_once()
        self.client.mark_read.assert_not_called()

    def test_manual_unread_during_automatic_read_cannot_start_a_second_write(self):
        self.set_initial_read()
        def reply(*args, **kwargs):
            context = self.manual_context()
            with self.assertRaises(ValueError):
                self.service.set_conversation_read(context, False)
            return self.ack()
        self.client.mark_read.side_effect = reply
        self.read()
        self.client.mark_read.assert_called_once()
        self.client.mark_unread.assert_not_called()

    def test_automatic_read_during_manual_unread_cannot_reverse_it(self):
        self.set_initial_read()
        context = self.manual_context()
        def reply(*args, **kwargs):
            with self.assertRaises(ValueError):
                self.read()
            return self.ack(mark=fixture.STAMP - 1, unread=1)
        self.client.mark_unread.side_effect = reply
        self.service.set_conversation_read(context, False)
        self.client.mark_unread.assert_called_once()
        self.client.mark_read.assert_not_called()
        self.assertTrue(self.service.read_receipt_state(303)['markedAsUnread'])

    def test_unknown_unread_uses_readback_only_and_does_not_repeat_write(self):
        self.set_initial_read()
        context = self.manual_context()
        self.client.mark_unread.side_effect = MaxWriteOutcomeUnknown('Synthetic lost ACK')
        with self.assertRaises(MaxWriteOutcomeUnknown):
            self.service.set_conversation_read(context, False)
        observed = copy.deepcopy(self.raw)
        observed.update(markedAsUnread=True, newMessages=1)
        observed['participants']['101'] = fixture.STAMP - 1
        self.client.chat_info.return_value = {'chats': [observed]}
        result = self.service.refresh_conversation_read_state(context)
        self.assertFalse(result['pending'])
        self.client.mark_unread.assert_called_once()
        self.client.mark_read.assert_not_called()
        self.client.chat_info.assert_called_once_with([303], expected_session=self.context)

    def test_manual_read_ack_keeps_new_arrival_count(self):
        context = self.manual_context()
        def reply(*args, **kwargs):
            self.service._on_push(self.case.push(self.case.raw(identity=8, timestamp=fixture.STAMP + 1)))
            return self.ack()
        self.client.mark_read.side_effect = reply
        self.service.set_conversation_read(context, True)
        self.assertEqual(self.store.get_chat(303).unread, 6)
        self.assertFalse(self.service.get_conversation_read_state(context)['available'])

    def test_newer_read_push_wins_over_manual_unread_ack(self):
        self.set_initial_read()
        context = self.manual_context()
        def reply(*args, **kwargs):
            self.push_receipt(fixture.STAMP + 1, 0)
            return self.ack(mark=fixture.STAMP - 1, unread=1)
        self.client.mark_unread.side_effect = reply
        self.service.set_conversation_read(context, False)
        self.assertEqual(self.service.read_receipt_state(303),
                         {'mark': fixture.STAMP + 1, 'unread': 0, 'markedAsUnread': False})

    def test_manual_late_ack_cannot_change_same_id_chat_of_another_owner(self):
        context = self.manual_context()
        def reply(*args, **kwargs):
            self.case.change_account()
            self.store.upsert_chat(Chat(id=303, unread=11))
            return self.ack()
        self.client.mark_read.side_effect = reply
        with self.assertRaises(MaxWriteOutcomeUnknown):
            self.service.set_conversation_read(context, True)
        self.assertEqual(self.store.get_chat(303).unread, 11)

    def test_equal_mark_readback_clears_unknown_read_and_counter(self):
        # The server may confirm the same own mark after a lost response while
        # the local unread counter still reflects the earlier chat snapshot.
        self.raw.update(markedAsUnread=False, newMessages=5)
        self.raw['participants'] = {'101': fixture.STAMP, '202': fixture.STAMP}
        context = self.manual_context()
        self.client.mark_read.side_effect = MaxWriteOutcomeUnknown('Synthetic lost ACK')
        with self.assertRaises(MaxWriteOutcomeUnknown):
            self.service.set_conversation_read(context, True)
        observed = copy.deepcopy(self.raw)
        observed['newMessages'] = 0
        self.client.chat_info.return_value = {'chats': [observed]}
        result = self.service.refresh_conversation_read_state(context)
        self.assertFalse(result['pending'])
        self.assertEqual(self.store.get_chat(303).unread, 0)
        self.client.mark_read.assert_called_once()

    def test_unknown_read_is_resolved_at_captured_frontier_with_newer_unread_message(self):
        context = self.manual_context()
        self.client.mark_read.side_effect = MaxWriteOutcomeUnknown('Synthetic lost ACK')
        with self.assertRaises(MaxWriteOutcomeUnknown):
            self.service.set_conversation_read(context, True)
        self.service._on_push(self.case.push(self.case.raw(identity=8, timestamp=fixture.STAMP + 1)))
        fresh = self.manual_context()
        observed = copy.deepcopy(self.raw)
        observed.update(markedAsUnread=False, newMessages=1)
        observed['participants'] = {'101': fixture.STAMP, '202': fixture.STAMP}
        self.client.chat_info.return_value = {'chats': [observed]}
        result = self.service.refresh_conversation_read_state(fresh)
        self.assertFalse(result['pending'])
        self.assertEqual(self.store.get_chat(303).unread, 1)
        self.assertFalse(self.service.read_receipt_state(303)['markedAsUnread'])
        self.client.mark_read.assert_called_once()

    def test_refresh_without_pending_does_not_start_unnecessary_readback(self):
        context = self.manual_context()
        try:
            self.service.refresh_conversation_read_state(context)
        except ValueError:
            pass
        self.client.chat_info.assert_not_called()
        self.client.mark_read.assert_not_called()
        self.client.mark_unread.assert_not_called()

    def test_unknown_readback_reserves_chat_against_another_refresh_and_write(self):
        self.set_initial_read()
        context = self.manual_context()
        self.client.mark_unread.side_effect = MaxWriteOutcomeUnknown('Synthetic lost ACK')
        with self.assertRaises(MaxWriteOutcomeUnknown):
            self.service.set_conversation_read(context, False)
        observed = copy.deepcopy(self.raw)
        observed.update(markedAsUnread=True, newMessages=1)
        observed['participants']['101'] = fixture.STAMP - 1
        def reply(*args, **kwargs):
            for operation in (lambda: self.service.refresh_conversation_read_state(context),
                              lambda: self.service.set_conversation_read(context, False), self.read):
                with self.assertRaises(ValueError):
                    operation()
            return {'chats': [observed]}
        self.client.chat_info.side_effect = reply
        result = self.service.refresh_conversation_read_state(context)
        self.assertFalse(result['pending'])
        self.client.chat_info.assert_called_once()
        self.client.mark_unread.assert_called_once()
        self.client.mark_read.assert_not_called()

    def test_stale_readback_frontier_cannot_erase_already_received_new_message(self):
        context = self.manual_context()
        observed = copy.deepcopy(self.raw)
        observed.update(markedAsUnread=False, newMessages=0)
        observed['participants'] = {'101': fixture.STAMP, '202': fixture.STAMP}
        self.client.mark_read.side_effect = MaxWriteOutcomeUnknown('Synthetic lost ACK')
        with self.assertRaises(MaxWriteOutcomeUnknown):
            self.service.set_conversation_read(context, True)
        self.service._on_push(self.case.push(self.case.raw(identity=8, timestamp=fixture.STAMP + 1)))
        self.client.chat_info.return_value = {'chats': [observed]}
        self.service.refresh_conversation_read_state(self.manual_context())
        self.assertEqual(self.store.get_chat(303).unread, 6)
        self.assertEqual(self.raw['lastMessage']['id'], 8)
        self.client.mark_read.assert_called_once()


if __name__ == '__main__':
    unittest.main()
