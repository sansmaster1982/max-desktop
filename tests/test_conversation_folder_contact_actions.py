"""Info folder/contact intents use fresh state and bound fake transports only."""
import copy
import unittest

import test_conversation_info_service as info_fixture
from maxclient.core.folder_block_service import FolderBlockServiceMixin
from maxclient.core.service import MaxService
from maxclient.protocol.client import AuthenticatedSession, MaxError, MaxWriteNotStarted, MaxWriteOutcomeUnknown


def folder(identity='target', **fields):
    return {'id': identity, 'title': 'Synthetic', 'include': [], 'favorites': [],
            'filters': [], 'options': [], **fields}


class _InfoActionsService(FolderBlockServiceMixin, info_fixture._Service):
    _history_cache_write = MaxService._history_cache_write
    _remember_contact_info = MaxService._remember_contact_info
    _invalidate_contacts = MaxService._invalidate_contacts


class _State:
    def __init__(self, client):
        self.client = client
        self.folders = [folder('all.chat.folder'), folder(), folder('other')]
        self.order = [row['id'] for row in self.folders]
        self.contact = {'id': 202, 'updateTime': 1_790_000_000_000,
                        'names': [{'name': 'Synthetic peer'}], 'flags': 0}
        self.reads, self.writes = [], []
        self.missing = False
        self.after_read = lambda kind: None
        self.before_write = lambda: None
        self.after_write = lambda: None
        self.error = None
        self.fail_readback = False

    def guard(self, expected):
        if expected is not self.client.authenticated_session:
            raise MaxWriteNotStarted('Synthetic context changed before send')

    def read_folders(self, folder_sync=None, *, expected_session=None):
        self.guard(expected_session)
        self.reads.append(('folders', expected_session))
        if self.fail_readback: raise MaxError('Synthetic readback failure')
        result = {'folderSync': 1_790_000_000_000, 'foldersOrder': list(self.order),
                  'folders': copy.deepcopy(self.folders[:1] if self.missing else self.folders)}
        self.after_read('folders')
        return result

    def read_details(self, ids, *, expected_session=None):
        self.guard(expected_session)
        self.reads.append(('details', expected_session))
        result = {'folders': copy.deepcopy([row for row in self.folders if row['id'] in ids])}
        self.after_read('details')
        return result

    def write(self, kind, payload, expected):
        self.before_write()
        self.guard(expected)
        self.writes.append((kind, copy.deepcopy(payload), expected))
        if self.error: raise self.error

    def update_folder(self, payload, *, expected_session=None):
        self.write('folder', payload, expected_session)
        self.folders = [copy.deepcopy(payload) if row['id'] == payload['id'] else row for row in self.folders]
        self.after_write()
        return {'folder': copy.deepcopy(payload), 'folderSync': 1_790_000_000_000}

    def read_contact(self, ids, *, expected_session=None):
        self.guard(expected_session)
        self.reads.append(('contact', expected_session))
        if self.fail_readback: raise MaxError('Synthetic readback failure')
        result = {'contacts': [copy.deepcopy(self.contact)]}
        self.after_read('contact')
        return result

    def read_blocked(self, *, expected_session=None):
        self.guard(expected_session)
        self.reads.append(('blocked', expected_session))
        result = {'contacts': [copy.deepcopy(self.contact)] if self.contact.get('status') == 'BLOCKED' else []}
        self.after_read('blocked')
        return result

    def block(self, identity, *, expected_session=None):
        self.write('block', identity, expected_session)
        self.contact['status'] = 'BLOCKED'
        self.after_write()
        return {}

    def unblock(self, identity, *, expected_session=None):
        self.write('unblock', identity, expected_session)
        self.contact.pop('status', None)
        self.after_write()
        return {}


