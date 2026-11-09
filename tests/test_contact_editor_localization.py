"""Contact editor localization with synthetic Qt/services; no account operations."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import re
import string
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtWidgets import QApplication, QMessageBox
from shiboken6 import isValid

from maxclient.core._contact_editor_translations import CONTACT_EDITOR_TRANSLATIONS
from maxclient.core.i18n import tr
from maxclient.ui.contact_profile import ContactEditPanel, confirm_action
import test_contact_profile_ui as contact_fixture


LANGUAGES = ('en', 'es', 'pt', 'fr', 'uz', 'be', 'ru')


class ContactEditorLocalizationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.panel = ContactEditPanel(language='en')
        self.context = object()
        self.snapshot = {'context': self.context, 'contact_id': 303,
                         'first_name': 'Сохранить', 'last_name': '<b>{name}</b>', 'title': 'Сохранить',
                         'availability': {'edit': True, 'delete': True}}
        self.panel.set_contact(self.snapshot)

    def tearDown(self):
        if isValid(self.panel):
            self.panel.close()
            self.panel.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()

    def window_fixture(self, language='en'):
        fixture = contact_fixture.ContactProfileUiTests()
        fixture.app = self.app
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        fixture.window.settings.ui_language = language
        context = fixture.select(fixture.alice)
        fixture.finish()
        fixture.window._contact_action('edit', context)
        return fixture, fixture.window, fixture.window._contact_edit_panel, context

    def test_catalogue_has_all_columns_and_preserves_template_fields(self):
        self.assertEqual(len(CONTACT_EDITOR_TRANSLATIONS), 8)
        for source, values in CONTACT_EDITOR_TRANSLATIONS.items():
            self.assertEqual(len(values), 6)
            fields = [name for _, name, _, _ in string.Formatter().parse(source) if name]
            for language, value in zip(LANGUAGES, values):
                self.assertEqual(tr(source, language), value)
                self.assertEqual([name for _, name, _, _ in string.Formatter().parse(value) if name], fields)
                if language != 'be':
                    self.assertIsNone(re.search('[А-Яа-яЁё]', value))
            self.assertEqual(tr(source, 'pt-BR'), values[2])

    def test_retranslate_preserves_exact_draft_selection_caret_initial_context_and_signals(self):
        p = self.panel
        p.first_name.setText('  Сохранить  ')
        p.last_name.setText('<b>{name}</b>  ')
        p.first_name.setSelection(2, 5)
        p.last_name.setCursorPosition(4)
        initial = p._initial
        state = (p.first_name.text(), p.last_name.text(), p.first_name.cursorPosition(),
                 p.first_name.selectionStart(), p.first_name.selectedText(), p.last_name.cursorPosition(),
                 p.save_button.isHidden(), p.save_button.isEnabled(), p.delete_button.isEnabled())
        called = Mock()
        for signal in (p.first_name.textChanged, p.last_name.textChanged, p.submitted,
                       p.delete_requested, p.back_requested):
            signal.connect(called)
        with patch.object(p, 'set_contact') as reset, patch.object(p, '_changed') as changed:
            for language in LANGUAGES:
                p.retranslate(language)
                self.assertEqual(p.heading.text(), tr('Настройки контакта', language))
                self.assertEqual(p.first_name.placeholderText(), tr('Имя', language))
                self.assertEqual(p.last_name.accessibleName(), tr('Фамилия', language))
                self.assertEqual(p.save_button.text(), tr('Сохранить', language))
                self.assertEqual(p.delete_button.text(), tr('Удалить контакт', language))
                self.assertEqual(p.back_button.toolTip(), tr('Назад', language))
                self.assertIs(p._context, self.context)
                self.assertIs(p._initial, initial)
                self.assertEqual((p.first_name.text(), p.last_name.text(), p.first_name.cursorPosition(),
                                 p.first_name.selectionStart(), p.first_name.selectedText(), p.last_name.cursorPosition(),
                                 p.save_button.isHidden(), p.save_button.isEnabled(), p.delete_button.isEnabled()), state)
            reset.assert_not_called()
            changed.assert_not_called()
        called.assert_not_called()

    def test_owned_status_retranslates_but_equal_unmarked_status_stays_literal(self):
        p = self.panel
        source = 'Не удалось сохранить изменения. Введённые данные сохранены в форме.'
        p.set_busy(False, status_source=source)
        for language in LANGUAGES:
            p.retranslate(language)
            self.assertEqual(p.status.text(), tr(source, language))
        p.set_busy(False, source)
        for language in LANGUAGES:
            p.retranslate(language)
            self.assertEqual(p.status.text(), source)
            self.assertIsNone(p._status_source)
        self.assertEqual(p.status.textFormat(), Qt.TextFormat.PlainText)

    def test_busy_and_revoked_capabilities_remain_disabled_without_draft_normalization(self):
        p = self.panel
        p.first_name.setText('  New value  ')
        for busy in (False, True):
            p.update_capabilities({'availability': {'edit': False, 'delete': False}})
            p.set_busy(busy, status_source='Сохранение…' if busy else None)
            for language in LANGUAGES:
                p.retranslate(language)
                self.assertIs(p._busy, busy)
                self.assertFalse(p.first_name.isEnabled())
                self.assertFalse(p.delete_button.isEnabled())
                self.assertFalse(p.save_button.isEnabled())
                self.assertEqual(p.first_name.text(), '  New value  ')
                if busy:
                    self.assertEqual(p.status.text(), tr('Сохранение…', language))

    def test_retranslate_after_cpp_deletion_is_safe(self):
        self.panel.deleteLater()
        QCoreApplication.sendPostedEvents(self.panel, QEvent.Type.DeferredDelete)
        self.assertFalse(isValid(self.panel))
        self.panel.retranslate('fr')

    def test_confirmation_is_plaintext_localized_and_default_escape_cancel(self):
        p = self.panel
        name = '<b>Сохранить</b> {name}'
        for language in LANGUAGES:
            p.settings = SimpleNamespace(ui_language=language)
            title = tr('Удалить контакт', language)
            text = tr('Удалить контакт «{name}»? Переписка останется.', language, name=name)
            def inspect(box):
                yes, no = (box.button(QMessageBox.StandardButton.Yes), box.button(QMessageBox.StandardButton.No))
                self.assertEqual(box.windowTitle(), title)
                self.assertEqual(box.text(), text)
                self.assertIn(name, box.text())
                self.assertEqual(box.textFormat(), Qt.TextFormat.PlainText)
                self.assertEqual(yes.text(), title)
                self.assertEqual(no.text(), tr('Отмена', language))
                self.assertIs(box.defaultButton(), no)
                self.assertIs(box.escapeButton(), no)
                return QMessageBox.StandardButton.No
            with patch.object(QMessageBox, 'exec', inspect):
                self.assertFalse(confirm_action(p, title, text))

    def test_constructor_and_mainwindow_language_hook_preserve_editor_draft_and_do_not_queue(self):
        fixture, window, panel, context = self.window_fixture()
        self.assertEqual(panel.heading.text(), 'Contact settings')
        panel.first_name.setText('  Raw user draft  ')
        panel.first_name.setSelection(3, 4)
        initial, selected = panel._initial, panel.first_name.selectedText()
        for language in LANGUAGES:
            window.settings.ui_language = language
            window._apply_local_language()
            self.assertEqual(panel.heading.text(), tr('Настройки контакта', language))
            self.assertEqual(panel.first_name.text(), '  Raw user draft  ')
            self.assertEqual(panel.first_name.selectedText(), selected)
            self.assertIs(panel._context, context)
            self.assertIs(panel._initial, initial)
            self.assertEqual(fixture.queue, [])
            self.assertEqual(fixture.service.writes, [])

    def test_queued_save_language_change_preserves_request_and_uses_new_language_on_failure(self):
        fixture, window, panel, context = self.window_fixture()
        panel.first_name.setText('Unsaved local draft')
        fixture.service.rename_contact_profile = Mock(side_effect=ValueError('Сохранение…'))
        panel._save()
        request, queued = window._contact_job, list(fixture.queue)
        window.settings.ui_language = 'fr'
        window._apply_local_language()
        self.assertIs(window._contact_job, request)
        self.assertEqual(fixture.queue, queued)
        self.assertTrue(panel._busy)
        self.assertEqual(panel.status.text(), tr('Сохранение…', 'fr'))
        self.assertEqual(window._contact_panel.status.text(), tr('Сохранение…', 'fr'))
        fixture.finish()
        self.assertIs(window._conversation_pages.currentWidget(), panel)
        self.assertIs(panel._context, context)
        self.assertEqual(panel.first_name.text(), 'Unsaved local draft')
        self.assertEqual(panel.status.text(), tr('Не удалось сохранить изменения. Введённые данные сохранены в форме.', 'fr'))
        self.assertEqual(window._contact_panel.status.text(), tr('Не удалось подтвердить действие. Обновите сведения.', 'fr'))
        self.assertEqual(fixture.service.writes, [])

    def test_snapshot_raw_status_and_phone_are_not_translated_after_owned_status(self):
        fixture, window, _panel, _context = self.window_fixture()
        source = 'Не удалось подтвердить действие. Обновите сведения.'
        window._render_contact_profile(status_source=source)
        self.assertEqual(window._contact_panel.status.text(), tr(source, 'en'))
        window._contact_snapshot.update(action_status=source, phone='Сохранить')
        window._render_contact_profile()
        window.settings.ui_language = 'fr'
        window._apply_local_language()
        self.assertEqual(window._contact_panel.status.text(), source)
        self.assertEqual(window._contact_panel.phone.text(), 'Сохранить')
        window._render_contact_profile(status=source)
        self.assertEqual(window._contact_panel.status.text(), source)
        self.assertIsNone(window._contact_panel._snapshot.get('action_status_source'))
        self.assertEqual(fixture.queue, [])

    def test_localized_confirmation_retains_name_and_rechecks_replaced_session(self):
        fixture, window, _panel, context = self.window_fixture()
        raw = '<b>Удалить контакт</b> {name}'
        window._contact_snapshot['title'] = raw
        def replaced(_parent, title, text):
            self.assertEqual(title, 'Delete contact')
            self.assertEqual(text, 'Delete contact “' + raw + '”? The chat history will remain.')
            fixture.service.auth = object()
            return True
        with patch('maxclient.ui.contact_profile.confirm_action', side_effect=replaced):
            window._contact_action('delete', context)
        self.assertEqual(fixture.queue, [])
        self.assertEqual(fixture.service.writes, [])

    def test_old_queued_failure_after_account_change_cannot_replace_current_presenter(self):
        fixture, window, panel, _context = self.window_fixture()
        panel.first_name.setText('Old draft')
        panel._save()
        _method, _args, _kwargs, _done, failed = fixture.queue[0]
        window.settings.ui_language = 'fr'
        window._apply_local_language()
        fixture.service.auth = object()
        before = panel.status.text()
        failed('raw server error')
        self.assertEqual(panel.status.text(), before)
        self.assertEqual(panel.first_name.text(), 'Old draft')
        self.assertEqual(fixture.service.writes, [])


if __name__ == '__main__':
    unittest.main()
