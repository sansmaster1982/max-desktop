"""Only local navigation/focus; no service, account, clipboard or network."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import unittest
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QFontDatabase
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QLineEdit, QVBoxLayout, QWidget

from maxclient.ui.keyboard_shortcuts import (
    KeyboardReference, NAVIGATION_SHORTCUTS, SHORTCUT_SECTIONS, UNAVAILABLE_NOTE,
    install_navigation_shortcuts,
)
from maxclient.ui.theme import build_stylesheet
from test_desktop_shell import _load_offscreen_font


class KeyboardShortcutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        _load_offscreen_font(cls.app)
        font = Path('C:/Windows/Fonts/seguisb.ttf')
        if font.is_file():
            QFontDatabase.addApplicationFont(str(font))

    def setUp(self):
        self.window = QWidget()
        layout = QVBoxLayout(self.window)
        self.chats = QLineEdit('Локальный поиск')
        self.messages = QLineEdit('Черновик поиска')
        layout.addWidget(self.chats)
        layout.addWidget(self.messages)
        self.calls = []

        def focus_chats():
            self.calls.append('search_chats')
            self.chats.setFocus()

        def focus_messages():
            self.calls.append('search_in_chat')
            self.messages.setFocus()

        self.shortcuts = install_navigation_shortcuts(
            self.window, search_chats=focus_chats, search_in_chat=focus_messages,
            close_chat=lambda: self.calls.append('close_chat'))
        self.window.show()
        self.window.activateWindow()
        self.app.processEvents()

    def tearDown(self):
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()

    def test_native_search_keys_focus_existing_fields_and_preserve_text(self):
        self.messages.setFocus()
        QTest.keyClick(self.messages, Qt.Key.Key_K, Qt.KeyboardModifier.ControlModifier)
        self.assertEqual(self.calls, ['search_chats'])
        self.assertTrue(self.chats.hasFocus())
        QTest.keyClick(self.chats, Qt.Key.Key_F, Qt.KeyboardModifier.ControlModifier)
        self.assertEqual(self.calls, ['search_chats', 'search_in_chat'])
        self.assertTrue(self.messages.hasFocus())
        self.assertEqual(self.chats.text(), 'Локальный поиск')
        self.assertEqual(self.messages.text(), 'Черновик поиска')

    def test_different_modifier_combinations_do_not_invoke_navigation(self):
        for mods in (Qt.KeyboardModifier.AltModifier, Qt.KeyboardModifier.MetaModifier,
                     Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier):
            QTest.keyClick(self.messages, Qt.Key.Key_K, mods)
            QTest.keyClick(self.messages, Qt.Key.Key_F, mods)
        self.assertEqual(self.calls, [])

    def test_modal_dialog_does_not_trigger_underlying_window_navigation(self):
        dialog = QDialog(self.window)
        dialog.setWindowModality(Qt.WindowModality.WindowModal)
        entry = QLineEdit(dialog)
        QVBoxLayout(dialog).addWidget(entry)
        dialog.show()
        dialog.activateWindow()
        entry.setFocus()
        self.app.processEvents()
        try:
            QTest.keyClick(entry, Qt.Key.Key_K, Qt.KeyboardModifier.ControlModifier)
            QTest.keyClick(entry, Qt.Key.Key_F, Qt.KeyboardModifier.ControlModifier)
            self.assertEqual(self.calls, [])
            self.assertTrue(entry.hasFocus())
        finally:
            dialog.close()
            dialog.deleteLater()
            self.app.processEvents()

    def test_only_supported_catalogue_rows_are_installed(self):
        reference = KeyboardReference()
        try:
            supported = {key for key, row in reference.row_widgets.items() if row.property('available')}
            self.assertEqual(supported, {spec.identifier for spec in NAVIGATION_SHORTCUTS} | {'reply_last'})
            self.assertEqual(len(self.shortcuts), len(NAVIGATION_SHORTCUTS))
            for spec, shortcut in zip(NAVIGATION_SHORTCUTS, self.shortcuts):
                label, keys = reference.shortcut_rows[spec.identifier]
                self.assertEqual(label.text(), spec.label)
                self.assertEqual(keys.text().replace(' ', ''), shortcut.key().toString())
                self.assertEqual(label.textFormat(), Qt.TextFormat.PlainText)
                self.assertFalse(shortcut.autoRepeat())
            unsupported = {key for key in reference.shortcut_rows if key not in supported}
            self.assertEqual(len(unsupported), 11)
            for key in unsupported:
                self.assertEqual(reference.row_widgets[key].toolTip(), UNAVAILABLE_NOTE)
                self.assertEqual(reference.shortcut_rows[key][0].accessibleDescription(), UNAVAILABLE_NOTE)
            self.assertIn('загруженных', reference.row_widgets['search_in_chat'].toolTip())
            self.assertIn('Сообщение не отправляется', reference.row_widgets['reply_last'].toolTip())
        finally:
            reference.deleteLater()

    def test_escape_has_one_plain_key_binding(self):
        self.messages.setFocus()
        QTest.keyClick(self.messages, Qt.Key.Key_Escape, Qt.KeyboardModifier.ControlModifier)
        self.assertEqual(self.calls, [])
        QTest.keyClick(self.messages, Qt.Key.Key_Escape)
        self.assertEqual(self.calls, ['close_chat'])

    def test_catalogue_observed_order_and_unavailable_actions_are_inert(self):
        reference = KeyboardReference()
        try:
            self.assertEqual(list(reference.shortcut_rows), [
                'search_in_chat', 'search_chats', 'close_chat', 'delete_selected',
                'reply_last', 'record_voice', 'format_heading', 'format_bold',
                'format_italic', 'format_underline', 'format_strike', 'format_mono',
                'format_quote', 'format_link', 'format_plain'])
            for _title, specs in SHORTCUT_SECTIONS[1:]:
                for spec in specs:
                    QTest.keySequence(self.messages, spec.sequence)
            self.assertEqual(self.calls, [])
        finally:
            reference.deleteLater()

    def test_retranslate_retains_sequences_and_availability(self):
        reference = KeyboardReference()
        try:
            for language in ('en', 'ru'):
                reference.retranslate(language)
                self.assertEqual(reference.shortcut_rows['close_chat'][1].text(), 'Esc')
                self.assertTrue(reference.row_widgets['close_chat'].property('available'))
                self.assertFalse(reference.row_widgets['delete_selected'].property('available'))
            self.assertEqual(reference.shortcut_rows['close_chat'][0].text(), 'Закрыть текущий чат')
        finally:
            reference.deleteLater()

    def test_reference_fits_minimum_width_and_keeps_theme_roles(self):
        reference = KeyboardReference()
        reference.resize(260, 300)
        old_style = self.app.styleSheet()
        try:
            for theme in ('light', 'dark'):
                self.app.setStyleSheet(build_stylesheet(theme, '#0085FF'))
                reference.apply_theme(theme)
                reference.show()
                self.app.processEvents()
                self.assertLessEqual(reference.minimumSizeHint().width(), 260)
                for label, keys in reference.shortcut_rows.values():
                    self.assertGreater(label.width(), 60)
                    self.assertLess(label.geometry().right(), keys.geometry().left())
                    self.assertGreaterEqual(label.height(), label.heightForWidth(label.width()))
                    self.assertTrue(keys.isVisibleTo(reference))
        finally:
            self.app.setStyleSheet(old_style)
            reference.close()
            reference.deleteLater()

    def test_native_card_width_and_row_geometry_at_640(self):
        reference = KeyboardReference()
        reference.resize(1000, 1100)
        reference.apply_theme('light')
        reference.show()
        self.app.processEvents()
        try:
            self.assertEqual(reference.width(), 640)
            self.assertEqual([card.width() for card in reference.cards], [640] * 3)
            for row in reference.row_widgets.values():
                self.assertEqual(row.height(), 56)
            for label, keys in reference.shortcut_rows.values():
                self.assertEqual(label.geometry().left(), 12)
                self.assertEqual(keys.parentWidget().width() - keys.geometry().right() - 1, 12)
        finally:
            reference.close()
            reference.deleteLater()


if __name__ == '__main__':
    unittest.main()
