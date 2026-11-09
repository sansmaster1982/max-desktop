"""Read-only call history rendering; synthetic data and mocked workers."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
from PySide6.QtWidgets import QApplication
from maxclient.ui.call_history_dialog import CallHistoryDialog


class CallHistoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.service = SimpleNamespace(my_id=42, store=SimpleNamespace(get_chat=lambda _id: None), list_call_history=Mock())
        with patch('maxclient.ui.call_history_dialog.QTimer.singleShot'):
            self.dialog = CallHistoryDialog(self.service)

    def tearDown(self):
        self.dialog.close()

    def test_server_item_produces_actual_direction_duration_and_literal_title(self):
        self.dialog._received({'callHistoryItems':[
            {'historyId':1,'callerId':42,'chatId':8,'callType':'AUDIO','time':1793459502000,
             'durationMs':65000,'callName':'<b>Synthetic</b>'}]})
        self.assertEqual(self.dialog.calls.count(), 1)
        text = self.dialog.calls.item(0).text()
        self.assertIn('<b>Synthetic</b>', text)
        self.assertIn('Исходящий', text)
        self.assertIn('1:05', text)

    def test_bad_response_preserves_previous_rows_and_does_not_claim_empty(self):
        self.dialog.calls.addItem('previous synthetic call')
        self.dialog._received({'unexpected':[]})
        self.assertEqual(self.dialog.calls.count(), 1)
        self.assertIn('Не удалось', self.dialog.status.text())

    def test_busy_refresh_does_not_start_parallel_requests(self):
        with patch('maxclient.ui.call_history_dialog.run_async') as run:
            self.dialog.refresh()
            self.dialog.refresh()
            run.assert_called_once()
            self.assertFalse(self.dialog.refresh_button.isEnabled())

    def test_only_explicit_successful_empty_list_claims_no_calls(self):
        self.dialog._received({'callHistoryItems':[]})
        self.assertEqual(self.dialog.status.text(), 'История звонков пуста.')


if __name__ == '__main__':
    unittest.main()
