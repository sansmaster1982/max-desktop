"""FILE_DOWNLOAD88 syntax/ACK and session-bound fake transport only."""
import math
import unittest
from unittest.mock import Mock

from maxclient.protocol.client import (
    AuthenticatedSession, ConnectionState, MaxClient, MaxError, MaxFrame,
    MaxTimeout, MaxWriteNotStarted,
)
from maxclient.protocol.file_download_contracts import (
    FileDownloadReply, build_file_download, parse_file_download,
)


URL = 'https://files.example.test/actual?signature=synthetic-private'


class FileDownloadContractsTests(unittest.TestCase):
    def client(self, opcode=88, data=None):
        client = MaxClient(auto_reconnect=False)
        client._sock = Mock()
        client._device_type = 'WEB'
        client._state = ConnectionState.CONNECTED
        client._auth_session = AuthenticatedSession(101, client.device_id, 'WEB', client._sock)
        client.request = Mock(return_value=MaxFrame(1, 1, opcode, b'', data))
        return client

    def test_exact_request_preserves_webnumber_file_id_and_known_item_types(self):
        for identity in (0, -1, 12.5):
            for kind in ('REGULAR', 'DELAYED'):
                self.assertEqual(build_file_download(identity, 303, 7, item_type=kind),
                    {'fileId': identity, 'chatId': 303, 'messageId': 7, 'itemType': kind})
        for identity in (True, None, '12', math.nan, math.inf):
            with self.assertRaises(ValueError): build_file_download(identity, 303, 7)
        for chat, message in ((True, 7), (303, False), ('303', 7), (303, 2**63)):
            with self.assertRaises(ValueError): build_file_download(12.5, chat, message)
        with self.assertRaises(ValueError): build_file_download(1, 303, 7, item_type='GUESS')

    def test_reply_requires_actual_unsafe_boolean_and_supported_url(self):
        for unsafe in (True, False):
            reply = parse_file_download({'url': URL, 'unsafe': unsafe, 'name': 'not-authoritative'})
            self.assertIsInstance(reply, FileDownloadReply)
            self.assertEqual(reply.url, URL)
            self.assertIs(reply.unsafe, unsafe)
            self.assertNotIn('signature', repr(reply))
            self.assertFalse(hasattr(reply, 'name'))
        for raw in ({'url': URL}, {'url': URL, 'unsafe': 0}, {'url': URL, 'unsafe': None},
                    {'url': 'file:///private', 'unsafe': False}, {'url': 'https://127.0.0.1/private', 'unsafe': False}):
            with self.assertRaises(ValueError) as error: parse_file_download(raw)
            self.assertNotIn('signature', str(error.exception))

    def test_strict_wrapper_one_read_with_bound_context_and_legacy_call_is_unchanged(self):
        client = self.client(data={'url': URL, 'unsafe': False})
        session = client.authenticated_session
        reply = client.conversation_file_download(12.5, 303, 7, item_type='DELAYED', expected_session=session)
        self.assertIs(reply.unsafe, False)
        client.request.assert_called_once_with(88,
            {'fileId': 12.5, 'chatId': 303, 'messageId': 7, 'itemType': 'DELAYED'}, expected_session=session)
        client.request.reset_mock()
        self.assertEqual(client.request_file_download(12, 303, 7), {'url': URL, 'unsafe': False})
        client.request.assert_called_once_with(88, {'fileId': 12, 'chatId': 303, 'messageId': 7})

    def test_strict_wrapper_never_defaults_missing_safety_or_retries_timeout(self):
        for ack in ({}, {'url': URL}, {'url': URL, 'unsafe': 'false'}):
            client = self.client(data=ack)
            with self.assertRaises(MaxError) as result:
                client.conversation_file_download(12, 303, 7, expected_session=client.authenticated_session)
            self.assertNotIn('signature', str(result.exception))
            client.request.assert_called_once()
        client = self.client()
        client.request.side_effect = MaxTimeout('synthetic')
        with self.assertRaises(MaxTimeout):
            client.conversation_file_download(12, 303, 7, expected_session=client.authenticated_session)
        client.request.assert_called_once()

    def test_no_anonymous_context_and_no_other_opcode_or_raw_error_leak(self):
        client = self.client(data={'url': URL, 'unsafe': False})
        with self.assertRaises(MaxWriteNotStarted): client.conversation_file_download(12, 303, 7, expected_session=None)
        client.request.assert_not_called()
        client = self.client(opcode=89, data={'url': URL, 'unsafe': False})
        with self.assertRaises(MaxError):
            client.conversation_file_download(12, 303, 7, expected_session=client.authenticated_session)
        client.request.return_value = MaxFrame(3, 1, 88, b'', {'error': 'synthetic-private-error'})
        with self.assertRaises(MaxError) as result:
            client.conversation_file_download(12, 303, 7, expected_session=client.authenticated_session)
        self.assertNotIn('synthetic-private-error', str(result.exception))

    def test_replaced_session_in_handoff_cannot_receive88_on_either_socket(self):
        client = self.client()
        old, context = client._sock, client.authenticated_session
        new = Mock()
        def replace(*args, **kwargs):
            client._sock = new
            client._auth_session = AuthenticatedSession(101, client.device_id, 'WEB', new)
            return MaxClient.request(client, *args, **kwargs)
        client.request = replace
        with self.assertRaises(MaxWriteNotStarted):
            client.conversation_file_download(12, 303, 7, expected_session=context)
        old.sendall.assert_not_called()
        new.sendall.assert_not_called()
        self.assertEqual(client._pending, {})


if __name__ == '__main__':
    unittest.main()
