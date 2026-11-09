"""Ctrl+R prepares a reply from synthetic loaded history without any RPC."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import unittest
from unittest.mock import patch

from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QLineEdit, QMenu, QVBoxLayout

from maxclient.core.config import Settings
from maxclient.core.models import Chat, Message
from maxclient.protocol.client import AuthenticatedSession, ConnectionState
from maxclient.ui.chat_view import ChatView
from test_desktop_shell import _Service


class ReplyLastShortcutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.service = _Service()
        self.service.my_id = 123
        self.service.client.authenticated_session = AuthenticatedSession(123, 'synthetic-device', 'WEB', object())
        self.messages = [Message(1, id=101, sender=201, text='First', time_ms=1),
                         Message(1, id=102, sender=123, text='Latest loaded', time_ms=2, outgoing=True)]
        self.service.local_history = lambda _id: list(self.messages)
        self.runner_patch = patch('maxclient.ui.chat_view.run_async')
        self.runner = self.runner_patch.start()
        self.view = ChatView(self.service, Settings())
        self.view.resize(700, 500)
        self.view.show()
        self.view.activateWindow()
        self.view.open_chat(Chat(1, title='Synthetic chat'))
        self.view.input.setPlainText('Draft stays intact')
        self.view.input.setFocus()
        self.app.processEvents()
        self.runner.reset_mock()

    def tearDown(self):
        self.view.close()
        self.view.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()
        self.runner_patch.stop()

    def press(self, target=None, modifiers=Qt.KeyboardModifier.ControlModifier):
        QTest.keyClick(target or self.view.input, Qt.Key.Key_R, modifiers)
        self.app.processEvents()

    def test_actual_ctrl_r_selects_latest_confirmed_loaded_message_preserving_draft(self):
        self.press()
        self.assertIs(self.view._reply_target, self.messages[-1])
        self.assertTrue(self.view.reply_bar.isVisible())
        self.assertTrue(self.view.input.hasFocus())
        self.assertEqual(self.view.input.toPlainText(), 'Draft stays intact')
        self.assertFalse(self.view._reply_last_shortcut.autoRepeat())
        self.runner.assert_not_called()

    def test_pending_removed_invalid_and_other_chat_rows_are_not_reply_targets(self):
        invalid = [Message(1, id=103, text='Pending', status='pending'),
                   Message(1, id=104, text='Removed', status='removed'),
                   Message(1, id=True, text='Invalid id'), Message(2, id=999, text='Different chat')]
        self.view._rows.extend({'msg': message} for message in invalid)
        self.press()
        self.assertIs(self.view._reply_target, self.messages[-1])
        self.runner.assert_not_called()

    def test_empty_history_and_closed_chat_do_not_set_reply_or_send(self):
        self.view._full_render([])
        self.press()
        self.assertIsNone(self.view._reply_target)
        self.view.show_placeholder()
        self.view.reply_to_last_loaded()
        self.assertIsNone(self.view._reply_target)
        self.runner.assert_not_called()

    def test_account_change_disconnect_and_disabled_input_block_shortcut(self):
        self.service.my_id = 999
        self.press()
        self.assertIsNone(self.view._reply_target)
        self.service.my_id = 123
        self.service.client.state = ConnectionState.DISCONNECTED
        self.press()
        self.assertIsNone(self.view._reply_target)
        self.service.client.state = ConnectionState.CONNECTED
        self.view.input.setReadOnly(True)
        self.press()
        self.assertIsNone(self.view._reply_target)
        self.runner.assert_not_called()

    def test_modal_dialog_cannot_reply_to_underlying_chat(self):
        dialog = QDialog(self.view)
        dialog.setWindowModality(Qt.WindowModality.WindowModal)
        entry = QLineEdit(dialog)
        QVBoxLayout(dialog).addWidget(entry)
        dialog.show()
        dialog.activateWindow()
        entry.setFocus()
        self.app.processEvents()
        try:
            self.press(entry)
            self.view.reply_to_last_loaded()
            self.assertIsNone(self.view._reply_target)
            self.runner.assert_not_called()
        finally:
            dialog.close()
            dialog.deleteLater()

    def test_open_popup_or_hidden_chat_cannot_reply(self):
        menu = QMenu(self.view)
        menu.addAction('Synthetic menu')
        menu.popup(self.view.mapToGlobal(self.view.rect().center()))
        self.app.processEvents()
        self.view.reply_to_last_loaded()
        self.assertIsNone(self.view._reply_target)
        menu.close()
        menu.deleteLater()
        self.view.hide()
        self.view.reply_to_last_loaded()
        self.assertIsNone(self.view._reply_target)
        self.runner.assert_not_called()

    def test_other_modifiers_do_not_prepare_reply(self):
        for modifiers in (Qt.KeyboardModifier.AltModifier, Qt.KeyboardModifier.MetaModifier,
                          Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier):
            self.press(modifiers=modifiers)
        self.assertIsNone(self.view._reply_target)
        self.runner.assert_not_called()


if __name__ == '__main__':
    unittest.main()
