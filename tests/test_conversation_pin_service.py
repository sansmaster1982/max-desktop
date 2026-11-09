"""Folder-pin deltas with in-memory storage and fake transports; no live RPC."""
import copy
from dataclasses import FrozenInstanceError
import unittest

import test_conversation_info_service as info_fixture
import test_conversation_folder_contact_actions as folder_fixture
from maxclient.core.conversation_pin_service import ConversationPinServiceMixin
from maxclient.protocol.client import AuthenticatedSession, MaxError, MaxWriteNotStarted, MaxWriteOutcomeUnknown


class _Service(ConversationPinServiceMixin, folder_fixture._InfoActionsService):
    _remember_config = info_fixture.MaxService._remember_config


class ConversationPinServiceTests(unittest.TestCase):
    def setUp(self):
        fixture = info_fixture.ConversationInfoServiceTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.service = _Service()
        self.service.__dict__.update(fixture.service.__dict__)
        self.service.get_folder_view = lambda row: {'hidden': False}
        self.service._remember_config({'server': {'max-favorite-chats': 5}})
        self.client = self.service.client
        self.state = folder_fixture._State(self.client)
        self.client.get_folders = self.state.read_folders
        self.client.get_folder_details = self.state.read_details
        self.client.update_folder = self.state.update_folder
        self.conversation = self.service.conversation_context(303, activate_files=False)
        self.context = self.service.capture_conversation_pin(self.conversation, 'target')

    def tearDown(self):
        self.client.request.assert_not_called()
        self.client.chat_info.assert_not_called()
        self.client.contact_info.assert_not_called()
        self.service.rename_conversation.assert_not_called()

    def replace_context(self):
        self.client._auth_session = AuthenticatedSession(
            101, self.client.device_id, 'WEB', self.client._sock)

    def capture(self, identity='target'):
        conversation = self.service.conversation_context(303, activate_files=False)
        return self.service.capture_conversation_pin(conversation, identity)

    def test_capture_and_local_revalidation_have_no_rpc_sql_or_guessed_pin(self):
        before = self.service.store._db.total_changes
        state = self.service.conversation_pin_status(self.context)
        self.assertIs(state['context'], self.context)
        self.assertTrue(state['available'])
        self.assertFalse(state['pending'])
        self.assertNotIn('pinned', state)
        self.assertEqual(state['limit'], 5)
        self.assertTrue(self.service.is_current_conversation_pin(self.context))
        self.assertEqual((self.state.reads, self.state.writes), ([], []))
        self.assertEqual(self.service.store._db.total_changes, before)
        with self.assertRaises(FrozenInstanceError): self.context.folder_id = 'other'

    def test_fresh_pin_state_is_detached_and_resolves_only_exact_missing_folder(self):
        self.state.missing = True
        before = self.service.store._db.total_changes
        state = self.service.get_conversation_pin_state(self.context)
        self.assertFalse(state['pinned'])
        self.assertTrue(state['available'])
        self.assertEqual(state['folder'], self.state.folders[1])
        state['folder']['favorites'].append(999)
        self.assertEqual(self.state.folders[1]['favorites'], [])
        self.assertEqual([kind for kind, _session in self.state.reads], ['folders', 'details'])
        self.assertTrue(all(session is self.conversation.session_context for _kind, session in self.state.reads))
        self.assertEqual(self.service.store._db.total_changes, before)
        self.assertEqual(self.state.writes, [])

    def test_pin_prepends_actual_chat_and_preserves_unrelated_favorites_and_fields(self):
        original = folder_fixture.folder(include=[77, 88], favorites=[22, 11],
                                         filters=[4, 1], options=[2, 3, 4])
        self.state.folders[1] = copy.deepcopy(original)
        result = self.service.set_conversation_pinned(self.context, True)
        self.assertEqual(self.state.writes[0][1], {**original, 'favorites': [303, 22, 11]})
        self.assertIs(self.state.writes[0][2], self.conversation.session_context)
        self.assertEqual(len(self.state.writes), 1)
        self.assertTrue(result['accepted'])
        self.assertTrue(result['confirmed'])
        self.assertTrue(result['pinned'])
        self.assertFalse(result['pending'])
        self.assertEqual(result['folder']['favorites'], [303, 22, 11])
        self.assertEqual([kind for kind, _session in self.state.reads], ['folders', 'folders'])

    def test_unpin_removes_only_captured_chat_and_does_not_reorder_others(self):
        self.state.folders[1] = folder_fixture.folder(favorites=[22, 303, 11])
        result = self.service.set_conversation_pinned(self.context, False)
        self.assertEqual(self.state.writes[0][1]['favorites'], [22, 11])
        self.assertFalse(result['pinned'])
        self.assertTrue(result['confirmed'])

    def test_explicit_desired_state_is_idempotent_against_fresh_server_state(self):
        self.state.folders[1]['favorites'] = [303, 22]
        result = self.service.set_conversation_pinned(self.context, True)
        self.assertFalse(result['accepted'])
        self.assertTrue(result['unchanged'])
        self.assertTrue(result['confirmed'])
        self.assertEqual(result['folder']['favorites'], [303, 22])
        self.assertEqual(self.state.writes, [])

    def test_all_folder_pin_is_allowed_and_other_selected_folder_is_untouched(self):
        context = self.capture('all.chat.folder')
        self.state.folders[0]['favorites'] = [11]
        self.state.folders[1]['favorites'] = [22]
        result = self.service.set_conversation_pinned(context, True)
        self.assertTrue(result['pinned'])
        self.assertEqual(self.state.folders[0]['favorites'], [303, 11])
        self.assertEqual(self.state.folders[1]['favorites'], [22])

    def test_chat_blocked_is_not_the_info_folder_gate_but_missing_status_fails(self):
        self.service._server_chats[303]['status'] = 'BLOCKED'
        result = self.service.set_conversation_pinned(self.context, True)
        self.assertTrue(result['confirmed'])
        self.service._server_chats[303].pop('status')
        self.assertFalse(self.service.is_current_conversation_pin(self.context))
        with self.assertRaises(ValueError): self.service.conversation_pin_status(self.context)

    def test_bad_context_or_boolean_cannot_read_or_write(self):
        for value in (True, None, '', 1, [], {}):
            with self.assertRaises((ValueError, TypeError)):
                self.service.capture_conversation_pin(self.conversation, value)
        for value in (None, 1, 'true', []):
            with self.assertRaises(ValueError): self.service.set_conversation_pinned(self.context, value)
        with self.assertRaises(ValueError): self.service.capture_conversation_pin(object(), 'target')
        self.assertEqual((self.state.reads, self.state.writes), ([], []))

    def test_missing_or_removed_selected_folder_never_falls_back_to_all(self):
        self.state.order.remove('target')
        with self.assertRaises(ValueError): self.service.set_conversation_pinned(self.context, True)
        self.state.order.append('target')
        self.state.folders = [row for row in self.state.folders if row['id'] != 'target']
        with self.assertRaises(ValueError): self.service.set_conversation_pinned(self.context, True)
        self.assertEqual(self.state.writes, [])

    def test_duplicate_or_malformed_folder_fields_never_authorize_write(self):
        original = copy.deepcopy(self.state.folders)
        for delta in ({'favorites': [11, 11]}, {'favorites': [True]}, {'include': 'bad'},
                      {'options': [99]}, {'title': ' Existing '}):
            with self.subTest(delta=delta):
                self.state.folders = copy.deepcopy(original)
                self.state.folders[1].update(delta)
                with self.assertRaises(ValueError): self.service.set_conversation_pinned(self.context, True)
        self.state.folders = original + [copy.deepcopy(original[1])]
        with self.assertRaises(ValueError): self.service.set_conversation_pinned(self.context, True)
        self.assertEqual(self.state.writes, [])

    def test_omitted_optional_arrays_use_explicit_source_defaults(self):
        self.state.folders[1] = {'id': 'target', 'title': 'Synthetic'}
        self.service.set_conversation_pinned(self.context, True)
        self.assertEqual(self.state.writes[0][1], {'id': 'target', 'title': 'Synthetic',
                         'favorites': [303], 'include': [], 'filters': [], 'options': []})

    def test_limit_reached_disables_pin_but_allows_valid_unpin(self):
        self.service.store.merge_config(101, {'server': {'max-favorite-chats': 2}})
        self.state.folders[1]['favorites'] = [11, 22]
        self.assertFalse(self.service.get_conversation_pin_state(self.context)['available'])
        with self.assertRaises(ValueError): self.service.set_conversation_pinned(self.context, True)
        self.state.folders[1]['favorites'] = [303, 22]
        self.assertTrue(self.service.get_conversation_pin_state(self.context)['available'])
        self.assertTrue(self.service.set_conversation_pinned(self.context, False)['confirmed'])
        self.assertEqual(len(self.state.writes), 1)

    def test_malformed_cached_limit_is_not_replaced_by_bundle_default_five(self):
        for value in (True, False, '5', 5.0, -1, 1001, [], {}):
            with self.subTest(value=value):
                self.service.store.merge_config(101, {'server': {'max-favorite-chats': value}})
                self.assertFalse(self.service.get_conversation_pin_state(self.context)['available'])
                with self.assertRaises(ValueError): self.service.set_conversation_pinned(self.context, True)
        self.assertEqual(self.state.writes, [])

    def test_config_ingestion_retains_supplied_limit_and_unrelated_update_does_not_reset_it(self):
        self.service._remember_config({'server': {'max-favorite-chats': 2}})
        self.service._remember_config({'server': {'bot-complaint-enabled': True}})
        self.assertEqual(self.service.store.load_config(101)['server']['max-favorite-chats'], 2)
        self.assertEqual(self.service.conversation_pin_status(self.context)['limit'], 2)
        self.state.folders[1]['favorites'] = [11, 22]
        self.assertFalse(self.service.get_conversation_pin_state(self.context)['available'])
        with self.assertRaises(ValueError): self.service.set_conversation_pinned(self.context, True)
        self.assertEqual(self.state.writes, [])

    def test_config_ingestion_missing_or_null_limit_uses_confirmed_default_five(self):
        # Fresh synthetic cache follows normal CONFIG ingestion, not a manual
        # injected limit. This is the regression for a grey runtime pin menu.
        self.service.store._db.execute("DELETE FROM cache_metadata WHERE key='account_config'")
        self.service._remember_config({'server': {'bot-complaint-enabled': True}})
        self.assertNotIn('max-favorite-chats', self.service.store.load_config(101)['server'])
        state = self.service.get_conversation_pin_state(self.context)
        self.assertEqual(state['limit'], 5)
        self.assertTrue(state['available'])
        self.service._remember_config({'server': {'max-favorite-chats': 2}})
        self.service._remember_config({'server': {'max-favorite-chats': None}})
        self.assertIsNone(self.service.store.load_config(101)['server']['max-favorite-chats'])
        self.state.folders[1]['favorites'] = [11, 22, 33, 44]
        self.assertTrue(self.service.get_conversation_pin_state(self.context)['available'])
        result = self.service.set_conversation_pinned(self.context, True)
        self.assertTrue(result['confirmed'])
        self.assertEqual(result['limit'], 5)
        self.assertEqual(result['folder']['favorites'], [303, 11, 22, 33, 44])
        self.assertEqual(len(self.state.writes), 1)

    def test_malformed_config_ingestion_invalidates_old_limit_until_valid_update(self):
        for value in (True, False, '5', 5.0, -1, 1001, [], {'unexpected': 5}):
            with self.subTest(value=value):
                self.service._remember_config({'server': {'max-favorite-chats': 5}})
                self.service._remember_config({'server': {'max-favorite-chats': value}})
                self.assertIs(self.service.store.load_config(101)['server']['max-favorite-chats'], False)
                self.assertIsNone(self.service.conversation_pin_status(self.context)['limit'])
                self.assertFalse(self.service.get_conversation_pin_state(self.context)['available'])
                with self.assertRaises(ValueError): self.service.set_conversation_pinned(self.context, True)
        self.assertEqual(self.state.writes, [])
        self.service._remember_config({'server': {'max-favorite-chats': 4}})
        self.assertTrue(self.service.get_conversation_pin_state(self.context)['available'])
        self.assertEqual(self.service.conversation_pin_status(self.context)['limit'], 4)

    def test_zero_limit_allows_unpin_of_the_last_item(self):
        self.service.store.merge_config(101, {'server': {'max-favorite-chats': 0}})
        self.state.folders[1]['favorites'] = [303]
        self.assertTrue(self.service.get_conversation_pin_state(self.context)['available'])
        self.assertTrue(self.service.set_conversation_pinned(self.context, False)['confirmed'])

    def test_missing_write_wrapper_is_known_unavailable_not_an_unknown_write(self):
        self.client.update_folder = None
        self.assertFalse(self.service.conversation_pin_status(self.context)['available'])
        with self.assertRaises(ValueError): self.service.set_conversation_pinned(self.context, True)
        self.assertEqual(self.state.writes, [])
        self.assertIsNone(getattr(self.service, '_pending_folder_write', None))

    def test_changed_context_after_any_read_never_writes_with_new_session(self):
        for stage in ('folders', 'details'):
            with self.subTest(stage=stage):
                context = self.capture()
                self.state.missing = stage == 'details'
                self.state.after_read = lambda kind: self.replace_context() if kind == stage else None
                with self.assertRaises(ValueError): self.service.set_conversation_pinned(context, True)
        self.assertEqual(self.state.writes, [])

    def test_changed_context_at_write_entry_is_known_zero_send_and_unlocks(self):
        self.state.before_write = self.replace_context
        with self.assertRaises(MaxWriteNotStarted): self.service.set_conversation_pinned(self.context, True)
        self.assertEqual(self.state.writes, [])
        self.assertIsNone(self.service._pending_folder_write)

    def test_changed_context_after_ack_is_unknown_and_never_reads_new_session(self):
        self.state.after_write = self.replace_context
        with self.assertRaises(MaxWriteOutcomeUnknown): self.service.set_conversation_pinned(self.context, True)
        self.assertEqual(len(self.state.writes), 1)
        self.assertEqual([kind for kind, _session in self.state.reads], ['folders'])
        self.assertIsNotNone(self.service._pending_folder_write)

    def test_unknown_blocks_repeat_and_other_folder_mutation_until_exact_pin_readback(self):
        self.state.error = MaxWriteOutcomeUnknown('Synthetic unresolved')
        with self.assertRaises(MaxWriteOutcomeUnknown): self.service.set_conversation_pinned(self.context, True)
        with self.assertRaises(MaxWriteOutcomeUnknown): self.service.set_conversation_pinned(self.context, True)
        with self.assertRaises(MaxWriteOutcomeUnknown):
            self.service.set_conversation_folder_included(self.conversation, 'other', True)
        self.assertEqual(len(self.state.writes), 1)
        self.assertTrue(self.service.conversation_pin_status(self.context)['pending'])
        self.state.folders[1] = copy.deepcopy(self.state.writes[0][1])
        self.state.error = None
        # Ordinary folder refresh has no originating pin/session evidence.
        self.assertTrue(self.service.get_conversation_folders(self.conversation)['writePending'])
        self.assertFalse(self.service.get_conversation_pin_state(self.capture())['pending'])
        self.assertEqual(len(self.state.writes), 1)

    def test_matching_favorites_but_unrelated_fields_differ_does_not_settle_unknown(self):
        self.state.error = MaxWriteOutcomeUnknown('Synthetic unresolved')
        with self.assertRaises(MaxWriteOutcomeUnknown): self.service.set_conversation_pinned(self.context, True)
        self.state.folders[1] = copy.deepcopy(self.state.writes[0][1])
        self.state.folders[1]['include'] = [444]
        self.assertTrue(self.service.get_conversation_pin_state(self.context)['pending'])
        with self.assertRaises(MaxWriteOutcomeUnknown): self.service.set_conversation_pinned(self.context, False)
        self.assertEqual(len(self.state.writes), 1)

    def test_exact_read_while_write_is_inflight_does_not_unlock_pending(self):
        during = []
        self.state.after_write = lambda: during.append(self.service.get_conversation_pin_state(self.context))
        result = self.service.set_conversation_pinned(self.context, True)
        self.assertTrue(during[0]['pending'])
        self.assertTrue(during[0]['inflight'])
        self.assertFalse(during[0]['available'])
        self.assertTrue(result['confirmed'])

    def test_fresh_same_owner_session_resolves_exact_unknown_target_without_reusing_old_session(self):
        self.state.error = MaxWriteOutcomeUnknown('Synthetic unresolved')
        with self.assertRaises(MaxWriteOutcomeUnknown): self.service.set_conversation_pinned(self.context, True)
        self.state.folders[1] = copy.deepcopy(self.state.writes[0][1])
        self.replace_context()
        current = self.capture()
        reads_before = len(self.state.reads)
        with self.assertRaises(ValueError): self.service.get_conversation_pin_state(self.context)
        with self.assertRaises(ValueError): self.service.set_conversation_pinned(self.context, True)
        self.assertEqual(len(self.state.reads), reads_before)
        self.assertFalse(self.service.get_conversation_pin_state(current)['pending'])
        self.assertIs(self.state.reads[-1][1], current.conversation.session_context)
        self.assertIsNot(self.state.reads[-1][1], self.conversation.session_context)
        self.assertTrue(self.service.set_conversation_pinned(current, True)['unchanged'])
        self.assertEqual(len(self.state.writes), 1)

    def test_other_owner_exact_folder_match_cannot_resolve_previous_owners_intent(self):
        self.state.error = MaxWriteOutcomeUnknown('Synthetic unresolved')
        with self.assertRaises(MaxWriteOutcomeUnknown): self.service.set_conversation_pinned(self.context, True)
        pending = self.service._pending_folder_write
        self.state.folders[1] = copy.deepcopy(self.state.writes[0][1])
        self.service.session.my_user_id = 201
        self.service._server_metadata_owner = 201
        self.service.store.bind_owner(201)
        self.service.store.upsert_chat(info_fixture.Chat(id=303, chat_type='DIALOG', peer_user_id=202))
        self.service.store.merge_config(201, {'server': {'max-favorite-chats': 5}})
        self.service._server_chats[303] = info_fixture.chat_dto(owner=201, participants={'201': 0, '202': 0})
        self.client._auth_session = AuthenticatedSession(201, self.client.device_id, 'WEB', self.client._sock)
        current = self.capture()
        self.assertEqual(current.conversation.owner_id, 201)
        state = self.service.get_conversation_pin_state(current)
        self.assertTrue(state['pinned'])
        self.assertIs(self.service._pending_folder_write, pending)
        self.assertEqual(pending['owner'], 101)
        with self.assertRaises(ValueError): self.service.get_conversation_pin_state(self.context)
        self.assertEqual(len(self.state.writes), 1)

    def test_reconnect_still_cannot_settle_unknown_with_different_peer_or_folder(self):
        self.state.error = MaxWriteOutcomeUnknown('Synthetic unresolved')
        with self.assertRaises(MaxWriteOutcomeUnknown): self.service.set_conversation_pinned(self.context, True)
        pending = self.service._pending_folder_write
        self.state.folders[1] = copy.deepcopy(self.state.writes[0][1])
        self.replace_context()
        other_folder = self.capture('other')
        self.service.get_conversation_pin_state(other_folder)
        self.assertIs(self.service._pending_folder_write, pending)
        self.service.store.upsert_chat(info_fixture.Chat(id=303, chat_type='DIALOG', peer_user_id=222))
        self.service._server_chats[303]['participants'] = {'101': 0, '222': 0}
        self.service._server_contacts[222] = {'id': 222, 'updateTime': 1,
                                            'names': [{'name': 'Another synthetic peer'}]}
        different_peer = self.capture()
        self.assertTrue(self.service.get_conversation_pin_state(different_peer)['pending'])
        self.assertIs(self.service._pending_folder_write, pending)
        self.assertEqual(len(self.state.writes), 1)

    def test_explicit_rejection_unlocks_but_ambiguous_failure_remains_pending(self):
        self.state.error = MaxError('Synthetic negative ACK')
        with self.assertRaises(MaxError): self.service.set_conversation_pinned(self.context, True)
        self.assertIsNone(self.service._pending_folder_write)
        self.state.error = OSError('Synthetic connection failure after entry')
        with self.assertRaises(MaxWriteOutcomeUnknown): self.service.set_conversation_pinned(self.context, True)
        self.assertIsNotNone(self.service._pending_folder_write)
        self.assertFalse(self.service._pending_folder_write['inflight'])

    def test_successful_ack_with_failed_readback_is_pending_not_success(self):
        self.state.after_write = lambda: setattr(self.state, 'fail_readback', True)
        with self.assertRaises(MaxWriteOutcomeUnknown): self.service.set_conversation_pinned(self.context, True)
        self.assertIsNotNone(self.service._pending_folder_write)
        self.assertEqual(len(self.state.writes), 1)
        self.state.fail_readback = False
        self.assertFalse(self.service.get_conversation_pin_state(self.context)['pending'])

    def test_lock_and_other_pending_folder_intents_prevent_parallel_writes(self):
        lock = self.service._feature_action_lock('folders')
        lock.acquire()
        try:
            with self.assertRaises(ValueError): self.service.set_conversation_pinned(self.context, True)
        finally:
            lock.release()
        self.assertEqual(self.state.reads, [])
        self.service._pending_folder_write = {'owner': 101, 'kind': 'upsert',
            'expected': folder_fixture.folder('other', include=[999])}
        with self.assertRaises(MaxWriteOutcomeUnknown): self.service.set_conversation_pinned(self.context, True)
        self.assertEqual(self.state.writes, [])


if __name__ == '__main__':
    unittest.main()
