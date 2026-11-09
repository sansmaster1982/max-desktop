"""The real MainWindow shortcut callbacks run against synthetic local data."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import unittest
from pathlib import Path
from unittest.mock import patch

from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QLineEdit, QMenu, QVBoxLayout

from maxclient.core.config import Settings
from maxclient.core.models import Message
from maxclient.ui.main_window import MainWindow
from maxclient.ui.theme import build_stylesheet
from test_desktop_shell import _load_offscreen_font
from test_settings_workspace import SyntheticService


class KeyboardShellTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        _load_offscreen_font(cls.app)

    def setUp(self):
        self.service = SyntheticService()
        self.settings = Settings(theme='light', accent='#0085FF')
        self.main_patcher = patch('maxclient.ui.main_window.run_async')
        self.history_patcher = patch('maxclient.ui.chat_view.run_async')
        self.main_async = self.main_patcher.start()
        self.history_async = self.history_patcher.start()
        self.window = MainWindow(self.service, self.settings)
        self.window.resize(1240, 760)
        self.window.show()
        self.window.activateWindow()
        self.app.processEvents()
        self.main_async.reset_mock()
        self.history_async.reset_mock()

    def tearDown(self):
        with patch.object(self.settings, 'save'):
            self.window.close()
        self.window.deleteLater()
        self.app.processEvents()
        self.history_patcher.stop()
        self.main_patcher.stop()

    def test_global_search_from_settings_keeps_current_query_and_draft(self):
        self.window._on_chat_clicked(self.window.chat_list.item(0))
        self.window.chat_view.input.setPlainText('Тестовый черновик')
        self.window.search.setText('локальный запрос')
        self.window._open_settings()
        self.main_async.reset_mock()
        self.history_async.reset_mock()
        field = self.window._settings_workspace.first_name
        field.setFocus()
        QTest.keyClick(field, Qt.Key.Key_K, Qt.KeyboardModifier.ControlModifier)
        self.assertIs(self.window._workspace_pages.currentWidget(), self.window.splitter)
        self.assertTrue(self.window.search.hasFocus())
        self.assertEqual(self.window.search.selectedText(), 'локальный запрос')
        self.assertEqual(self.window.chat_view.input.toPlainText(), 'Тестовый черновик')
        self.assertEqual(self.service.profile_writes, [])
        self.main_async.assert_not_called()
        self.history_async.assert_not_called()

    def test_chat_search_opens_existing_loaded_history_search_without_rpc(self):
        self.window._on_chat_clicked(self.window.chat_list.item(0))
        view = self.window.chat_view
        view.input.setPlainText('Не отправлять')
        view.history_search.setText('сохранённый поиск')
        self.main_async.reset_mock()
        self.history_async.reset_mock()
        QTest.keyClick(view.input, Qt.Key.Key_F, Qt.KeyboardModifier.ControlModifier)
        self.assertTrue(view.search_bar.isVisible())
        self.assertTrue(view.history_search.hasFocus())
        self.assertEqual(view.history_search.selectedText(), 'сохранённый поиск')
        self.assertEqual(view.input.toPlainText(), 'Не отправлять')
        self.main_async.assert_not_called()
        self.history_async.assert_not_called()

    def test_chat_search_without_open_chat_is_inert(self):
        self.window.search.setFocus()
        QTest.keyClick(self.window.search, Qt.Key.Key_F, Qt.KeyboardModifier.ControlModifier)
        self.assertIsNone(self.window.chat_view.chat)
        self.assertTrue(self.window.chat_view.search_bar.isHidden())
        self.main_async.assert_not_called()
        self.history_async.assert_not_called()

    def test_chat_search_does_not_leave_settings_to_an_old_chat(self):
        self.window._on_chat_clicked(self.window.chat_list.item(0))
        self.window._open_settings()
        workspace = self.window._settings_workspace
        workspace.first_name.setFocus()
        self.main_async.reset_mock()
        self.history_async.reset_mock()
        QTest.keyClick(workspace.first_name, Qt.Key.Key_F, Qt.KeyboardModifier.ControlModifier)
        self.assertIs(self.window._workspace_pages.currentWidget(), workspace)
        self.assertTrue(self.window.chat_view.search_bar.isHidden())
        self.main_async.assert_not_called()
        self.history_async.assert_not_called()

    def test_disabled_search_context_blocks_both_callbacks(self):
        self.window._open_settings()
        workspace = self.window._settings_workspace
        self.window._search_enabled = False
        self.main_async.reset_mock()
        self.history_async.reset_mock()
        workspace.first_name.setFocus()
        for key in (Qt.Key.Key_K, Qt.Key.Key_F):
            QTest.keyClick(workspace.first_name, key, Qt.KeyboardModifier.ControlModifier)
        self.assertIs(self.window._workspace_pages.currentWidget(), workspace)
        self.main_async.assert_not_called()
        self.history_async.assert_not_called()

    def test_escape_closes_and_reopen_restores_multiline_draft_and_reply(self):
        item = self.window.chat_list.item(0)
        self.window._on_chat_clicked(item)
        view = self.window.chat_view
        chat = view.chat
        draft = 'Не отправлять\n  вторая строка  '
        reply = Message(chat_id=chat.id, id=500, sender=456, text='Локальная цитата')
        view.input.setPlainText(draft)
        view._reply_to(reply)
        late_history = self.history_async.call_args.kwargs['on_done']
        generation = view._history_generation
        self.main_async.reset_mock()
        self.history_async.reset_mock()
        QTest.keyClick(view.input, Qt.Key.Key_Escape)
        self.assertIsNone(view.chat)
        self.assertIsNone(self.window._current_chat_id)
        self.assertEqual(self.window.chat_list.selectedItems(), [])
        self.assertEqual(view._drafts[chat.id], draft)
        self.assertIs(view._reply_targets[chat.id], reply)
        self.assertGreater(view._history_generation, generation)
        self.assertTrue(view.input_bar.isHidden())
        self.assertTrue(self.window.search.hasFocus())
        self.main_async.assert_not_called()
        self.history_async.assert_not_called()
        late_history([Message(chat_id=chat.id, id=501, text='Запоздалый синтетический ответ')])
        self.assertIsNone(view.chat)
        self.assertEqual(view._rows, [])
        self.window._on_chat_clicked(item)
        self.assertEqual(view.input.toPlainText(), draft)
        self.assertIs(view._reply_target, reply)
        self.assertTrue(view.reply_bar.isVisible())
        self.main_async.assert_not_called()

    def test_escape_does_not_close_an_old_chat_from_settings(self):
        self.window._on_chat_clicked(self.window.chat_list.item(0))
        chat = self.window.chat_view.chat
        self.window.chat_view.input.setPlainText('Сохраняемый черновик')
        self.window._open_settings()
        workspace = self.window._settings_workspace
        workspace.first_name.setFocus()
        self.main_async.reset_mock()
        self.history_async.reset_mock()
        QTest.keyClick(workspace.first_name, Qt.Key.Key_Escape)
        self.assertIs(self.window._workspace_pages.currentWidget(), workspace)
        self.assertIs(self.window.chat_view.chat, chat)
        self.assertEqual(self.window.chat_view.input.toPlainText(), 'Сохраняемый черновик')
        self.main_async.assert_not_called()
        self.history_async.assert_not_called()

    def test_escape_draft_and_reply_do_not_leak_to_another_chat(self):
        first, second = self.window.chat_list.item(0), self.window.chat_list.item(1)
        self.window._on_chat_clicked(first)
        view = self.window.chat_view
        reply = Message(chat_id=view.chat.id, id=500, text='Цитата первого чата')
        view.input.setPlainText('Черновик первого чата')
        view._reply_to(reply)
        QTest.keyClick(view.input, Qt.Key.Key_Escape)
        self.window._on_chat_clicked(second)
        self.assertEqual(view.input.toPlainText(), '')
        self.assertIsNone(view._reply_target)
        self.assertFalse(view.reply_bar.isVisible())
        view.input.setPlainText('Второй черновик')
        QTest.keyClick(view.input, Qt.Key.Key_Escape)
        self.window._on_chat_clicked(first)
        self.assertEqual(view.input.toPlainText(), 'Черновик первого чата')
        self.assertIs(view._reply_target, reply)

    def test_escape_dismisses_popup_without_closing_conversation(self):
        self.window._on_chat_clicked(self.window.chat_list.item(0))
        view = self.window.chat_view
        chat = view.chat
        view.input.setPlainText('Черновик под меню')
        menu = QMenu(self.window)
        menu.addAction('Локальная строка')
        menu.popup(self.window.mapToGlobal(QPoint(200, 200)))
        self.app.processEvents()
        self.main_async.reset_mock()
        self.history_async.reset_mock()
        try:
            self.assertIs(QApplication.activePopupWidget(), menu)
            self.window._close_current_chat()  # Also gate queued/manual activation.
            self.assertIs(view.chat, chat)
            QTest.keyClick(menu, Qt.Key.Key_Escape)
            self.assertFalse(menu.isVisible())
            self.assertIs(view.chat, chat)
            self.assertEqual(view.input.toPlainText(), 'Черновик под меню')
            self.main_async.assert_not_called()
            self.history_async.assert_not_called()
        finally:
            menu.close()
            menu.deleteLater()

    def test_escape_dismisses_modal_without_closing_conversation(self):
        self.window._on_chat_clicked(self.window.chat_list.item(0))
        view = self.window.chat_view
        chat = view.chat
        view.input.setPlainText('Черновик под диалогом')
        dialog = QDialog(self.window)
        dialog.setWindowModality(Qt.WindowModality.WindowModal)
        entry = QLineEdit(dialog)
        QVBoxLayout(dialog).addWidget(entry)
        dialog.show()
        dialog.activateWindow()
        entry.setFocus()
        self.app.processEvents()
        self.main_async.reset_mock()
        self.history_async.reset_mock()
        try:
            self.window._close_current_chat()
            QTest.keyClick(entry, Qt.Key.Key_Escape)
            self.assertFalse(dialog.isVisible())
            self.assertIs(view.chat, chat)
            self.assertEqual(view.input.toPlainText(), 'Черновик под диалогом')
            self.main_async.assert_not_called()
            self.history_async.assert_not_called()
        finally:
            dialog.close()
            dialog.deleteLater()

    def test_escape_invalidates_file_scope_and_closes_visible_history_search(self):
        self.window._on_chat_clicked(self.window.chat_list.item(0))
        view = self.window.chat_view
        context = object()
        view._file_context = context
        cleared = []
        self.service.clear_timeline_files = cleared.append
        self.window._focus_history_search()
        self.main_async.reset_mock()
        self.history_async.reset_mock()
        QTest.keyClick(view.history_search, Qt.Key.Key_Escape)
        self.assertIsNone(view.chat)
        self.assertIsNone(view._file_context)
        self.assertEqual(cleared, [context])
        self.assertTrue(view.search_bar.isHidden())
        self.main_async.assert_not_called()
        self.history_async.assert_not_called()

    def test_keyboard_reference_render_uses_only_synthetic_profile(self):
        self.window._open_settings()
        workspace = self.window._settings_workspace
        workspace.select_page('keyboard')
        original_style = self.app.styleSheet()
        output = Path('.test-tmp/keyboard_reference_light.png')
        output.parent.mkdir(parents=True, exist_ok=True)
        try:
            for theme in ('light', 'dark'):
                self.settings.theme = theme
                self.app.setStyleSheet(build_stylesheet(theme, '#0085FF'))
                workspace.apply_theme()
                self.app.processEvents()
                self.assertTrue(self.window.grab().save(str(output.with_name(f'keyboard_reference_{theme}.png'))))
        finally:
            self.app.setStyleSheet(original_style)


if __name__ == '__main__':
    unittest.main()
