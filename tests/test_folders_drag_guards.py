"""Folder gesture guards with synthetic account/session state and fake RPC only."""
import copy
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from maxclient.core.service import MaxService
from maxclient.protocol.client import (
    AuthenticatedSession, ConnectionState, MaxClient, MaxError, MaxFrame,
    MaxTimeout, MaxWriteNotStarted, MaxWriteOutcomeUnknown,
)
from maxclient.protocol.folder_contracts import ALL_FOLDER


DEVICE = '00112233-4455-6677-8899-aabbccddeeff'
STAMP = 1_790_000_000_000
BASE = [ALL_FOLDER, 'work', 'personal']
MOVED = [ALL_FOLDER, 'personal', 'work']


class BoundFolderState:
    """A detached server model whose hooks exercise exact RPC boundaries."""
    def __init__(self):
        self.device_id = DEVICE
        self.socket = object()
        self.authenticated_session = AuthenticatedSession(101, DEVICE, 'WEB', self.socket)
        self.order = list(BASE)
        self.folders = [{'id': identity, 'title': 'Synthetic ' + str(i)}
                        for i, identity in enumerate(BASE)]
        self.reads, self.writes = [], []
        self.list_filter = None
        self.details_filter = None
        self.list_transform = lambda value: value
        self.details_transform = lambda value: value
        self.after_read = lambda kind: None
        self.before_write = lambda: None
        self.after_write = lambda: None
        self.error = None
        self.install_write = True

    def require(self, expected):
        if expected is not None and expected is not self.authenticated_session:
            raise MaxWriteNotStarted('Synthetic session replaced before transport')

    def get_folders(self, folder_sync=None, *, expected_session=None):
        self.require(expected_session)
        self.reads.append((272, expected_session))
        rows = self.folders if self.list_filter is None else self.list_filter(self.folders)
        value = self.list_transform(copy.deepcopy(
            {'folderSync': STAMP, 'folders': rows, 'foldersOrder': self.order}))
        self.after_read(272)
        return value

    def get_folder_details(self, ids, *, expected_session=None):
        self.require(expected_session)
        self.reads.append((273, expected_session))
        rows = [row for row in self.folders if row['id'] in ids]
        if self.details_filter is not None:
            rows = self.details_filter(rows)
        value = self.details_transform(copy.deepcopy({'folders': rows}))
        self.after_read(273)
        return value

    def reorder_folders(self, order, *, expected_session=None):
        self.before_write()
        self.require(expected_session)
        self.writes.append((275, copy.deepcopy(order), expected_session))
        if self.error is not None:
            raise self.error
        if self.install_write:
            self.order = list(order)
        self.after_write()
        # An ACK echo alone is intentionally not authoritative list readback.
        return {'folderSync': STAMP, 'foldersOrder': list(order)}

    def update_folder(self, *_args, **_kwargs):
        raise AssertionError('A pending reorder must prevent unrelated folder writes')

    delete_folders = update_folder


