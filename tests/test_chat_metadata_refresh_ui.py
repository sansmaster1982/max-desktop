"""Owner-bound cache metadata -> queued Qt header refresh; no account or RPC."""
import threading
import time
import unittest
from dataclasses import replace
from unittest.mock import Mock, patch

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest

import test_message_change_ui as pipeline
from maxclient.core.models import Chat, Contact, Message
from maxclient.protocol.client import ConnectionState


class ChatMetadataRefreshUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        pipeline.MessageChangeUITests.setUpClass()

    def setUp(self):
        self.case = pipeline.MessageChangeUITests()
        self.case.setUp()
        self.window, self.view = self.case.window, self.case.view
        self.service, self.store, self.client = self.case.service, self.case.store, self.case.client
        self.service._server_contacts.clear()
        self.service._contacts_cache = None
        self.store.upsert_chat(Chat(id=303, title='Чат 303', peer_user_id=202, chat_type='DIALOG', unread=5))
        self.window._current_chat_id = 303
        self.view.open_chat(self.store.get_chat(303))
        self.window._reload_chats()
        self.select(303)
        self.worker_count = len(self.case.callbacks)
        # All metadata tests replace avatar loading, including accidental URL
        # fetches. There is never a real image request in this test module.
        self.avatar = patch.object(self.view.h_avatar, 'set_photo')
        self.photo = self.avatar.start()

    def tearDown(self):
        self.assertEqual(len(self.case.callbacks), self.worker_count)
        self.avatar.stop()
        self.case.tearDown()

    def select(self, chat_id):
        for i in range(self.window.chat_list.count()):
            item = self.window.chat_list.item(i)
            if item.data(Qt.ItemDataRole.UserRole) == chat_id:
                self.window.chat_list.setCurrentItem(item)
                return item
        raise AssertionError('Synthetic chat missing')

    def metadata(self, name='<b>Синтетический контакт & имя</b>'):
        self.service._server_contacts[202] = {'id': 202, 'names': [{'type': 'ONEME', 'name': name}]}
        self.store.upsert_contact(Contact(id=202, name=name))
        self.service._contacts_cache = None
        return name

    def emit_and_wait(self, identity=303):
        worker = threading.Thread(target=self.service.on_chat_changed, args=(identity,))
        worker.start()
        worker.join(1)
        self.assertFalse(worker.is_alive())
        deadline = time.monotonic() + 1
        # One actual queued signal and the product's real80ms coalescing timer.
        while time.monotonic() < deadline:
            self.case.app.processEvents()
            if not self.window._reload_timer.isActive():
                return
            QTest.qWait(5)
        self.fail('Metadata coalescing timer did not complete')

    def test_queued_same_order_reload_updates_actual_header_and_existing_sidebar_row(self):
        item = self.window.chat_list.currentItem()
        widget = self.window.chat_list.itemWidget(item)
        self.assertEqual(self.view.h_title.text(), 'Чат 303')
        name = self.metadata()
        self.emit_and_wait()
        self.assertEqual(self.view.h_title.text(), name)
        self.assertEqual(self.view.h_title.textFormat(), Qt.TextFormat.PlainText)
        self.assertIs(self.window.chat_list.currentItem(), item)
        self.assertIs(self.window.chat_list.itemWidget(item), widget)
        self.assertEqual(self.view.chat.display, name)

    def test_metadata_refresh_preserves_draft_reply_query_rows_target_and_scope(self):
        self.view.input.setPlainText('Синтетический незаконченный черновик')
        reply = self.view._rows[0]['msg']
        self.view._set_reply_target(reply)
        self.view.history_search.setText('Synthetic')
        self.view._history_target = (303, self.view._history_generation, 7)
        self.view._spotlight_id = 7
        self.view._history_target_pending = True
        before = (self.view._history_generation, self.view._file_context, self.view._history_target,
                  self.view._spotlight_id, self.view._search_index, self.view._search_matches[:],
                  tuple(row['bubble'] for row in self.view._rows))
        subtitle = self.view.h_sub.text()
        self.metadata()
        self.emit_and_wait()
        self.assertEqual(self.view.input.toPlainText(), 'Синтетический незаконченный черновик')
        self.assertIs(self.view._reply_target, reply)
        self.assertEqual(self.view.history_search.text(), 'Synthetic')
        self.assertEqual((self.view._history_generation, self.view._file_context, self.view._history_target,
                          self.view._spotlight_id, self.view._search_index, self.view._search_matches,
                          tuple(row['bubble'] for row in self.view._rows)), before)
        self.assertTrue(self.view._history_target_pending)
        self.assertEqual(self.view.h_sub.text(), subtitle)

    def test_reordered_sidebar_preserves_selection_and_message_scroll(self):
        for identity in range(20, 70):
            self.store.insert_message(Message(chat_id=303, id=identity, time_ms=1793459500000 + identity,
                                              text='Synthetic long body ' * 10))
        self.view.reload()
        self.case.settle()
        bar = self.view.scroll.verticalScrollBar()
        bar.setValue(bar.maximum() // 2)
        position, maximum = bar.value(), bar.maximum()
        self.store.upsert_chat(Chat(id=404, title='Другой синтетический чат', last_time_ms=2**52))
        self.metadata()
        self.emit_and_wait()
        self.assertEqual(self.window.chat_list.currentItem().data(Qt.ItemDataRole.UserRole), 303)
        self.assertEqual((bar.value(), bar.maximum()), (position, maximum))
        self.assertEqual(self.view.chat.id, 303)

    def test_active_chat_not_in_current_filter_still_gets_fresh_header(self):
        self.window._section = 'channels'
        name = self.metadata()
        self.emit_and_wait()
        self.assertEqual(self.window.chat_list.count(), 0)
        self.assertEqual(self.view.h_title.text(), name)

    def test_unrelated_chat_metadata_does_not_replace_current_header(self):
        self.store.upsert_chat(Chat(id=404, title='Сторонний чат'))
        self.emit_and_wait(404)
        self.assertEqual(self.view.h_title.text(), 'Чат 303')
        self.assertEqual(self.view.chat.id, 303)

    def test_owner_change_during_cache_read_rejects_header_snapshot(self):
        original = self.service.list_chats
        def read():
            rows = original()
            self.case.case.change_account()
            return rows
        self.metadata()
        with patch.object(self.service, 'list_chats', side_effect=read):
            self.window._reload_chats()
        self.assertEqual(self.view.h_title.text(), 'Чат 303')

    def test_same_owner_new_session_repairs_metadata_without_rebinding_effect_scopes(self):
        name = self.metadata()
        scope = self.view._file_context
        generation = self.view._history_generation
        self.client._auth_session = self.case.case.context_for(self.client)
        self.emit_and_wait()
        self.assertEqual(self.view.h_title.text(), name)
        self.assertIs(self.view._file_context, scope)
        self.assertEqual(self.view._history_generation, generation)

    def test_same_owner_session_change_during_cache_read_rejects_old_read(self):
        original = self.service.list_chats
        def read():
            rows = original()
            self.client._auth_session = self.case.case.context_for(self.client)
            return rows
        self.metadata()
        with patch.object(self.service, 'list_chats', side_effect=read):
            self.window._reload_chats()
        self.assertEqual(self.view.h_title.text(), 'Чат 303')

    def test_bare_signal_after_owner_replacement_does_not_relabel_old_body(self):
        self.case.case.change_account()
        self.store.upsert_chat(Chat(id=303, title='Чужой новый владелец'))
        self.client._auth_session = replace(self.case.case.context_for(self.client), owner_id=404)
        self.emit_and_wait()
        self.assertEqual(self.view.h_title.text(), 'Чат 303')
        self.assertEqual(self.view.chat.display, 'Чат 303')

    def test_disconnected_open_repairs_header_after_confirmed_same_owner_reconnect(self):
        self.client._state = ConnectionState.DISCONNECTED
        self.view.open_chat(self.store.get_chat(303))
        subtitle = self.view.h_sub.text()
        self.client._state = ConnectionState.CONNECTED
        self.client._auth_session = self.case.case.context_for(self.client)
        name = self.metadata()
        self.emit_and_wait()
        self.assertEqual(self.view.h_title.text(), name)
        self.assertEqual(self.view.h_sub.text(), subtitle)

    def test_chat_switch_during_cache_read_does_not_overwrite_replacement_header(self):
        self.store.upsert_chat(Chat(id=404, title='Новая открытая переписка'))
        original = self.service.list_chats
        def read():
            rows = original()
            self.view.open_chat(self.store.get_chat(404))
            return rows
        self.metadata()
        with patch.object(self.service, 'list_chats', side_effect=read):
            self.window._reload_chats()
        self.worker_count += 1  # Explicit synthetic chat switch, not metadata IO.
        self.assertEqual(self.view.chat.id, 404)
        self.assertEqual(self.view.h_title.text(), 'Новая открытая переписка')

    def test_disconnected_warning_is_preserved_without_authentication_or_history(self):
        self.client._state = ConnectionState.DISCONNECTED
        self.view.open_chat(self.store.get_chat(303))
        self.assertIn('Нет подтверждённого соединения', self.view.h_sub.text())
        subtitle = self.view.h_sub.text()
        self.metadata()
        self.emit_and_wait()
        self.assertEqual(self.view.h_sub.text(), subtitle)
        self.assertEqual(self.view.h_title.text(), 'Чат 303')
        self.client.connect.assert_not_called()
        self.client.login.assert_not_called()


if __name__ == '__main__':
    unittest.main()
