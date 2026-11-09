"""Folder/contact actions use fake current state and never real accounts."""
import copy
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from maxclient.core.service import MaxService
from maxclient.protocol.client import MaxError, MaxWriteOutcomeUnknown
from test_folders_blocks_protocol import STAMP, contact


def folder(identity='one', **fields):
    return {'id': identity, 'title': 'Synthetic', 'include': [], 'filters': [], 'options': [], 'favorites': [], **fields}


class CurrentServer:
    def __init__(self):
        self.folders = [folder('all.chat.folder', options=[1, 2]), folder(), folder('two')]
        self.order = [item['id'] for item in self.folders]
        self.contact = contact()
        self.writes = []
        self.error = None
        self.folder_read_error = False
        self.contact_read_error = False
        self.after_write = lambda: None

    def get_folders(self, folder_sync=None):
        if self.folder_read_error:raise MaxError('synthetic read failure')
        return copy.deepcopy({'folderSync': STAMP, 'folders': self.folders, 'foldersOrder': self.order})

    def get_folder_details(self, ids):raise AssertionError('No details missing in this fixture')

    def _write(self, operation, payload):
        self.writes.append((operation, copy.deepcopy(payload)))
        if self.error:raise self.error

    def update_folder(self, payload):
        self._write(274, payload)
        self.folders = [item for item in self.folders if item['id'] != payload['id']] + [copy.deepcopy(payload)]
        if payload['id'] not in self.order:self.order.append(payload['id'])
        self.after_write()
        return {'folder': payload, 'folderSync': STAMP}

    def delete_folders(self, ids):
        self._write(276, ids)
        self.order = [item for item in self.order if item not in ids]
        self.folders = [item for item in self.folders if item['id'] not in ids]
        self.after_write()
        return {'folderSync': STAMP, 'foldersOrder': self.order}

    def reorder_folders(self, order):
        self._write(275, order);self.order = list(order);self.after_write()
        return {'folderSync': STAMP, 'foldersOrder': self.order}

    def contact_info(self, ids):
        if self.contact_read_error:raise MaxError('synthetic contact read failure')
        return {'contacts': [copy.deepcopy(self.contact)]}

    def list_blocked_contacts(self):
        return {'contacts': [copy.deepcopy(self.contact)] if self.contact.get('status') == 'BLOCKED' else []}

    def block_contact(self, identity):
        self._write(34, {'contactId': identity, 'action': 'BLOCK'})
        self.contact['status'] = 'BLOCKED';self.after_write();return {}

    def unblock_contact(self, identity):
        self._write(34, {'contactId': identity, 'action': 'UNBLOCK'})
        self.contact.pop('status', None);self.after_write();return {}


