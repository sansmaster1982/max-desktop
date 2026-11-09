"""Synthetic row-to-preview integration; no account files or server access."""
import unittest
from unittest.mock import patch

from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication
from shiboken6 import isValid

from maxclient.core.config import Settings
from maxclient.core.conversation_preview_service import ConversationPreviewResult
from maxclient.core.models import Message
from maxclient.ui.main_window import MainWindow
from maxclient.ui.chat_row_menu import chat_row_entries
from test_chat_row_menu_context import RowService
from test_chat_row_menu_layout import snapshot


class PreviewService(RowService):
    def __init__(self):
        super().__init__()
        self.preview_reads = []

    def load_conversation_preview(self, context, *, count=20):
        if not self.is_current_conversation(context):
            raise ValueError('Synthetic stale preview')
        self.preview_reads.append(context)
        message = Message.from_server(context.chat_id, {
            'id': 170, 'type': 'USER', 'sender': 7, 'time': 1770000000000,
            'text': 'Synthetic preview'}, self.profile.id)
        return ConversationPreviewResult(context, 'Synthetic title', (message,), message.time_ms)


class ChatPreviewMenuTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.service = PreviewService()
        self.queue = []
        self.patches = [patch('maxclient.ui.main_window.run_async'),
                        patch('maxclient.ui.chat_view.run_async'),
                        patch('maxclient.ui.settings_workspace.run_async'),
                        patch('maxclient.ui.chat_preview.run_async', side_effect=self._queue),
                        patch.object(Settings, 'save')]
        for item in self.patches:
            item.start()
        self.window = MainWindow(self.service, Settings())
        self.window.show()
        self.app.processEvents()

    def tearDown(self):
        self.window.close()
        self.window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()
        for item in reversed(self.patches):
            item.stop()

    def _queue(self, function, *args, on_done=None, on_error=None, **kwargs):
        self.queue.append((function, args, kwargs, on_done, on_error))

    def _open(self):
        self.window._build_chat_row_menu(self.service.chats[0].id)
        context = self.window._row_menu_context
        self.window._chat_row_menu_action('preview', None, context)
        return context, self.window._chat_preview_dialog

    def test_preview_descriptor_requires_local_handler_and_no_pending_operation(self):
        for enabled, pending, expected in ((False, False, False), (True, False, True),
                                           (True, True, False)):
            entries = chat_row_entries(snapshot(pending=pending), preview_available=enabled)
            row = next(row for row in entries if row['action'] == 'preview')
            self.assertEqual(row['available'], expected)
            self.assertIs(row['write'], False)

    def test_preview_keeps_active_chat_selection_drafts_and_unread_unchanged(self):
        view = self.window.chat_view
        view._drafts[1234] = 'Unsent synthetic draft'
        selected = self.window.chat_list.currentItem()
        chat_id = self.window._current_chat_id
        unread = [chat.unread for chat in self.service.chats]
        context, dialog = self._open()
        self.assertIs(QApplication.activeModalWidget(), dialog)
        self.assertFalse(self.service.file_activations[-1])
        work, args, kwargs, done, _ = self.queue.pop()
        done(work(*args, **kwargs))
        self.assertEqual(self.service.preview_reads, [context])
        self.assertEqual(len(dialog._bubbles), 1)
        self.assertEqual(self.service.writes, [])
        self.assertEqual([chat.unread for chat in self.service.chats], unread)
        self.assertEqual(self.window._current_chat_id, chat_id)
        self.assertIs(self.window.chat_list.currentItem(), selected)
        self.assertEqual(view._drafts[1234], 'Unsent synthetic draft')
        self.assertFalse(view.read_receipts._foreground_visible)

    def test_cancelled_menu_scope_rejects_preview_and_late_reply(self):
        _context, dialog = self._open()
        work, args, kwargs, done, _ = self.queue.pop()
        result = work(*args, **kwargs)
        self.window._cancel_chat_row_menu()
        done(result)
        self.assertTrue(dialog._revoked)
        self.assertEqual(dialog._bubbles, [])
        self.assertIsNone(self.window._chat_preview_dialog)

    def test_closed_before_worker_begins_does_not_issue_history_read(self):
        _context, dialog = self._open()
        work, args, kwargs, done, _ = self.queue.pop()
        dialog.reject()
        self.assertIsNone(work(*args, **kwargs))
        done(None)
        self.assertEqual(self.service.preview_reads, [])

    def test_pending_or_missing_handler_never_opens_preview(self):
        self.window._build_chat_row_menu(self.service.chats[0].id)
        context = self.window._row_menu_context
        self.service.pending = True
        self.window._chat_row_menu_action('preview', None, context)
        self.assertIsNone(self.window._chat_preview_dialog)
        self.service.pending = False
        self.service.load_conversation_preview = None
        self.window._chat_row_menu_action('preview', None, context)
        self.assertIsNone(self.window._chat_preview_dialog)

    def test_parent_close_revokes_and_clears_detached_preview(self):
        _context, dialog = self._open()
        self.window.close()
        self.assertTrue(dialog._revoked)
        self.assertFalse(dialog.isVisible())
        self.assertIsNone(self.window._chat_preview_dialog)

