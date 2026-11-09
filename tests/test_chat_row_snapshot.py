"""Row-menu cache reads preserve the active file scope; synthetic account only."""
import unittest

import test_conversation_file_service as fixture
from maxclient.core.conversation_info_service import ConversationUnavailable
from maxclient.protocol.client import AuthenticatedSession


class ChatRowSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture.ConversationFileServiceTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.service = self.fixture.service

    def tearDown(self):
        self.fixture.tearDown()

    def test_row_context_preserves_open_info_file_descriptor(self):
        descriptor = self.fixture.descriptor(self.fixture.load())
        old_context = self.fixture.context
        row_context = self.service.conversation_context(303, activate_files=False)
        self.assertIsNot(row_context, old_context)
        self.assertTrue(self.service.is_current_conversation(row_context))
        self.service.conversation_snapshot(row_context)
        self.assertTrue(self.service.is_current_conversation_file(old_context, descriptor))
        self.fixture.client.conversation_file_download.assert_not_called()

    def test_default_factory_still_revokes_old_file_scope(self):
        descriptor = self.fixture.descriptor(self.fixture.load())
        self.service.conversation_context(303)
        self.assertFalse(self.service.is_current_conversation_file(self.fixture.context, descriptor))

    def test_row_factory_does_not_relax_session_guards(self):
        context = self.service.conversation_context(303, activate_files=False)
        client = self.fixture.client
        old = client._auth_session
        client._auth_session = AuthenticatedSession(old.owner_id, old.device_id, old.device_type, client._sock)
        with self.assertRaises(ConversationUnavailable):
            self.service.conversation_snapshot(context)

    def test_file_activation_option_must_be_explicit_boolean(self):
        for value in (None, 0, 1, 'false'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.service.conversation_context(303, activate_files=value)

    def test_read_labels_keep_unknown_separate_from_zero(self):
        original = dict(self.service._server_chats[303])
        cases = [
            ({'newMessages': 0}, None),
            ({'newMessages': 0, 'markedAsUnread': False}, False),
            ({'newMessages': 0, 'markedAsUnread': True}, True),
            ({'newMessages': 3}, True),
            ({'newMessages': True}, None),
            ({'newMessages': 0, 'lastEventTime': 1790000000000}, False),
        ]
        for fields, expected in cases:
            with self.subTest(fields=fields):
                self.service._server_chats[303] = {**original, **fields}
                context = self.service.conversation_context(303, activate_files=False)
                state = self.service.conversation_snapshot(context)['row_menu']
                self.assertIs(state['unread'], expected)
                self.assertEqual(state['chat_status'], 'ACTIVE')
                self.assertNotIn('pinned', state)


if __name__ == '__main__':
    unittest.main()
