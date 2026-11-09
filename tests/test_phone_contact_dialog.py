"""Phone lookup presentation with synthetic contacts and no background workers."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QListWidgetItem

from maxclient.core.models import Contact
from maxclient.ui.contacts_dialog import ContactsDialog


class PhoneContactDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.context = object()
        self.service = SimpleNamespace(my_id=101, search_context=lambda: self.context,
            list_contacts=lambda query='': [], add_contact_by_phone=Mock())
        self.dialog = ContactsDialog(self.service)
        self.opened = []
        self.dialog.open_chat.connect(lambda identity, name: self.opened.append((identity, name)))
        self.dialog.show()
        self.worker_patch = patch('maxclient.ui.contacts_dialog.run_async')
        self.worker = self.worker_patch.start()
        self.input_patch = patch('maxclient.ui.contacts_dialog.QInputDialog.getText',
                                 return_value=('+70000000000', True))
        self.input = self.input_patch.start()

    def tearDown(self):
        self.dialog.close()
        self.dialog.deleteLater()
        self.app.processEvents()
        self.input_patch.stop()
        self.worker_patch.stop()
        self.service.add_contact_by_phone.assert_not_called()

    def start(self):
        self.dialog._add_by_phone()
        call = self.worker.call_args
        self.assertIs(call.kwargs['context'], self.context)
        return call.kwargs

    def test_self_lookup_shows_name_phone_and_explicit_saved_button(self):
        job = self.start()
        job['on_done'](Contact(101, 'Тестовый Профиль', '+70000000000'))
        text = self.dialog.result_label.text()
        self.assertIn('Это ваш профиль', text)
        self.assertIn('Тестовый Профиль', text)
        self.assertIn('+70000000000', text)
        self.assertNotIn('Контакт добавлен', text)
        self.assertEqual(self.dialog.open_result_btn.text(), 'Открыть Избранное')
        self.assertEqual(self.opened, [])
        self.dialog.open_result_btn.click()
        self.assertEqual(self.opened, [(101, 'Тестовый Профиль')])

    def test_other_lookup_does_not_claim_server_add_and_keeps_text_literal(self):
        job = self.start()
        job['on_done'](Contact(202, '<b>Тестовый контакт</b>', '+70000000001'))
        self.assertIn('Контакт найден', self.dialog.result_label.text())
        self.assertIn('<b>Тестовый контакт</b>', self.dialog.result_label.text())
        self.assertEqual(self.dialog.result_label.textFormat(), Qt.TextFormat.PlainText)
        self.assertEqual(self.dialog.open_result_btn.text(), 'Открыть чат')
        self.assertEqual(self.opened, [])

    def test_lookup_action_is_distinct_from_server_contact_creation(self):
        self.assertEqual(self.dialog.add_btn.text(), 'Найти по номеру')
        self.assertEqual(self.dialog.windowTitle(), 'Локальные контакты')
        self.start()
        self.assertEqual(self.input.call_args.args[1], 'Найти по номеру')
        self.assertIn('Поиск', self.dialog.result_label.text())

    def test_lookup_normalizes_phone_before_starting_worker(self):
        self.input.return_value = (' 8 (900) 000-00-01 ', True)
        self.start()
        self.assertEqual(self.worker.call_args.args,
                         (self.service.add_contact_by_phone, '+79000000001'))

    def test_incomplete_and_malformed_numbers_never_start_lookup(self):
        for phone in ('', '+7', 'bad-format-+7', '+7+9000000001',
                      '+7\n9000000001', '+1234567890123456'):
            with self.subTest(phone=phone):
                self.input.return_value = (phone, True)
                self.dialog._add_by_phone()
                self.worker.assert_not_called()
                self.assertIn('полный номер', self.dialog.result_label.text())
                self.assertTrue(self.dialog.add_btn.isEnabled())
                self.assertIsNone(self.dialog._lookup_request)

    def test_cancel_does_not_start_lookup(self):
        self.input.return_value = ('+79000000001', False)
        self.dialog._add_by_phone()
        self.worker.assert_not_called()
        self.assertTrue(self.dialog.result_label.isHidden())

    def test_invalid_new_phone_clears_previous_result_navigation(self):
        job = self.start()
        job['on_done'](Contact(202, 'Previous result'))
        self.input.return_value = ('+7', True)
        self.dialog._add_by_phone()
        self.dialog._open_found()
        self.assertEqual(self.worker.call_count, 1)
        self.assertIsNone(self.dialog._found_contact)
        self.assertIsNone(self.dialog._found_context)
        self.assertTrue(self.dialog.open_result_btn.isHidden())
        self.assertEqual(self.opened, [])

    def test_pending_lookup_cannot_start_duplicate_worker(self):
        self.start()
        self.dialog._add_by_phone()
        self.assertEqual(self.worker.call_count, 1)

    def test_late_success_after_close_cannot_show_or_open_contact(self):
        job = self.start()
        self.dialog.reject()
        job['on_done'](Contact(101, 'Late'))
        self.assertIsNone(self.dialog._found_contact)
        self.assertEqual(self.opened, [])

    def test_late_success_after_account_change_is_discarded(self):
        job = self.start()
        self.context = object()
        job['on_done'](Contact(101, 'Late'))
        self.assertIsNone(self.dialog._found_contact)
        self.assertEqual(self.dialog.result_label.text(), '')
        self.assertIsNone(self.dialog._lookup_request)
        self.assertIsNone(self.dialog._dialog_context)
        self.assertFalse(self.dialog.add_btn.isEnabled())
        self.assertFalse(self.dialog.import_btn.isEnabled())
        self.assertIn('Сессия изменилась', self.dialog.empty.text())

    def test_late_error_after_account_change_expires_pending_lookup(self):
        job = self.start()
        self.context = object()
        job['on_error']('Old connection error')
        self.assertIsNone(self.dialog._lookup_request)
        self.assertIsNone(self.dialog._dialog_context)
        self.assertFalse(self.dialog.add_btn.isEnabled())
        self.assertIn('Сессия изменилась', self.dialog.empty.text())
        self.assertNotIn('Old connection', self.dialog.result_label.text())

    def test_session_change_in_phone_prompt_expires_without_worker(self):
        def switch_account(*args, **kwargs):
            self.context = object()
            return '+79000000001', True
        self.input.side_effect = switch_account
        self.dialog._add_by_phone()
        self.worker.assert_not_called()
        self.assertFalse(self.dialog.add_btn.isEnabled())
        self.assertIn('Сессия изменилась', self.dialog.empty.text())

    def test_found_button_cannot_navigate_after_account_change(self):
        job = self.start()
        job['on_done'](Contact(101, 'Test'))
        self.context = object()
        self.dialog.open_result_btn.click()
        self.assertEqual(self.opened, [])
        self.assertIsNone(self.dialog._found_contact)
        self.assertIn('Сессия изменилась', self.dialog.empty.text())

    def test_error_can_retry_and_old_completion_is_ignored(self):
        first = self.start()
        first['on_error']('Синтетическая ошибка')
        self.assertTrue(self.dialog.add_btn.isEnabled())
        self.assertTrue(self.dialog.open_result_btn.isHidden())
        second = self.start()
        first['on_done'](Contact(202, 'Stale'))
        self.assertIsNone(self.dialog._found_contact)
        second['on_done'](Contact(101, 'Current'))
        self.assertEqual(self.dialog._found_contact.name, 'Current')

    def test_missing_session_does_not_start_lookup(self):
        self.context = None
        self.dialog._add_by_phone()
        self.worker.assert_not_called()
        self.assertFalse(self.dialog.add_btn.isEnabled())
        self.assertIn('Сессия изменилась', self.dialog.empty.text())

    def test_invalid_contact_identity_cannot_enable_navigation(self):
        for contact in (None, Contact(True, 'Boolean'), Contact('202', 'String'),
                        Contact(2**63, 'Overflow')):
            with self.subTest(contact=contact):
                job = self.start()
                job['on_done'](contact)
                self.assertIsNone(self.dialog._found_contact)
                self.assertTrue(self.dialog.open_result_btn.isHidden())
                self.assertTrue(self.dialog.add_btn.isEnabled())
        self.assertEqual(self.opened, [])

    def test_existing_contact_row_cannot_navigate_after_context_change(self):
        item = QListWidgetItem(self.dialog.list)
        item.setData(Qt.ItemDataRole.UserRole, Contact(202, 'Old account row'))
        self.context = object()
        self.dialog._on_click(item)
        self.assertEqual(self.opened, [])

    def test_rename_and_delete_cannot_modify_contact_after_context_change(self):
        self.service.rename_contact = Mock()
        self.service.delete_contact = Mock()
        self.context = object()
        self.dialog._rename(Contact(202, 'Old row'))
        self.dialog._delete(Contact(202, 'Old row'))
        self.service.rename_contact.assert_not_called()
        self.service.delete_contact.assert_not_called()

    def test_rename_rechecks_account_after_nested_input_dialog(self):
        self.service.rename_contact = Mock()
        def switch_account(*args, **kwargs):
            self.context = object()
            return 'New name', True
        with patch('maxclient.ui.contacts_dialog.QInputDialog.getText', side_effect=switch_account):
            self.dialog._rename(Contact(202, 'Old row'))
        self.service.rename_contact.assert_not_called()

    def test_local_rename_passes_the_dialogs_pinned_session_to_core(self):
        self.service.rename_contact = Mock()
        with patch('maxclient.ui.contacts_dialog.QInputDialog.getText',
                   return_value=('  New local alias  ', True)):
            self.dialog._rename(Contact(202, 'Old local alias'))
        self.service.rename_contact.assert_called_once_with(
            202, 'New local alias', context=self.context)

    def test_local_delete_passes_the_dialogs_pinned_session_to_core(self):
        self.service.delete_contact = Mock()
        self.dialog._delete(Contact(202, 'Old local alias'))
        self.service.delete_contact.assert_called_once_with(202, context=self.context)

    def test_delete_failure_keeps_dialog_available_and_reports_error(self):
        self.service.delete_contact = Mock(side_effect=RuntimeError('Synthetic storage error'))
        with patch('maxclient.ui.contacts_dialog.QMessageBox.warning') as warning:
            self.dialog._delete(Contact(202, 'Old local alias'))
        warning.assert_called_once_with(
            self.dialog, 'Не удалось удалить контакт', 'Synthetic storage error')
        self.assertTrue(self.dialog.add_btn.isEnabled())

    def test_session_change_during_delete_expires_without_stale_error_popup(self):
        def expired(*args, **kwargs):
            self.context = object()
            raise ValueError('Old account')
        self.service.delete_contact = Mock(side_effect=expired)
        with patch('maxclient.ui.contacts_dialog.QMessageBox.warning') as warning:
            self.dialog._delete(Contact(202, 'Old local alias'))
        warning.assert_not_called()
        self.assertFalse(self.dialog.add_btn.isEnabled())
        self.assertIn('Сессия изменилась', self.dialog.empty.text())

    def test_refresh_discards_rows_and_found_result_if_read_changes_account(self):
        job = self.start()
        def switch_account(query=''):
            self.context = object()
            return [Contact(202, 'Stale account')]
        self.service.list_contacts = switch_account
        job['on_done'](Contact(101, 'Old owner'))
        self.assertEqual(self.dialog.list.count(), 0)
        self.assertIsNone(self.dialog._found_contact)
        self.assertTrue(self.dialog.result_label.isHidden())
        self.assertTrue(self.dialog.open_result_btn.isHidden())


if __name__ == '__main__':
    unittest.main()