class FolderBlockCoreTests(unittest.TestCase):
    def setUp(self):
        self.service = MaxService.__new__(MaxService)
        self.service.session = SimpleNamespace(my_user_id=42)
        self.service._metadata_lock = threading.RLock()
        self.service._server_metadata_owner = 42
        self.service._server_contacts = {}
        self.service._server_chats = {}
        self.service.store = Mock()
        self.server = self.service.client = CurrentServer()

    def test_create_uses_generated_uuid_and_actual_readback(self):
        result = self.service.create_folder(' New ', include=[77], filters=[4])
        self.assertTrue(result['accepted']);self.assertFalse(result['pending'])
        self.assertEqual(result['folderId'], self.server.writes[0][1]['id'])
        self.assertEqual(len(result['folderId']), 36)
        self.assertEqual(result['snapshot']['folders'][-1]['title'], 'New')
        self.assertFalse(result['snapshot']['writePending'])
        self.assertEqual(len(self.server.writes), 1)

    def test_update_preserves_server_options_and_favorite_order(self):
        self.server.folders[1] = folder(options=[0], favorites=[8, 7], include=[9])
        result = self.service.update_folder('one', title='Renamed')
        sent = self.server.writes[0][1]
        self.assertEqual((sent['options'], sent['favorites'], sent['include']), ([0], [8, 7], [9]))
        self.assertFalse(result['pending'])

    def test_protected_folder_options_stop_writes(self):
        self.server.folders[1] = folder(options=[1, 2, 3])
        for operation in (lambda: self.service.delete_folder('one'),
                          lambda: self.service.update_folder('one', title='Other'),
                          lambda: self.service.update_folder('one', filters=[4]),
                          lambda: self.service.delete_folder('all.chat.folder')):
            with self.assertRaises(ValueError):operation()
        self.assertEqual(self.server.writes, [])
        self.assertFalse(self.service.update_folder('one', include=[77])['pending'])

    def test_noop_and_wrong_order_do_not_write(self):
        self.assertTrue(self.service.update_folder('one', title='Synthetic')['unchanged'])
        self.assertTrue(self.service.reorder_folders(self.server.order)['unchanged'])
        for order in (['one', 'all.chat.folder', 'two'], ['all.chat.folder', 'one']):
            with self.assertRaises(ValueError):self.service.reorder_folders(order)
        self.assertEqual(self.server.writes, [])

    def test_order_and_delete_use_fresh_order_readback(self):
        self.assertFalse(self.service.reorder_folders(['all.chat.folder', 'two', 'one'])['pending'])
        result = self.service.delete_folder('one')
        self.assertFalse(result['pending'])
        self.assertNotIn('one', result['snapshot']['foldersOrder'])
        self.assertEqual([item[0] for item in self.server.writes], [275, 276])

    def test_unknown_create_blocks_repetition_until_matching_uuid_readback(self):
        self.server.error = MaxWriteOutcomeUnknown('synthetic unknown')
        with self.assertRaises(MaxWriteOutcomeUnknown):self.service.create_folder('New')
        with self.assertRaises(MaxWriteOutcomeUnknown):self.service.create_folder('New')
        self.assertEqual(len(self.server.writes), 1)
        expected = self.server.writes[0][1]
        self.server.folders.append(expected);self.server.order.append(expected['id'])
        self.assertFalse(self.service.get_folders()['writePending'])

    def test_post_write_read_failure_is_unknown_not_success_or_repeat(self):
        self.server.after_write = lambda: setattr(self.server, 'folder_read_error', True)
        with self.assertRaises(MaxWriteOutcomeUnknown):self.service.update_folder('one', title='New')
        self.assertIsNotNone(self.service._pending_folder_write)
        self.assertEqual(len(self.server.writes), 1)
        self.server.folder_read_error = False
        self.assertFalse(self.service.get_folders()['writePending'])

    def test_explicit_nack_clears_pending_but_does_not_retry(self):
        self.server.error = MaxError('synthetic negative ACK')
        with self.assertRaises(MaxError):self.service.delete_folder('one')
        self.assertIsNone(self.service._pending_folder_write)
        self.assertEqual(len(self.server.writes), 1)

    def test_owner_switch_after_write_is_unknown(self):
        def switch():
            self.service.session.my_user_id = 99
            self.service._server_metadata_owner = 99
        self.server.after_write = switch
        with self.assertRaises(MaxWriteOutcomeUnknown):self.service.delete_folder('one')

    def test_missing_unconfirmed_owner_prevents_new_rpc(self):
        self.service._server_metadata_owner = None
        with self.assertRaises(ValueError):self.service.create_folder('New')
        with self.assertRaises(ValueError):self.service.list_blocked_contacts()
        self.assertEqual(self.server.writes, [])

    def test_block_and_unblock_require_contact_readback_not_empty_ack(self):
        result = self.service.block_contact(77)
        self.assertFalse(result['pending']);self.assertTrue(result['blocked'])
        self.assertEqual(result['snapshot']['pagination'], 'first_page_only')
        result = self.service.unblock_contact(77)
        self.assertFalse(result['pending']);self.assertFalse(result['blocked'])
        self.assertNotIn('status', self.server.contact)
        self.assertEqual(result['snapshot']['contacts'], [])
        self.assertEqual(len(self.server.writes), 2)

    def test_missing_status_on_full_typed_contact_is_normal_not_account_status(self):
        self.server.contact['accountStatus'] = 1
        self.assertFalse(self.service.get_contact_block_status(77)['blocked'])

    def test_first_page_absence_never_resolves_unknown_unblock(self):
        self.server.contact['status'] = 'BLOCKED'
        self.server.error = MaxWriteOutcomeUnknown('synthetic unknown')
        with self.assertRaises(MaxWriteOutcomeUnknown):self.service.unblock_contact(77)
        self.server.list_blocked_contacts = lambda: {'contacts': []}
        self.service.list_blocked_contacts()
        self.assertIsNotNone(self.service._pending_contact_write)
        with self.assertRaises(MaxWriteOutcomeUnknown):self.service.unblock_contact(77)
        self.assertEqual(len(self.server.writes), 1)
        self.server.contact.pop('status')
        self.service.get_contact_block_status(77)
        self.assertIsNone(self.service._pending_contact_write)

    def test_contact_postwrite_read_failure_is_unknown(self):
        self.server.after_write = lambda: setattr(self.server, 'contact_read_error', True)
        with self.assertRaises(MaxWriteOutcomeUnknown):self.service.block_contact(77)
        self.assertIsNotNone(self.service._pending_contact_write)

    def test_matching_contact_status_noop_and_self_do_not_write(self):
        self.server.contact['status'] = 'BLOCKED'
        self.assertTrue(self.service.block_contact(77)['unchanged'])
        with self.assertRaises(ValueError):self.service.block_contact(42)
        self.assertEqual(self.server.writes, [])

    def test_wrong_contact_and_invalid_status_fail_before_write(self):
        self.server.contact['id'] = 99
        with self.assertRaises(ValueError):self.service.block_contact(77)
        self.server.contact['id'] = 77;self.server.contact['status'] = 'UNBLOCKED'
        with self.assertRaises(ValueError):self.service.block_contact(77)
        self.assertEqual(self.server.writes, [])

    def test_concurrent_folder_action_is_rejected_before_read_or_write(self):
        lock = self.service._feature_action_lock('folders')
        lock.acquire()
        try:
            with self.assertRaises(ValueError):self.service.create_folder('New')
        finally:lock.release()
        self.assertEqual(self.server.writes, [])


if __name__ == '__main__':unittest.main()
