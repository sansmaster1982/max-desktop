"""Current public folder/contact packets, fake transport only."""
import unittest
from unittest.mock import Mock

from maxclient.protocol.client import MaxClient, MaxFrame, MaxError, MaxTimeout, MaxNotConnected, MaxWriteOutcomeUnknown


STAMP = 1_700_000_000_000


def contact(**fields):
    return {'id': 77, 'updateTime': STAMP, 'names': [{'type': 'ONEME', 'firstName': 'Synthetic'}], **fields}


class FolderBlockProtocolTests(unittest.TestCase):
    def client(self, opcode, data):
        client = MaxClient(auto_reconnect=False)
        client.request = Mock(return_value=MaxFrame(1, 7, opcode, b'', data))
        return client

    def test_folder274_uses_only_proven_payload_and_preserves_authoritative_ack(self):
        payload = {'id': 'synthetic', 'title': ' Name ', 'include': [-77], 'filters': [4], 'options': [], 'favorites': [-77]}
        ack = {'folder': {**payload, 'title': 'Name'}, 'folderSync': STAMP}
        client = self.client(274, ack)
        self.assertEqual(client.update_folder(payload), ack)
        client.request.assert_called_once_with(274, {**payload, 'title': 'Name'})

    def test_invalid_folder_input_sends_nothing(self):
        client = self.client(274, {})
        for patch in ({'title': ''}, {'title': 'x' * 21}, {'title': '\U0001f600' * 11},
                      {'include': [True]}, {'favorites': [1, 1]}, {'filters': [15]},
                      {'options': [5]}, {'exclude': [1]}, {'emoji': 'unproven'}):
            with self.subTest(patch=patch), self.assertRaises(ValueError):
                client.update_folder({'id': 'x', 'title': 'Valid', **patch})
        client.request.assert_not_called()

    def test_folder_write_unknown_ack_is_not_retried(self):
        for ack in ({}, {'folder': {'id': 'other', 'title': 'Other'}}, {'folder': {'id': 'x', 'title': 'X'}, 'folderSync': True}):
            client = self.client(274, ack)
            with self.assertRaises(MaxWriteOutcomeUnknown):client.update_folder({'id': 'x', 'title': 'X'})
            self.assertEqual(client.request.call_count, 1)

    def test_all_folder_writes_convert_disconnect_timeout_without_retry(self):
        for error in (MaxTimeout('synthetic'), MaxNotConnected('synthetic')):
            for method, args in (('update_folder', ({'id': 'x', 'title': 'X'},)),
                                 ('reorder_folders', (['all.chat.folder', 'x'],)), ('delete_folders', (['x'],))):
                client = self.client(274, {})
                client.request.side_effect = error
                with self.assertRaises(MaxWriteOutcomeUnknown):getattr(client, method)(*args)
                self.assertEqual(client.request.call_count, 1)

    def test_order_and_delete_are_distinct_exact_packets(self):
        ack = {'folderSync': STAMP, 'foldersOrder': ['all.chat.folder']}
        client = self.client(275, ack)
        self.assertEqual(client.reorder_folders(['all.chat.folder']), ack)
        client.request.assert_called_once_with(275, {'foldersOrder': ['all.chat.folder']})
        client = self.client(276, ack)
        client.delete_folders(['synthetic'])
        client.request.assert_called_once_with(276, {'folderIds': ['synthetic']})

    def test_missing_required_order_ack_is_unknown(self):
        for ack in ({}, {'folderSync': STAMP}, {'folderSync': STAMP, 'foldersOrder': [7]}):
            client = self.client(276, ack)
            with self.assertRaises(MaxWriteOutcomeUnknown):client.delete_folders(['synthetic'])
            self.assertEqual(client.request.call_count, 1)

    def test_other_opcode_reply_never_confirms_a_write(self):
        client = self.client(275, {'folder': {'id': 'x', 'title': 'X'}})
        with self.assertRaises(MaxWriteOutcomeUnknown):client.update_folder({'id': 'x', 'title': 'X'})
        client = self.client(274, {'folderSync': STAMP, 'foldersOrder': []})
        with self.assertRaises(MaxWriteOutcomeUnknown):client.delete_folders(['x'])
        client = self.client(36, {'contact': contact()})
        with self.assertRaises(MaxWriteOutcomeUnknown):client.block_contact(77)

    def test_block_list_uses_actual_first_page_and_no_invented_cursor(self):
        ack = {'contacts': [contact(status='BLOCKED')]}
        client = self.client(36, ack)
        self.assertEqual(client.list_blocked_contacts(), ack)
        client.request.assert_called_once_with(36, {'status': 'BLOCKED', 'from': 0, 'count': 100}, timeout=30)

    def test_block_unblock_ack_contact_is_optional_and_not_a_fake_status(self):
        for method, action in (('block_contact', 'BLOCK'), ('unblock_contact', 'UNBLOCK')):
            client = self.client(34, {})
            self.assertEqual(getattr(client, method)(-77), {})
            client.request.assert_called_once_with(34, {'contactId': -77, 'action': action})

    def test_contact_ack_must_match_typed_contact_when_supplied(self):
        for ack in ({'contact': contact(id=99)}, {'contact': {'id': 77}}, {'contact': contact(status='UNBLOCKED')}, None):
            client = self.client(34, ack)
            with self.assertRaises(MaxWriteOutcomeUnknown):client.block_contact(77)
            self.assertEqual(client.request.call_count, 1)

    def test_invalid_contact_ids_never_request(self):
        client = self.client(34, {})
        for identity in (True, None, '77', 2 ** 63):
            with self.assertRaises(ValueError):client.block_contact(identity)
        client.request.assert_not_called()

    def test_contact_timeout_unknown_and_explicit_nack_are_distinct(self):
        client = self.client(34, {})
        client.request.side_effect = MaxTimeout('synthetic')
        with self.assertRaises(MaxWriteOutcomeUnknown):client.unblock_contact(77)
        self.assertEqual(client.request.call_count, 1)
        client = self.client(34, {})
        client.request.return_value = MaxFrame(3, 7, 34, b'', {'error': 'synthetic.denied'})
        with self.assertRaises(MaxError) as caught:client.block_contact(77)
        self.assertNotIsInstance(caught.exception, MaxWriteOutcomeUnknown)

    def test_blocked_read_rejects_malformed_or_other_status_contacts(self):
        for ack in ({}, {'contacts': [contact(status='HIDDEN')]}, {'contacts': [{'id': 77}]},
                    {'contacts': [contact(names=['untyped'])]}):
            client = self.client(36, ack)
            with self.assertRaises(MaxError):client.list_blocked_contacts()


if __name__ == '__main__':unittest.main()
