"""Bound folder editor writes, synthetic sessions and fake RPC only."""
import copy
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

import test_folders_drag_guards as base
from maxclient.core.service import MaxService
from maxclient.protocol.client import (
    AuthenticatedSession, ConnectionState, MaxClient, MaxError, MaxFrame,
    MaxTimeout, MaxWriteNotStarted, MaxWriteOutcomeUnknown,
)


class EditorState(base.BoundFolderState):
    def _write(self, opcode, payload, expected_session):
        self.before_write()
        self.require(expected_session)
        self.writes.append((opcode, copy.deepcopy(payload), expected_session))
        if self.error is not None:
            raise self.error

    def update_folder(self, payload, *, expected_session=None):
        self._write(274, payload, expected_session)
        if self.install_write:
            self.folders = [row for row in self.folders if row['id'] != payload['id']] + [copy.deepcopy(payload)]
            if payload['id'] not in self.order:
                self.order.append(payload['id'])
        self.after_write()
        return {'folder': copy.deepcopy(payload), 'folderSync': base.STAMP}

    def delete_folders(self, identities, *, expected_session=None):
        self._write(276, identities, expected_session)
        if self.install_write:
            self.order = [identity for identity in self.order if identity not in identities]
            self.folders = [row for row in self.folders if row['id'] not in identities]
        self.after_write()
        return {'folderSync': base.STAMP, 'foldersOrder': list(self.order)}


