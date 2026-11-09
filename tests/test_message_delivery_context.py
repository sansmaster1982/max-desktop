"""Synthetic push/queued delivery races, in-memory cache; no transport or UI."""
from dataclasses import FrozenInstanceError
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

import test_history_reconnect_review as history_fixture
from maxclient.core.message_delivery import MessageDelivery
from maxclient.core.models import Chat
from maxclient.protocol.client import MaxFrame
from maxclient.ui.main_window import MainWindow


class MessageDeliveryContextTests(unittest.TestCase):
    def setUp(self):
        self.case = history_fixture.HistoryReconnectReviewTests()
        self.case.setUp()
        self.addCleanup(self.case.tearDown)
        self.service = self.case.service

    def view(self, service=None):
        return SimpleNamespace(service=service or self.service,
                               _notifications=SimpleNamespace(receive=Mock()),
                               chat_view=SimpleNamespace(on_incoming=Mock()),
                               _schedule_reload=Mock())

    def delivery(self):
        self.service._on_push(self.case.push())
        event = self.service.on_message.call_args.args[0]
        self.assertIsInstance(event, MessageDelivery)
        return event

    def test_all_old_context_account_pushes_cannot_mutate_replacement_owner(self):
        c = self.case
        frames = [
            (134, {'config': {'user': {'PUSH_DETAILS': False}}}),
            (131, {'contact': {'id': 202, 'names': [{'name': 'Old synthetic contact'}]}}),
            (135, {'chat': c.chat()}),
            (130, {'chatId': 303, 'userId': 404, 'unread': 0}),
            (155, {'chatId': 303, 'messageId': 7, 'totalCount': 1}),
            (142, {'chat': c.chat(), 'messageIds': [7], 'ttl': False}),
            (128, {'chatId': 303, 'message': c.raw()}),
        ]
        c.change_account()
        for opcode, data in frames:
            with self.subTest(opcode=opcode):
                self.service._on_push(MaxFrame(0, 9, opcode, b'', data, auth_context=c.context))
        self.assertEqual(c.store.cache_owner, 404)
        self.assertEqual(c.store.list_chats(), [])
        self.assertEqual(c.store.list_contacts(), [])
        self.assertEqual(c.store.load_config(404), {})
        self.assertEqual(self.service._server_chats, {})
        self.assertEqual(self.service._server_contacts, {})
        self.service.on_message.assert_not_called()
        self.service.on_chat_changed.assert_not_called()

    def test_current_chat_push_updates_only_confirmed_cache_without_any_request(self):
        c = self.case
        chat = c.chat()
        chat['title'] = 'Current synthetic chat'
        self.service._on_push(MaxFrame(0, 9, 135, b'', {'chat': chat}, auth_context=c.context))
        self.assertEqual(c.store.get_chat(303).title, 'Current synthetic chat')
        self.service.on_chat_changed.assert_called_once_with(303)
        c.client.chat_info.assert_not_called()
        c.client.contact_info.assert_not_called()

    def test_owner_switch_after_cache_commit_does_not_emit_old_message(self):
        original = self.service._history_cache_write
        def switch_after_commit(binding, apply):
            result = original(binding, apply)
            self.case.change_account()
            return result
        self.service._history_cache_write = switch_after_commit
        self.service._on_push(self.case.push())
        self.service.on_message.assert_not_called()
        self.service.on_chat_changed.assert_not_called()

    def test_queued_event_is_rejected_after_owner_switch_even_if_chat_id_exists(self):
        event = self.delivery()
        self.case.change_account()
        self.case.store.upsert_chat(Chat(id=303, title='Replacement owner synthetic chat'))
        view = self.view()
        MainWindow._on_message(view, event)
        self.assertFalse(self.service.is_current_message_event(event))
        view._notifications.receive.assert_not_called()
        view.chat_view.on_incoming.assert_not_called()
        view._schedule_reload.assert_not_called()

    def test_queued_event_is_rejected_after_same_owner_new_login(self):
        event = self.delivery()
        self.case.client._auth_session = self.case.context_for(self.case.client)
        view = self.view()
        MainWindow._on_message(view, event)
        view._notifications.receive.assert_not_called()
        view.chat_view.on_incoming.assert_not_called()

    def test_current_event_is_unwrapped_only_after_gui_guard(self):
        event = self.delivery()
        view = self.view()
        MainWindow._on_message(view, event)
        view._notifications.receive.assert_called_once_with(event.message)
        view.chat_view.on_incoming.assert_called_once_with(event.message)
        view._schedule_reload.assert_called_once_with()

    def test_real_service_rejects_bare_message_but_legacy_ui_double_can_receive_it(self):
        event = self.delivery()
        current = self.view()
        MainWindow._on_message(current, event.message)
        current._notifications.receive.assert_not_called()
        legacy = self.view(SimpleNamespace())
        MainWindow._on_message(legacy, event.message)
        legacy._notifications.receive.assert_called_once_with(event.message)

    def test_envelope_is_frozen_and_its_repr_does_not_disclose_message_or_identity(self):
        event = self.delivery()
        with self.assertRaises(FrozenInstanceError):
            event.owner_id = 404
        self.assertNotIn(event.message.text, repr(event))
        self.assertNotIn(history_fixture.DEVICE, repr(event))
        self.assertNotIn('session_context=', repr(event))
        self.assertNotIn('source_client=', repr(event))
        self.assertIsNot(event.message, self.service.store.messages(303)[0])


if __name__ == '__main__':
    unittest.main()
