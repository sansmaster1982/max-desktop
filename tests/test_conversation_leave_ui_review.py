"""Independent confirmation behavior; offscreen widgets, no service or network."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import unittest
from unittest.mock import Mock, patch

from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QDialogButtonBox, QLabel
from shiboken6 import isValid

from maxclient.ui.conversation_leave_dialog import (
    ConversationLeaveDialog, confirm_conversation_leave,
)
from test_desktop_shell import _load_offscreen_font


class ConversationLeaveUiIndependentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        _load_offscreen_font(cls.app)

    def setUp(self):
        self.dialogs = []

    def tearDown(self):
        for dialog in self.dialogs:
            if isValid(dialog):
                dialog.close()
                dialog.deleteLater()
                QCoreApplication.sendPostedEvents(dialog, QEvent.Type.DeferredDelete)
        self.app.processEvents()

    def dialog(self, **kwargs):
        dialog = ConversationLeaveDialog('<b>Synthetic group</b>', **kwargs)
        self.dialogs.append(dialog)
        dialog.open()
        self.app.processEvents()
        return dialog

    def test_default_enter_cancels_instead_of_departing(self):
        dialog = self.dialog()
        cancelled = dialog.buttons.button(QDialogButtonBox.StandardButton.Cancel)
        self.assertTrue(cancelled.isDefault())
        self.assertFalse(dialog.leave_button.isDefault())
        self.assertFalse(dialog.leave_button.autoDefault())
        QTest.keyClick(dialog, Qt.Key.Key_Return)
        self.app.processEvents()
        self.assertEqual(dialog.result(), QDialog.DialogCode.Rejected)
        self.assertFalse(dialog.isVisible())

    def test_escape_closes_and_stops_scope_timer(self):
        dialog = self.dialog()
        QTest.keyClick(dialog, Qt.Key.Key_Escape)
        self.app.processEvents()
        self.assertEqual(dialog.result(), QDialog.DialogCode.Rejected)
        self.assertFalse(dialog.isVisible())
        self.assertFalse(dialog._scope_timer.isActive())

    def test_losing_context_closes_modal_and_late_accept_is_rejected(self):
        current = Mock(return_value=True)
        dialog = self.dialog(context_is_current=current)
        current.return_value = False
        QTest.qWait(210)
        self.assertFalse(dialog.isVisible())
        dialog.accept()
        self.assertEqual(dialog.result(), QDialog.DialogCode.Rejected)

    def test_missing_context_scope_does_not_open_confirmation(self):
        dialog = self.dialog(context_is_current=lambda: False)
        self.assertFalse(dialog.isVisible())
        self.assertFalse(dialog._scope_timer.isActive())
        self.assertEqual(dialog.result(), QDialog.DialogCode.Rejected)

    def test_title_is_literal_and_private_warning_is_visible(self):
        dialog = self.dialog(private=True)
        labels = dialog.findChildren(QLabel)
        title = next(label for label in labels if '<b>' in label.text())
        self.assertEqual(title.textFormat(), Qt.TextFormat.PlainText)
        self.assertTrue(any('приглашению' in label.text() and label.isVisible() for label in labels))

    def test_parent_scope_is_rechecked_after_modal_returns_accepted(self):
        state = {'current': True}
        snapshot = {'title': 'Synthetic', 'leave_private': True, 'action_pending': False,
                    'action_capabilities': {'supported': True, 'kind': 'group',
                        'owner': False, 'participant': True, 'more': ['leave']}}
        def accept_after_scope_changed(_dialog):
            state['current'] = False
            return QDialog.DialogCode.Accepted
        with patch.object(QDialog, 'exec', new=accept_after_scope_changed):
            result = confirm_conversation_leave(None, snapshot,
                context_is_current=lambda: state['current'])
        self.assertFalse(result)


if __name__ == '__main__':
    unittest.main()