class FolderEditorContextTests(unittest.TestCase):
    def setUp(self):
        self.reset_state()

    def reset_state(self):
        self.service = MaxService.__new__(MaxService)
        self.service.session = SimpleNamespace(my_user_id=101, device_id=base.DEVICE)
        self.service.store = SimpleNamespace(cache_owner=101)
        self.service._metadata_lock = threading.RLock()
        self.service._server_metadata_owner = 101
        self.state = self.service.client = EditorState()
        self.context = self.service._history_read_context()

    def submit(self, kind='update', **kwargs):
        if kind == 'create':
            return self.service.create_folder('New synthetic', include=[0, -42], filters=[8], context=self.context, **kwargs)
        if kind == 'delete':
            return self.service.delete_folder('work', context=self.context, **kwargs)
        return self.service.update_folder('work', include=[0, -42], filters=[8], context=self.context, **kwargs)

    def reconnect(self):
        self.state.authenticated_session = AuthenticatedSession(101, base.DEVICE, 'WEB', self.state.socket)

    def switch_owner(self):
        self.service.session.my_user_id = 404
        self.service._server_metadata_owner = 404
        self.service.store.cache_owner = 404
        self.state.authenticated_session = AuthenticatedSession(404, base.DEVICE, 'WEB', self.state.socket)

    def test_create_update_delete_bind_reads_writes_results_and_snapshot(self):
        for kind in ('create', 'update', 'delete'):
            with self.subTest(kind=kind):
                self.reset_state()
                self.state.list_filter = lambda rows: rows[:1]
                result = self.submit(kind)
                self.assertTrue(result['accepted'])
                self.assertFalse(result['pending'])
                self.assertIs(result['editor_context'], self.context)
                self.assertIs(result['snapshot']['editor_context'], self.context)
                self.assertEqual(len(self.state.writes), 1)
                self.assertEqual(self.state.writes[0][0], 276 if kind == 'delete' else 274)
                self.assertIs(self.state.writes[0][2], self.context[2])
                self.assertEqual([op for op, _ in self.state.reads], [272, 273, 272, 273])
                self.assertTrue(all(session is self.context[2] for _, session in self.state.reads))

    def test_queued_operation_rejects_replaced_owner_client_or_session_before_any_read(self):
        for kind in ('create', 'update', 'delete'):
            for change in ('owner', 'client', 'session'):
                with self.subTest(kind=kind, change=change):
                    self.reset_state()
                    old = self.state
                    if change == 'owner':
                        self.switch_owner()
                    elif change == 'client':
                        self.service.client = EditorState()
                    else:
                        self.reconnect()
                    with self.assertRaises(MaxWriteNotStarted):
                        self.submit(kind)
                    self.assertEqual(old.reads, [])
                    self.assertEqual(old.writes, [])
                    self.assertEqual(self.service.client.writes, [])

    def test_context_changes_during_272_or_273_stop_before_any_editor_write(self):
        for kind in ('create', 'update', 'delete'):
            for step in (272, 273):
                with self.subTest(kind=kind, step=step):
                    self.reset_state()
                    self.state.list_filter = lambda rows: rows[:1]
                    self.state.after_read = lambda op: self.reconnect() if op == step else None
                    with self.assertRaises(MaxWriteNotStarted):
                        self.submit(kind)
                    self.assertEqual(self.state.writes, [])
                    self.assertIsNone(getattr(self.service, '_pending_folder_write', None))

    def test_atomic_transport_rejection_clears_only_unstarted_intent(self):
        for kind in ('create', 'update', 'delete'):
            with self.subTest(kind=kind):
                self.reset_state()
                self.state.before_write = self.reconnect
                with self.assertRaises(MaxWriteNotStarted):
                    self.submit(kind)
                self.assertEqual(self.state.writes, [])
                self.assertIsNone(self.service._pending_folder_write)

    def test_reconnect_or_owner_change_after_write_preserves_unknown_without_reading_new_session(self):
        for kind in ('create', 'update', 'delete'):
            for owner_change in (False, True):
                with self.subTest(kind=kind, owner_change=owner_change):
                    self.reset_state()
                    self.state.after_write = self.switch_owner if owner_change else self.reconnect
                    with self.assertRaises(MaxWriteOutcomeUnknown):
                        self.submit(kind)
                    self.assertEqual(len(self.state.writes), 1)
                    self.assertEqual([op for op, _ in self.state.reads], [272])
                    self.assertEqual(self.service._pending_folder_write['owner'], 101)

    def test_read_only_current_session_recovers_same_owner_pending_without_replay(self):
        self.state.after_write = self.reconnect
        with self.assertRaises(MaxWriteOutcomeUnknown):
            self.submit()
        current = self.service._history_read_context()
        snapshot = self.service._get_reorder_folders(current)
        self.assertFalse(snapshot['writePending'])
        self.assertIsNone(self.service._pending_folder_write)
        self.assertEqual(len(self.state.writes), 1)

    def test_unknown_and_conflicting_readback_do_not_allow_repeat_write(self):
        for error in (MaxWriteOutcomeUnknown('Synthetic timeout'), ValueError('Synthetic post-dispatch parse')):
            with self.subTest(error=type(error).__name__):
                self.reset_state()
                self.state.error = error
                with self.assertRaises(MaxWriteOutcomeUnknown):
                    self.submit()
                pending = self.service._pending_folder_write
                with self.assertRaises(MaxWriteOutcomeUnknown):
                    self.submit('delete')
                self.assertIs(self.service._pending_folder_write, pending)
                self.assertEqual(len(self.state.writes), 1)
        self.reset_state()
        self.state.install_write = False
        self.assertTrue(self.submit()['pending'])
        self.assertEqual(len(self.state.writes), 1)

    def test_explicit_negative_ack_clears_intent_but_never_retries(self):
        for kind in ('create', 'update', 'delete'):
            with self.subTest(kind=kind):
                self.reset_state()
                self.state.error = MaxError('Synthetic negative ACK')
                with self.assertRaises(MaxError) as caught:
                    self.submit(kind)
                self.assertNotIsInstance(caught.exception, MaxWriteOutcomeUnknown)
                self.assertIsNone(self.service._pending_folder_write)
                self.assertEqual(len(self.state.writes), 1)

    def test_foreign_pending_cannot_be_overwritten_or_reconciled_by_editor(self):
        pending = {'owner': 404, 'kind': 'delete', 'expected': 'absent'}
        self.service._pending_folder_write = pending
        for kind in ('create', 'update', 'delete'):
            with self.assertRaises(MaxWriteNotStarted):
                self.submit(kind)
        self.assertIs(self.service._pending_folder_write, pending)
        self.assertEqual(self.state.reads, [])
        self.assertEqual(self.state.writes, [])

    def test_invalid_values_locks_and_missing_folder_are_known_not_started(self):
        for operation in (lambda: self.service.create_folder('', context=self.context),
                          lambda: self.service.update_folder('work', include=[True], context=self.context),
                          lambda: self.service.delete_folder(base.ALL_FOLDER, context=self.context),
                          lambda: self.service.delete_folder('absent', context=self.context)):
            with self.assertRaises(MaxWriteNotStarted):
                operation()
        self.state.folders[1]['options'] = [1, 2, 3]
        for operation in (lambda: self.service.update_folder('work', title='Changed', context=self.context),
                          lambda: self.service.update_folder('work', filters=[8], context=self.context),
                          lambda: self.service.delete_folder('work', context=self.context)):
            with self.assertRaises(MaxWriteNotStarted):
                operation()
        self.assertEqual(self.state.writes, [])

    def test_noop_is_bound_and_preserves_omitted_fields_and_exact_favorite_order(self):
        original = dict(self.state.folders[1], include=[7], filters=[8], options=[0], favorites=[81, -42])
        self.state.folders[1] = original
        result = self.service.update_folder('work', include=[7], context=self.context)
        self.assertTrue(result['unchanged'])
        self.assertIs(result['editor_context'], self.context)
        self.assertIs(result['snapshot']['editor_context'], self.context)
        self.assertEqual(self.state.writes, [])
        self.service.update_folder('work', include=[7, 0], context=self.context)
        payload = self.state.writes[0][1]
        self.assertEqual((payload['filters'], payload['options'], payload['favorites']), ([8], [0], [81, -42]))

    def test_input_lists_are_detached_before_preflight_reads(self):
        include, filters = [0, -42], [8]
        def change_inputs(_op):
            include.append(777)
            filters.append(9)
        self.state.after_read = change_inputs
        self.service.update_folder('work', include=include, filters=filters, context=self.context)
        self.assertEqual(self.state.writes[0][1]['include'], [0, -42])
        self.assertEqual(self.state.writes[0][1]['filters'], [8])

    def test_busy_and_malformed_context_fail_before_network(self):
        lock = self.service._feature_action_lock('folders')
        with lock:
            with self.assertRaises(MaxWriteNotStarted):
                self.submit()
        for context in ((101, self.state, object()), (True, self.state, self.context[2]), (), 'context'):
            with self.assertRaises(MaxWriteNotStarted):
                self.service.delete_folder('work', context=context)
        self.assertEqual(self.state.reads, [])
        self.assertEqual(self.state.writes, [])


