"""Synthetic own read receipts, ordered counters and account isolation; no RPC."""
import unittest
from unittest.mock import Mock

import test_history_reconnect_review as fixture
from maxclient.core.models import Chat, Message
from maxclient.protocol.client import MaxFrame


class ReadReceiptCoreTests(unittest.TestCase):
    def setUp(self):
        self.case = fixture.HistoryReconnectReviewTests()
        self.case.setUp()
        self.addCleanup(self.case.tearDown)
        self.service, self.store, self.client = self.case.service, self.case.store, self.case.client
        self.context = self.case.context
        self.raw = self.service._server_chats[303]
        self.raw['participants'][101] = fixture.STAMP - 1
        self.raw['markedAsUnread'] = True
        self.client.mark_read = Mock(return_value=self.ack())
        self.client.request = Mock(side_effect=AssertionError('No live request'))
        self.addCleanup(self.client.request.assert_not_called)

    def ack(self, mark=fixture.STAMP, unread=0):
        return {'mark': mark, 'unread': unread, 'success': True}

    def read(self, **kwargs):
        return self.service.mark_chat_read(303, message_id=7, context=self.context, **kwargs)

    def push(self, **kwargs):
        data = {'chatId': 303, 'userId': 101, 'mark': fixture.STAMP, 'unread': 0}
        data.update(kwargs)
        self.service._on_push(MaxFrame(0, 9, 130, b'', data, auth_context=self.context))

    def test_ack_updates_sql_and_canonical_folder_state_with_real_anchor(self):
        self.read()
        self.assertEqual(self.store.get_chat(303).unread, 0)
        self.assertEqual(self.raw['newMessages'], 0)
        self.assertFalse(self.raw['markedAsUnread'])
        self.assertEqual(self.raw['participants']['101'], fixture.STAMP)
        self.client.mark_read.assert_called_once_with(303, message_id=7, mark=fixture.STAMP,
                                                      expected_session=self.context)

    def test_push_from_this_owner_clears_sql_and_canonical_counter(self):
        self.push()
        self.assertEqual(self.service.read_receipt_state(303, context=self.context),
                         {'mark': fixture.STAMP, 'unread': 0, 'markedAsUnread': False})
        self.service.on_chat_changed.assert_called_once_with(303)
        self.client.mark_read.assert_not_called()

    def test_other_user_receipt_does_not_clear_our_unread(self):
        self.push(userId=202)
        self.assertEqual(self.store.get_chat(303).unread, 5)
        self.assertTrue(self.raw['markedAsUnread'])

    def test_older_or_duplicate_read_marker_does_not_restore_counter(self):
        self.push(mark=fixture.STAMP + 100, unread=0)
        self.push(mark=fixture.STAMP, unread=4)
        self.push(mark=fixture.STAMP + 100, unread=3)
        self.assertEqual(self.store.get_chat(303).unread, 0)
        self.assertEqual(self.raw['newMessages'], 0)

    def test_explicit_mark_unread_can_move_marker_backwards(self):
        self.push()
        self.push(mark=fixture.STAMP - 100, unread=3, setAsUnread=True)
        self.assertEqual(self.service.read_receipt_state(303),
                         {'mark': fixture.STAMP - 100, 'unread': 3, 'markedAsUnread': True})

    def test_missing_optional_count_preserves_count_but_updates_marker(self):
        self.service._on_push(MaxFrame(0, 9, 130, b'',
            {'chatId':303, 'userId':101, 'mark':fixture.STAMP}, auth_context=self.context))
        self.assertEqual(self.store.get_chat(303).unread, 5)
        self.assertFalse(self.raw['markedAsUnread'])

    def test_malformed_push_cannot_clear_counters(self):
        for changes in ({'mark': None}, {'mark': True}, {'unread': None}, {'unread': True}, {'unread': -1},
                        {'unread': 2**31}, {'setAsUnread': 'false'}, {'chatId': True}):
            with self.subTest(changes=changes):
                self.push(**changes)
                self.assertEqual(self.store.get_chat(303).unread, 5)

    def test_incomplete_or_malformed_ack_never_optimistically_resets(self):
        for response in ({}, {'success': True}, {'unread': 0}, {'mark': fixture.STAMP},
                         self.ack(unread=True), self.ack(unread=-1), self.ack(mark=True)):
            with self.subTest(response=response):
                self.client.mark_read.return_value = response
                self.read()
                self.assertEqual(self.store.get_chat(303).unread, 5)

    def test_rejected_or_exception_ack_preserves_count(self):
        self.client.mark_read.return_value = {'success': False, 'mark': fixture.STAMP, 'unread': 0}
        with self.assertRaises(ValueError): self.read()
        self.client.mark_read.side_effect = ConnectionError('Synthetic failure')
        with self.assertRaises(ConnectionError): self.read()
        self.assertEqual(self.store.get_chat(303).unread, 5)

    def test_old_session_rejected_before_any_request(self):
        self.client._auth_session = self.case.context_for(self.client)
        with self.assertRaises(ValueError): self.read()
        self.client.mark_read.assert_not_called()

    def test_session_replaced_during_request_cannot_apply_late_count(self):
        def reply(*args, **kwargs):
            self.client._auth_session = self.case.context_for(self.client)
            return self.ack()
        self.client.mark_read.side_effect = reply
        with self.assertRaises(ValueError): self.read()
        self.assertEqual(self.store.get_chat(303).unread, 5)

    def test_owner_changed_during_request_cannot_clear_same_id_replacement_chat(self):
        def reply(*args, **kwargs):
            self.case.change_account()
            self.store.upsert_chat(Chat(id=303, unread=11))
            return self.ack()
        self.client.mark_read.side_effect = reply
        with self.assertRaises(ValueError): self.read()
        self.assertEqual(self.store.get_chat(303).unread, 11)

    def test_new_incoming_during_wait_is_not_erased_by_late_ack(self):
        def reply(*args, **kwargs):
            self.service._on_push(self.case.push(self.case.raw(identity=8, timestamp=fixture.STAMP+1)))
            return self.ack()
        self.client.mark_read.side_effect = reply
        self.read()
        self.assertEqual(self.store.get_chat(303).unread, 6)
        self.assertEqual(self.raw['participants']['101'], fixture.STAMP)

    def test_newer_receipt_during_wait_wins_over_late_ack(self):
        def reply(*args, **kwargs):
            self.push(mark=fixture.STAMP+100, unread=2)
            return self.ack(unread=0)
        self.client.mark_read.side_effect = reply
        self.read()
        self.assertEqual(self.store.get_chat(303).unread, 2)
        self.assertEqual(self.raw['newMessages'], 2)

    def test_newer_explicit_unread_notification_wins_even_if_its_marker_is_lower(self):
        def reply(*args, **kwargs):
            self.push(mark=fixture.STAMP-100, unread=5, setAsUnread=True)
            return self.ack()
        self.client.mark_read.side_effect = reply
        self.read()
        self.assertEqual(self.store.get_chat(303).unread,5)
        self.assertTrue(self.raw['markedAsUnread'])
        self.assertEqual(self.raw['participants']['101'],fixture.STAMP-100)

    def test_unknown_message_and_mismatched_time_reject_without_request(self):
        for kwargs in ({'message_id': 99}, {'message_id': True}, {'message_id': 7, 'mark': fixture.STAMP+1}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.service.mark_chat_read(303, **kwargs, context=self.context)
        self.client.mark_read.assert_not_called()

    def test_signed_int64_message_and_chat_ids_are_preserved(self):
        cid, mid = -(2**63), 2**63-1
        self.store.upsert_chat(Chat(id=cid, unread=1))
        self.store.insert_message(Message(chat_id=cid,id=mid,time_ms=fixture.STAMP, status='received'))
        self.service.mark_chat_read(cid, message_id=mid, context=self.context)
        self.client.mark_read.assert_called_once_with(cid,message_id=mid,mark=fixture.STAMP,
                                                      expected_session=self.context)
        self.assertEqual(self.store.get_chat(cid).unread, 0)

    def test_pending_message_never_uses_local_timestamp_as_read_anchor(self):
        self.store.insert_message(Message(chat_id=303,id=88,time_ms=fixture.STAMP,local_id='draft',status='pending'))
        with self.assertRaises(ValueError):
            self.service.mark_chat_read(303,message_id=88,context=self.context)
        self.client.mark_read.assert_not_called()

    def test_fractional_timestamp_is_not_rounded_or_rejected(self):
        timestamp = fixture.STAMP + 0.5
        self.store.insert_message(Message(chat_id=303,id=88,time_ms=timestamp,status='received'))
        self.client.mark_read.return_value = self.ack(mark=timestamp)
        self.service.mark_chat_read(303,message_id=88,mark=timestamp,context=self.context)
        self.client.mark_read.assert_called_once_with(303,message_id=88,mark=timestamp,
                                                      expected_session=self.context)
        self.assertEqual(self.service.read_receipt_state(303)['mark'], timestamp)
