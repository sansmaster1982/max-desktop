"""Session-bound cache change delivery; fake transport and memory SQLite only."""
from dataclasses import FrozenInstanceError, replace
import unittest
from unittest.mock import Mock

import test_history_reconnect_review as fixture
from maxclient.core.message_delivery import MessageChange
from maxclient.core.models import Message
from maxclient.protocol.client import MaxFrame


class MessageChangeContextTests(unittest.TestCase):
    def setUp(self):
        self.case = fixture.HistoryReconnectReviewTests()
        self.case.setUp()
        self.addCleanup(self.case.tearDown)
        self.service, self.store, self.client = self.case.service, self.case.store, self.case.client
        self.service.on_message_changed = Mock()

    def send(self, opcode, data):
        self.service._on_push(MaxFrame(0, 8, opcode, b'', data, auth_context=self.case.context))

    def seed(self, *, chat_id=303, message_id=7, version=100, reactions=None):
        raw = self.case.raw(message_id, text='Exact synthetic original')
        raw['updateTime'] = version
        if reactions is not None:
            raw['reactionInfo'] = reactions
        self.send(128, {'chatId': chat_id, 'message': raw})
        return raw

    def event(self):
        event = self.service.on_message_changed.call_args.args[0]
        self.assertIsInstance(event, MessageChange)
        self.assertTrue(self.service.is_current_message_change(event))
        return event

    def assert_no_read_rpc(self):
        self.client.chat_info.assert_not_called()
        self.client.contact_info.assert_not_called()
        self.client.chat_history_window.assert_not_called()

    def test_new_delivery_and_changed_duplicate_have_separate_callbacks_including_zero(self):
        raw = self.seed(chat_id=0, message_id=0)
        self.service.on_message_changed.assert_not_called()
        self.assertEqual(self.service.on_message.call_count, 1)
        updated = dict(raw, text='Exact synthetic edited', updateTime=200.25)
        observed = []
        self.service.on_message_changed.side_effect = lambda event: observed.append(
            self.service.cached_messages_for_change(event)[0].text)
        self.send(128, {'chatId': 0, 'message': updated})
        event = self.event()
        self.assertEqual((event.chat_id, event.message_ids), (0, (0,)))
        self.assertEqual(observed, [updated['text']])  # Cache already committed at callback.
        self.assertEqual(self.service.on_message.call_count, 1)
        self.assertEqual(self.store.get_chat(0).unread, 1)
        self.assert_no_read_rpc()

    def test_identical_stale_or_unversioned_known_replay_has_no_changed_event(self):
        raw = self.seed(version=200)
        self.send(128, {'chatId': 303, 'message': raw})
        self.send(128, {'chatId': 303, 'message': dict(raw, text='Older', updateTime=100)})
        unknown = dict(raw, text='Unversioned older'); unknown.pop('updateTime')
        self.send(128, {'chatId': 303, 'message': unknown})
        self.service.on_message_changed.assert_not_called()
        self.assertEqual(self.service.on_message.call_count, 1)
        self.assertEqual(self.store.message(303, 7).text, raw['text'])
        self.assert_no_read_rpc()

    def test_155_replaces_counters_and_preserves_viewer_selection(self):
        self.seed(reactions={'totalCount': 2, 'counters': [{'reaction': 'A', 'count': 2}],
                             'yourReaction': 'A'})
        self.send(155, {'chatId': 303, 'messageId': 7, 'totalCount': 3,
                        'counters': [{'reaction': 'B', 'count': 3}]})
        actual = self.service.cached_messages_for_change(self.event())[7].reaction_info
        self.assertEqual(actual, {'totalCount': 3, 'counters': [{'reaction': 'B', 'count': 3}],
                                  'yourReaction': 'A'})
        self.send(155, {'chatId': 303, 'messageId': 7, 'totalCount': 0})
        self.assertEqual(self.store.message(303, 7).reaction_info,
                         {'totalCount': 0, 'counters': [], 'yourReaction': 'A'})
        self.assertEqual(self.service.on_message_changed.call_count, 2)
        self.send(155, {'chatId': 303, 'messageId': 7, 'totalCount': 0})
        self.assertEqual(self.service.on_message_changed.call_count, 2)
        self.assertEqual(self.service.on_message.call_count, 1)
        self.assert_no_read_rpc()

    def test_156_replaces_selection_and_absent_info_clears_snapshot(self):
        self.seed(reactions={'totalCount': 2, 'counters': [{'reaction': 'A', 'count': 2}],
                             'yourReaction': 'A'})
        self.send(156, {'chatId': 303, 'messageId': 7, 'reactionInfo': {
            'totalCount': 1, 'counters': [{'reaction': 'B', 'count': 1}], 'yourReaction': 'B'}})
        self.assertEqual(self.service.cached_messages_for_change(self.event())[7].reaction_info,
                         {'totalCount': 1, 'counters': [{'reaction': 'B', 'count': 1}], 'yourReaction': 'B'})
        self.send(156, {'chatId': 303, 'messageId': 7})
        self.assertEqual(self.store.message(303, 7).reaction_info, {})
        self.assertEqual(self.service.on_message_changed.call_count, 2)
        self.send(156, {'chatId': 303, 'messageId': 7, 'reactionInfo': {}})
        self.assertEqual(self.service.on_message_changed.call_count, 2)
        self.assertEqual(self.service.on_message.call_count, 1)
        self.assert_no_read_rpc()

    def test_source_default_count_zero_and_zero_ids_are_supported(self):
        self.seed(chat_id=0, message_id=0, reactions={'yourReaction': 'A'})
        self.send(155, {'chatId': 0, 'messageId': 0, 'totalCount': 0, 'counters': [{'reaction': 'A'}]})
        self.assertEqual(self.store.message(0, 0).reaction_info,
                         {'totalCount': 0, 'counters': [{'reaction': 'A', 'count': 0}], 'yourReaction': 'A'})
        self.send(156, {'chatId': 0, 'messageId': 0, 'reactionInfo': {'counters': [{'reaction': 'B'}]}})
        self.assertEqual(self.service.cached_messages_for_change(self.event())[0].reaction_info,
                         {'counters': [{'reaction': 'B', 'count': 0}]})

    def test_confirmed_delete_emits_only_exact_ids_and_subset_absence(self):
        self.seed(chat_id=0, message_id=0)
        self.seed(chat_id=0, message_id=10)
        self.service.on_message.reset_mock()
        self.send(142, {'chat': {'id': 0, 'type': 'DIALOG', 'participants': {101: 1}},
                        'messageIds': [0, 0, 999], 'ttl': False})
        event = self.event()
        self.assertEqual(event.message_ids, (0, 999))
        self.assertEqual(self.service.cached_messages_for_change(event), {0: None, 999: None})
        self.assertIsNotNone(self.store.message(0, 10))
        self.service.on_message.assert_not_called()
        self.assert_no_read_rpc()

    def test_thread_or_malformed_events_do_not_mutate_ordinary_rows(self):
        self.seed(reactions={'totalCount': 2, 'counters': [{'reaction': 'A', 'count': 2}],
                             'yourReaction': 'A'})
        baseline = self.store.message(303, 7)
        cases = [
            (128, {'chatId': 303, 'postId': 9, 'message': dict(self.case.raw(),
                text='Thread duplicate', updateTime=300)}),
            (128, {'chatId': 303, 'postId': 0, 'message': self.case.raw(identity=999)}),
            (155, {'chatId': 303, 'messageId': 7, 'totalCount': 0, 'postId': 0}),
            (156, {'chatId': 303, 'messageId': 7, 'postId': 9}),
            (142, {'chat': self.case.chat(), 'messageIds': [7], 'ttl': False, 'postId': 0}),
            (155, {'chatId': True, 'messageId': 7, 'totalCount': 0}),
            (155, {'chatId': 303, 'messageId': '7', 'totalCount': 0}),
            (155, {'chatId': 303, 'messageId': 7, 'totalCount': True}),
            (155, {'chatId': 303, 'messageId': 7, 'totalCount': 1,
                   'counters': [{'reaction': 'A', 'count': True}]}),
            (156, {'chatId': 303, 'messageId': 7, 'reactionInfo': None}),
            (156, {'chatId': 303, 'messageId': 7, 'reactionInfo': {'yourReaction': None}}),
            (142, {'chat': self.case.chat(), 'messageIds': [7, '8'], 'ttl': False}),
            (142, {'chat': self.case.chat(), 'messageIds': [7], 'ttl': 0}),
            (142, {'chat': self.case.chat(), 'messageIds': [7]}),
        ]
        for opcode, data in cases:
            with self.subTest(opcode=opcode):
                self.send(opcode, data)
                self.assertEqual(self.store.message(303, 7), baseline)
        self.assertIsNone(self.store.message(303, 999))
        self.assertEqual(self.service.on_message.call_count, 1)
        self.service.on_message_changed.assert_not_called()
        self.assert_no_read_rpc()

    def test_reaction_for_uncached_message_does_not_fetch_or_emit(self):
        self.send(155, {'chatId': 303, 'messageId': 999, 'totalCount': 1})
        self.send(156, {'chatId': 303, 'messageId': 999, 'reactionInfo': {'yourReaction': 'A'}})
        self.assertIsNone(self.store.message(303, 999))
        self.service.on_message_changed.assert_not_called()
        self.service.on_message.assert_not_called()
        self.assert_no_read_rpc()

    def test_exact_reader_does_not_treat_old_target_outside_last300_as_deleted(self):
        self.store.insert_messages([Message.from_server(303, self.case.raw(identity, identity), 101)
                                    for identity in range(321)])
        self.assertNotIn(0, {row.id for row in self.store.messages(303)})
        event = MessageChange(101, self.client, self.case.context, 303, (0, 999))
        result = self.service.cached_messages_for_change(event)
        self.assertEqual(set(result), {0, 999})
        self.assertEqual(result[0].id, 0)
        self.assertIsNone(result[999])
        self.assert_no_read_rpc()

    def test_frozen_body_free_event_rejects_invalid_ids_and_expired_context(self):
        self.seed()
        self.send(128, {'chatId': 303, 'message': dict(self.case.raw(), updateTime=200, text='Updated')})
        event = self.event()
        with self.assertRaises(FrozenInstanceError): event.chat_id = 404
        rendered = repr(event)
        for secret in ('Updated', fixture.DEVICE, 'source_client=', 'session_context=', 'message='):
            self.assertNotIn(secret, rendered)
        self.assertFalse(hasattr(event, 'message'))
        for ids in ((), (True,), ('7',), [7], (7, 7)):
            self.assertFalse(self.service.is_current_message_change(replace(event, message_ids=ids)))
        self.client._auth_session = self.case.context_for(self.client)
        self.assertFalse(self.service.is_current_message_change(event))
        with self.assertRaises(ValueError): self.service.cached_messages_for_change(event)
        self.service.on_message_changed.reset_mock()
        self.send(155, {'chatId': 303, 'messageId': 7, 'totalCount': 0})
        self.service.on_message_changed.assert_not_called()

    def test_owner_switch_after_commit_prevents_change_callback(self):
        self.seed()
        self.service.on_message.reset_mock()
        self.service.on_chat_changed.reset_mock()
        original = self.service._history_cache_write
        def switch_after_commit(binding, apply):
            result = original(binding, apply)
            self.case.change_account()
            return result
        self.service._history_cache_write = switch_after_commit
        self.send(128, {'chatId': 303, 'message': dict(self.case.raw(), updateTime=200, text='Changed')})
        self.service.on_message_changed.assert_not_called()
        self.service.on_message.assert_not_called()
        self.service.on_chat_changed.assert_not_called()

    def test_exact_reader_rechecks_context_changed_mid_read(self):
        self.seed()
        event = MessageChange(101, self.client, self.case.context, 303, (7,))
        original = self.store.message
        def replace_session(*args):
            result = original(*args)
            self.client._auth_session = self.case.context_for(self.client)
            return result
        self.store.message = Mock(side_effect=replace_session)
        with self.assertRaises(ValueError): self.service.cached_messages_for_change(event)
        self.assertFalse(self.service.is_current_message_change(event))
