"""Self-contact navigation retains the proven Saved ID zero and display model."""
import unittest
from unittest.mock import Mock

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QListWidgetItem
from maxclient.core.models import Chat
import test_search_shell as fixture


class SavedChatNavigationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        fixture.SearchShellTests.setUp(self)
        self.saved = Chat(0, title='Избранное', chat_type='DIALOG', peer_user_id=101)
        self.service.chats[101] = Chat(101, title='Legacy duplicate', peer_user_id=101)
        self.service.chats[0] = self.saved

    def tearDown(self):
        fixture.SearchShellTests.tearDown(self)

    def test_list_click_opens_display_projection_not_old_sql_title(self):
        self.service.store.get_chat = Mock(return_value=Chat(0, title='Broken name'))
        item = QListWidgetItem(self.window.chat_list)
        item.setData(Qt.ItemDataRole.UserRole, 0)
        self.window._on_chat_clicked(item)
        self.assertEqual(self.window._current_chat_id, 0)
        self.window.chat_view.open_chat.assert_called_once_with(self.saved)
        self.service.store.get_chat.assert_called_once_with(0)

    def test_self_lookup_opens_saved_not_placeholder_and_clears_phone_search(self):
        self.window.search.setText('+70000000000')
        self.window._open_contact_by_id(101, 'Own profile')
        self.window.chat_view.open_chat.assert_called_once_with(self.saved)
        self.assertEqual(self.window.search.text(), '')
        self.assertEqual(self.window._current_chat_id, 0)

    def test_missing_saved_dialog_never_opens_legacy_equal_contact_id(self):
        del self.service.chats[0]
        self.window._open_contact_by_id(101, 'Own profile')
        self.window.chat_view.open_chat.assert_not_called()
        self.service.store.ensure_chat.assert_not_called()

    def test_service_resolver_is_authoritative_over_old_list_candidates(self):
        self.service.dialog_for_contact = Mock(return_value=None)
        self.window._open_contact_by_id(101, 'Own profile')
        self.service.dialog_for_contact.assert_called_once_with(101)
        self.window.chat_view.open_chat.assert_not_called()


if __name__ == '__main__':
    unittest.main()
