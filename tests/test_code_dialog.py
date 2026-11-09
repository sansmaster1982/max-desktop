import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from PySide6.QtWidgets import QApplication
from maxclient.ui.verification_codes_dialog import VerificationCodesDialog


class CodeDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        context = object()
        def require_context(value):
            if value is not context:
                raise ValueError('Synthetic stale context')
            return context
        self.dialog = VerificationCodesDialog(SimpleNamespace(
            verification_code_messages=lambda **_kw: None,
            _history_read_context=lambda: context,
            _require_history_context=require_context))

    def tearDown(self):
        self.dialog.close()

    def test_missing_code_chat_does_not_show_code(self):
        self.dialog._received({'status': 'chat_not_available', 'messages': []})
        self.assertIn('не доступен', self.dialog.status.text())
        self.assertEqual(self.dialog.messages.toPlainText(), '')
        self.assertFalse(self.dialog.copy_button.isEnabled())

    def test_visible_text_keeps_literal_characters_and_server_time(self):
        self.dialog._received({'status': 'available', 'messages': [
            {'id': 2, 'time_ms': 1793459502000, 'text': '<b>123456</b>'},
            {'id': 1, 'time_ms': None, 'text': '999999'},
        ]})
        self.assertIn('<b>123456</b>', self.dialog.messages.toPlainText())
        self.assertNotIn('999999', self.dialog.messages.toPlainText())
        self.assertEqual(self.dialog._last_text, '<b>123456</b>')

    def test_unknown_result_is_error_not_false_chat_found(self):
        self.dialog._received({'status': 'not_verified', 'messages': [{'time_ms':1793459502000,'text':'123456'}]})
        self.assertIn('Не удалось', self.dialog.status.text())
        self.assertEqual(self.dialog.messages.toPlainText(), '')

    def test_duplicate_refresh_does_not_start_parallel_requests(self):
        with patch('maxclient.ui.verification_codes_dialog.run_async') as run:
            self.dialog.refresh()
            self.dialog.refresh()
            self.assertEqual(run.call_count, 1)
            self.assertFalse(self.dialog.refresh_button.isEnabled())

    def test_finite_float_and_zero_server_timestamps_are_displayed_without_guessing(self):
        for stamp in (0, 1793459502000.25):
            with self.subTest(stamp=stamp):
                self.dialog._received({'status': 'available', 'messages': [
                    {'time_ms': stamp, 'text': 'Synthetic button text\n000123'}]})
                self.assertIn('Synthetic button text\n000123', self.dialog.messages.toPlainText())
                self.assertTrue(self.dialog.copy_button.isEnabled())

    def test_malformed_records_or_invalid_time_clear_previous_copy_state(self):
        self.dialog._received({'status': 'available', 'messages': [{'time_ms': 0, 'text': 'old'}]})
        self.dialog._received({'status': 'available', 'messages': 'malformed'})
        self.assertEqual(self.dialog.messages.toPlainText(), '')
        self.assertFalse(self.dialog.copy_button.isEnabled())
        for stamp in (True, '1700000000000', float('inf'), float('nan')):
            self.dialog._received({'status': 'available', 'messages': [{'time_ms': stamp, 'text': 'old'}]})
            self.assertEqual(self.dialog.messages.toPlainText(), '')
            self.assertFalse(self.dialog.copy_button.isEnabled())


if __name__ == '__main__':
    unittest.main()