class ConversationFolderContactActionsTests(unittest.TestCase):
    def setUp(self):
        self.fixture = info_fixture.ConversationInfoServiceTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.service = _InfoActionsService()
        self.service.__dict__.update(self.fixture.service.__dict__)
        self.service.get_folder_view = lambda row: {'hidden': False}
        self.client = self.service.client
        self.state = _State(self.client)
        self.client.get_folders = self.state.read_folders
        self.client.get_folder_details = self.state.read_details
        self.client.update_folder = self.state.update_folder
        self.client.contact_info = self.state.read_contact
        self.client.list_blocked_contacts = self.state.read_blocked
        self.client.block_contact = self.state.block
        self.client.unblock_contact = self.state.unblock
        self.context = self.service.conversation_context(303)

    def tearDown(self):
        self.client.request.assert_not_called()
        self.service.rename_conversation.assert_not_called()

    def replace_context(self):
        self.client._auth_session = AuthenticatedSession(101, self.client.device_id, 'WEB', self.client._sock)

    def test_bound_detached_folder_read_resolves_actual_ordered_details(self):
        self.state.missing = True
        before = self.service.store._db.total_changes
        result = self.service.get_conversation_folders(self.context)
        self.assertIs(result['context'], self.context)
        self.assertEqual([row['id'] for row in result['orderedFolders']], self.state.order)
        self.assertEqual([name for name, _ctx in self.state.reads], ['folders', 'details'])
        self.assertTrue(all(ctx is self.context.session_context for _name, ctx in self.state.reads))
        self.assertEqual(self.service.store._db.total_changes, before)
        self.assertEqual(self.state.writes, [])

    def test_add_delta_uses_fresh_members_and_preserves_all_other_fields_and_order(self):
        # A menu may have shown an older [10] list; only this fresh server list is used.
        current = folder(include=[99, 88], favorites=[8, 7], options=[4, 3, 2], filters=[4, 1])
        self.state.folders[1] = current
        before_order, before_sql = list(self.state.order), self.service.store._db.total_changes
        result = self.service.set_conversation_folder_included(self.context, 'target', True)
        sent = self.state.writes[0][1]
        self.assertEqual(sent, {**current, 'include': [99, 88, 303]})
        self.assertFalse(result['pending'])
        self.assertIs(result['context'], self.context)
        self.assertEqual(self.state.order, before_order)
        self.assertEqual(self.service.store._db.total_changes, before_sql)
        self.assertEqual(len(self.state.writes), 1)

    def test_removal_keeps_unrelated_include_order_and_favorites_even_when_still_semantic_member(self):
        self.state.folders[1] = folder(include=[9, 303, 8], favorites=[303, 8], filters=[4])
        self.service.get_folder_view = lambda row: {'hidden': None, 'chatIds': [303]}
        self.service.set_conversation_folder_included(self.context, 'target', False)
        sent = self.state.writes[0][1]
        self.assertEqual(sent['include'], [9, 8])
        self.assertEqual(sent['favorites'], [303, 8])
        self.assertEqual(sent['filters'], [4])

    def test_membership_noop_is_idempotent_not_a_stale_toggle(self):
        self.state.folders[1] = folder(include=[303, 9])
        result = self.service.set_conversation_folder_included(self.context, 'target', True)
        self.assertTrue(result['unchanged'])
        self.assertEqual(self.state.writes, [])

    def test_membership_delta_never_silently_normalizes_a_protected_existing_title(self):
        self.state.folders[1] = folder(title=' Existing ', options=[2])
        with self.assertRaises(ValueError):
            self.service.set_conversation_folder_included(self.context, 'target', True)
        self.assertEqual(self.state.writes, [])

    def test_all_folder_bad_boolean_and_missing_target_cannot_write(self):
        for identity, included in [('all.chat.folder', True), ('target', 1), ('target', None), ('absent', True)]:
            with self.assertRaises(ValueError):
                self.service.set_conversation_folder_included(self.context, identity, included)
        self.assertEqual(self.state.writes, [])

    def test_only_actual_hidden_true_excludes_folder_not_option4_or_unknown_projection(self):
        self.state.folders[1] = folder(options=[4])
        self.service.get_folder_view = lambda row: {'hidden': True}
        with self.assertRaises(ValueError):
            self.service.set_conversation_folder_included(self.context, 'target', True)
        self.assertEqual(self.state.writes, [])
        self.service.get_folder_view = lambda row: {'hidden': None}
        self.assertFalse(self.service.set_conversation_folder_included(self.context, 'target', True)['pending'])

    def test_capability_loss_or_context_change_during_any_folder_read_stops_before_write(self):
        for kind in ('folders', 'details'):
            with self.subTest(kind=kind):
                self.context = self.service.conversation_context(303)
                self.state.missing = kind == 'details'
                self.state.after_read = lambda stage: self.replace_context() if stage == kind else None
                with self.assertRaises(ValueError):
                    self.service.set_conversation_folder_included(self.context, 'target', True)
        self.assertEqual(self.state.writes, [])

    def test_atomic_write_context_failure_is_known_zero_send_and_clears_pending(self):
        self.state.before_write = self.replace_context
        with self.assertRaises(MaxWriteNotStarted):
            self.service.set_conversation_folder_included(self.context, 'target', True)
        self.assertIsNone(self.service._pending_folder_write)
        self.assertEqual(self.state.writes, [])

    def test_unknown_folder_outcome_blocks_repeat_until_authoritative_readback(self):
        self.state.error = MaxWriteOutcomeUnknown('Synthetic unknown')
        with self.assertRaises(MaxWriteOutcomeUnknown):
            self.service.set_conversation_folder_included(self.context, 'target', True)
        with self.assertRaises(MaxWriteOutcomeUnknown):
            self.service.set_conversation_folder_included(self.context, 'target', True)
        self.assertEqual(len(self.state.writes), 1)
        self.state.folders[1] = copy.deepcopy(self.state.writes[0][1])
        self.state.error = None
        self.assertFalse(self.service.get_conversation_folders(self.context)['writePending'])

    def test_changed_context_after_folder_ack_is_unknown_and_does_not_read_new_session(self):
        self.state.after_write = self.replace_context
        with self.assertRaises(MaxWriteOutcomeUnknown):
            self.service.set_conversation_folder_included(self.context, 'target', True)
        self.assertEqual(len(self.state.writes), 1)
        self.assertEqual([name for name, _ctx in self.state.reads], ['folders'])

    def test_folder_lock_blocks_parallel_intent_without_rpc(self):
        lock = self.service._feature_action_lock('folders')
        lock.acquire()
        try:
            with self.assertRaises(ValueError): self.service.set_conversation_folder_included(self.context, 'target', True)
        finally: lock.release()
        self.assertEqual((self.state.reads, self.state.writes), ([], []))

    def test_block_and_unblock_are_bound_to_actual_peer_and_typed_readbacks(self):
        blocked = self.service.set_conversation_contact_blocked(self.context, True)
        self.assertTrue(blocked['blocked'])
        self.assertFalse(blocked['pending'])
        self.assertIs(blocked['context'], self.context)
        unblocked = self.service.set_conversation_contact_blocked(self.context, False)
        self.assertFalse(unblocked['blocked'])
        self.assertFalse(unblocked['pending'])
        self.assertEqual([(kind, target) for kind, target, _ctx in self.state.writes], [('block', 202), ('unblock', 202)])
        self.assertTrue(all(ctx is self.context.session_context for _kind, _target, ctx in self.state.writes))
        self.assertTrue(all(ctx is self.context.session_context for _kind, ctx in self.state.reads))
        self.assertEqual(self.service.store.cache_owner, 101)

    def test_block_context_change_during_initial_contact_read_cannot_write_or_update_cache(self):
        self.state.after_read = lambda kind: self.replace_context() if kind == 'contact' else None
        before = self.service.store._db.total_changes
        with self.assertRaises(ValueError): self.service.set_conversation_contact_blocked(self.context, True)
        self.assertEqual(self.state.writes, [])
        self.assertEqual(self.service.store._db.total_changes, before)

    def test_block_atomic_zero_send_and_unknown_send_have_distinct_pending_states(self):
        self.state.before_write = self.replace_context
        with self.assertRaises(MaxWriteNotStarted): self.service.set_conversation_contact_blocked(self.context, True)
        self.assertIsNone(self.service._pending_contact_write)
        self.assertEqual(self.state.writes, [])
        self.context = self.service.conversation_context(303)
        self.state.before_write = lambda: None
        self.state.error = MaxWriteOutcomeUnknown('Synthetic unknown')
        with self.assertRaises(MaxWriteOutcomeUnknown): self.service.set_conversation_contact_blocked(self.context, True)
        with self.assertRaises(MaxWriteOutcomeUnknown): self.service.set_conversation_contact_blocked(self.context, True)
        self.assertEqual(len(self.state.writes), 1)
        self.assertIsNotNone(self.service._pending_contact_write)

    def test_contact_postwrite_context_change_preserves_unknown_and_cannot_read_next_session(self):
        self.state.after_write = self.replace_context
        with self.assertRaises(MaxWriteOutcomeUnknown): self.service.set_conversation_contact_blocked(self.context, True)
        self.assertEqual(len(self.state.writes), 1)
        self.assertEqual([kind for kind, _ctx in self.state.reads], ['contact'])

    def test_blocked_chat_or_nonboolean_intent_cannot_start_any_account_read(self):
        with self.assertRaises(ValueError): self.service.set_conversation_contact_blocked(self.context, 1)
        self.service._server_chats[303]['status'] = 'BLOCKED'
        with self.assertRaises(ValueError): self.service.set_conversation_contact_blocked(self.context, True)
        with self.assertRaises(ValueError): self.service.get_conversation_folders(self.context)
        self.assertEqual((self.state.reads, self.state.writes), ([], []))

    def test_noop_unblock_does_not_claim_success_when_two_readbacks_conflict(self):
        def contradictory(*, expected_session=None):
            self.state.guard(expected_session)
            return {'contacts': [{**copy.deepcopy(self.state.contact), 'status': 'BLOCKED'}]}
        self.client.list_blocked_contacts = contradictory
        result = self.service.set_conversation_contact_blocked(self.context, False)
        self.assertFalse(result['accepted'])
        self.assertTrue(result['pending'])
        self.assertIsNone(result['blocked'])
        self.assertNotIn('unchanged', result)
        self.assertEqual(self.state.writes, [])
        self.assertIsNotNone(self.service._pending_contact_write)


if __name__ == '__main__':
    unittest.main()
