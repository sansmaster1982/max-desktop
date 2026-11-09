"""Independent direct-dialog regressions with an in-memory owner and fake RPC."""
from __future__ import annotations

import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import unittest
from unittest.mock import Mock, patch

from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication

import test_contact_lookup_core as base
from maxclient.core.models import Chat, Contact
from maxclient.core.config import Settings
from maxclient.protocol.client import AuthenticatedSession, MaxFrame, MaxWriteOutcomeUnknown
from maxclient.ui.chat_view import ChatView


class ContactDialogIndependentTests(unittest.TestCase):
    def setUp(self):
        self.fixture = base.ContactLookupCoreTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.service = self.fixture.service
        self.store = self.fixture.store
        self.client = self.fixture.client
        self.service.on_chat_changed = None
        self.service._chat_fetch_anchor = {}
        self.client.chat_info = Mock(return_value={'chats': []})
        self.client.contact_info = Mock(return_value={'contacts': [self.contact(202)]})
        self.client._write_frame = Mock(side_effect=AssertionError('Opening a contact must not write'))
        self.service._remember_contact_info({'contacts': [self.contact(202)]})
        self.context = self.service.contact_context(202)
        self.route = 101 ^ 202

    def tearDown(self):
        self.client.request.assert_not_called()
        self.client.contact_by_phone.assert_not_called()

    @staticmethod
    def contact(identity):
        return {'id': identity, 'flags': 512, 'updateTime': 200,
                'names': [{'type': 'ONEME', 'firstName': 'Selected', 'lastName': 'Person'}]}

    def server_dialog(self, **changes):
        return {'id': self.route, 'type': 'DIALOG', 'status': 'ACTIVE',
                'participants': {101: 1, 202: 1}, **changes}

    def test_missing_server_dialog_opens_exact_peer_without_sending_or_adding(self):
        result = self.service.open_contact_dialog(self.context)
        self.assertIs(result['context'], self.context)
        self.assertEqual((result['chat'].id, result['chat'].peer_user_id), (self.route, 202))
        self.assertTrue(result['local_draft'])
        self.assertNotEqual(result['chat'].id, self.context.contact_id)
        self.client.chat_info.assert_called_once_with([self.route], expected_session=self.context.session_context)
        self.client._write_frame.assert_not_called()
        self.assertEqual(self.store.messages(self.route), [])

    def test_known_actual_dialog_wins_over_computed_route(self):
        self.fixture.dialog(901, 202)
        result = self.service.open_contact_dialog(self.context)
        self.assertEqual(result['chat'].id, 901)
        self.assertFalse(result['local_draft'])
        self.client.chat_info.assert_not_called()
        self.client._write_frame.assert_not_called()

    def test_cached_group_at_resolved_id_is_never_reclassified(self):
        group = Chat(self.route, 'Other group', is_group=True, chat_type='CHAT')
        self.store.upsert_chat(group)
        with self.assertRaises(ValueError):
            self.service.open_contact_dialog(self.context)
        self.assertEqual(self.store.get_chat(self.route), group)
        self.client._write_frame.assert_not_called()

    def test_readback_for_other_peer_is_not_cached_or_opened(self):
        self.client.chat_info.return_value = {'chats': [self.server_dialog(participants={101: 1, 404: 1})]}
        with self.assertRaises(ValueError):
            self.service.open_contact_dialog(self.context)
        self.assertEqual(self.store.list_chats(), [])
        self.client._write_frame.assert_not_called()

    def test_non_dialog_readback_is_not_converted_to_contact_chat(self):
        self.client.chat_info.return_value = {'chats': [self.server_dialog(type='CHAT')]}
        with self.assertRaises(ValueError):
            self.service.open_contact_dialog(self.context)
        self.assertEqual(self.store.list_chats(), [])
        self.client._write_frame.assert_not_called()

    def test_failed_chat_read_cannot_be_interpreted_as_confirmed_absence(self):
        self.client.chat_info.side_effect = TimeoutError('Synthetic read timeout')
        with self.assertRaises((TimeoutError, ValueError)):
            self.service.open_contact_dialog(self.context)
        self.assertEqual(self.store.list_chats(), [])
        self.client._write_frame.assert_not_called()

    def test_ambiguous_duplicate_dto_cannot_create_local_draft(self):
        self.client.chat_info.return_value = {'chats': [self.server_dialog(), self.server_dialog()]}
        with self.assertRaises(ValueError):
            self.service.open_contact_dialog(self.context)
        self.assertEqual(self.store.list_chats(), [])
        self.client._write_frame.assert_not_called()

    def test_same_owner_reconnect_during_read_discards_draft(self):
        def reconnect(*_args, **_kwargs):
            self.fixture.switch(same_owner=True)
            return {'chats': []}
        self.client.chat_info.side_effect = reconnect
        with self.assertRaises(ValueError):
            self.service.open_contact_dialog(self.context)
        self.assertEqual(self.store.list_chats(), [])
        self.client._write_frame.assert_not_called()

    def test_switch_owner_during_read_never_creates_new_owner_dialog(self):
        def switch(*_args, **_kwargs):
            self.fixture.switch()
            return {'chats': [self.server_dialog()]}
        self.client.chat_info.side_effect = switch
        with self.assertRaises(ValueError):
            self.service.open_contact_dialog(self.context)
        self.assertEqual(self.store.list_chats(), [])
        self.client._write_frame.assert_not_called()

    def test_large_contact_id_has_no_32_bit_truncation(self):
        identity = 2**52 + 203
        self.service._remember_contact_info({'contacts': [self.contact(identity)]})
        self.client.contact_info.return_value = {'contacts': [self.contact(identity)]}
        context = self.service.contact_context(identity)
        result = self.service.open_contact_dialog(context)
        self.assertEqual(result['chat'].id, 101 ^ identity)
        self.assertEqual(result['chat'].peer_user_id, identity)
        self.client._write_frame.assert_not_called()

    def test_own_contact_uses_saved_zero_route_without_outgoing_message(self):
        self.service._remember_contact_info({'contacts': [self.contact(101)]})
        self.client.contact_info.return_value = {'contacts': [self.contact(101)]}
        context = self.service.contact_context(101)
        result = self.service.open_contact_dialog(context)
        self.assertEqual((result['chat'].id, result['chat'].peer_user_id), (0, 101))
        self.client._write_frame.assert_not_called()

    def test_open_empty_draft_does_not_issue_history_rpc_or_any_write(self):
        result = self.service.open_contact_dialog(self.context)
        self.client.chat_info.reset_mock()
        self.assertEqual(self.service.open_chat(result['chat'].id), [])
        self.client.chat_info.assert_not_called()
        self.client._write_frame.assert_not_called()
        self.assertNotIn(self.route, self.service._server_chats)

    def ack(self, chat_id=None):
        return MaxFrame(1, 1, 64, b'', {'chatId': self.route if chat_id is None else chat_id,
            'message': {'id': 876, 'sender': 101, 'time': 9000, 'text': 'Server-confirmed text'}})

    def test_explicit_first_send_uses_selected_peer_one_write_and_captured_session(self):
        self.service.open_contact_dialog(self.context)
        self.client._write_frame.side_effect = None
        self.client._write_frame.return_value = self.ack()
        result = self.service.send_text(self.route, 'Explicit synthetic message',
                                       context=self.context.session_context)
        self.assertEqual((result.status, result.text), ('sent', 'Server-confirmed text'))
        self.client._write_frame.assert_called_once()
        args, kwargs = self.client._write_frame.call_args
        self.assertEqual(args[0], 64)
        self.assertEqual(args[1]['userId'], 202)
        self.assertNotIn('chatId', args[1])
        self.assertIs(kwargs['expected_session'], self.context.session_context)

    def test_queued_send_after_reconnect_does_not_create_pending_message_or_write(self):
        self.service.open_contact_dialog(self.context)
        self.fixture.switch(same_owner=True)
        with self.assertRaises(ValueError):
            self.service.send_text(self.route, 'Stale queued text', context=self.context.session_context)
        self.assertEqual(self.store.messages(self.route), [])
        self.client._write_frame.assert_not_called()

    def test_wrong_chat_ack_is_unknown_without_importing_message_into_other_chat(self):
        self.service.open_contact_dialog(self.context)
        self.client._write_frame.side_effect = None
        self.client._write_frame.return_value = self.ack(999)
        result = self.service.send_text(self.route, 'Explicit synthetic message',
                                       context=self.context.session_context)
        self.assertEqual(result.status, 'unknown')
        self.assertIsNone(self.store.get_chat(999))
        self.assertEqual(self.store.messages(999), [])
        self.client._write_frame.assert_called_once()

    def test_owner_switch_after_send_never_applies_ack_or_pending_status_to_new_owner(self):
        self.service.open_contact_dialog(self.context)
        def switch(*_args, **_kwargs):
            self.fixture.switch()
            return self.ack()
        self.client._write_frame.side_effect = switch
        result = self.service.send_text(self.route, 'Explicit synthetic message',
                                       context=self.context.session_context)
        self.assertEqual(result.status, 'unknown')
        self.assertEqual(self.store.messages(self.route), [])
        self.assertEqual(self.store.list_chats(), [])
        self.client._write_frame.assert_called_once()

    def test_timeout_first_send_is_unknown_and_not_retried(self):
        self.service.open_contact_dialog(self.context)
        self.client._write_frame.side_effect = MaxWriteOutcomeUnknown('Synthetic outcome unknown')
        result = self.service.send_text(self.route, 'Explicit synthetic message',
                                       context=self.context.session_context)
        self.assertEqual(result.status, 'unknown')
        self.client._write_frame.assert_called_once()


class ContactDialogSendUiIndependentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        fixture = base.ContactLookupCoreTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.client = fixture.client
        self.service = Mock()
        self.service.client = self.client
        self.service.my_id = 101
        self.service.local_history.return_value = []
        self.worker = patch('maxclient.ui.chat_view.run_async')
        self.run = self.worker.start()
        self.addCleanup(self.worker.stop)
        self.view = ChatView(self.service, Settings())
        self.view.open_chat(Chat(175, 'Selected person', peer_user_id=202, chat_type='DIALOG'))

    def tearDown(self):
        self.view.close()
        self.view.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()

    def test_just_opening_never_queues_message_send(self):
        methods = [call.args[0] for call in self.run.call_args_list]
        self.assertNotIn(self.service.send_text, methods)
        self.assertNotIn(self.service.reply_to_message, methods)

    def test_explicit_send_queue_contains_captured_authenticated_session(self):
        self.run.reset_mock()
        self.view.input.setPlainText('Explicit synthetic text')
        self.view._send_text()
        self.assertEqual(self.run.call_args.args,
                         (self.service.send_text, 175, 'Explicit synthetic text'))
        self.assertIs(self.run.call_args.kwargs['context'], self.client.authenticated_session)

    def test_old_visible_chat_cannot_send_after_account_switch(self):
        self.run.reset_mock()
        self.view.input.setPlainText('Keep this unsent text')
        self.service.my_id = 404
        old = self.client.authenticated_session
        self.client._auth_session = AuthenticatedSession(404, old.device_id, 'WEB', self.client._sock)
        self.view._send_text()
        self.run.assert_not_called()
        self.assertEqual(self.view.input.toPlainText(), 'Keep this unsent text')


if __name__ == '__main__':
    unittest.main()
