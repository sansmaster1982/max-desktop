"""Supplied code-viewer contexts never recapture a different account/session."""
import copy
import unittest
from unittest.mock import patch

from maxclient.protocol.client import AuthenticatedSession
import test_code_service as fixture
from test_token_service import confirm_fake_transport


class VerificationCodesContextTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture.CodeServiceTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.service = self.fixture.service
        self.fixture.seed()
        self.client = self.service.client
        self.context = self.service._history_read_context()

    def assert_stale_without_reads(self, context=None):
        with patch.object(self.service, '_history_read_context',
                          side_effect=AssertionError('A supplied context cannot be recaptured')):
            with self.assertRaises(ValueError):
                self.service.verification_code_messages(context=self.context if context is None else context)
        self.assertEqual(self.client.read_calls, [])

    def test_supplied_context_is_used_without_recapture_and_preserves_result(self):
        with patch.object(self.service, '_history_read_context',
                          side_effect=AssertionError('No recapture')):
            result = self.service.verification_code_messages(context=self.context)
        self.assertEqual(result, {'status': 'available', 'chat_id': 42, 'messages': [
            {'id': 101, 'time_ms': fixture.STAMP, 'text': 'Synthetic visible verification message'}]})
        self.assertEqual(self.client.read_calls, [('CHAT_INFO', [42])])
        self.assertEqual(self.service.store.messages(42), [])

    def test_no_argument_and_explicit_none_keep_legacy_capture(self):
        capture = self.service._history_read_context
        with patch.object(self.service, '_history_read_context', wraps=capture) as current:
            first = self.service.verification_code_messages()
            second = self.service.verification_code_messages(context=None)
        self.assertEqual(current.call_count, 2)
        self.assertEqual(first, second)
        self.assertEqual(self.client.read_calls, [('CHAT_INFO', [42]), ('CHAT_INFO', [42])])

    def test_same_owner_new_authenticated_session_rejects_queued_context(self):
        confirm_fake_transport(self.service)
        self.assert_stale_without_reads()

    def test_identical_session_fields_do_not_replace_exact_session_identity(self):
        old = self.context[2]
        self.client.authenticated_session = AuthenticatedSession(
            old.owner_id, old.device_id, old.device_type, old.transport)
        self.assert_stale_without_reads()

    def test_replaced_client_with_same_session_is_rejected(self):
        replacement = fixture.CodeClient(device_id=self.client.device_id,
            app_version=self.client.app_version, auto_reconnect=self.client.auto_reconnect)
        replacement.authenticated_session = self.context[2]
        with patch.object(self.service, 'client', replacement):
            self.assert_stale_without_reads()
        self.assertEqual(replacement.read_calls, [])

    def test_switched_cache_owner_cannot_read_or_repopulate_old_account(self):
        self.service.store.bind_owner(456)
        self.assert_stale_without_reads()
        self.assertEqual(self.service.store.list_chats(), [])
        self.assertEqual(self.service.store.list_contacts(), [])

    def test_missing_session_and_wrong_owner_are_rejected_before_reads(self):
        with patch.object(self.client, 'authenticated_session', None):
            self.assert_stale_without_reads()
        self.assert_stale_without_reads(context=(456, self.client, self.context[2]))

    def test_supplied_context_is_rechecked_after_waiting_for_refresh_lock(self):
        service = self.service
        class ReauthenticateOnEnter:
            def __enter__(self):
                confirm_fake_transport(service)
            def __exit__(self, *_args):
                return False
        with patch.object(service, '_code_refresh_lock', ReauthenticateOnEnter()):
            self.assert_stale_without_reads()

    def test_both_metadata_reads_receive_the_exact_supplied_session(self):
        # A repeated same-owner LOGIN deliberately retains known contacts.
        # Make this synthetic cache lack the peer before requesting its metadata.
        self.service._server_contacts.clear()
        self.fixture.seed(contacts=[])
        self.context = self.service._history_read_context()
        self.client.contact_response = {'contacts': [fixture.code_contact()]}
        with patch.object(self.client, 'chat_info', wraps=self.client.chat_info) as chats, \
                patch.object(self.client, 'contact_info', wraps=self.client.contact_info) as contacts:
            result = self.service.verification_code_messages(context=self.context)
        self.assertEqual(result['status'], 'available')
        chats.assert_called_once_with([42], expected_session=self.context[2])
        contacts.assert_called_once_with([77], expected_session=self.context[2])
        self.assertEqual(self.service.store.messages(42), [])

    def test_session_change_after_chat_response_rejects_before_cache_or_contact_read(self):
        self.service._server_contacts.clear()
        self.fixture.seed(contacts=[])
        self.context = self.service._history_read_context()
        before = copy.deepcopy(self.service._server_chats)
        def late(_ids, *, expected_session):
            self.assertIs(expected_session, self.context[2])
            confirm_fake_transport(self.service)
            return {'chats': [fixture.code_chat(title='Synthetic changed metadata')]}
        with patch.object(self.client, 'chat_info', side_effect=late), \
                patch.object(self.client, 'contact_info', side_effect=AssertionError('No second read')):
            with self.assertRaises(ValueError):
                self.service.verification_code_messages(context=self.context)
        self.assertEqual(self.service._server_chats, before)
        self.assertEqual(self.service._server_contacts, {})


if __name__ == '__main__':
    unittest.main()