class FolderDragCoreGuardsTests(unittest.TestCase):
    def setUp(self):
        self.service = MaxService.__new__(MaxService)
        self.service.session = SimpleNamespace(my_user_id=101, device_id=DEVICE)
        self.service.store = SimpleNamespace(cache_owner=101)
        self.service._metadata_lock = threading.RLock()
        self.service._server_metadata_owner = 101
        self.state = self.service.client = BoundFolderState()
        self.context = self.service._history_read_context()

    def submit(self, order=None, expected=None):
        return self.service.reorder_folders(
            list(MOVED) if order is None else order, context=self.context,
            expected_order=list(BASE) if expected is None else expected)

    def replace_session(self):
        self.state.authenticated_session = AuthenticatedSession(101, DEVICE, 'WEB', self.state.socket)

    def switch_owner(self, owner):
        self.service.session.my_user_id = owner
        self.service._server_metadata_owner = owner
        self.service.store.cache_owner = owner
        self.state.authenticated_session = AuthenticatedSession(owner, DEVICE, 'WEB', self.state.socket)

    def test_bound_reorder_resolves_details_and_uses_one_275_then_full_readback(self):
        self.state.list_filter = lambda rows: rows[:1]
        result = self.submit()
        self.assertTrue(result['accepted'])
        self.assertFalse(result['pending'])
        self.assertEqual(result['snapshot']['foldersOrder'], MOVED)
        self.assertEqual([opcode for opcode, _ in self.state.reads], [272, 273, 272, 273])
        self.assertTrue(all(session is self.context[2] for _, session in self.state.reads))
        self.assertEqual(self.state.writes, [(275, MOVED, self.context[2])])

    def test_partial_context_pair_and_malformed_baseline_fail_before_any_read(self):
        for kwargs in ({'context': self.context}, {'expected_order': BASE},
                       {'context': self.context, 'expected_order': BASE + ['work']},
                       {'context': self.context, 'expected_order': ['work', ALL_FOLDER, 'personal']},
                       {'context': self.context, 'expected_order': 'work'},
                       {'context': (101, self.state, object()), 'expected_order': BASE}):
            with self.subTest(kwargs=kwargs), self.assertRaises(MaxWriteNotStarted):
                self.service.reorder_folders(MOVED, **kwargs)
        self.assertEqual(self.state.reads, [])
        self.assertEqual(self.state.writes, [])

    def test_same_ids_with_changed_remote_order_reject_even_if_drop_matches_remote(self):
        self.state.order = list(MOVED)
        with self.assertRaisesRegex(MaxWriteNotStarted, 'Порядок папок изменился'):
            self.submit()
        self.assertEqual(self.state.writes, [])
        self.assertIsNone(getattr(self.service, '_pending_folder_write', None))

    def test_added_or_removed_remote_folder_invalidates_captured_complete_order(self):
        for order in (BASE + ['new'], [ALL_FOLDER, 'work']):
            with self.subTest(order=order):
                self.state.order = order
                self.state.folders = [{'id': identity, 'title': 'Synthetic'} for identity in order]
                with self.assertRaises(ValueError):
                    self.submit()
        self.assertEqual(self.state.writes, [])

    def test_unrelated_title_update_does_not_invalidate_same_order(self):
        self.state.folders[1]['title'] = 'New server title'
        result = self.submit()
        self.assertFalse(result['pending'])
        work = next(row for row in result['snapshot']['folders'] if row['id'] == 'work')
        self.assertEqual(work['title'], 'New server title')
        self.assertEqual(self.state.writes, [(275, MOVED, self.context[2])])

    def test_delta_without_full_order_and_duplicate_orders_cannot_authorize_write(self):
        for transform in (lambda value: {'folderSync': STAMP},
                          lambda value: {**value, 'foldersOrder': BASE + ['work']}):
            with self.subTest(transform=transform):
                self.state.list_transform = transform
                with self.assertRaises(ValueError):
                    self.submit()
        self.assertEqual(self.state.writes, [])

    def test_unknown_other_folder_details_do_not_drop_its_id_from_order(self):
        self.state.list_filter = lambda rows: rows[:2]
        self.state.details_filter = lambda rows: []
        result = self.submit()
        self.assertFalse(result['pending'])
        self.assertEqual(result['snapshot']['foldersOrder'], MOVED)
        self.assertEqual(result['snapshot']['missingFolderIds'], ['personal'])
        self.assertIsNone(result['snapshot']['orderedFolders'][1])
        self.assertEqual(self.state.writes[0][1], MOVED)

    def test_foreign_or_duplicate_detail_rows_fail_closed(self):
        self.state.list_filter = lambda rows: rows[:1]
        for rows in ([{'id': 'foreign', 'title': 'Other'}],
                     [{'id': 'work', 'title': 'First'}, {'id': 'work', 'title': 'Second'}]):
            with self.subTest(rows=rows):
                self.state.details_transform = lambda value: {'folders': rows}
                with self.assertRaises(ValueError):
                    self.submit()
        self.assertEqual(self.state.writes, [])

    def test_client_replacement_or_same_owner_reauth_before_worker_never_reads(self):
        original = self.state.authenticated_session
        self.replace_session()
        with self.assertRaises(ValueError):
            self.submit()
        self.state.authenticated_session = original
        self.service.client = BoundFolderState()
        with self.assertRaises(ValueError):
            self.submit()
        self.assertEqual(self.state.reads, [])
        self.assertEqual(self.state.writes, [])
        self.assertEqual(self.service.client.reads, [])

    def test_context_switch_during_list_or_details_stops_before_275(self):
        for opcode in (272, 273):
            with self.subTest(opcode=opcode):
                self.state.authenticated_session = self.context[2]
                self.state.list_filter = lambda rows: rows[:1]
                self.state.after_read = lambda actual: self.replace_session() if actual == opcode else None
                with self.assertRaises(ValueError):
                    self.submit()
        self.assertEqual(self.state.writes, [])
        self.assertIsNone(getattr(self.service, '_pending_folder_write', None))

    def test_account_switch_during_fresh_read_does_not_send_either_account_order(self):
        self.state.after_read = lambda _opcode: self.switch_owner(202)
        with self.assertRaises(ValueError):
            self.submit()
        self.assertEqual(self.state.writes, [])
        self.assertIsNone(getattr(self.service, '_pending_folder_write', None))

    def test_atomic_transport_session_switch_clears_unstarted_intent(self):
        self.state.before_write = self.replace_session
        with self.assertRaises(MaxWriteNotStarted):
            self.submit()
        self.assertEqual(self.state.writes, [])
        self.assertIsNone(self.service._pending_folder_write)

    def test_same_owner_reauth_after_write_is_unknown_without_new_session_readback(self):
        self.state.after_write = self.replace_session
        with self.assertRaises(MaxWriteOutcomeUnknown):
            self.submit()
        self.assertEqual(len(self.state.writes), 1)
        self.assertEqual(len(self.state.reads), 1)
        self.assertIsNotNone(self.service._pending_folder_write)

    def test_value_error_after_potential_write_is_never_reclassified_as_not_started(self):
        def fail_after_write():
            raise ValueError('Synthetic late processing failure')
        self.state.after_write = fail_after_write
        with self.assertRaises(MaxWriteOutcomeUnknown):
            self.submit()
        self.assertEqual(len(self.state.writes), 1)
        self.assertIsNotNone(self.service._pending_folder_write)

    def test_malformed_postwrite_readback_is_unknown_and_keeps_pending(self):
        self.state.after_write = lambda: setattr(self.state, 'list_transform', lambda _value: None)
        with self.assertRaises(MaxWriteOutcomeUnknown):
            self.submit()
        self.assertEqual(len(self.state.writes), 1)
        self.assertIsNotNone(self.service._pending_folder_write)

    def test_competing_folder_lock_stops_before_any_rpc(self):
        lock = self.service._feature_action_lock('folders')
        lock.acquire()
        try:
            with self.assertRaises(ValueError):
                self.submit()
        finally:
            lock.release()
        self.assertEqual(self.state.reads, [])
        self.assertEqual(self.state.writes, [])

    def test_unknown_write_blocks_other_mutations_until_read_only_exact_order(self):
        self.state.error = MaxWriteOutcomeUnknown('Synthetic unknown write')
        with self.assertRaises(MaxWriteOutcomeUnknown):
            self.submit()
        self.state.error = None
        operations = (self.submit, lambda: self.service.update_folder('work', title='Other'),
                      lambda: self.service.delete_folder('work'))
        for operation in operations:
            with self.assertRaises(MaxWriteOutcomeUnknown):
                operation()
        self.assertEqual(len(self.state.writes), 1)
        self.assertTrue(self.service.get_folders()['writePending'])
        self.state.order = list(MOVED)
        self.assertFalse(self.service.get_folders()['writePending'])
        self.assertEqual(len(self.state.writes), 1)

    def test_matching_ack_with_unchanged_readback_is_pending_not_optimistic_success(self):
        self.state.install_write = False
        result = self.submit()
        self.assertTrue(result['accepted'])
        self.assertTrue(result['pending'])
        self.assertEqual(result['snapshot']['foldersOrder'], BASE)
        self.assertEqual(self.service._pending_folder_write['expected'], MOVED)
        with self.assertRaises(MaxWriteOutcomeUnknown):
            self.submit()
        self.assertEqual(len(self.state.writes), 1)

    def test_explicit_nack_clears_intent_without_retry_or_order_install(self):
        self.state.error = MaxError('Synthetic negative ACK')
        with self.assertRaises(MaxError) as caught:
            self.submit()
        self.assertNotIsInstance(caught.exception, MaxWriteOutcomeUnknown)
        self.assertIsNone(self.service._pending_folder_write)
        self.assertEqual(self.state.order, BASE)
        self.assertEqual(len(self.state.writes), 1)

    def test_different_owner_read_cannot_resolve_original_owner_pending(self):
        self.state.error = MaxWriteOutcomeUnknown('Synthetic unknown write')
        with self.assertRaises(MaxWriteOutcomeUnknown):
            self.submit()
        pending = self.service._pending_folder_write
        self.switch_owner(202)
        self.state.order = list(MOVED)
        self.assertFalse(self.service.get_folders()['writePending'])
        self.assertIs(self.service._pending_folder_write, pending)
        self.switch_owner(101)
        self.assertFalse(self.service.get_folders()['writePending'])
        self.assertIsNone(self.service._pending_folder_write)
        self.assertEqual(len(self.state.writes), 1)

    def test_new_owner_bound_reorder_cannot_replace_previous_owner_unknown_intent(self):
        self.state.error = MaxWriteOutcomeUnknown('Synthetic unknown write')
        with self.assertRaises(MaxWriteOutcomeUnknown):
            self.submit()
        pending = self.service._pending_folder_write
        reads = list(self.state.reads)
        self.switch_owner(202)
        self.context = self.service._history_read_context()
        self.state.error = None
        with self.assertRaises(MaxWriteNotStarted):
            self.submit()
        self.assertIs(self.service._pending_folder_write, pending)
        self.assertEqual(self.state.reads, reads)
        self.assertEqual(len(self.state.writes), 1)

    def test_input_order_and_baseline_are_detached_before_fresh_read(self):
        desired, expected = list(MOVED), list(BASE)
        def mutate_inputs(_opcode):
            desired[:] = [ALL_FOLDER]
            expected[:] = [ALL_FOLDER]
        self.state.after_read = mutate_inputs
        result = self.submit(desired, expected)
        self.assertFalse(result['pending'])
        self.assertEqual(self.state.writes[0][1], MOVED)


