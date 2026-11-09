"""Confirmed66/142 deletion survives stale reads and edits, without real IO."""
import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

import test_history_reconnect_review as fixture
from maxclient.core.models import Chat, Message
from maxclient.core.store import Store
from maxclient.protocol.client import MaxError, MaxFrame, MaxWriteOutcomeUnknown


class MessageDeleteTombstoneTests(unittest.TestCase):
    def setUp(self):
        self.case = fixture.HistoryReconnectReviewTests()
        self.case.setUp()
        self.addCleanup(self.case.tearDown)
        self.service, self.store, self.client = self.case.service, self.case.store, self.case.client
        self.context = self.case.context
        self.service.on_message_changed = Mock()
        self.seed()
        self.client.delete_messages = Mock(return_value={'chatId': 303, 'messageIds': [7]})

    def seed(self, identity=7, *, timestamp=None, text='synthetic'):
        raw = self.case.raw(identity, fixture.STAMP if timestamp is None else timestamp, text=text)
        self.store.insert_message(Message.from_server(303, raw, 101))
        return raw

    def delete(self, ids=(7,)):
        return self.service.delete_messages(303, list(ids), context=self.context)

    def push_delete(self, ids=(7,), *, latest=None, chat=None):
        raw = copy.deepcopy(chat or self.case.chat())
        if latest is None:
            raw.pop('lastMessage', None)
        else:
            raw['lastMessage'] = latest
        self.service._on_push(MaxFrame(0, 1, 142, b'',
            {'chat': raw, 'messageIds': list(ids), 'ttl': False}, auth_context=self.context))

    def test_ack66_then_old_history_does_not_restore_row_or_preview(self):
        self.delete()
        self.service._cache_history_messages(303, [self.case.raw()])
        self.assertTrue(self.store.message_was_deleted(303, 7))
        self.assertIsNone(self.store.message(303, 7))
        self.assertIsNone(self.store.get_chat(303).last_preview)
        self.client.delete_messages.assert_called_once()

    def test_push142_then_old128_has_no_delivery_unread_or_preview(self):
        self.push_delete()
        before = self.store.get_chat(303).unread
        self.service.on_message_changed.reset_mock()
        self.service.on_chat_changed.reset_mock()
        self.service._on_push(self.case.push())
        self.assertIsNone(self.store.message(303, 7))
        self.assertIsNone(self.store.get_chat(303).last_preview)
        self.assertEqual(self.store.get_chat(303).unread, before)
        self.service.on_message.assert_not_called()
        self.service.on_message_changed.assert_not_called()
        self.service.on_chat_changed.assert_not_called()
        self.client.chat_info.assert_not_called()

    def test_late_edit_ack_after142_does_not_restore_deleted_message(self):
        def reply(*_args, **_kwargs):
            self.push_delete()
            return {'chatId': 303, 'message': self.case.raw(text='late edit'), 'chat': self.case.chat()}
        self.client.edit_message = Mock(side_effect=reply)
        self.service.edit_message(303, 7, 'requested edit', context=self.context)
        self.assertIsNone(self.store.message(303, 7))
        self.assertNotIn('lastMessage', self.service._server_chats[303])
        self.assertIsNone(self.store.get_chat(303).last_preview)

    def test_direct_insert_and_ack_cannot_bypass_tombstone(self):
        self.delete()
        self.assertFalse(self.store.insert_message(Message.from_server(303, self.case.raw(), 101)))
        self.assertEqual(self.store.insert_messages([Message.from_server(303, self.case.raw(), 101)]), 0)
        removed = self.store.apply_message_ack(303, self.case.raw(), my_id=101)
        self.assertEqual(removed.status, 'removed')
        self.assertIsNone(self.store.message(303, 7))

    def test_old_chat_metadata_cannot_restore_deleted_preview(self):
        self.delete()
        stale = self.case.chat()
        self.service._remember_chat_info({'chats': [stale]}, my_id=101)
        self.assertNotIn('lastMessage', self.service._server_chats[303])
        self.assertIsNone(self.store.get_chat(303).last_preview)
        self.assertIn('lastMessage', stale)  # Supplied DTO was not mutated.

    def test_stale_deleted_metadata_preserves_a_newer_live_anchor(self):
        self.delete()
        newer = self.case.raw(8, fixture.STAMP + 100, text='newer live')
        self.service._on_push(self.case.push(newer))
        self.service._remember_chat_info({'chats': [self.case.chat()]}, my_id=101)
        self.assertEqual(self.service._server_chats[303]['lastMessage']['id'], 8)
        self.assertEqual(self.store.get_chat(303).last_preview, 'newer live')

    def test_push142_previous_message_replaces_deleted_anchor_even_when_older(self):
        previous = self.seed(6, timestamp=fixture.STAMP - 100, text='previous')
        self.push_delete(latest=previous)
        self.assertIsNone(self.store.message(303, 7))
        self.assertEqual(self.service._server_chats[303]['lastMessage']['id'], 6)
        self.assertEqual(self.store.get_chat(303).last_preview, 'previous')
        self.assertEqual(self.store.get_chat(303).last_time_ms, fixture.STAMP - 100)

    def test_push142_uncached_previous_message_replaces_deleted_preview(self):
        previous = self.case.raw(6, fixture.STAMP - 100, text='uncached previous')
        self.push_delete(latest=previous)
        self.assertEqual(self.store.get_chat(303).last_preview, 'uncached previous')
        self.assertEqual(self.service._server_chats[303]['lastMessage']['id'], 6)

    def test_push142_omitted_last_message_clears_deleted_preview(self):
        self.push_delete()
        self.assertNotIn('lastMessage', self.service._server_chats[303])
        self.assertIsNone(self.store.get_chat(303).last_preview)
        self.assertIsNone(self.store.get_chat(303).last_time_ms)

    def test_ack66_uses_remaining_cached_message_for_preview(self):
        self.seed(6, timestamp=fixture.STAMP - 100, text='previous cached')
        self.delete()
        self.assertEqual(self.store.get_chat(303).last_preview, 'previous cached')
        self.assertIsNone(self.store.message(303, 7))

    def test_ack66_and142_preserve_newer_pending_preview(self):
        for operation in (self.delete, self.push_delete):
            with self.subTest(operation=operation.__name__):
                self.store.insert_message(Message(chat_id=303, time_ms=fixture.STAMP + 100,
                    text='new pending', status='pending', local_id=operation.__name__))
                self.store.update_chat_preview(303, fixture.STAMP + 100, 'new pending')
                operation()
                self.assertEqual(self.store.get_chat(303).last_preview, 'new pending')
                self.assertEqual(self.store.get_chat(303).last_time_ms, fixture.STAMP + 100)
                self.assertTrue(any(m.local_id == operation.__name__ for m in self.store.messages(303)))

    def test_delete_older_message_does_not_roll_back_newer_delivered_preview(self):
        newer = self.case.raw(8, fixture.STAMP + 100, text='newer live')
        self.service._on_push(self.case.push(newer))
        self.delete()
        self.assertEqual(self.store.get_chat(303).last_preview, 'newer live')
        self.assertEqual(self.service._server_chats[303]['lastMessage']['id'], 8)
        self.assertIsNotNone(self.store.message(303, 8))

    def test_push142_without_last_message_preserves_unaffected_new_delivery(self):
        newer = self.case.raw(8, fixture.STAMP + 100, text='newer live')
        self.service._on_push(self.case.push(newer))
        self.push_delete()
        self.assertEqual(self.store.get_chat(303).last_preview, 'newer live')
        self.assertEqual(self.service._server_chats[303]['lastMessage']['id'], 8)
        self.assertIsNone(self.store.message(303, 7))

    def test_legacy_numeric_string_metadata_cannot_bypass_deleted_id_filter(self):
        self.delete()
        raw = self.case.raw()
        raw['id'] = '7'
        self.service._cache_history_messages(303, [raw])
        chat = self.case.chat(); chat['lastMessage'] = raw
        self.service._remember_chat_info({'chats': [chat]}, my_id=101)
        self.assertIsNone(self.store.message(303, 7))
        self.assertIsNone(self.store.get_chat(303).last_preview)
        self.assertNotIn('lastMessage', self.service._server_chats[303])

    def test_partial_ack_tombstones_only_affected_subset(self):
        self.seed(8, timestamp=fixture.STAMP + 100)
        self.delete((7, 8))
        self.assertTrue(self.store.message_was_deleted(303, 7))
        self.assertFalse(self.store.message_was_deleted(303, 8))
        self.assertIsNotNone(self.store.message(303, 8))

    def test_unknown_rejected_or_foreign_ack_never_creates_tombstone(self):
        for result in (MaxWriteOutcomeUnknown('unknown'), MaxError('rejected'),
                       {'chatId': 303, 'messageIds': [7, 999]}, {'chatId': 404, 'messageIds': [7]}):
            with self.subTest(result_type=type(result).__name__):
                self.client.delete_messages.side_effect = result if isinstance(result, Exception) else None
                self.client.delete_messages.return_value = result
                with self.assertRaises((MaxError, MaxWriteOutcomeUnknown)):
                    self.delete()
                self.assertFalse(self.store.message_was_deleted(303, 7))
                self.assertIsNotNone(self.store.message(303, 7))

    def test_owner_switch_during_ack_cannot_tombstone_new_account(self):
        def reply(*_args, **_kwargs):
            self.case.change_account()
            self.store.insert_message(Message(chat_id=303, id=7, text='other owner', time_ms=1))
            return {'chatId': 303, 'messageIds': [7]}
        self.client.delete_messages.side_effect = reply
        with self.assertRaises(MaxWriteOutcomeUnknown):
            self.delete()
        self.assertFalse(self.store.message_was_deleted(303, 7))
        self.assertEqual(self.store.message(303, 7).text, 'other owner')
        self.store.bind_owner(101)
        self.assertFalse(self.store.message_was_deleted(303, 7))

    def test_atomic_delete_failure_leaves_row_preview_and_no_tombstone(self):
        before = self.store.get_chat(303)
        self.store._db.execute("CREATE TEMP TRIGGER deny_delete BEFORE DELETE ON messages "
                               "BEGIN SELECT RAISE(ABORT, 'synthetic delete failure'); END")
        with self.assertRaises(MaxWriteOutcomeUnknown):
            self.delete()
        self.assertFalse(self.store.message_was_deleted(303, 7))
        self.assertIsNotNone(self.store.message(303, 7))
        self.assertEqual(self.store.get_chat(303), before)
        self.assertIn('lastMessage', self.service._server_chats[303])

    def test_unknown_owner_and_invalid_ids_cannot_write_tombstones(self):
        for owner, chat, ids in ((404, 303, [7]), (True, 303, [7]), (101, True, [7]),
                                 (101, 303, [True]), (101, 303, [2**63])):
            with self.subTest(owner=owner, chat=chat), self.assertRaises(ValueError):
                self.store.confirm_message_deletions(owner, chat, ids)
        self.assertFalse(self.store.message_was_deleted(303, 7))

    def test_metadata_only_event_cannot_revive_deleted_conversation(self):
        self.store.clear_confirmed_history(101, 303, fixture.STAMP, deleted=True)
        chat = self.case.chat(); chat.pop('lastMessage')
        chat['lastEventTime'] = fixture.STAMP + 100
        self.service._remember_chat_info({'chats': [chat]}, my_id=101)
        self.assertEqual(self.service.list_chats(), [])
        self.assertIsNone(self.store.get_chat(303).last_preview)
        self.assertTrue(self.store.history_boundary(303)['deleted'])
        self.assertEqual(self.service._server_chats[303]['lastEventTime'], fixture.STAMP + 100)

    def test_actual_new_message_can_revive_deleted_conversation(self):
        self.store.clear_confirmed_history(101, 303, fixture.STAMP, deleted=True)
        chat = self.case.chat()
        chat['lastMessage'] = self.case.raw(8, fixture.STAMP + 100, text='new message')
        self.service._remember_chat_info({'chats': [chat]}, my_id=101)
        self.assertEqual([c.id for c in self.service.list_chats()], [303])
        self.assertEqual(self.store.get_chat(303).last_preview, 'new message')
        self.assertEqual(self.store.history_boundary(303)['cutoff'], fixture.STAMP)

    def test_metadata_omission_preserves_new_message_already_in_sql(self):
        self.store.clear_confirmed_history(101, 303, fixture.STAMP, deleted=True)
        self.seed(8, timestamp=fixture.STAMP + 100, text='actual newer row')
        self.store.update_chat_preview(303, fixture.STAMP + 100, 'actual newer row')
        chat = self.case.chat(); chat.pop('lastMessage')
        chat['lastEventTime'] = fixture.STAMP + 200
        self.service._remember_chat_info({'chats': [chat]}, my_id=101)
        self.assertEqual([c.id for c in self.service.list_chats()], [303])
        self.assertEqual(self.store.get_chat(303).last_preview, 'actual newer row')
        self.assertEqual(self.store.get_chat(303).last_time_ms, fixture.STAMP + 100)

    def test_reopen_owner_chat_and_zero_id_isolation(self):
        root = Path(__file__).resolve().parents[1] / '.test-tmp'
        root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='message-tombstone-', dir=root) as temporary:
            path = Path(temporary) / 'synthetic.sqlite'
            store = Store(path)
            try:
                store.bind_owner(101)
                store.confirm_message_deletions(101, 0, [0])
            finally:
                store.close()
            store = Store(path)
            try:
                self.assertTrue(store.message_was_deleted(0, 0))
                self.assertFalse(store.insert_message(Message(chat_id=0, id=0, text='old', time_ms=1)))
                self.assertTrue(store.insert_message(Message(chat_id=303, id=0, text='other chat', time_ms=1)))
                store.bind_owner(404)
                self.assertFalse(store.message_was_deleted(0, 0))
                self.assertTrue(store.insert_message(Message(chat_id=0, id=0, text='other owner', time_ms=1)))
                store.bind_owner(101)
                self.assertTrue(store.message_was_deleted(0, 0))
            finally:
                store.close()


if __name__ == '__main__':
    unittest.main()
