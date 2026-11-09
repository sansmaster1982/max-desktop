"""New phone form tests: local validation, bounded workers, and session lifetime."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtWidgets import QApplication
from shiboken6 import isValid

from maxclient.core.models import Contact
from maxclient.ui.phone_lookup_dialog import PhoneLookupDialog, lookup_phone_input


class PhoneLookupInputTests(unittest.TestCase):
    def test_normalization_reuses_existing_local_number_conventions(self):
        for value, expected in [('  +44 (20) 1234-5678 ', '+442012345678'),
                                ('+1 202 555 0123', '+12025550123'),
                                ('8 (900) 000-00-01', '+79000000001'),
                                ('9000000001', '+79000000001')]:
            with self.subTest(value=value):
                self.assertEqual(lookup_phone_input(value), expected)

    def test_incomplete_or_malformed_or_oversize_input_is_disabled(self):
        for value in ['', '+7', '+123456', '+012345678', 'prefix+79000000001',
                      '+7;print(1)', '+7+9000000001', '+7\n9000000001',
                      '+１２３４５６７８９', '+1234567890123456', ' ' * 65, None]:
            with self.subTest(value=value):
                self.assertIsNone(lookup_phone_input(value))


class PhoneLookupDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.context = object()
        self.service = SimpleNamespace(my_id=101, search_context=lambda: self.context,
                                       add_contact_by_phone=Mock())
        self.worker_patch = patch('maxclient.ui.phone_lookup_dialog.run_async')
        self.worker = self.worker_patch.start()
        self.dialog = PhoneLookupDialog(self.service)
        self.dialog.show()
        self.opened = []
        self.dialog.open_chat.connect(lambda identity, name: self.opened.append((identity, name)))

    def tearDown(self):
        if isValid(self.dialog):
            self.dialog.close()
            self.dialog.deleteLater()
            QCoreApplication.sendPostedEvents(self.dialog, QEvent.Type.DeferredDelete)
        self.app.processEvents()
        self.worker_patch.stop()
        self.service.add_contact_by_phone.assert_not_called()

    def start(self, phone='+7 900 000 00 01'):
        self.dialog.phone.setText(phone)
        self.dialog._submit()
        call = self.worker.call_args
        self.assertIs(call.kwargs['context'], self.context)
        return call.kwargs

    def test_open_typing_and_cancel_do_not_start_lookup(self):
        self.assertFalse(self.dialog.find_button.isEnabled())
        self.dialog.phone.setText('+44 20 1234 5678')
        self.assertTrue(self.dialog.find_button.isEnabled())
        self.dialog.reject()
        self.worker.assert_not_called()
        self.assertEqual(self.opened, [])

    def test_only_explicit_submit_passes_normalized_phone_and_captured_context(self):
        self.start()
        self.assertEqual(self.worker.call_args.args, (self.service.add_contact_by_phone, '+79000000001'))
        self.assertFalse(self.dialog.phone.isEnabled())
        self.assertFalse(self.dialog.find_button.isEnabled())
        self.dialog._submit()
        self.assertEqual(self.worker.call_count, 1)

    def test_self_result_does_not_claim_add_and_opens_saved_only_on_explicit_action(self):
        job = self.start()
        job['on_done'](Contact(101, 'Synthetic Self', '+79000000001'))
        self.assertIn('Это ваш профиль', self.dialog.result_label.text())
        self.assertIn('Synthetic Self', self.dialog.result_label.text())
        self.assertNotIn('добавлен', self.dialog.result_label.text())
        self.assertEqual(self.dialog.open_result_btn.text(), 'Открыть Избранное')
        self.assertEqual(self.opened, [])
        self.dialog.open_result_btn.click()
        self.assertEqual(self.opened, [(101, 'Synthetic Self')])

    def test_result_fields_are_literal_and_64_bit_identity_survives_signal(self):
        job = self.start()
        job['on_done'](Contact(2**40, '<b>Literal</b>', '<i>phone</i>'))
        self.assertEqual(self.dialog.result_label.textFormat(), Qt.TextFormat.PlainText)
        self.assertIn('<b>Literal</b>', self.dialog.result_label.text())
        self.dialog.open_result_btn.click()
        self.assertEqual(self.opened, [(2**40, '<b>Literal</b>')])

    def test_late_success_after_reject_or_qt_deletion_is_discarded(self):
        job = self.start()
        self.dialog.reject()
        job['on_done'](Contact(202, 'Late'))
        self.assertIsNone(self.dialog._found_contact)
        self.dialog.deleteLater()
        QCoreApplication.sendPostedEvents(self.dialog, QEvent.Type.DeferredDelete)
        job['on_error']('Late error')
        self.assertEqual(self.opened, [])

    def test_session_switch_during_request_clears_result_and_disables_form(self):
        job = self.start()
        self.context = object()
        job['on_done'](Contact(202, 'Foreign now'))
        self.assertIsNone(self.dialog._found_contact)
        self.assertFalse(self.dialog.find_button.isEnabled())
        self.assertIn('Сессия изменилась', self.dialog.result_label.text())
        self.assertEqual(self.opened, [])

    def test_session_switch_after_result_cannot_open_contact(self):
        job = self.start()
        job['on_done'](Contact(202, 'Previous context'))
        self.context = object()
        self.dialog.open_result_btn.click()
        self.assertEqual(self.opened, [])
        self.assertIsNone(self.dialog._found_contact)

    def test_failed_request_can_retry_old_completion_cannot_override_new_request(self):
        first = self.start()
        first['on_error']('Синтетическая ошибка')
        self.assertTrue(self.dialog.find_button.isEnabled())
        second = self.start('+44 20 1234 5678')
        first['on_done'](Contact(202, 'Old'))
        self.assertIsNone(self.dialog._found_contact)
        second['on_done'](Contact(303, 'New'))
        self.assertEqual(self.dialog._found_contact.name, 'New')

    def test_edit_after_result_invalidates_old_open_action(self):
        job = self.start()
        job['on_done'](Contact(202, 'Previous number'))
        self.dialog.phone.setText('+44 20 1234 5678')
        self.dialog._open_found()
        self.assertIsNone(self.dialog._found_contact)
        self.assertTrue(self.dialog.open_result_btn.isHidden())
        self.assertEqual(self.opened, [])

    def test_programmatic_input_change_while_pending_discards_mismatched_response(self):
        job = self.start()
        self.dialog.phone.setText('+44 20 1234 5678')
        job['on_done'](Contact(202, 'Old number'))
        self.assertIsNone(self.dialog._found_contact)
        self.assertTrue(self.dialog.phone.isEnabled())
        self.assertEqual(self.opened, [])

    def test_invalid_contact_result_does_not_enable_navigation(self):
        for contact in [None, {}, Contact(True, 'Boolean'), Contact('202', 'Text'),
                        Contact(-(2**63) - 1, 'Underflow'), Contact(2**63, 'Overflow')]:
            job = self.start()
            job['on_done'](contact)
            self.assertIsNone(self.dialog._found_contact)
            self.assertTrue(self.dialog.open_result_btn.isHidden())
        self.assertEqual(self.opened, [])

    def test_signed_zero_contact_identity_is_not_confused_with_self_chat(self):
        job = self.start()
        job['on_done'](Contact(0, 'Zero actual contact'))
        self.assertEqual(self.dialog.open_result_btn.text(), 'Открыть чат')
        self.dialog.open_result_btn.click()
        self.assertEqual(self.opened, [(0, 'Zero actual contact')])

    def test_missing_session_cannot_submit(self):
        self.context = None
        self.dialog.phone.setText('+79000000001')
        self.dialog._submit()
        self.worker.assert_not_called()


if __name__ == '__main__':
    unittest.main()
