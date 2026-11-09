"""Independent preview lifecycle/interaction review; synthetic offscreen data only."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from dataclasses import replace
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLabel, QAbstractButton, QTextEdit
from shiboken6 import isValid

from maxclient.core.config import Settings
from maxclient.core.conversation_info_service import ConversationContext
from maxclient.core.conversation_preview_service import ConversationPreviewResult
from maxclient.core.models import Message
from maxclient.protocol.client import AuthenticatedSession
from maxclient.ui.chat_preview import ChatPreviewDialog
from maxclient.ui.timeline_photos import PhotoPreview
from maxclient.ui.widgets.message_bubble import MessageBubble
from test_desktop_shell import _load_offscreen_font
import test_read_receipts_ui as receipt_fixture


class ChatPreviewIndependentReviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        _load_offscreen_font(cls.app)

    def setUp(self):
        session = AuthenticatedSession(101, 'synthetic-device', 'WEB', object())
        self.context = ConversationContext(101, object(), session, 303, 202)
        self.service = SimpleNamespace(
            is_current_conversation=Mock(return_value=True),
            load_conversation_preview=Mock(side_effect=AssertionError('No real history requests')),
        )
        self.tasks = []
        self.work = patch('maxclient.ui.chat_preview.run_async',
                          side_effect=lambda method, *args, **kw: self.tasks.append((method, args, kw)))
        self.work.start()
        self.addCleanup(self.work.stop)
        self.dialogs = []

    def tearDown(self):
        for dialog in self.dialogs:
            if isValid(dialog):
                dialog.close()
                dialog.deleteLater()
                QCoreApplication.sendPostedEvents(dialog, QEvent.Type.DeferredDelete)
        self.app.processEvents()

    def dialog(self, *, parent=None, current=None):
        dialog = ChatPreviewDialog(self.service, Settings(theme='light'), self.context,
                                   title='Synthetic preview', parent=parent, context_is_current=current)
        self.dialogs.append(dialog)
        dialog.open()
        self.app.processEvents()
        dialog.start_loading()
        return dialog

    def result(self, *, context=None, messages=None):
        return ConversationPreviewResult(
            self.context if context is None else context, '<b>Literal title</b>',
            (Message(chat_id=303, id=7, time_ms=1000, text='Synthetic private message'),)
            if messages is None else messages, 1000,
        )

    def finish(self, result=None, index=-1):
        self.tasks[index][2]['on_done'](self.result() if result is None else result)
        self.app.processEvents()

    def test_single_pending_load_and_no_composer_or_normal_timeline(self):
        dialog = self.dialog()
        dialog.start_loading()
        self.assertEqual(len(self.tasks), 1)
        self.finish()
        self.assertEqual(dialog.title.text(), '<b>Literal title</b>')
        self.assertEqual(dialog.title.textFormat(), Qt.TextFormat.PlainText)
        self.assertEqual(len(dialog.findChildren(MessageBubble)), 1)
        self.assertEqual(dialog.findChildren(QTextEdit), [])
        self.assertFalse(hasattr(dialog, 'read_receipts'))
        self.service.load_conversation_preview.assert_not_called()

    def test_closed_dialog_discards_late_success_and_error_without_reopening(self):
        dialog = self.dialog()
        queued = self.tasks[0]
        dialog.reject()
        self.finish()
        queued[2]['on_error']('SYNTHETIC_SECRET_RESPONSE')
        self.app.processEvents()
        self.assertFalse(dialog.isVisible())
        self.assertEqual(dialog.findChildren(MessageBubble), [])
        self.assertIsNone(queued[0]())
        self.service.load_conversation_preview.assert_not_called()

    def test_result_with_equal_valued_but_replaced_context_is_rejected(self):
        dialog = self.dialog()
        self.finish(self.result(context=replace(self.context)))
        self.assertEqual(dialog.findChildren(MessageBubble), [])
        self.assertTrue(dialog.retry.isVisible())

    def test_wrong_chat_message_is_rejected_before_rendering(self):
        dialog = self.dialog()
        other_chat = Message(chat_id=404, id=7, time_ms=1000, text='Other-chat content')
        self.finish(self.result(messages=(other_chat,)))
        self.assertEqual(dialog.findChildren(MessageBubble), [])
        self.assertNotIn('Other-chat content', dialog.notice.text())

    def test_session_replacement_discards_queued_result_and_closes_rendered_content(self):
        dialog = self.dialog()
        self.service.is_current_conversation.return_value = False
        self.finish()
        self.assertEqual(dialog.findChildren(MessageBubble), [])
        dialog._check_scope()
        self.assertFalse(dialog.isVisible())

        self.service.is_current_conversation.return_value = True
        rendered = self.dialog()
        self.finish()
        self.assertEqual(len(rendered.findChildren(MessageBubble)), 1)
        self.service.is_current_conversation.return_value = False
        # Exercise the actual periodic invalidation, not a manually fired callback.
        QTest.qWait(260)
        self.assertFalse(rendered.isVisible())
        self.assertEqual(rendered._bubbles, [])

    def test_replaced_menu_scope_cannot_accept_an_old_preview_even_if_session_stays_valid(self):
        current = Mock(return_value=True)
        dialog = self.dialog(current=current)
        current.return_value = False
        self.finish()
        dialog._check_scope()
        self.assertFalse(dialog.isVisible())
        self.assertEqual(dialog.findChildren(MessageBubble), [])

    def test_transport_error_is_generic_and_retry_is_explicit(self):
        dialog = self.dialog()
        self.tasks[0][2]['on_error']('SYNTHETIC_PRIVATE_TOKEN')
        self.app.processEvents()
        self.assertNotIn('SYNTHETIC_PRIVATE_TOKEN', dialog.notice.text())
        self.assertTrue(dialog.retry.isVisible())
        QTest.qWait(230)
        self.assertEqual(len(self.tasks), 1)
        dialog.retry.click()
        self.assertEqual(len(self.tasks), 2)

    def test_queued_worker_sanitizes_exception_and_does_not_query_qwidgets(self):
        dialog = self.dialog()
        self.service.load_conversation_preview.side_effect = ValueError('SYNTHETIC_PRIVATE_TOKEN')
        with patch.object(dialog, 'isVisible', side_effect=AssertionError('QWidget read on worker')):
            with self.assertRaisesRegex(RuntimeError, '^Conversation preview read failed$'):
                self.tasks[0][0]()
        self.service.load_conversation_preview.assert_called_once_with(self.context)

    def test_links_buttons_reactions_and_remote_photo_are_passive(self):
        raw = {
            'id': 8, 'time': 1000, 'type': 'USER', 'sender': 202,
            'text': 'https://example.test/message',
            'attaches': [
                {'_type': 'PHOTO', 'baseUrl': 'https://example.test/image', 'width': 300, 'height': 200},
                {'_type': 'SHARE', 'shareId': 3, 'url': 'https://example.test/share'},
                {'_type': 'INLINE_KEYBOARD', 'callbackId': 'synthetic', 'keyboard': {'buttons': [[
                    {'type': 'LINK', 'text': 'Open', 'url': 'https://example.test/button'},
                    {'type': 'CLIPBOARD', 'text': 'Copy', 'payload': 'SYNTHETIC_CLIPBOARD'},
                ]]}}
            ],
        }
        message = Message.from_server(303, raw, 101)
        message.reaction_info = {'counters': [{'reaction': '👍', 'count': 1}]}
        dialog = self.dialog()
        with patch('maxclient.ui.message_links.QDesktopServices.openUrl') as external:
            with patch('maxclient.ui.widgets.message_bubble.QApplication.clipboard') as clipboard:
                self.finish(self.result(messages=(message,)))
                bubble = dialog.findChild(MessageBubble)
                buttons = bubble.findChildren(QAbstractButton)
                self.assertGreaterEqual(len(buttons), 3)
                for button in buttons:
                    self.assertFalse(button.isEnabled())
                    button.click()
                for label in bubble.findChildren(QLabel):
                    self.assertFalse(label.openExternalLinks())
                    self.assertFalse(label.textInteractionFlags() & Qt.TextInteractionFlag.LinksAccessibleByMouse)
                    self.assertFalse(label.textInteractionFlags() & Qt.TextInteractionFlag.LinksAccessibleByKeyboard)
                    label.linkActivated.emit('https://example.test/forced-signal')
                photo = bubble.findChild(PhotoPreview)
                self.assertIsNotNone(photo)
                self.assertNotIn('Загрузка', photo._text)
                photo.clicked.emit()
                external.assert_not_called()
                clipboard.assert_not_called()

    def test_preview_modal_blocks_existing_timeline_and_cancels_already_queued_receipt(self):
        # This fixture owns only in-memory SQLite and synthetic authenticated transport.
        case = receipt_fixture.VisibleReadReceiptTests()
        case.app = self.app
        case.setUp()
        self.addCleanup(case.tearDown)
        case.receipts.flush()
        self.assertEqual(len(case.calls), 1)
        case.receipts.suspend_for_overlay()
        self.assertTrue(case._run_queued()['cancelled'])
        case.foreground.stop()
        case.view.isActiveWindow = Mock(return_value=True)
        dialog = self.dialog(parent=case.view)
        self.assertIs(QApplication.activeModalWidget(), dialog)
        self.assertFalse(case.receipts._foreground())
        case.receipts.flush()
        self.assertEqual(len(case.calls), 1)
        case.service.mark_chat_read.assert_not_called()

