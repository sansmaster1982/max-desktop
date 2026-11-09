"""UI password validation and forwarding; no network or real credentials."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import unittest
from unittest.mock import Mock, patch
from PySide6.QtWidgets import QApplication, QLineEdit
from maxclient.ui.settings_dialog import Change2FADialog, RecoveryEmailDialog


class PasswordDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_change_requires_masked_current_password_before_worker(self):
        dialog = Change2FADialog(Mock())
        self.assertEqual(dialog.current.echoMode(), QLineEdit.EchoMode.Password)
        dialog.new.setText('synthetic-new')
        dialog.confirm.setText('synthetic-new')
        with patch('maxclient.ui.settings_dialog.run_async') as run:
            dialog._submit()
            run.assert_not_called()
            self.assertIn('текущий', dialog.error.text())
            dialog.current.setText('synthetic-current')
            dialog._submit()
            self.assertEqual(run.call_args.kwargs['current_password'], 'synthetic-current')
            self.assertFalse(dialog.btn.isEnabled())
        dialog.close()

    def test_recovery_checks_current_password_and_clears_it_after_track(self):
        dialog = RecoveryEmailDialog(Mock())
        self.assertEqual(dialog.current.echoMode(), QLineEdit.EchoMode.Password)
        dialog.email.setText('synthetic@example.invalid')
        with patch('maxclient.ui.settings_dialog.run_async') as run:
            dialog._send()
            run.assert_not_called()
            dialog.current.setText('synthetic-current')
            dialog._send()
            self.assertEqual(run.call_args.kwargs['current_password'], 'synthetic-current')
        with patch.object(dialog, '_plain_information'):
            dialog._code_sent('synthetic-track')
        self.assertEqual(dialog.current.text(), '')
        self.assertFalse(dialog.current.isEnabled())
        dialog.close()


if __name__ == '__main__':
    unittest.main()
