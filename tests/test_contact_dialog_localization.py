"""Local dialog wording and async language changes; every worker is mocked."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import re
from string import Formatter
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QLabel, QPushButton, QWidget
from shiboken6 import isValid

from maxclient.core._contact_dialog_translations import CONTACT_DIALOG_TRANSLATIONS
from maxclient.core.contact_create import ContactCreateResult
from maxclient.core.i18n import SUPPORTED_LANGUAGES, tr, translation_available
from maxclient.core.models import Contact
from maxclient.protocol.client import MaxError, MaxWriteOutcomeUnknown
from maxclient.ui.contact_create_dialog import ContactCreateDialog, _Failure, _save
from maxclient.ui.phone_lookup_dialog import PhoneLookupDialog
from maxclient.ui.contacts_dialog import ContactsDialog
from test_desktop_shell import _load_offscreen_font


class ContactDialogLocalizationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        _load_offscreen_font(cls.app)

    def setUp(self):
        self.context = object()
        self.service = SimpleNamespace(my_id=101, search_context=lambda: self.context,
            list_contacts=Mock(return_value=[]), add_contact_by_phone=Mock(),
            import_contacts_from_file=Mock(), rename_contact=Mock(), delete_contact=Mock(),
            create_contact_by_phone=Mock(), verify_contact_creation=Mock())
        self.parent = QWidget()
        self.parent.settings = SimpleNamespace(ui_language='en')
        self.dialogs = []
        self.runners = {}
        for module in ('contact_create_dialog', 'phone_lookup_dialog', 'contacts_dialog'):
            target = patch('maxclient.ui.' + module + '.run_async')
            self.runners[module] = target.start()
            self.addCleanup(target.stop)

    def tearDown(self):
        for dialog in self.dialogs:
            if isValid(dialog):
                dialog.close()
        self.parent.close()
        self.app.processEvents()

    def dialog(self, kind):
        dialog = kind(self.service, self.parent)
        self.dialogs.append(dialog)
        return dialog

    def test_catalog_has_six_columns_preserved_placeholders_and_all_locale_lookups(self):
        fields = lambda value: {name for _, name, _, _ in Formatter().parse(value) if name}
        self.assertEqual(len(CONTACT_DIALOG_TRANSLATIONS), 47)
        for source, row in CONTACT_DIALOG_TRANSLATIONS.items():
            self.assertEqual(len(row), 6)
            for language, translated in zip(SUPPORTED_LANGUAGES[1:], row):
                self.assertTrue(translated)
                self.assertEqual(fields(source), fields(translated), (source, language))
                self.assertTrue(translation_available(source, language), (source, language))
                if language != 'be':
                    self.assertIsNone(re.search('[А-Яа-яЁё]', translated), (source, language))

    def test_all_three_dialogs_inherit_language_and_switch_without_workers_or_data_changes(self):
        create = self.dialog(ContactCreateDialog)
        phone = self.dialog(PhoneLookupDialog)
        contacts = self.dialog(ContactsDialog)
        self.assertEqual(create.windowTitle(), 'Add contact')
        self.assertEqual(contacts.windowTitle(), 'Local contacts')
        self.assertEqual(contacts.search.placeholderText(), 'Search by name or number')
        create.first_name.setText('Контакт найден')
        create.last_name.setText('<b>Raw name</b>')
        for language in SUPPORTED_LANGUAGES:
            for dialog in (create, phone, contacts):
                dialog.set_language(language)
                values = [dialog.windowTitle()]
                values.extend(widget.text() for widget in dialog.findChildren(QLabel))
                values.extend(widget.text() for widget in dialog.findChildren(QPushButton))
                if language not in ('ru', 'be'):
                    self.assertFalse(any(re.search('[А-Яа-яЁё]', text) for text in values), language)
        self.assertEqual(create.first_name.text(), 'Контакт найден')
        self.assertEqual(create.last_name.text(), '<b>Raw name</b>')
        self.assertEqual(create.phone.text(), '+7')
        self.assertEqual(phone.phone.text(), '+7')
        for runner in self.runners.values():
            runner.assert_not_called()
        self.service.list_contacts.assert_called_once_with('')

    def test_save_pending_uses_callback_language_and_keeps_verification_guard(self):
        dialog = self.dialog(ContactCreateDialog)
        dialog.phone.setText('+79990000000')
        dialog.first_name.setText('Synthetic')
        dialog._submit()
        callback = self.runners['contact_create_dialog'].call_args.kwargs['on_done']
        dialog.set_language('fr')
        callback(_Failure('Сохранение пока не подтверждено. Проверьте результат перед повторной попыткой.', True))
        self.assertIn('pas encore confirmé', dialog.result_label.text())
        self.assertTrue(dialog.verify_button.isEnabled())
        self.assertFalse(dialog.save_button.isEnabled())
        dialog.set_language('en')
        self.assertIn('not been confirmed', dialog.result_label.text())
        self.assertEqual(self.runners['contact_create_dialog'].call_count, 1)

    def test_save_existing_name_notice_and_raw_service_error_remain_distinct(self):
        dialog = self.dialog(ContactCreateDialog)
        dialog.phone.setText('+79990000000')
        dialog.first_name.setText('Synthetic')
        dialog._submit()
        notice = Mock()
        dialog.save_notice.connect(notice)
        callback = self.runners['contact_create_dialog'].call_args.kwargs['on_done']
        callback(ContactCreateResult(Contact(202, 'Сохранение…', '+79990000000'), False, False))
        self.assertEqual(notice.call_args.args[0], 'The contact already exists in MAX. The existing name was kept; you can change it in the contact profile.')
        self.service.create_contact_by_phone.side_effect = MaxError('Контакт найден')
        failure = _save(self.service, {'phone': '+79990000000', 'firstName': 'Raw'}, self.context)
        self.assertFalse(failure.app_owned)
        other = self.dialog(ContactCreateDialog)
        other._message(failure.message, app_owned=failure.app_owned)
        other.set_language('fr')
        self.assertEqual(other.result_label.text(), 'Контакт найден')

    def test_phone_lookup_translates_found_status_but_preserves_name_phone_and_markup(self):
        dialog = self.dialog(PhoneLookupDialog)
        dialog.phone.setText('+79990000000')
        dialog._submit()
        callback = self.runners['phone_lookup_dialog'].call_args.kwargs['on_done']
        dialog.set_language('fr')
        callback(Contact(101, '<b>Контакт найден</b>', '+79990000000'))
        self.assertIn('C’est votre profil', dialog.result_label.text())
        self.assertIn('<b>Контакт найден</b>', dialog.result_label.text())
        self.assertIn('+79990000000', dialog.result_label.text())
        self.assertEqual(dialog.result_label.textFormat(), Qt.TextFormat.PlainText)
        dialog.set_language('en')
        self.assertEqual(dialog.open_result_btn.text(), 'Open Favorites')
        self.assertIn('This is your profile', dialog.result_label.text())
        self.assertEqual(self.runners['phone_lookup_dialog'].call_count, 1)

    def test_lookup_raw_error_is_never_translated_even_if_it_equals_an_app_key(self):
        for kind, module in ((PhoneLookupDialog, 'phone_lookup_dialog'), (ContactsDialog, 'contacts_dialog')):
            dialog = self.dialog(kind)
            if kind is PhoneLookupDialog:
                dialog.phone.setText('+79990000000')
                dialog._submit()
            else:
                with patch('maxclient.ui.contacts_dialog.QInputDialog.getText', return_value=('+79990000000', True)):
                    dialog._add_by_phone()
            self.runners[module].call_args.kwargs['on_error']('Контакт найден')
            dialog.set_language('fr')
            self.assertEqual(dialog.result_label.text(), 'Контакт найден')

    def test_contacts_prompts_and_warning_titles_translate_without_translating_values(self):
        dialog = self.dialog(ContactsDialog)
        contact = Contact(202, 'Контакт найден', '+79990000000')
        with patch('maxclient.ui.contacts_dialog.QInputDialog.getText', return_value=('Raw renamed', False)) as prompt:
            dialog._rename(contact)
        self.assertEqual(prompt.call_args.args[1:3], ('Rename contact', 'Name:'))
        self.assertEqual(prompt.call_args.kwargs['text'], 'Контакт найден')
        self.service.rename_contact.assert_not_called()
        self.service.delete_contact.side_effect = ValueError('PRIVATE_RAW_ERROR')
        with patch('maxclient.ui.contacts_dialog.QMessageBox.warning') as warning:
            dialog._delete(contact)
        self.assertEqual(warning.call_args.args[1:3], ('Could not remove contact', 'PRIVATE_RAW_ERROR'))

    def test_import_progress_and_completion_translate_at_current_language_with_counts(self):
        dialog = self.dialog(ContactsDialog)
        with patch('PySide6.QtWidgets.QFileDialog.getOpenFileName', return_value=('synthetic.vcf', '')) as file_dialog:
            dialog._import()
        self.assertEqual(file_dialog.call_args.args[1], 'Choose a contacts file')
        self.assertIn('*.vcf *.csv', file_dialog.call_args.args[3])
        callbacks = self.runners['contacts_dialog'].call_args.kwargs
        callbacks['on_progress'](2, 5)
        self.assertEqual(dialog._import_dialog.labelText(), 'Checking: 2 of 5')
        dialog.set_language('fr')
        self.assertEqual(dialog._import_dialog.labelText(), 'Vérification : 2 sur 5')
        with patch('maxclient.ui.contacts_dialog.QMessageBox.information') as message:
            callbacks['on_done'](3)
        self.assertEqual(message.call_args.args[1:3], ('Importation terminée', 'Trouvés dans MAX : 3'))
        self.service.import_contacts_from_file.assert_not_called()

    def test_import_cancellation_keeps_count_and_does_not_claim_rollback(self):
        dialog = self.dialog(ContactsDialog)
        with patch('PySide6.QtWidgets.QFileDialog.getOpenFileName', return_value=('synthetic.csv', '')):
            dialog._import()
        callbacks = self.runners['contacts_dialog'].call_args.kwargs
        dialog._import_dialog.canceled.emit()
        self.assertTrue(callbacks['cancel_event'].is_set())
        self.assertIn('current check', dialog.result_label.text())
        dialog.set_language('fr')
        callbacks['on_done'](2)
        self.assertEqual(dialog.result_label.text(), 'Importation annulée. Déjà trouvés dans MAX : 2.')
        dialog.set_language('en')
        self.assertEqual(dialog.result_label.text(), 'Import canceled. Already found in MAX: 2.')

    def test_expired_dialog_statuses_retranslate_without_reenabling_actions(self):
        dialogs = [self.dialog(kind) for kind in (ContactCreateDialog, PhoneLookupDialog, ContactsDialog)]
        self.context = object()
        dialogs[0]._submit()
        dialogs[1]._submit()
        dialogs[2]._add_by_phone()
        for dialog in dialogs:
            dialog.set_language('fr')
            text = dialog.empty.text() if isinstance(dialog, ContactsDialog) else dialog.result_label.text()
            self.assertIn('session a changé', text)
        self.assertFalse(dialogs[0].save_button.isEnabled())
        self.assertFalse(dialogs[1].find_button.isEnabled())
        self.assertFalse(dialogs[2].add_btn.isEnabled())
        for runner in self.runners.values():
            runner.assert_not_called()


if __name__ == '__main__':
    unittest.main()
