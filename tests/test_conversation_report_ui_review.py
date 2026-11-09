"""Independent offscreen report dialog races, with queued fake workers only."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog
from shiboken6 import isValid

from maxclient.protocol.client import MaxWriteOutcomeUnknown
from maxclient.ui.conversation_report_dialog import ConversationReportDialog
from test_desktop_shell import _load_offscreen_font


class ConversationReportUiIndependentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        _load_offscreen_font(cls.app)

    def setUp(self):
        self.context = SimpleNamespace(chat_id=303, peer_id=None)
        self.service = SimpleNamespace(is_current_conversation=Mock(return_value=True),
            get_conversation_report_reasons=Mock(), submit_conversation_report=Mock())
        self.jobs = []
        self.dialogs = []
        self.scope = {'current': True}
        def queue(work, *args, on_done, on_error, **kwargs):
            self.jobs.append((lambda: work(*args, **kwargs), on_done, on_error))
        self.patch = patch('maxclient.ui.conversation_report_dialog.run_async', side_effect=queue)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def tearDown(self):
        for dialog in self.dialogs:
            if isValid(dialog):
                dialog.close()
                dialog.deleteLater()
                QCoreApplication.sendPostedEvents(dialog, QEvent.Type.DeferredDelete)
        self.app.processEvents()

    def dialog(self):
        snapshot = {'context': self.context, 'title': '<b>Synthetic & channel</b>',
            'action_capabilities': {'supported': True, 'kind': 'channel', 'more': ['report']}}
        dialog = ConversationReportDialog(self.service, self.context, snapshot,
            context_is_current=lambda: self.scope['current'])
        self.dialogs.append(dialog)
        dialog.open()
        self.app.processEvents()
        dialog.start_loading()
        return dialog

    def payload(self, **fields):
        return {'context': self.context, 'chat_id': 303, 'type_id': 2, 'target_id': 303,
            'pending': False, 'already_reported': False, 'available': True,
            'reasons': [{'reason_id': 1, 'title': 'Synthetic reason'}], 'ticket': object(), **fields}

    def loaded(self, **fields):
        dialog = self.dialog()
        self.service.get_conversation_report_reasons.return_value = self.payload(**fields)
        worker, done, _error = self.jobs[0]
        done(worker())
        self.app.processEvents()
        return dialog

    def select(self, dialog):
        button = dialog.group.buttons()[0]
        button.setChecked(True)
        self.app.processEvents()
        self.assertTrue(dialog.submit_button.isEnabled())

    def completion(self, **fields):
        return {'context': self.context, 'chat_id': 303, 'type_id': 2, 'target_id': 303,
            'action': 'report', 'confirmed': True, 'pending': False, 'already_reported': True, **fields}

    def test_initial_reason_is_not_selected_and_title_is_plain_text(self):
        dialog = self.loaded()
        self.assertEqual(dialog.group.checkedId(), -1)
        self.assertFalse(dialog.submit_button.isEnabled())
        self.assertEqual(dialog.title.textFormat(), Qt.TextFormat.PlainText)
        self.assertEqual(dialog.title.text(), '<b>Synthetic & channel</b>')
        dialog.submit()
        self.assertEqual(len(self.jobs), 1)
        self.service.submit_conversation_report.assert_not_called()

    def test_enter_defaults_to_cancel_after_reason_selection(self):
        dialog = self.loaded()
        self.select(dialog)
        self.assertTrue(dialog.cancel_button.isDefault())
        self.assertFalse(dialog.submit_button.autoDefault())
        QTest.keyClick(dialog, Qt.Key.Key_Return)
        self.app.processEvents()
        self.assertFalse(dialog.isVisible())
        self.assertEqual(dialog.result(), QDialog.DialogCode.Rejected)
        self.assertEqual(len(self.jobs), 1)

    def test_escape_stops_timer_and_does_not_submit(self):
        dialog = self.loaded()
        self.select(dialog)
        QTest.keyClick(dialog, Qt.Key.Key_Escape)
        self.app.processEvents()
        self.assertFalse(dialog.isVisible())
        self.assertFalse(dialog._scope_timer.isActive())
        self.assertEqual(len(self.jobs), 1)

    def test_cancel_before_reasons_worker_starts_skips_service(self):
        dialog = self.dialog()
        dialog.reject()
        worker, done, _error = self.jobs[0]
        self.assertIsNone(worker())
        done(self.payload())
        self.assertEqual(dialog.group.buttons(), [])
        self.service.get_conversation_report_reasons.assert_not_called()

    def test_scope_loss_while_loading_ignores_late_reasons(self):
        dialog = self.dialog()
        self.scope['current'] = False
        QTest.qWait(210)
        self.assertFalse(dialog.isVisible())
        self.jobs[0][1](self.payload())
        self.assertEqual(dialog.group.buttons(), [])
        self.assertFalse(dialog.submit_button.isEnabled())

    def test_reasons_for_other_target_cannot_enable_submit(self):
        dialog = self.loaded(target_id=404)
        self.assertEqual(dialog.group.buttons(), [])
        self.assertFalse(dialog.submit_button.isEnabled())
        self.assertTrue(dialog.retry.isVisible())

    def test_unavailable_reasons_response_cannot_enable_submit(self):
        dialog = self.loaded(available=False)
        for button in dialog.group.buttons():
            button.setChecked(True)
        self.assertFalse(dialog.submit_button.isEnabled())

    def test_empty_title_not_replaced_with_invented_reason(self):
        dialog = self.loaded(reasons=[{'reason_id': 1, 'title': ''}])
        self.assertEqual(dialog.group.buttons(), [])
        self.assertFalse(dialog.submit_button.isEnabled())

    def test_large_and_negative_reason_id_remain_wire_value(self):
        dialog = self.loaded(reasons=[{'reason_id': -(2**40), 'title': 'Synthetic signed integer'}])
        self.select(dialog)
        dialog.submit()
        self.service.submit_conversation_report.return_value = self.completion()
        worker, done, _error = self.jobs[1]
        done(worker())
        self.service.submit_conversation_report.assert_called_once_with(
            self.context, -(2**40), ticket=dialog._ticket)

    def test_unknown_outcome_keeps_submit_and_retry_disabled(self):
        dialog = self.loaded()
        self.select(dialog)
        dialog.submit()
        dialog.submit()
        self.assertEqual(len(self.jobs), 2)
        self.service.submit_conversation_report.side_effect = MaxWriteOutcomeUnknown('Synthetic unknown')
        worker, done, _error = self.jobs[1]
        done(worker())
        self.assertIn('Повторная отправка остановлена', dialog.status.text())
        self.assertFalse(dialog.submit_button.isEnabled())
        self.assertFalse(dialog.retry.isVisible())
        dialog.submit()
        dialog.start_loading()
        self.assertEqual(len(self.jobs), 2)
        self.service.submit_conversation_report.assert_called_once()

    def test_cancel_before_submit_worker_runs_skips_write(self):
        dialog = self.loaded()
        self.select(dialog)
        dialog.submit()
        dialog.reject()
        worker, done, _error = self.jobs[1]
        envelope = worker()
        done(envelope)
        self.assertEqual(envelope, ('not_sent', None))
        self.service.submit_conversation_report.assert_not_called()

    def test_closed_dialog_ignores_late_true_completion(self):
        dialog = self.loaded()
        self.select(dialog)
        completed = Mock()
        dialog.completed.connect(completed)
        dialog.submit()
        dialog.reject()
        self.jobs[1][1](('result', self.completion()))
        completed.assert_not_called()
        self.assertFalse(dialog.isVisible())

    def test_wrong_context_success_is_not_shown_as_success(self):
        dialog = self.loaded()
        self.select(dialog)
        completed = Mock()
        dialog.completed.connect(completed)
        dialog.submit()
        self.jobs[1][1](('result', self.completion(context=SimpleNamespace(chat_id=303))))
        self.assertNotEqual(dialog.status.text(), 'Жалоба отправлена')
        completed.assert_called_once_with(None)

    def test_duplicate_completion_callback_only_notifies_once(self):
        dialog = self.loaded()
        self.select(dialog)
        completed = Mock()
        dialog.completed.connect(completed)
        dialog.submit()
        result = self.completion()
        self.jobs[1][1](('result', result))
        self.jobs[1][1](('result', result))
        completed.assert_called_once_with(result)
        self.assertEqual(dialog.status.text(), 'Жалоба отправлена')

    def test_malformed_completion_never_claims_success(self):
        dialog = self.loaded()
        self.select(dialog)
        dialog.submit()
        self.jobs[1][1]({'success': True})
        self.assertNotEqual(dialog.status.text(), 'Жалоба отправлена')
        self.assertFalse(dialog.submit_button.isEnabled())


if __name__ == '__main__':
    unittest.main()
