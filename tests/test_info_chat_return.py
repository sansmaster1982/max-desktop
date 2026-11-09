"""Info navigation back to an already open, sidebar-hidden empty chat."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import unittest
from unittest.mock import Mock, patch

from PySide6.QtWidgets import QApplication
from shiboken6 import isValid

from maxclient.core.config import Settings
from maxclient.core.models import Chat
from maxclient.ui.main_window import MainWindow
from test_conversation_info_shell import FakeService


class InfoChatReturnTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.service = FakeService()
        self.patches = [patch('maxclient.ui.main_window.run_async'),
                        patch('maxclient.ui.main_window.DesktopNotifications')]
        self.jobs, self.notifications = [item.start() for item in self.patches]
        settings = Settings(theme='light')
        settings.save = Mock()
        self.window = MainWindow(self.service, settings)
        self.chat = self.service.chats[303]
        self.window.chat_view.chat = self.chat
        self.window.chat_view.open_chat = Mock()

    def tearDown(self):
        if isValid(self.window):
            self.window.close()
            self.window.deleteLater()
        self.app.processEvents()
        for item in reversed(self.patches):
            item.stop()

    def open_info(self, *, hidden=True):
        if hidden:
            # list_chats is a sidebar projection; the exact cached chat remains.
            self.service.list_chats = Mock(return_value=[])
        self.window._open_conversation_info(self.chat)
        self.assertIs(self.window._conversation_pages.currentWidget(), self.window._info_panel)
        context = self.window._info_context
        self.jobs.reset_mock()
        return context

    def assert_returned_without_worker(self):
        self.assertIs(self.window._conversation_pages.currentWidget(), self.window.chat_view)
        self.assertIsNone(self.window._info_context)
        self.assertIs(self.window.chat_view.chat, self.chat)
        self.window.chat_view.open_chat.assert_not_called()
        self.jobs.assert_not_called()
        self.service.store.ensure_chat.assert_not_called()

    def test_chat_tile_returns_to_hidden_empty_current_chat(self):
        self.open_info()
        self.window._info_panel.buttons['chat'].click()
        self.assert_returned_without_worker()

    def test_find_tile_returns_to_hidden_current_chat_and_focuses_search(self):
        self.open_info()
        self.window._info_panel.buttons['find'].click()
        self.assert_returned_without_worker()
        self.assertTrue(self.window.chat_view.search_button.isChecked())

    def test_saved_zero_id_can_return_without_a_sidebar_row(self):
        self.chat = Chat(0, title='Избранное', peer_user_id=101)
        self.service.chats[0] = self.chat
        self.window.chat_view.chat = self.chat
        self.open_info()
        self.window._info_panel.buttons['chat'].click()
        self.assert_returned_without_worker()

    def test_current_visible_chat_does_not_reload_on_return(self):
        self.open_info(hidden=False)
        self.window._info_panel.buttons['chat'].click()
        self.assert_returned_without_worker()

    def test_replaced_session_cannot_return_using_old_info_context(self):
        context = self.open_info()
        self.service.context = object()
        self.window._info_panel.buttons['chat'].click()
        self.assertIs(self.window._info_context, context)
        self.assertIs(self.window._conversation_pages.currentWidget(), self.window._info_panel)
        self.window.chat_view.open_chat.assert_not_called()
        self.jobs.assert_not_called()

    def test_different_active_chat_does_not_bypass_missing_sidebar_target(self):
        context = self.open_info()
        self.window.chat_view.chat = Chat(404, title='Другой чат', peer_user_id=505)
        self.window._info_panel.buttons['chat'].click()
        self.assertIs(self.window._info_context, context)
        self.assertIs(self.window._conversation_pages.currentWidget(), self.window._info_panel)
        self.window.chat_view.open_chat.assert_not_called()
        self.jobs.assert_not_called()

    def test_different_visible_target_preserves_existing_open_route(self):
        self.open_info(hidden=False)
        self.window.chat_view.chat = Chat(404, title='Другой чат', peer_user_id=505)
        self.window._info_panel.buttons['chat'].click()
        self.assertIsNone(self.window._info_context)
        self.assertIs(self.window._conversation_pages.currentWidget(), self.window.chat_view)
        self.window.chat_view.open_chat.assert_called_once_with(self.chat)


if __name__ == '__main__':
    unittest.main()
