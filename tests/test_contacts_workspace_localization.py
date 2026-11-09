"""Contact sidebar locale changes preserve identity/state and never load data."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from types import SimpleNamespace
import re
import unittest
from unittest.mock import Mock

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel

from maxclient.core._contacts_workspace_translations import CONTACTS_WORKSPACE_TRANSLATIONS
from maxclient.core.i18n import tr
from maxclient.core.models import Contact
from maxclient.ui.contacts_workspace import ContactsWorkspace
import test_contacts_workspace as fixtures


LANGUAGES = ('ru', 'en', 'es', 'pt', 'fr', 'uz', 'be')


class ContactsWorkspaceLocalizationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixtures.ContactsWorkspaceTests.setUpClass()

    def setUp(self):
        self.f = fixtures.ContactsWorkspaceTests()
        self.f.setUp()
        self.addCleanup(self.f.tearDown)

    def test_catalogue_integrates_six_translations_and_pt_br_alias(self):
        self.assertEqual(len(CONTACTS_WORKSPACE_TRANSLATIONS), 25)
        for source, values in CONTACTS_WORKSPACE_TRANSLATIONS.items():
            self.assertEqual(len(values), 6)
            self.assertEqual(tr(source, 'ru'), source)
            for language, value in zip(LANGUAGES[1:], values):
                with self.subTest(source=source, language=language):
                    self.assertTrue(value.strip())
                    self.assertEqual(tr(source, language), value)
                    if language != 'be':
                        self.assertIsNone(re.search('[А-Яа-яЁё]', value))
            self.assertEqual(tr(source, 'pt-BR'), values[2])

    def test_constructor_uses_typed_settings_language_without_reading_contacts(self):
        self.f.service.settings = SimpleNamespace(ui_language='en')
        widget = ContactsWorkspace(self.f.service)
        try:
            self.assertEqual(widget.title.text(), 'Contacts')
            self.assertEqual(widget.plus_button.toolTip(), 'Contacts menu')
            self.assertEqual(widget.search.placeholderText(), 'Search')
            self.assertEqual(widget.search.accessibleName(), 'Search contacts by name or number')
            self.assertEqual(widget.empty.text(), 'Open contacts after connecting to MAX.')
            self.f.service.list_contacts.assert_not_called()
            self.assertFalse(widget.plus_button.isEnabled())
        finally:
            widget.deleteLater()

    def test_untyped_mock_language_falls_back_to_russian(self):
        self.f.service.settings = Mock()
        self.f.widget.retranslate()
        self.assertEqual(self.f.widget.title.text(), 'Контакты')
        self.assertEqual(self.f.widget.language_epoch, 0)
        self.f.service.list_contacts.assert_not_called()

    def test_all_owned_labels_and_five_empty_states_follow_each_language(self):
        states = ('Откройте контакты после подключения к MAX.', 'Пока нет контактов',
                  'Ничего не найдено', 'Не удалось загрузить контакты. Откройте раздел заново.',
                  'Сессия изменилась. Откройте контакты заново.')
        for language in LANGUAGES:
            with self.subTest(language=language):
                w = self.f.widget
                w.retranslate(language)
                self.assertEqual(w.title.text(), tr('Контакты', language))
                self.assertEqual(w.list.accessibleName(), tr('Контакты', language))
                self.assertEqual(w.plus_button.toolTip(), tr('Меню контактов', language))
                self.assertEqual(w.plus_button.accessibleName(), tr('Меню контактов', language))
                self.assertEqual(w.search.placeholderText(), tr('Найти', language))
                self.assertEqual(w.search.accessibleName(), tr('Поиск контактов по имени или номеру', language))
                for source in states:
                    w._set_empty(source)
                    self.assertEqual(w.empty.text(), tr(source, language))
                if language not in ('ru', 'be'):
                    for text in (w.title.text(), w.list.accessibleName(), w.plus_button.toolTip(),
                                 w.search.placeholderText(), w.search.accessibleName(), w.empty.text()):
                        self.assertIsNone(re.search('[А-Яа-яЁё]', text))

    def test_retranslate_preserves_query_selection_scroll_rows_context_and_signals(self):
        self.f.rows[:] = [Contact(i, f'Synthetic Person {i}') for i in range(50)]
        w = self.f.widget
        w.activate_context()
        w.search.setText('Person')
        w.list.setCurrentRow(30)
        w.list.scrollToItem(w.list.currentItem())
        self.f.app.processEvents()
        item = w.list.currentItem()
        row = w.list.itemWidget(item)
        snapshot, context = w._rows_snapshot, w._context
        scroll = w.list.verticalScrollBar().value()
        signals = Mock()
        w.plus_requested.connect(signals)
        w.context_requested.connect(signals)
        w.search.textChanged.connect(signals)
        w.list.currentItemChanged.connect(signals)
        self.f.service.list_contacts.reset_mock()
        self.f.service.search_context = Mock(side_effect=AssertionError('Retranslate must not check/rebind session'))
        self.f.photo.reset_mock()
        for language in LANGUAGES:
            w.retranslate(language)
        self.assertEqual(w.search.text(), 'Person')
        self.assertIs(w.list.currentItem(), item)
        self.assertIs(w.list.itemWidget(item), row)
        self.assertIs(w._rows_snapshot, snapshot)
        self.assertIs(w._context, context)
        self.assertTrue(w._active)
        self.assertEqual(w.list.verticalScrollBar().value(), scroll)
        self.assertEqual(self.f.opened, [])
        signals.assert_not_called()
        self.f.service.list_contacts.assert_not_called()
        self.f.service.search_context.assert_not_called()
        self.f.photo.assert_not_called()

    def test_literal_names_phones_and_verified_badge_do_not_rebuild_rows(self):
        self.f.rows[:] = [Contact(303, 'Контакты', '<b>Найти</b>', None, True)]
        w = self.f.widget
        w.activate_context()
        item = w.list.item(0)
        row = w.list.itemWidget(item)
        phone = row.findChild(QLabel, 'ContactPhone')
        self.f.photo.reset_mock()
        for language in LANGUAGES:
            w.retranslate(language)
            self.assertIs(w.list.item(0), item)
            self.assertIs(w.list.itemWidget(item), row)
            self.assertEqual(row.name._full_text, 'Контакты')
            self.assertEqual(row.name.accessibleName(), 'Контакты')
            self.assertEqual(row.name.toolTip(), 'Контакты')
            self.assertEqual(item.data(Qt.ItemDataRole.AccessibleTextRole), 'Контакты')
            self.assertEqual(phone._full_text, '<b>Найти</b>')
            self.assertEqual(phone.textFormat(), Qt.TextFormat.PlainText)
            self.assertEqual(row.verified_badge.accessibleName(), tr('Подтверждённый аккаунт', language))
        self.f.photo.assert_not_called()

    def test_actual_empty_search_failure_and_expired_transitions_use_current_language(self):
        w = self.f.widget
        w.retranslate('en')
        self.f.rows.clear()
        self.assertTrue(w.activate_context())
        self.assertEqual(w.empty.text(), 'No contacts yet')
        w.search.setText('Synthetic missing')
        self.assertEqual(w.empty.text(), 'Nothing found')
        self.f.service.list_contacts.side_effect = RuntimeError('synthetic-private-error')
        self.assertFalse(w.refresh())
        self.assertEqual(w.empty.text(), 'Could not load contacts. Open this section again.')
        w.retranslate('fr')
        self.assertEqual(w.empty.text(), 'Impossible de charger les contacts. Rouvrez cette section.')
        self.assertNotIn('synthetic-private', w.empty.text())
        self.assertTrue(w.plus_button.isEnabled())
        self.f.context = object()
        self.assertFalse(w.refresh())
        self.assertEqual(w.empty.text(), 'La session a changé. Rouvrez les contacts.')
        self.assertFalse(w.plus_button.isEnabled())
        w.deactivate()
        self.assertEqual(w.empty.text(), 'Ouvrez les contacts après la connexion à MAX.')

    def test_language_epoch_changes_only_with_effective_language(self):
        w = self.f.widget
        self.assertEqual(w.language_epoch, 0)
        w.retranslate('en')
        first = w.language_epoch
        self.assertEqual(first, 1)
        w.retranslate('en')
        self.assertEqual(w.language_epoch, first)
        w.retranslate('pt-BR')
        self.assertEqual(w.language_epoch, first + 1)
        w.retranslate('pt')
        self.assertEqual(w.language_epoch, first + 1)
        w.retranslate('ru')
        self.assertEqual(w.language_epoch, first + 2)

    def test_new_rows_use_current_language_and_navigation_retains_raw_identity(self):
        w = self.f.widget
        w.retranslate('en')
        self.f.rows[:] = [Contact(2**40+1, 'Найти', None, None, True)]
        w.activate_context()
        item = w.list.item(0)
        row = w.list.itemWidget(item)
        self.assertEqual(row.verified_badge.accessibleName(), 'Verified account')
        w._open_item(item)
        self.assertEqual(self.f.opened, [(2**40+1, 'Найти')])

    def test_retranslation_does_not_adopt_replacement_session_or_enable_old_rows(self):
        w = self.f.widget
        w.activate_context()
        old_context = w._context
        item = w.list.item(0)
        self.f.context = object()
        w.retranslate('en')
        self.assertIs(w._context, old_context)
        w._open_item(item)
        self.assertEqual(self.f.opened, [])
        signal = Mock()
        w.plus_requested.connect(signal)
        w._request_plus()
        signal.assert_not_called()
        self.assertFalse(w.plus_button.isEnabled())
        self.assertEqual(w.empty.text(), 'The session changed. Open contacts again.')

    def test_narrow_sidebar_labels_remain_accessible_without_horizontal_scroll(self):
        w = self.f.widget
        w.resize(320, 560)
        self.f.rows[:] = [Contact(303, 'Synthetic long literal contact name ' * 8, None, None, True)]
        w.activate_context()
        for language in LANGUAGES:
            w.retranslate(language)
            self.f.app.processEvents()
            self.assertEqual(w.list.horizontalScrollBar().maximum(), 0)
            self.assertFalse(w.plus_button.isHidden())
            self.assertTrue(w.plus_button.accessibleName())
            self.assertLessEqual(w.plus_button.geometry().right(), w.width())
            self.assertTrue(w.search.accessibleName())


if __name__ == '__main__':
    unittest.main()
