"""Real queued Qt callbacks after deferred destruction; synthetic messages only."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import sys
import unittest
from unittest.mock import Mock, patch

from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication
from shiboken6 import isValid

from maxclient.core.config import Settings
from maxclient.core.models import Chat, Message
from maxclient.ui.chat_view import ChatView


class ChatViewLifetimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.service = Mock()
        self.service.local_history.return_value = []
        self.service.calls_available.return_value = False
        self.callbacks = []

        def capture(method, chat_id, *, on_done, on_error, **params):
            self.callbacks.append((on_done, on_error))

        self.worker = patch('maxclient.ui.chat_view.run_async', side_effect=capture)
        self.worker.start()
        self.view = ChatView(self.service, Settings())

    def tearDown(self):
        if isValid(self.view):
            self.view.close()
            self.view.deleteLater()
            QCoreApplication.sendPostedEvents(self.view, QEvent.Type.DeferredDelete)
        self.app.processEvents()
        self.worker.stop()

    @staticmethod
    def destroy(widget):
        widget.deleteLater()
        QCoreApplication.sendPostedEvents(widget, QEvent.Type.DeferredDelete)

    def test_queued_bottom_scroll_after_scroll_area_destruction_does_not_touch_deleted_qt(self):
        original = self.view._scroll_to_bottom
        with patch.object(self.view, '_scroll_to_bottom', side_effect=original) as queued:
            self.view._full_render([Message(chat_id=42, id=7, text='Synthetic message')])
            self.destroy(self.view.scroll)
            self.assertTrue(isValid(self.view))
            self.assertFalse(isValid(self.view.scroll))
            with patch.object(sys, 'excepthook') as errors:
                self.app.processEvents()
                queued.assert_called_once()
                errors.assert_not_called()

    def test_queued_target_and_bottom_callbacks_after_view_destruction_are_rejected(self):
        original = self.view._scroll_to_search_target
        with patch.object(self.view, '_scroll_to_search_target', side_effect=original) as target:
            self.view.open_chat(Chat(id=42), message_id=7, time_ms=100)
            self.callbacks[0][0]([Message(chat_id=42, id=7, time_ms=100, text='Synthetic target')])
            self.destroy(self.view)
            self.assertFalse(isValid(self.view))
            self.assertFalse(isValid(self.view.scroll))
            with patch.object(sys, 'excepthook') as errors:
                self.app.processEvents()
                target.assert_called_once()
                errors.assert_not_called()

    def test_history_success_and_error_arriving_after_destruction_do_not_render(self):
        self.view.open_chat(Chat(id=42))
        success, error = self.callbacks[0]
        self.destroy(self.view)
        with patch.object(self.view, '_set_messages') as render:
            success([Message(chat_id=42, id=7, text='Synthetic late history')])
            error('synthetic late failure')
            render.assert_not_called()

    def test_live_scroll_unrelated_error_is_not_swallowed_by_lifetime_check(self):
        with patch.object(self.view.scroll, 'verticalScrollBar', side_effect=RuntimeError('synthetic unrelated error')):
            with self.assertRaisesRegex(RuntimeError, 'synthetic unrelated error'):
                self.view._scroll_to_bottom()


if __name__ == '__main__':
    unittest.main()