class FolderDragProtocolGuardsTests(unittest.TestCase):
    def client(self, data=None):
        client = MaxClient(device_id=DEVICE, auto_reconnect=False)
        client._sock = Mock()
        client._device_type = 'WEB'
        client._state = ConnectionState.CONNECTED
        client._auth_session = AuthenticatedSession(101, DEVICE, 'WEB', client._sock)
        client.request = Mock(return_value=MaxFrame(1, 7, 275, b'',
            {'folderSync': STAMP, 'foldersOrder': MOVED} if data is None else data))
        return client

    def test_expected_session_reaches_275_with_no_added_wire_fields(self):
        client = self.client()
        session = client.authenticated_session
        client.reorder_folders(MOVED, expected_session=session)
        client.request.assert_called_once_with(275, {'foldersOrder': MOVED}, expected_session=session)

    def test_handoff_replacement_at_atomic_request_sends_to_neither_socket(self):
        client = self.client()
        original, session = client._sock, client.authenticated_session
        replacement = Mock()
        def replace(*args, **kwargs):
            client._sock = replacement
            client._auth_session = AuthenticatedSession(101, DEVICE, 'WEB', replacement)
            return MaxClient.request(client, *args, **kwargs)
        client.request = replace
        with self.assertRaises(MaxWriteNotStarted):
            client.reorder_folders(MOVED, expected_session=session)
        original.sendall.assert_not_called()
        replacement.sendall.assert_not_called()
        self.assertEqual(client._pending, {})

    def test_timeout_or_malformed_ack_is_unknown_without_retry(self):
        for ack in ({}, {'folderSync': True, 'foldersOrder': MOVED},
                    {'folderSync': STAMP, 'foldersOrder': [1]}):
            with self.subTest(ack=ack):
                client = self.client(ack)
                with self.assertRaises(MaxWriteOutcomeUnknown):
                    client.reorder_folders(MOVED, expected_session=client.authenticated_session)
                client.request.assert_called_once()
        client = self.client()
        client.request.side_effect = MaxTimeout('Synthetic timeout')
        with self.assertRaises(MaxWriteOutcomeUnknown):
            client.reorder_folders(MOVED, expected_session=client.authenticated_session)
        client.request.assert_called_once()

    def test_legacy_reorder_and_delete_call_shapes_are_unchanged(self):
        client = self.client()
        client.reorder_folders(MOVED)
        client.request.assert_called_once_with(275, {'foldersOrder': MOVED})
        client = self.client()
        client.request.return_value = MaxFrame(1, 7, 276, b'', {'folderSync': STAMP, 'foldersOrder': BASE[:1]})
        client.delete_folders(['work'])
        client.request.assert_called_once_with(276, {'folderIds': ['work']})


if __name__ == '__main__':
    unittest.main()