class FolderEditorDeleteProtocolTests(unittest.TestCase):
    def client(self):
        client = MaxClient(device_id=base.DEVICE, auto_reconnect=False)
        client._sock = Mock()
        client._device_type = 'WEB'
        client._state = ConnectionState.CONNECTED
        client._auth_session = AuthenticatedSession(101, base.DEVICE, 'WEB', client._sock)
        client.request = Mock(return_value=MaxFrame(1, 1, 276, b'',
            {'folderSync': base.STAMP, 'foldersOrder': [base.ALL_FOLDER]}))
        return client

    def test_delete_expected_session_reaches_276_without_new_wire_fields(self):
        client = self.client()
        session = client.authenticated_session
        client.delete_folders(['work'], expected_session=session)
        client.request.assert_called_once_with(276, {'folderIds': ['work']}, expected_session=session)

    def test_bound_delete_handoff_sends_to_neither_old_nor_replacement_socket(self):
        client = self.client()
        old, session = client._sock, client.authenticated_session
        replacement = Mock()
        def replace(*args, **kwargs):
            client._sock = replacement
            client._auth_session = AuthenticatedSession(101, base.DEVICE, 'WEB', replacement)
            return MaxClient.request(client, *args, **kwargs)
        client.request = replace
        with self.assertRaises(MaxWriteNotStarted):
            client.delete_folders(['work'], expected_session=session)
        old.sendall.assert_not_called()
        replacement.sendall.assert_not_called()
        self.assertEqual(client._pending, {})

    def test_bound_delete_timeout_stays_unknown_and_legacy_shape_unchanged(self):
        client = self.client()
        client.request.side_effect = MaxTimeout('Synthetic timeout')
        with self.assertRaises(MaxWriteOutcomeUnknown):
            client.delete_folders(['work'], expected_session=client.authenticated_session)
        client.request.assert_called_once()
        client = self.client()
        client.delete_folders(['work'])
        client.request.assert_called_once_with(276, {'folderIds': ['work']})


if __name__ == '__main__':
    unittest.main()
