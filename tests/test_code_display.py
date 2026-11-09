"""Synthetic, offline GUI regressions for message display and history callbacks."""
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QLabel

from maxclient.core.config import Settings
from maxclient.core.models import Chat, Message
from maxclient.ui.chat_view import ChatView
from maxclient.ui.theme import palette_for
from maxclient.ui.widgets.message_bubble import MessageBubble


class CodeDisplayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.service = Mock()
        self.service.local_history.return_value = []
        self.view = ChatView(self.service, Settings())
        self.callbacks = []
        self.worker_patch = patch('maxclient.ui.chat_view.run_async', side_effect=self.capture_worker)
        self.worker_patch.start()

    def tearDown(self):
        self.worker_patch.stop()
        self.view.close()
        self.view.deleteLater()
        self.app.processEvents()

    def capture_worker(self, method, chat_id, *, on_done, on_error):
        self.callbacks.append((chat_id, on_done, on_error))

    def test_history_from_previous_chat_cannot_replace_current_chat(self):
        self.view.open_chat(Chat(id=1, title='First synthetic chat'))
        self.view.open_chat(Chat(id=2, title='Second synthetic chat'))
        render = Mock()
        self.view._set_messages = render
        stale = [Message(chat_id=1, id=10, text='old synthetic message')]
        self.callbacks[0][1](stale)
        render.assert_not_called()
        current = [Message(chat_id=2, id=20, text='current synthetic message')]
        self.callbacks[1][1](current)
        render.assert_called_once_with(current)

    def test_older_request_cannot_replace_newer_request_for_same_chat(self):
        self.view.open_chat(Chat(id=1))
        self.view.open_chat(Chat(id=2))
        self.view.open_chat(Chat(id=1))
        render = Mock()
        self.view._set_messages = render
        self.callbacks[0][1]([Message(chat_id=1, id=10, text='stale')])
        render.assert_not_called()
        self.callbacks[2][1]([Message(chat_id=1, id=11, text='new')])
        self.assertEqual(render.call_count, 1)

    def test_refresh_error_preserves_cache_and_reports_error_only_for_current_chat(self):
        self.view.open_chat(Chat(id=1))
        self.view.open_chat(Chat(id=2))
        render = Mock()
        self.view._set_messages = render
        self.callbacks[0][2]('old error containing no real data')
        self.assertEqual(self.view.h_sub.text(), 'Обновление сообщений…')
        self.callbacks[1][2]('network failure')
        self.assertEqual(self.view.h_sub.text(), 'Не удалось обновить сообщения')
        render.assert_not_called()

    def test_placeholder_invalidates_inflight_history(self):
        self.view.open_chat(Chat(id=1))
        self.view.show_placeholder()
        render = Mock()
        self.view._set_messages = render
        self.callbacks[0][1]([Message(chat_id=1, text='stale')])
        render.assert_not_called()

    def test_message_text_is_plain_and_literal(self):
        literal = '<b>123456</b> & 000007\n••••••'
        bubble = MessageBubble(Message(chat_id=1, text=literal), '#2B7FFF', palette_for('dark'))
        labels = [label for label in bubble.findChildren(QLabel) if label.text() == literal]
        self.assertEqual(len(labels), 1)
        self.assertEqual(labels[0].textFormat(), Qt.TextFormat.PlainText)
        self.assertEqual(labels[0].text(), literal)
        self.assertTrue(labels[0].textInteractionFlags() & Qt.TextInteractionFlag.TextSelectableByMouse)
        bubble.deleteLater()

    def test_history_search_uses_visible_loaded_text_and_resets_for_next_chat(self):
        self.view.open_chat(Chat(id=1))
        self.callbacks[0][1]([
            Message(chat_id=1, id=1, text='Привет, тест'),
            Message(chat_id=1, id=2, text='Другой текст'),
            Message(chat_id=1, id=3, text='ТЕСТ снова'),
        ])
        self.view.history_search.setText('тест')
        self.assertEqual(self.view._search_matches, [0, 2])
        self.assertEqual(self.view.search_result.text(), '1 / 2')
        self.view._next_match()
        self.assertEqual(self.view.search_result.text(), '2 / 2')
        self.view.open_chat(Chat(id=2))
        self.assertEqual(self.view.history_search.text(), '')
        self.assertEqual(self.view._search_matches, [])

    def test_opening_chat_hides_placeholder_before_deferred_widget_deletion(self):
        placeholders = self.view.findChildren(QLabel, 'EmptyHint')
        self.assertEqual(len(placeholders), 1)
        self.view.open_chat(Chat(id=1))
        self.assertTrue(placeholders[0].isHidden())


if __name__ == '__main__':
    unittest.main()
