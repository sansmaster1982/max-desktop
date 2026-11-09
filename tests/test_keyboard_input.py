"""Composer keyboard checks use an isolated editor, never a service or account."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import unittest
from types import SimpleNamespace

from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QKeyEvent
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import QApplication

from maxclient.ui.chat_view import _InputEdit


class ComposerKeyboardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.settings = SimpleNamespace(send_on_enter=True)
        self.editor = _InputEdit(self.settings)
        self.editor.show()
        self.sent = QSignalSpy(self.editor.send_requested)
        self.editor.setPlainText('Синтетический черновик')
        self.editor.moveCursor(self.editor.textCursor().MoveOperation.End)

    def tearDown(self):
        self.editor.close()
        self.editor.deleteLater()
        self.app.processEvents()

    def test_only_selected_exact_combination_emits_for_both_return_keys(self):
        modifiers = Qt.KeyboardModifier
        combinations = (
            modifiers.NoModifier, modifiers.ControlModifier, modifiers.ShiftModifier,
            modifiers.AltModifier, modifiers.MetaModifier,
            modifiers.ControlModifier | modifiers.ShiftModifier,
            modifiers.ControlModifier | modifiers.AltModifier,
            modifiers.ControlModifier | modifiers.MetaModifier,
            modifiers.AltModifier | modifiers.ShiftModifier,
        )
        for send_on_enter in (True, False):
            self.settings.send_on_enter = send_on_enter
            for key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                for keypad in (modifiers.NoModifier, modifiers.KeypadModifier):
                    for mods in combinations:
                        with self.subTest(send_on_enter=send_on_enter, key=key, mods=mods, keypad=keypad):
                            before = self.sent.count()
                            event = QKeyEvent(QEvent.Type.KeyPress, key, mods | keypad)
                            self.editor.keyPressEvent(event)
                            expected = mods == (modifiers.NoModifier if send_on_enter
                                                else modifiers.ControlModifier)
                            self.assertEqual(self.sent.count() - before, int(expected))

    def test_holding_send_key_cannot_repeat_send(self):
        for send_on_enter in (True, False):
            self.settings.send_on_enter = send_on_enter
            mods = Qt.KeyboardModifier.NoModifier if send_on_enter else Qt.KeyboardModifier.ControlModifier
            for key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                before = self.sent.count()
                event = QKeyEvent(QEvent.Type.KeyPress, key, mods, '\r', True, 4)
                self.editor.keyPressEvent(event)
                self.assertEqual(self.sent.count(), before)
                self.assertTrue(event.isAccepted())

    def test_shift_enter_is_a_newline_in_both_send_modes(self):
        for send_on_enter in (True, False):
            self.settings.send_on_enter = send_on_enter
            self.editor.setPlainText('Черновик')
            self.editor.moveCursor(self.editor.textCursor().MoveOperation.End)
            QTest.keyClick(self.editor, Qt.Key.Key_Return, Qt.KeyboardModifier.ShiftModifier)
            self.assertEqual(self.editor.toPlainText(), 'Черновик\n')
        self.assertEqual(self.sent.count(), 0)

    def test_plain_enter_is_a_newline_in_ctrl_send_mode(self):
        self.settings.send_on_enter = False
        QTest.keyClick(self.editor, Qt.Key.Key_Return)
        self.assertEqual(self.editor.toPlainText(), 'Синтетический черновик\n')
        self.assertEqual(self.sent.count(), 0)

    def test_send_signal_does_not_clear_or_modify_draft(self):
        QTest.keyClick(self.editor, Qt.Key.Key_Return)
        self.assertEqual(self.sent.count(), 1)
        self.assertEqual(self.editor.toPlainText(), 'Синтетический черновик')

    def test_readonly_or_disabled_editor_never_emits_send(self):
        self.editor.setReadOnly(True)
        self.editor.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Return, Qt.KeyboardModifier.NoModifier))
        self.editor.setReadOnly(False)
        self.editor.setEnabled(False)
        self.editor.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Return, Qt.KeyboardModifier.NoModifier))
        self.assertEqual(self.sent.count(), 0)


if __name__ == '__main__':
    unittest.main()
