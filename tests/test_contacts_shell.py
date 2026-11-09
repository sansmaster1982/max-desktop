"""Sidebar integration regression tests with synthetic identities and no network."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import unittest
from unittest.mock import Mock, patch

from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtWidgets import QApplication

from maxclient.core.config import Settings
from maxclient.core.models import Chat, Contact
from maxclient.ui.main_window import MainWindow
from test_settings_workspace import SyntheticService


class ContactsShellTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.context = object()
        self.service = SyntheticService()
        self.service.search_context = lambda: self.context
        self.service.list_contacts = Mock(return_value=[Contact(2**40 + 7, 'Local Person', '+70000000007')])
        self.service.add_contact_by_phone = Mock(side_effect=AssertionError('No live lookup in shell tests'))
        self.patches = [patch('maxclient.ui.main_window.run_async'),
                        patch('maxclient.ui.chat_view.run_async'),
                        patch('maxclient.ui.settings_workspace.run_async'),
                        patch('maxclient.ui.contacts_workspace.Avatar.set_photo'),
                        patch.object(Settings, 'save')]
        for item in self.patches:
            item.start()
        self.window = MainWindow(self.service, Settings())
        self.window.show()
        self.app.processEvents()

    def tearDown(self):
        self.window.close()
        self.window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()
        for item in reversed(self.patches):
            item.stop()

    def test_contacts_replace_only_sidebar_and_restore_chat_filter(self):
        self.window._select_section('new')
        self.window.search.setText('group')
        right = self.window._conversation_pages.currentWidget()
        self.window._open_contacts()
        contacts = self.window._contacts_workspace
        self.assertIs(self.window._sidebar_pages.currentWidget(), contacts)
        self.assertIs(self.window._conversation_pages.currentWidget(), right)
        self.assertTrue(self.window.contacts_button.isChecked())
        self.assertFalse(any(button.isChecked() for button in self.window.nav_buttons.values()))
        self.assertEqual(contacts.list.count(), 1)
        self.window._show_chat_workspace()
        self.assertIs(self.window._sidebar_pages.currentWidget(), self.window.sidebar)
        self.assertEqual(self.window.search.text(), 'group')
        self.assertEqual(self.window._section, 'new')
        self.assertFalse(self.window.contacts_button.isChecked())
        self.assertEqual(contacts.list.count(), 0)
        self.service.add_contact_by_phone.assert_not_called()

    def test_contacts_settings_roundtrip_reuses_single_sidebar(self):
        self.window._open_contacts()
        contacts = self.window._contacts_workspace
        self.window._open_settings()
        self.assertFalse(self.window.contacts_button.isChecked())
        self.assertFalse(contacts._active)
        self.assertEqual(contacts.list.count(), 0)
        self.window._open_contacts()
        self.assertIs(self.window._contacts_workspace, contacts)
        self.assertEqual(self.window._sidebar_pages.count(), 2)
        self.assertFalse(self.window.settings_button.isChecked())
        self.assertEqual(contacts.list.count(), 1)

    def test_refresh_and_folder_events_do_not_displace_contacts(self):
        self.window._open_contacts()
        contacts = self.window._contacts_workspace
        self.window._on_folders({'folderSync': 1, 'foldersOrder': ['all.chat.folder'],
            'orderedFolders': [{'id': 'all.chat.folder', 'title': 'All'}]}, context=self.context)
        self.assertIs(self.window._sidebar_pages.currentWidget(), contacts)
        self.assertTrue(self.window.contacts_button.isChecked())
        self.assertFalse(any(button.isChecked() for button in self.window.nav_buttons.values()))

    def test_menu_exposes_create_and_find_as_separate_actions(self):
        menu = self.window._build_new_menu(contacts=True)
        actions = {action.text(): action for action in menu.actions() if not action.isSeparator()}
        self.assertTrue(actions['Найти по номеру'].isEnabled())
        self.assertTrue(actions['Добавить контакт'].isEnabled())
        actions['Добавить контакт'].trigger()
        self.assertIsNotNone(self.window._contact_create_dialog)
        self.assertFalse(self.window._contact_create_dialog.save_button.isEnabled())
        self.assertIsNone(self.window._phone_lookup_dialog)
        self.window._contact_create_dialog.reject()
        for label in ('Создать группу', 'Создать канал'):
            self.assertTrue(actions[label].isEnabled())
        self.assertTrue(actions['Пригласить по ссылке'].isEnabled())
        for label in ('Создать групповой звонок',):
            self.assertFalse(actions[label].isEnabled())
        self.assertIn('Управление локальными контактами…', actions)
        chat_menu = self.window._build_new_menu(contacts=False)
        self.assertNotIn('Добавить контакт', [action.text() for action in chat_menu.actions()])
        self.service.add_contact_by_phone.assert_not_called()

    def test_phone_dialog_is_single_instance_and_closes_with_session(self):
        self.window._open_phone_lookup()
        first = self.window._phone_lookup_dialog
        self.window._open_phone_lookup()
        self.assertIs(self.window._phone_lookup_dialog, first)
        self.assertFalse(first.find_button.isEnabled())
        self.window._stop_search()
        self.assertTrue(first._closed)
        self.assertIsNone(self.window._phone_lookup_dialog)
        self.window._open_phone_lookup()
        self.assertIsNone(self.window._phone_lookup_dialog)
        self.service.add_contact_by_phone.assert_not_called()

    def test_group_and_channel_menu_routes_open_distinct_local_forms(self):
        self.service.pending_conversation_creation = Mock(return_value=None)
        self.service.create_conversation = Mock(side_effect=AssertionError('No live creation'))
        for contacts in (False, True):
            for label, kind in (('Создать группу', 'group'), ('Создать канал', 'channel')):
                menu = self.window._build_new_menu(contacts=contacts)
                action = next(row for row in menu.actions() if row.text() == label)
                action.trigger()
                dialog = self.window._conversation_create_dialog
                self.assertEqual(dialog.kind, kind)
                self.assertFalse(dialog.create_button.isEnabled())
                self.assertEqual(dialog.pages.currentIndex(), 0 if kind == 'group' else 1)
                dialog.reject()
                self.assertIsNone(self.window._conversation_create_dialog)
                menu.deleteLater()
        self.service.create_conversation.assert_not_called()
        self.service.add_contact_by_phone.assert_not_called()

    def test_conversation_creation_has_single_owner_and_session_cleanup(self):
        self.service.pending_conversation_creation = Mock(return_value=None)
        self.window._open_conversation_create('group')
        dialog = self.window._conversation_create_dialog
        self.window._open_conversation_create('channel')
        self.assertIs(self.window._conversation_create_dialog, dialog)
        self.assertEqual(dialog.kind, 'group')
        self.window._stop_search()
        self.assertTrue(dialog._closed)
        self.assertIsNone(self.window._conversation_create_dialog)
        self.window._open_conversation_create('group')
        self.assertIsNone(self.window._conversation_create_dialog)
        menu = self.window._build_new_menu(contacts=True)
        for action in menu.actions():
            if action.text() in ('Создать группу', 'Создать канал'):
                self.assertFalse(action.isEnabled())

    def test_created_conversation_clears_filter_and_opens_real_cached_id(self):
        actual = Chat(2**40 + 55, 'Created synthetic group', is_group=True, chat_type='CHAT')
        self.service.chats.append(actual)
        self.window._select_section('channels')
        self.window.search.setText('unrelated filter')
        self.window._open_contacts()
        self.window.chat_view.open_chat = Mock()
        self.window._conversation_created(actual, context=self.context)
        self.assertEqual(self.window._section, 'all')
        self.assertEqual(self.window.search.text(), '')
        self.assertIs(self.window._sidebar_pages.currentWidget(), self.window.sidebar)
        self.assertEqual(self.window.chat_list.currentItem().data(Qt.ItemDataRole.UserRole), actual.id)
        self.window.chat_view.open_chat.assert_called_once_with(actual)

    def test_stale_or_uncached_creation_cannot_navigate(self):
        actual = Chat(2**40 + 55, 'Not confirmed in cache', is_group=True, chat_type='CHAT')
        self.window.chat_view.open_chat = Mock()
        self.window._conversation_created(actual, context=self.context)
        self.service.chats.append(actual)
        previous = self.context
        self.context = object()
        self.window._conversation_created(actual, context=previous)
        for malformed in (None, {'id': actual.id}, Chat(True, 'Invalid ID')):
            self.window._conversation_created(malformed, context=self.context)
        self.window.chat_view.open_chat.assert_not_called()

    def test_account_switch_during_created_cache_lookup_cannot_navigate(self):
        actual = Chat(71, 'Created synthetic channel', is_channel=True, chat_type='CHANNEL')
        def switch_on_read(_identity):
            self.context = object()
            return actual
        self.window.chat_view.open_chat = Mock()
        with patch.object(self.window, '_display_chat', side_effect=switch_on_read):
            self.window._conversation_created(actual, context=self.context)
        self.window.chat_view.open_chat.assert_not_called()

    def test_confirmed_lookup_refreshes_active_sidebar_without_another_lookup(self):
        self.window._open_contacts()
        self.window._open_phone_lookup()
        self.service.list_contacts.return_value.append(Contact(301, 'New confirmed contact'))
        self.window._phone_lookup_dialog.contact_found.emit()
        self.assertEqual(self.window._contacts_workspace.list.count(), 2)
        self.service.add_contact_by_phone.assert_not_called()

    def test_found_contact_without_dialog_keeps_lookup_result_visible(self):
        self.service.dialog_for_contact = Mock(return_value=None)
        self.window._open_phone_lookup()
        dialog = self.window._phone_lookup_dialog
        dialog._found_contact = Contact(2**40 + 7, 'Synthetic Person')
        dialog._open_found()
        self.assertFalse(dialog._closed)
        self.assertIn('Диалог с ним пока не загружен', dialog.result_label.text())
        self.service.dialog_for_contact.assert_called_once_with(2**40 + 7)
        self.service.add_contact_by_phone.assert_not_called()

    def test_found_contact_with_actual_dialog_closes_lookup_after_navigation(self):
        peer = 2**40 + 7
        actual = Chat(809, 'Confirmed Dialog', peer_user_id=peer)
        self.service.dialog_for_contact = Mock(return_value=actual)
        self.window.chat_view.open_chat = Mock()
        self.window._open_phone_lookup()
        dialog = self.window._phone_lookup_dialog
        dialog._found_contact = Contact(peer, 'Synthetic Person')
        dialog._open_found()
        self.assertTrue(dialog._closed)
        self.window.chat_view.open_chat.assert_called_once_with(actual)

    def test_local_rename_preserves_context_across_nested_name_dialog(self):
        self.service.rename_conversation = Mock()
        with patch('maxclient.ui.main_window.QInputDialog.getText', return_value=('Alias', True)):
            self.window._rename_chat(1)
        self.service.rename_conversation.assert_called_once_with(1, 'Alias', context=self.context)
        self.service.rename_conversation.reset_mock()

        def switch_account(*args, **kwargs):
            self.context = object()
            return ('Wrong account alias', True)

        with patch('maxclient.ui.main_window.QInputDialog.getText', side_effect=switch_account):
            self.window._rename_chat(1)
        self.service.rename_conversation.assert_not_called()

    def test_exact_contact_signal_uses_confirmed_chat_not_contact_id(self):
        self.window._open_contacts()
        peer = 2**40 + 7
        actual = Chat(809, 'Confirmed Dialog', peer_user_id=peer)
        self.service.dialog_for_contact = Mock(return_value=actual)
        self.window.chat_view.open_chat = Mock()
        contacts = self.window._contacts_workspace
        contacts._open_item(contacts.list.item(0))
        self.service.dialog_for_contact.assert_called_once_with(peer)
        self.window.chat_view.open_chat.assert_called_once_with(actual)
        self.assertEqual(self.window._current_chat_id, 809)
        self.assertIs(self.window._sidebar_pages.currentWidget(), self.window.sidebar)

    def test_stale_contact_page_cannot_navigate_after_account_change(self):
        self.window._open_contacts()
        contacts = self.window._contacts_workspace
        item = contacts.list.item(0)
        self.service.dialog_for_contact = Mock()
        self.context = object()
        contacts._open_item(item)
        self.service.dialog_for_contact.assert_not_called()
        self.window._reload_chats()
        self.assertEqual(contacts.list.count(), 0)
        self.assertFalse(contacts.plus_button.isEnabled())


if __name__ == '__main__':
    unittest.main()
