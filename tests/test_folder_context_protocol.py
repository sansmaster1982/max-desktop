"""Optional folder context propagation, no actual sockets or account data."""
import unittest
from unittest.mock import Mock

from maxclient.protocol.client import (
    AuthenticatedSession, ConnectionState, MaxClient, MaxFrame, MaxTimeout,
    MaxWriteNotStarted, MaxWriteOutcomeUnknown,
)


FOLDER = {'id': 'synthetic', 'title': 'Synthetic', 'include': [303]}


class FolderContextProtocolTests(unittest.TestCase):
    def client(self, opcode, data):
        client = MaxClient(auto_reconnect=False)
        client._sock = Mock()
        client._device_type = 'WEB'
        client._state = ConnectionState.CONNECTED
        client._auth_session = AuthenticatedSession(101, client.device_id, 'WEB', client._sock)
        client.request = Mock(return_value=MaxFrame(1, 1, opcode, b'', data))
        return client

    def test_optional_context_reaches_atomic_request_for_all_three_wrappers(self):
        cases = [('get_folders', (), 272, {'folderSync': 1}, {}, {'timeout': 30}),
                 ('get_folder_details', (['synthetic'],), 273, {'folders': [FOLDER]},
                  {'folderIds': ['synthetic']}, {'timeout': 30}),
                 ('update_folder', (FOLDER,), 274, {'folder': FOLDER}, FOLDER, {})]
        for method, args, opcode, ack, payload, keywords in cases:
            with self.subTest(method=method):
                client = self.client(opcode, ack)
                context = client.authenticated_session
                self.assertEqual(getattr(client, method)(*args, expected_session=context), ack)
                client.request.assert_called_once_with(opcode, payload, expected_session=context, **keywords)

    def test_default_none_preserves_legacy_request_call_shapes(self):
        cases = [('get_folders', (), 272, {'folderSync': 1}, {}, {'timeout': 30}),
                 ('get_folder_details', (['synthetic'],), 273, {'folders': []},
                  {'folderIds': ['synthetic']}, {'timeout': 30}),
                 ('update_folder', (FOLDER,), 274, {'folder': FOLDER}, FOLDER, {})]
        for method, args, opcode, ack, payload, keywords in cases:
            with self.subTest(method=method):
                client = self.client(opcode, ack)
                getattr(client, method)(*args)
                client.request.assert_called_once_with(opcode, payload, **keywords)

    def test_handoff_replacement_never_reaches_old_or_new_socket(self):
        for method, args in [('get_folders', ()), ('get_folder_details', (['synthetic'],)), ('update_folder', (FOLDER,))]:
            with self.subTest(method=method):
                client = self.client(274, {})
                original_socket, context = client._sock, client.authenticated_session
                replacement = Mock()
                def replace(*args, **kwargs):
                    client._sock = replacement
                    client._auth_session = AuthenticatedSession(101, client.device_id, 'WEB', replacement)
                    return MaxClient.request(client, *args, **kwargs)
                client.request = replace
                with self.assertRaises(MaxWriteNotStarted):
                    getattr(client, method)(*args, expected_session=context)
                original_socket.sendall.assert_not_called()
                replacement.sendall.assert_not_called()
                self.assertEqual(client._pending, {})

    def test_folder_write_timeout_is_unknown_and_not_retried(self):
        client = self.client(274, {})
        client.request.side_effect = MaxTimeout('synthetic')
        with self.assertRaises(MaxWriteOutcomeUnknown):
            client.update_folder(FOLDER, expected_session=client.authenticated_session)
        client.request.assert_called_once()

    def test_contact_context_wrappers_propagate_and_none_keeps_legacy_shapes(self):
        for context_enabled in (False, True):
            for method, action in (('block_contact', 'BLOCK'), ('unblock_contact', 'UNBLOCK')):
                with self.subTest(context_enabled=context_enabled, method=method):
                    client = self.client(34, {})
                    kwargs = {'expected_session': client.authenticated_session} if context_enabled else {}
                    self.assertEqual(getattr(client, method)(202, **kwargs), {})
                    client.request.assert_called_once_with(34, {'contactId': 202, 'action': action}, **kwargs)
            client = self.client(36, {'contacts': []})
            kwargs = {'expected_session': client.authenticated_session} if context_enabled else {}
            self.assertEqual(client.list_blocked_contacts(**kwargs), {'contacts': []})
            client.request.assert_called_once_with(36, {'status': 'BLOCKED', 'from': 0, 'count': 100}, timeout=30, **kwargs)

    def test_contact_replacement_never_issues_block_unblock_or_blocklist_read(self):
        for method, args in [('block_contact', (202,)), ('unblock_contact', (202,)), ('list_blocked_contacts', ())]:
            with self.subTest(method=method):
                client = self.client(34, {})
                old, context = client._sock, client.authenticated_session
                replacement = Mock()
                def replace(*args, **kwargs):
                    client._sock = replacement
                    client._auth_session = AuthenticatedSession(101, client.device_id, 'WEB', replacement)
                    return MaxClient.request(client, *args, **kwargs)
                client.request = replace
                with self.assertRaises(MaxWriteNotStarted): getattr(client, method)(*args, expected_session=context)
                old.sendall.assert_not_called()
                replacement.sendall.assert_not_called()
                self.assertEqual(client._pending, {})


if __name__ == '__main__':
    unittest.main()
