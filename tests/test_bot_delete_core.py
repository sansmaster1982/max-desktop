"""Sequential bot119/delete52 with synthetic state and no account/transport IO."""
import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import test_conversation_lifecycle as fixture
from maxclient.core.models import Chat, Message
from maxclient.core.store import Store
from maxclient.protocol.client import AuthenticatedSession, MaxError, MaxWriteNotStarted, MaxWriteOutcomeUnknown


class BotDeleteCoreTests(unittest.TestCase):
    def setUp(self):
        self.case = fixture.ConversationLifecycleTests()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.s, self.ctx = self.case.service, self.case.context
        self.c, self.store = self.s.client, self.s.store
        self.case.remote.update(hasBots=True, isSuspended=False)
        self.s._server_chats[303] = copy.deepcopy(self.case.remote)
        self.trace = []
        self.c.chat_info.side_effect = self.read_chat
        self.c.contact_info.side_effect = self.read_contact
        self.c.stop_bot = Mock(side_effect=self.stop)
        self.c.change_conversation_lifecycle.side_effect = self.delete

    def tearDown(self):
        self.c.request.assert_not_called()

    def read_chat(self, identities, **kwargs):
        self.trace.append(('48', identities))
        self.assertEqual(identities, [303])
        self.assertIs(kwargs['expected_session'], self.c._auth_session)
        return {'chats': [copy.deepcopy(self.case.remote)]} if self.case.remote else {'chats': []}

    def read_contact(self, identities, **kwargs):
        self.trace.append(('32', identities))
        self.assertEqual(identities, [202])
        self.assertIs(kwargs['expected_session'], self.c._auth_session)
        return {'contacts': [copy.deepcopy(self.case.contact)]}

    def stop(self, peer, **kwargs):
        self.trace.append(('119', peer))
        self.assertEqual(peer, 202)
        self.assertIs(kwargs['expected_session'], self.ctx.session_context)
        self.case.remote.update(isSuspended=True, modified=120)
        return None

    def delete(self, action, identity, cutoff, for_all, **kwargs):
        self.trace.append(('52', identity, cutoff, for_all))
        self.assertEqual((action, identity, for_all), ('delete', 303, False))
        self.assertIs(kwargs['expected_session'], self.c._auth_session)
        self.case.remote.update(status='REMOVED', modified=max(cutoff, 140))
        self.case.remote.pop('lastMessage', None)
        return None

    def row(self):
        return self.store.conversation_action_journal(101, 303)

    def test_exact_sequential_writes_use_fresh_second_cutoff_and_confirm_both_effects(self):
        original = self.read_chat

        def read(identities, **kwargs):
            if self.c.stop_bot.call_count and self.c.chat_info.call_count == 3:
                self.case.remote['modified'] = 130
            return original(identities, **kwargs)

        self.c.chat_info.side_effect = read
        self.store.insert_message(Message(chat_id=303, id=10, time_ms=150, text='newer'))
        self.store.insert_message(Message(chat_id=303, time_ms=50, text='unsent', local_id='pending', status='pending'))
        result = self.s.delete_conversation(self.ctx)
        self.assertEqual([item[0] for item in self.trace], ['48', '32', '119', '48', '48', '32', '52', '48'])
        self.c.stop_bot.assert_called_once_with(202, expected_session=self.ctx.session_context)
        self.c.change_conversation_lifecycle.assert_called_once_with('delete', 303, 130, False,
                                                                     expected_session=self.ctx.session_context)
        self.assertTrue(result['confirmed'])
        self.assertTrue(result['accepted'])
        self.assertTrue(result['stop_confirmed'] and result['delete_confirmed'])
        self.assertEqual((result['action'], result['lifecycle_kind'], result['stage']), ('delete', 'bot_delete', 'complete'))
        self.assertEqual([row.text for row in self.store.messages(303)], ['unsent', 'newer'])
        self.assertEqual(self.row()['stage'], 'complete')
        self.assertIsNone(self.store.bot_delete_for_peer(101, 202))

    def test_explicit_already_suspended_is_noop119_but_still_fresh52(self):
        self.case.remote['isSuspended'] = True
        result = self.s.delete_conversation(self.ctx)
        self.assertTrue(result['confirmed'])
        self.assertFalse(result['stop_acknowledged'])
        self.assertTrue(self.row()['stop_preexisting'])
        self.c.stop_bot.assert_not_called()
        self.c.change_conversation_lifecycle.assert_called_once()

    def test_missing_or_false_suspended_never_allows52_after119(self):
        for suspended in ('missing', False):
            with self.subTest(suspended=suspended):
                self.c.stop_bot.side_effect = None
                self.c.stop_bot.return_value = None
                self.case.remote.update(lastEventTime=0, modified=120)
                if suspended == 'missing':
                    self.case.remote.pop('isSuspended', None)
                else:
                    self.case.remote['isSuspended'] = suspended
                result = self.s.delete_conversation(self.ctx)
                self.assertTrue(result['pending'])
                self.assertFalse(result['stop_confirmed'])
                self.c.change_conversation_lifecycle.assert_not_called()
                self.s.refresh_conversation_lifecycle(self.ctx)
                self.c.change_conversation_lifecycle.assert_not_called()
                self.store.finish_conversation_action(101, 303)

    def test119_unknown_never_sends52_refresh_only_observes_then_explicit_continue(self):
        self.c.stop_bot.side_effect = MaxWriteOutcomeUnknown('synthetic119')
        with self.assertRaises(MaxWriteOutcomeUnknown):
            self.s.delete_conversation(self.ctx)
        self.assertEqual(self.row()['stage'], 'stop_pending')
        self.c.change_conversation_lifecycle.assert_not_called()
        self.case.remote.update(isSuspended=True, modified=120)
        result = self.s.refresh_conversation_lifecycle(self.ctx)
        self.assertEqual(result['stage'], 'stopped')
        self.assertTrue(result['can_continue_delete'])
        self.assertFalse(result['confirmed'])
        self.assertFalse(result['stop_acknowledged'])
        self.c.change_conversation_lifecycle.assert_not_called()
        with self.assertRaises(ValueError):
            self.s.delete_conversation(self.ctx)
        result = self.s.continue_bot_conversation_delete(self.ctx)
        self.assertTrue(result['confirmed'])
        self.c.stop_bot.assert_called_once()
        self.c.change_conversation_lifecycle.assert_called_once()

    def test119_rejected_releases_reservation_without52_or_local_rollback(self):
        self.c.stop_bot.side_effect = MaxError('synthetic rejection')
        with self.assertRaises(MaxError):
            self.s.delete_conversation(self.ctx)
        self.assertIsNone(self.row())
        self.assertIsNone(self.store.bot_delete_for_peer(101, 202))
        self.c.change_conversation_lifecycle.assert_not_called()
        self.assertIsNotNone(self.store.message(303, 9))

    def test52_rejected_keeps_stopped_partial_and_continuation_only_sends52(self):
        self.c.change_conversation_lifecycle.side_effect = MaxError('synthetic52')
        result = self.s.delete_conversation(self.ctx)
        self.assertTrue(result['accepted'])
        self.assertTrue(result['partial'])
        self.assertTrue(result['can_continue_delete'])
        self.assertFalse(result['confirmed'])
        self.assertEqual(self.row()['delete_outcome'], 'rejected')
        self.assertTrue(self.s._server_chats[303]['isSuspended'])
        self.assertIsNotNone(self.store.message(303, 9))
        self.c.change_conversation_lifecycle.side_effect = self.delete
        self.assertTrue(self.s.continue_bot_conversation_delete(self.ctx)['confirmed'])
        self.c.stop_bot.assert_called_once()
        self.assertEqual(self.c.change_conversation_lifecycle.call_count, 2)

    def test52_unknown_is_durable_and_neither_entry_point_replays_write(self):
        self.c.change_conversation_lifecycle.side_effect = MaxWriteOutcomeUnknown('synthetic52')
        with self.assertRaises(MaxWriteOutcomeUnknown):
            self.s.delete_conversation(self.ctx)
        for method in (self.s.delete_conversation, self.s.continue_bot_conversation_delete):
            with self.assertRaises(ValueError):
                method(self.ctx)
        self.case.remote.update(status='REMOVED', modified=150)
        result = self.s.refresh_conversation_lifecycle(self.ctx)
        self.assertTrue(result['confirmed'])
        self.assertFalse(result['delete_acknowledged'])
        self.c.stop_bot.assert_called_once()
        self.c.change_conversation_lifecycle.assert_called_once()

    def test52_not_started_keeps_stop_and_allows_only_explicit_continuation(self):
        self.c.change_conversation_lifecycle.side_effect = MaxWriteNotStarted('synthetic before dispatch')
        result = self.s.delete_conversation(self.ctx)
        self.assertTrue(result['stop_confirmed'])
        self.assertTrue(result['can_continue_delete'])
        self.assertEqual(self.row()['delete_outcome'], 'not_started')
        self.assertFalse(result['delete_acknowledged'])
        self.assertFalse(result['confirmed'])
        self.s.refresh_conversation_lifecycle(self.ctx)
        self.c.stop_bot.assert_called_once()
        self.c.change_conversation_lifecycle.assert_called_once()

    def test_empty52_readback_never_confirms_or_erases_history(self):
        def remove(*args, **kwargs):
            self.delete(*args, **kwargs)
            self.case.remote = None

        self.c.change_conversation_lifecycle.side_effect = remove
        result = self.s.delete_conversation(self.ctx)
        self.assertTrue(result['pending'])
        self.assertTrue(result['stop_confirmed'])
        self.assertFalse(result['delete_confirmed'])
        self.assertIsNone(self.store.history_boundary(303))
        self.assertIsNotNone(self.store.message(303, 9))

    def test_changed_restrictions_between119_and52_preserve_honest_partial(self):
        def stop(peer, **kwargs):
            self.stop(peer, **kwargs)
            self.case.remote['restrictions'] = 128

        self.c.stop_bot.side_effect = stop
        result = self.s.delete_conversation(self.ctx)
        self.assertTrue(result['stop_confirmed'])
        self.assertTrue(result['partial'])
        self.assertFalse(result['can_continue_delete'])
        self.c.change_conversation_lifecycle.assert_not_called()

    def test_same_owner_session_change_after119_never_sends52_and_preserves_journal(self):
        def switch(peer, **kwargs):
            self.stop(peer, **kwargs)
            self.c._auth_session = AuthenticatedSession(101, self.c.device_id, 'WEB', self.c._sock)

        self.c.stop_bot.side_effect = switch
        with self.assertRaises(MaxWriteOutcomeUnknown):
            self.s.delete_conversation(self.ctx)
        self.assertEqual(self.row()['stage'], 'stop_pending')
        self.c.change_conversation_lifecycle.assert_not_called()
        current = self.s.conversation_context(303, activate_files=False)
        self.assertTrue(self.s.refresh_conversation_lifecycle(current)['can_continue_delete'])
        self.c.change_conversation_lifecycle.assert_not_called()

    def test_gate_rejects_service_restricted_self_and_for_all_before_any_write(self):
        for fields in ({'options': {'SERVICE_CHAT': True}}, {'restrictions': 128}):
            with self.subTest(fields=fields):
                self.case.remote = fixture.chat(hasBots=True, isSuspended=False, **fields)
                self.s._server_chats[303] = copy.deepcopy(self.case.remote)
                with self.assertRaises(ValueError):
                    self.s.delete_conversation(self.ctx)
        self.case.remote = fixture.chat(hasBots=True, isSuspended=False)
        self.s._server_chats[303] = copy.deepcopy(self.case.remote)
        for value in (True, 0, None, 'false'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.s.delete_conversation(self.ctx, for_all=value)
        self.s._server_chats[303] = fixture.chat(hasBots=True, participants={'101': 1})
        self.s._server_contacts[101] = {**self.case.contact, 'id': 101}
        self.store.upsert_chat(Chat(303, 'Self', chat_type='DIALOG', peer_user_id=101))
        own_context = self.s.conversation_context(303, activate_files=False)
        with self.assertRaises(ValueError):
            self.s.delete_conversation(own_context)
        self.c.stop_bot.assert_not_called()
        self.c.change_conversation_lifecycle.assert_not_called()

    def test_malformed119_ack_remains_unknown_with_no52(self):
        self.c.stop_bot.side_effect = None
        self.c.stop_bot.return_value = {}
        with self.assertRaises(MaxWriteOutcomeUnknown):
            self.s.delete_conversation(self.ctx)
        self.assertFalse(self.row()['stop_acknowledged'])
        self.c.change_conversation_lifecycle.assert_not_called()

    def test_malformed52_ack_does_not_claim_completion(self):
        self.c.change_conversation_lifecycle.side_effect = None
        self.c.change_conversation_lifecycle.return_value = {}
        with self.assertRaises(MaxWriteOutcomeUnknown):
            self.s.delete_conversation(self.ctx)
        self.assertEqual(self.row()['stage'], 'delete_pending')
        self.assertFalse(self.row()['delete_acknowledged'])
        self.assertIsNone(self.store.history_boundary(303))

    def test_journal_cas_replacement_before52_prevents_dispatch(self):
        original = self.s._bot_delete_read

        def replace(context, record, **kwargs):
            result = original(context, record, **kwargs)
            if kwargs.get('with_contact'):
                other = {**record, 'operation_id': 'replacement-operation'}
                self.store.compare_conversation_action(101, 303, record, other)
            return result

        with patch.object(self.s, '_bot_delete_read', side_effect=replace):
            with self.assertRaises(ValueError):
                self.s.delete_conversation(self.ctx)
        self.c.change_conversation_lifecycle.assert_not_called()
        self.assertEqual(self.row()['operation_id'], 'replacement-operation')


class BotDeletePersistenceTests(unittest.TestCase):
    def test_journal_peer_reservation_and_stage_survive_reopen_and_owner_switch(self):
        with tempfile.TemporaryDirectory(prefix='max-bot-delete-journal-') as directory:
            path = Path(directory) / 'synthetic.sqlite'
            record = {'action': 'bot_delete', 'owner_id': 101, 'chat_id': 303, 'peer_id': 202,
                      'operation_id': 'synthetic-operation', 'session_nonce': 'synthetic-session',
                      'stage': 'delete_pending', 'stop_confirmed': True, 'delete_confirmed': False}
            store = Store(path)
            try:
                store.bind_owner(101)
                store.reserve_conversation_action(101, 303, record)
            finally:
                store.close()
            store = Store(path)
            try:
                self.assertEqual(store.bot_delete_for_peer(101, 202), record)
                self.assertFalse(store.compare_conversation_action(101, 303, {**record, 'stage': 'stopped'}, None))
                store.bind_owner(404)
                self.assertIsNone(store.bot_delete_for_peer(404, 202))
                store.bind_owner(101)
                self.assertEqual(store.conversation_action_journal(101, 303), record)
                with self.assertRaises(ValueError):
                    store.reserve_conversation_action(101, 505, {**record, 'chat_id': 505})
                self.assertIsNone(store.conversation_action_journal(101, 505))
            finally:
                store.close()


if __name__ == '__main__':
    unittest.main()
