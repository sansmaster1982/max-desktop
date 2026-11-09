"""Offscreen text-hit context menus; no real clipboard, URL or service writes."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import unittest
from unittest.mock import Mock, patch

from PySide6.QtCore import QCoreApplication, QEvent, QPoint, Qt
from PySide6.QtGui import QContextMenuEvent
from PySide6.QtWidgets import QApplication, QLabel

from maxclient.core.config import Settings
from maxclient.core.models import Chat, Message
from maxclient.protocol.client import AuthenticatedSession
from maxclient.ui.chat_view import ChatView
from maxclient.ui.widgets.message_bubble import MessageBubble
from test_desktop_shell import _Service


class MessageTextContextMenuTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.service = _Service()
        self.service.my_id = 123
        self.context = AuthenticatedSession(123, 'synthetic-device', 'WEB', object())
        self.service.client.authenticated_session = self.context
        self.service.search_context = lambda: self.service.client.authenticated_session
        self.service.delete_messages = Mock()
        self.service.edit_message = Mock()
        self.worker = patch('maxclient.ui.chat_view.run_async')
        self.runner = self.worker.start()
        self.addCleanup(self.worker.stop)
        self.view = ChatView(self.service, Settings())
        self.view.resize(700, 500)
        self.view.show()
        self.view.open_chat(Chat(1, title='Synthetic'))
        self.render()

    def tearDown(self):
        self.view.close()
        self.view.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()

    def render(self, *, text='Synthetic original 😀 tail', outgoing=True):
        self.message = Message(1, id=100, text=text, outgoing=outgoing)
        self.view._full_render([self.message])
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()
        self.bubble = self.view.findChildren(MessageBubble)[0]
        self.label = self.bubble.findChild(QLabel, 'MessageText')
        self.assertIsNotNone(self.label)
        self.runner.reset_mock()

    def context_menu(self, callback=None, *, target=None):
        widget = self.label if target is None else target
        point = QPoint(8, 8)
        captured = []
        def popup(menu, global_point):
            captured.append(([a.text() for a in menu.actions()], global_point))
            if callback:
                callback(menu)
        original = self.view._message_menu
        def menu_for(message):
            menu = original(message)
            menu.exec = lambda global_point: popup(menu, global_point)
            return menu
        with patch.object(self.view, '_message_menu', side_effect=menu_for):
            event = QContextMenuEvent(QContextMenuEvent.Reason.Mouse, point, widget.mapToGlobal(point))
            QApplication.sendEvent(widget, event)
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0][1], widget.mapToGlobal(point))
        return captured[0][0]

    @staticmethod
    def action(menu, text):
        return next(a for a in menu.actions() if a.text() == text)

    def test_right_click_text_exposes_existing_edit_delete_menu(self):
        labels = self.context_menu()
        self.assertIn('Изменить…', labels)
        self.assertIn('Удалить…', labels)
        self.assertIn('Копировать текст', labels)
        self.assertIn('Выделить всё', labels)
        self.runner.assert_not_called()
        self.service.delete_messages.assert_not_called()
        self.service.edit_message.assert_not_called()

    def test_incoming_text_keeps_existing_no_edit_rule(self):
        self.render(outgoing=False)
        labels = self.context_menu()
        self.assertNotIn('Изменить…', labels)
        self.assertIn('Удалить…', labels)

    def test_text_delete_uses_existing_message_action_callback(self):
        with patch.object(self.view, '_delete_message') as delete:
            self.context_menu(lambda menu: self.action(menu, 'Удалить…').trigger())
        delete.assert_called_once()
        self.assertIs(delete.call_args.args[0], self.message)
        self.assertEqual(delete.call_args.args[1][0], 1)
        self.runner.assert_not_called()

    def test_selection_copy_and_full_copy_remain_distinct(self):
        self.label.setSelection(0, 9)
        self.assertEqual(self.label.selectedText(), 'Synthetic')
        clipboard = Mock()
        with patch('maxclient.ui.chat_view.QApplication.clipboard', return_value=clipboard):
            labels = self.context_menu(lambda menu: self.action(menu, 'Копировать выделенное').trigger())
            clipboard.setText.assert_called_once_with('Synthetic')
            self.assertEqual(self.label.selectedText(), 'Synthetic')
            clipboard.reset_mock()
            self.context_menu(lambda menu: self.action(menu, 'Копировать текст').trigger())
            clipboard.setText.assert_called_once_with(self.message.display_text)
        self.assertIn('Копировать выделенное', labels)

    def test_select_all_handles_utf16_surrogate_pair_and_preserves_flags(self):
        flags = self.label.textInteractionFlags()
        self.context_menu(lambda menu: self.action(menu, 'Выделить всё').trigger())
        self.assertEqual(self.label.selectedText(), self.message.display_text)
        self.assertEqual(self.label.textInteractionFlags(), flags)
        self.assertTrue(flags & Qt.TextInteractionFlag.TextSelectableByMouse)

    def test_url_activation_remains_bound_to_safe_existing_handler(self):
        url = 'https://example.test/synthetic?q=1'
        self.render(text='Synthetic link ' + url)
        self.context_menu()
        self.assertTrue(self.label.textInteractionFlags() & Qt.TextInteractionFlag.LinksAccessibleByMouse)
        self.assertFalse(self.label.openExternalLinks())
        with patch('maxclient.ui.message_links.QDesktopServices.openUrl', return_value=True) as opened:
            self.label.linkActivated.emit(url)
            opened.assert_called_once()
            self.assertEqual(opened.call_args.args[0].toString(), url)
            self.label.linkActivated.emit('file:///synthetic/private')
            self.assertEqual(opened.call_count, 1)

    def test_stale_selection_action_cannot_copy_after_session_switch(self):
        self.label.setSelection(0, 9)
        clipboard = Mock()
        def switch_then_copy(menu):
            self.service.client.authenticated_session = AuthenticatedSession(
                123, 'synthetic-device', 'WEB', object())
            self.action(menu, 'Копировать выделенное').trigger()
        with patch('maxclient.ui.chat_view.QApplication.clipboard', return_value=clipboard):
            self.context_menu(switch_then_copy)
        clipboard.setText.assert_not_called()
        self.runner.assert_not_called()

    def test_padding_context_still_uses_the_same_message_actions(self):
        labels = self.context_menu(target=self.bubble)
        self.assertIn('Изменить…', labels)
        self.assertIn('Удалить…', labels)
        self.assertNotIn('Копировать выделенное', labels)


if __name__ == '__main__':
    unittest.main()
