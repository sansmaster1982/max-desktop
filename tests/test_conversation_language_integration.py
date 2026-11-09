"""Locale wiring, stale menus and late Info reads with synthetic identities only."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import unittest
from unittest.mock import Mock

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from maxclient.core.i18n import SUPPORTED_LANGUAGES, tr, translation_available
from maxclient.ui.conversation_owner_transfer_dialog import owner_transfer_notice, owner_transfer_notice_parts
from maxclient.ui.main_window import _localized_info_status
from maxclient.ui.contact_profile import ContactEditPanel
import test_contacts_shell as contacts_fixture
import test_conversation_actions_shell as info_fixture


class ConversationLanguageMenuIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.f = contacts_fixture.ContactsShellTests()
        self.f.app = self.app
        self.f.setUp()
        self.addCleanup(self.f.tearDown)
        self.w = self.f.window

    def test_plus_labels_in_seven_languages_keep_routes_and_disabled_calls(self):
        sources = ('Добавить контакт', 'Создать канал', 'Создать группу', 'Создать групповой звонок',
                   'Найти по номеру', 'Пригласить по ссылке', 'Управление локальными контактами…')
        for language in SUPPORTED_LANGUAGES:
            self.w.settings.ui_language = language
            menu = self.w._build_new_menu(contacts=True)
            actions = [a for a in menu.actions() if not a.isSeparator()]
            self.assertEqual([a.text() for a in actions], [tr(s, language) for s in sources])
            self.assertFalse(actions[3].isEnabled())
            self.assertEqual(actions[3].toolTip(), tr('Пока недоступно', language))
            for source in (*sources, 'Пока недоступно'):
                self.assertTrue(translation_available(source, language), (source, language))
        self.f.service.add_contact_by_phone.assert_not_called()

    def test_old_qaction_cannot_dispatch_after_locale_change(self):
        self.w._open_phone_lookup = Mock()
        menu = self.w._build_new_menu(contacts=True)
        action = next(a for a in menu.actions() if a.text() == 'Найти по номеру')
        self.w.settings.ui_language = 'fr'
        self.w._apply_local_language()
        action.trigger()
        self.w._open_phone_lookup.assert_not_called()

    def test_old_qaction_cannot_dispatch_after_replacement_or_session_change(self):
        self.w._open_phone_lookup = Mock()
        menu = self.w._build_new_menu()
        old = next(a for a in menu.actions() if a.text() == 'Найти по номеру')
        replacement = self.w._build_new_menu()
        old.trigger()
        self.w._open_phone_lookup.assert_not_called()
        current = next(a for a in replacement.actions() if a.text() == 'Найти по номеру')
        self.f.context = object()
        current.trigger()
        self.w._open_phone_lookup.assert_not_called()

    def test_current_menu_dispatches_once_with_same_context(self):
        self.w._open_phone_lookup = Mock()
        menu = self.w._build_new_menu()
        action = next(a for a in menu.actions() if a.text() == 'Найти по номеру')
        action.trigger()
        self.w._open_phone_lookup.assert_called_once_with()

    def test_logout_invalidates_menu_even_if_action_kept_alive(self):
        self.w._open_phone_lookup = Mock()
        menu = self.w._build_new_menu()
        action = next(a for a in menu.actions() if a.text() == 'Найти по номеру')
        self.w._stop_search()
        action.trigger()
        self.w._open_phone_lookup.assert_not_called()

    def test_main_hook_relabels_contacts_without_reload_or_query_loss(self):
        self.w._open_contacts()
        workspace = self.w._contacts_workspace
        workspace.search.setText('Local')
        self.app.processEvents()
        item = workspace.list.item(0)
        workspace.list.setCurrentItem(item)
        calls = self.f.service.list_contacts.call_count
        self.w.settings.ui_language = 'en'
        self.w._apply_local_language()
        self.assertEqual(workspace.title.text(), 'Contacts')
        self.assertEqual(workspace.search.text(), 'Local')
        self.assertIs(workspace.list.currentItem(), item)
        self.assertEqual(self.f.service.list_contacts.call_count, calls)

    def test_main_hook_relabels_editor_without_losing_pending_draft_or_selection(self):
        panel = ContactEditPanel(self.w)
        self.w._contact_edit_panel = panel
        context = object()
        panel.set_contact({'context': context, 'contact_id': 424242,
                           'title': 'Synthetic', 'first_name': 'Before', 'last_name': '',
                           'availability': {'edit': True, 'delete': True}})
        panel.first_name.setText('  Сохранение…  ')
        panel.first_name.setSelection(2, 4)
        panel.last_name.setText('<b>{name}</b>')
        panel.set_busy(True, status_source='Сохранение…')
        submitted, deleted, back = Mock(), Mock(), Mock()
        panel.submitted.connect(submitted)
        panel.delete_requested.connect(deleted)
        panel.back_requested.connect(back)
        service_mocks = {key: value for key, value in vars(self.f.service).items()
                         if isinstance(value, Mock)}
        before_calls = {key: list(value.mock_calls) for key, value in service_mocks.items()}
        for language in SUPPORTED_LANGUAGES:
            self.w.settings.ui_language = language
            self.w._apply_local_language()
            self.assertEqual(panel.heading.text(), tr('Настройки контакта', language))
            self.assertEqual(panel.status.text(), tr('Сохранение…', language))
            self.assertEqual(panel.first_name.text(), '  Сохранение…  ')
            self.assertEqual(panel.first_name.selectionStart(), 2)
            self.assertEqual(panel.first_name.selectedText(), 'Сохр')
            self.assertEqual(panel.last_name.text(), '<b>{name}</b>')
            self.assertEqual(panel._initial, ('Before', ''))
            self.assertIs(panel._context, context)
            self.assertTrue(panel._busy)
            self.assertFalse(panel.save_button.isEnabled())
            self.assertFalse(panel.delete_button.isEnabled())
        self.assertEqual({key: list(value.mock_calls) for key, value in service_mocks.items()}, before_calls)
        submitted.assert_not_called()
        deleted.assert_not_called()
        back.assert_not_called()

    def test_main_hook_relabels_open_channel_draft_without_rechecking_or_writing(self):
        self.f.service.pending_conversation_creation = Mock(return_value=None)
        self.f.service.create_conversation = Mock(side_effect=AssertionError('No live creation'))
        menu = self.w._build_new_menu(contacts=True)
        next(a for a in menu.actions() if a.text() == 'Создать канал').trigger()
        dialog = self.w._conversation_create_dialog
        dialog.title_input.setText('Сохранение…')
        dialog.title_input.setSelection(1, 4)
        dialog.description_input.setPlainText('<b>{name}</b>')
        dialog._request = object()
        request, context = dialog._request, dialog._context
        pending_calls = self.f.service.pending_conversation_creation.call_count
        dialog._input_changed = Mock(side_effect=AssertionError('Relabel cannot revalidate'))
        dialog._restore_pending = Mock(side_effect=AssertionError('Relabel cannot read'))
        for language in SUPPORTED_LANGUAGES:
            self.w.settings.ui_language = language
            self.w._apply_local_language()
            self.assertEqual(dialog.windowTitle(), tr('Создать канал', language))
            self.assertEqual(dialog.title_input.text(), 'Сохранение…')
            self.assertEqual(dialog.title_input.selectionStart(), 1)
            self.assertEqual(dialog.title_input.selectedText(), 'охра')
            self.assertEqual(dialog.description_input.toPlainText(), '<b>{name}</b>')
            self.assertIs(dialog._request, request)
            self.assertIs(dialog._context, context)
        self.assertEqual(self.f.service.pending_conversation_creation.call_count, pending_calls)
        self.f.service.create_conversation.assert_not_called()
        dialog._request = None
        dialog.reject()


class ConversationLanguageInfoIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.f = info_fixture.ConversationActionsShellTests()
        self.f.app = self.app
        self.f.setUp()
        self.addCleanup(self.f.tearDown)
        self.w = self.f.window
        self.context = self.f.context

    def test_main_hook_preserves_info_request_context_search_and_chat(self):
        panel = self.w._info_panel
        panel.search.setText('Синтетический')
        current = self.w.chat_view.chat
        job_count = self.f.jobs.call_count
        self.addCleanup(setattr, panel, 'set_conversation', panel.set_conversation)
        self.addCleanup(setattr, panel, '_render_tab', panel._render_tab)
        panel.set_conversation = Mock(side_effect=AssertionError('Relabel must not reload'))
        panel._render_tab = Mock(side_effect=AssertionError('Relabel must not rebuild rows'))
        self.w.settings.ui_language = 'en'
        self.w._apply_local_language()
        self.assertEqual(panel.header_title.text(), 'Info')
        self.assertEqual(panel.search.text(), 'Синтетический')
        self.assertIs(panel._context, self.context)
        self.assertIs(self.w.chat_view.chat, current)
        self.assertEqual(self.f.jobs.call_count, job_count)
        self.w.service.load_conversation_tab.assert_not_called()

    def test_folder_titles_are_literal_while_generated_rows_have_source(self):
        rows = self.f.folders([{'id': 'synthetic.folder', 'title': 'Создать папку', 'include': []}])
        folder = next(row for row in rows if row['action'] == 'folder_include')
        create = next(row for row in rows if row['action'] == 'folder_create')
        self.assertEqual(folder['label'], 'Создать папку')
        self.assertNotIn('label_source', folder)
        self.assertEqual(create['label_source'], 'Создать папку')
        self.w.settings.ui_language = 'en'
        self.w._apply_local_language()
        self.assertEqual(folder['label'], 'Создать папку')
        self.assertEqual(folder['value'], {'folder_id': 'synthetic.folder', 'included': True})

    def test_late_folders_cannot_resurrect_old_owned_status_over_new_raw_status(self):
        snapshot = dict(self.w._info_snapshot)
        snapshot.update(_localized_info_status('Сохранение…', 'en'))
        self.w._info_snapshot = snapshot
        self.w._info_panel.set_conversation(snapshot)
        self.w.service.conversation_action_status = Mock(return_value={
            'context': self.context, 'available': True, 'muted': False, 'pending': True,
            'action_status': '<b>Сохранение…</b>'})
        self.w._poll_info_action_state()
        self.assertNotIn('action_status_source', self.w._info_snapshot)
        self.f.folders([])
        self.w.settings.ui_language = 'fr'
        self.w._apply_local_language()
        self.assertEqual(self.w._info_panel.status.text(), '<b>Сохранение…</b>')
        self.assertEqual(self.w._info_panel.status.textFormat(), Qt.TextFormat.PlainText)

    def test_owned_pending_status_retranslates_after_poll_without_new_worker(self):
        self.w.service.pending = True
        self.w._poll_info_action_state()
        before = self.f.jobs.call_count
        self.w.settings.ui_language = 'en'
        self.w._apply_local_language()
        self.assertEqual(self.w._info_panel.status.text(), tr('MAX пока не подтвердил изменение.', 'en'))
        self.assertEqual(self.f.jobs.call_count, before)

    def test_owner_notice_keeps_successor_name_literal_and_translates_only_fallback(self):
        for name in ('Сохранение…', '<b>{name}</b>', None):
            state = {'transfer_confirmed': True, 'leave_confirmed': False, 'successor_name': name}
            source, values, sources = owner_transfer_notice_parts(state)
            snapshot = dict(self.w._info_snapshot)
            snapshot.update(_localized_info_status(source, 'ru', values, sources))
            self.w._info_panel.set_conversation(snapshot)
            for language in SUPPORTED_LANGUAGES:
                self.w._info_panel.retranslate(language)
                self.assertEqual(self.w._info_panel.status.text(), owner_transfer_notice(state, language))
                if name is not None:
                    self.assertIn(name, self.w._info_panel.status.text())
            self.assertEqual(state['successor_name'], name)


if __name__ == '__main__':
    unittest.main()
