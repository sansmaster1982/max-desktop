"""Legacy contact menu regression tests: local synthetic data, no live requests."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication, QProgressDialog, QPushButton
from shiboken6 import isValid

import test_contact_lookup_core as fixture
from maxclient.core.models import Contact
from maxclient.ui.contacts_dialog import ContactsDialog


class ContactsMenuReviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.context = object()
        self.contact = Contact(2**40 + 9, 'Synthetic Person', '+70000000009')
        self.service = SimpleNamespace(my_id=101, search_context=lambda: self.context,
            list_contacts=Mock(return_value=[self.contact]),
            import_contacts_from_file=Mock(), add_contact_by_phone=Mock())
        self.patches = [patch('maxclient.ui.contacts_dialog.run_async'),
            patch('maxclient.ui.contacts_dialog.Avatar.set_photo'),
            patch('maxclient.ui.contacts_dialog.QMessageBox.information'),
            patch('maxclient.ui.contacts_dialog.QMessageBox.warning'),
            patch('PySide6.QtWidgets.QFileDialog.getOpenFileName',
                  return_value=('synthetic.csv', 'CSV'))]
        self.worker, _, self.info, self.warning, self.picker = [p.start() for p in self.patches]
        self.dialog = ContactsDialog(self.service)
        self.dialog.show()
        self.app.processEvents()

    def tearDown(self):
        if isValid(self.dialog):
            self.dialog.close()
            self.dialog.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()
        for item in reversed(self.patches):
            item.stop()
        self.service.import_contacts_from_file.assert_not_called()
        self.service.add_contact_by_phone.assert_not_called()

    def start_import(self):
        self.dialog._import()
        job = self.worker.call_args.kwargs
        self.assertIs(job['context'], self.context)
        self.assertFalse(job['cancel_event'].is_set())
        return job, self.dialog.findChild(QProgressDialog)

    def cancel(self, progress):
        next(button for button in progress.findChildren(QPushButton)
             if button.text() == 'Отмена').click()
        self.app.processEvents()

    def test_unavailable_list_contact_keeps_manager_and_explains_result(self):
        self.dialog.navigate_contact = Mock(return_value=False)
        self.dialog._on_click(self.dialog.list.item(0))
        self.dialog.navigate_contact.assert_called_once_with(self.contact.id, self.contact.name)
        self.assertTrue(self.dialog.isVisible())
        self.assertIn('Диалог с ним пока не загружен', self.dialog.result_label.text())
        self.worker.assert_not_called()

    def test_unavailable_lookup_result_stays_available_and_retries_navigation(self):
        self.dialog.navigate_contact = Mock(side_effect=[False, True])
        self.dialog._found_context = self.context
        self.dialog._on_added(self.contact)
        self.dialog.open_result_btn.click()
        self.assertTrue(self.dialog.isVisible())
        self.assertIs(self.dialog._found_contact, self.contact)
        self.dialog.open_result_btn.click()
        self.assertEqual(self.dialog.result(), ContactsDialog.DialogCode.Accepted)
        self.assertFalse(self.dialog.isVisible())
        self.assertEqual(self.dialog.navigate_contact.call_count, 2)

    def test_changed_context_cannot_navigate_or_accept_old_contact(self):
        self.dialog.navigate_contact = Mock(return_value=True)
        self.context = object()
        self.dialog._on_click(self.dialog.list.item(0))
        self.dialog.navigate_contact.assert_not_called()
        self.assertTrue(self.dialog.isVisible())

    def test_cancel_stops_worker_cooperatively_without_false_success_or_parallel_retry(self):
        job, progress = self.start_import()
        self.cancel(progress)
        self.assertTrue(job['cancel_event'].is_set())
        self.assertFalse(progress.isVisible())
        self.dialog._import()
        self.assertEqual(self.worker.call_count, 1)
        job['on_progress'](1, 3)
        self.assertFalse(progress.isVisible())
        job['on_done'](1)
        self.info.assert_not_called()
        self.assertIn('Импорт отменён', self.dialog.result_label.text())
        self.assertIn('1', self.dialog.result_label.text())
        self.assertTrue(self.dialog.import_btn.isEnabled())
        self.assertTrue(self.dialog.add_btn.isEnabled())

    def test_cancel_discards_late_error_and_allows_next_import_after_settlement(self):
        first, progress = self.start_import()
        self.cancel(progress)
        first['on_error']('Late transport failure')
        self.warning.assert_not_called()
        second, _ = self.start_import()
        first['on_done'](9)
        self.assertIs(self.dialog._import_cancel, second['cancel_event'])
        self.assertFalse(self.dialog.import_btn.isEnabled())

    def test_close_or_qt_delete_cancels_and_ignores_late_completion(self):
        job, _ = self.start_import()
        self.dialog.reject()
        self.assertTrue(job['cancel_event'].is_set())
        self.dialog.deleteLater()
        QCoreApplication.sendPostedEvents(self.dialog, QEvent.Type.DeferredDelete)
        job['on_progress'](1, 2)
        job['on_done'](1)
        job['on_error']('Late')
        self.info.assert_not_called()
        self.warning.assert_not_called()

    def test_qt_delete_without_finished_signal_still_cancels_worker(self):
        job, _ = self.start_import()
        self.dialog.deleteLater()
        QCoreApplication.sendPostedEvents(self.dialog, QEvent.Type.DeferredDelete)
        self.assertTrue(job['cancel_event'].is_set())
        job['on_done'](0)
        self.info.assert_not_called()

    def test_normal_completion_refreshes_once_without_marking_cancelled(self):
        job, _ = self.start_import()
        self.service.list_contacts.reset_mock()
        job['on_done'](2)
        self.assertFalse(job['cancel_event'].is_set())
        self.info.assert_called_once()
        self.service.list_contacts.assert_called_once()
        self.assertTrue(self.dialog.import_btn.isEnabled())

    def test_cancel_file_picker_does_not_queue_import(self):
        self.picker.return_value = ('', '')
        self.dialog._import()
        self.worker.assert_not_called()


class ContactImportCancellationCoreTests(unittest.TestCase):
    def setUp(self):
        self.case = fixture.ContactLookupCoreTests()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.cancel = threading.Event()
        self.entries = [('First', '+70000000001'), ('Second', '+70000000002')]
        self.parse = patch('maxclient.core.service.parse_contacts_file', return_value=self.entries)
        self.parser = self.parse.start()
        self.addCleanup(self.parse.stop)

    def run_import(self, progress=None):
        return self.case.service.import_contacts_from_file('synthetic.csv', progress,
            context=self.case.context, cancel_event=self.cancel)

    def test_cancel_before_worker_starts_reads_no_file_and_sends_no_request(self):
        self.cancel.set()
        self.assertEqual(self.run_import(), 0)
        self.parser.assert_not_called()
        self.case.client.contact_by_phone.assert_not_called()
        self.assertEqual(self.case.store.list_contacts(), [])

    def test_cancel_from_progress_stops_before_second_lookup(self):
        self.assertEqual(self.run_import(lambda *_: self.cancel.set()), 1)
        self.assertEqual(self.case.client.contact_by_phone.call_count, 1)
        self.assertEqual(len(self.case.store.list_contacts()), 1)
        self.case.client.request.assert_not_called()

    def test_inflight_lookup_can_finish_but_cancellation_never_starts_next(self):
        def response(*args, **kwargs):
            self.cancel.set()
            return self.case.contact()
        self.case.client.contact_by_phone.side_effect = response
        self.assertEqual(self.run_import(), 1)
        self.assertEqual(self.case.client.contact_by_phone.call_count, 1)
        self.assertEqual(len(self.case.store.list_contacts()), 1)
        self.case.client.request.assert_not_called()
