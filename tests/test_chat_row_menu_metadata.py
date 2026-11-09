"""Sparse row metadata recovery: explicit reads, retry, diagnostics, no writes."""
import unittest
from unittest.mock import patch

from PySide6.QtCore import QPoint
from PySide6.QtWidgets import QApplication

import test_chat_row_menu_context as fixture


class MetadataService(fixture.RowService):
    def __init__(self):
        super().__init__()
        self.supported = False
        self.ready_after_read = True
        self.metadata_reads = []
        self.failure = None
        self.logs = []
        self.log = self.logs.append

    def conversation_snapshot(self, context):
        value = super().conversation_snapshot(context)
        value['action_capabilities']['supported'] = self.supported
        return value

    def refresh_conversation_menu_metadata(self, context):
        if not self.is_current_conversation(context):
            raise ValueError('Changed account')
        self.metadata_reads.append(context)
        if self.failure is not None:
            raise self.failure
        self.supported = self.ready_after_read


class ChatRowMenuMetadataTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.case = fixture.ChatRowMenuContextTests()
        self.case.app = self.app
        with patch('test_chat_row_menu_context.RowService', MetadataService):
            self.case.setUp()
        self.window = self.case.window
        self.service = self.case.service
        self.queue = self.case.queue

    def tearDown(self):
        self.assertEqual(self.service.writes, [])
        self.case.tearDown()

    def popup(self):
        menu = self.window._build_chat_row_menu(1)
        self.assertIsNotNone(menu)
        menu.popup(self.window.mapToGlobal(QPoint(100, 100)))
        self.app.processEvents()
        return menu

    def test_known_metadata_does_not_trigger_recovery_read(self):
        self.service.supported = True
        self.popup()
        self.assertIsNone(self.window._row_metadata_job)
        self.assertEqual(self.service.metadata_reads, [])
        self.assertFalse(any('metadata_loading' in line for line in self.service.logs))

    def test_still_unsupported_shows_explicit_retry_and_no_auto_loop(self):
        self.service.ready_after_read = False
        menu = self.popup()
        self.assertEqual(len(self.queue), 1)
        self.case._run(self.queue.pop())
        self.assertEqual(len(self.service.metadata_reads), 1)
        self.assertEqual(self.queue, [])
        actions = [a for a in menu.actions() if a.isEnabled()]
        self.assertEqual([a.text() for a in actions], ['Повторить'])
        self.assertTrue(self.window._row_metadata_failed)
        self.window._load_chat_row_metadata(self.window._row_menu_context, menu)
        self.assertEqual(self.queue, [])
        self.service.ready_after_read = True
        actions[0].trigger()
        self.app.processEvents()
        self.assertEqual(len(self.queue), 1)
        replacement = self.window._row_menu
        self.assertIsNot(replacement, menu)
        self.case._run(self.queue.pop())
        self.assertTrue(any('уведомления' in a.text() for a in replacement.actions()))
        self.assertFalse(self.window._row_metadata_failed)

    def test_exception_text_and_untrusted_class_name_never_reach_log_or_async_error(self):
        private_name = 'PrivateClass_payload_123456'
        private_text = 'private-token / private-user / server-message'
        self.service.failure = type(private_name, (ValueError,), {})(private_text)
        menu = self.popup()
        job = self.queue.pop()
        with patch('maxclient.ui.main_window.MainWindow._log_chat_row_menu',
                   wraps=self.window._log_chat_row_menu) as logged:
            result = self.case._run(job)
        self.assertIs(result, False)
        self.assertTrue(logged.called)
        output = '\n'.join(self.service.logs + [a.text() + a.toolTip() for a in menu.actions()])
        self.assertNotIn(private_name, output)
        self.assertNotIn(private_text, output)
        self.assertIn('category=metadata_failed; error=OtherError', output)
        self.assertEqual(self.queue, [])

    def test_absent_refresh_api_keeps_explanatory_disabled_menu(self):
        self.service.refresh_conversation_menu_metadata = None
        menu = self.popup()
        self.assertIn('Не удалось обновить меню', [a.text() for a in menu.actions()])
        self.assertFalse(any(a.isEnabled() for a in menu.actions()))
        self.assertEqual(self.queue, [])

    def test_missing_read_and_pin_captures_retried_only_after_metadata_arrives(self):
        self.window._folders = {'all': {'id': 'all.chat.folder'}}
        read_context, pin_context = object(), object()
        captured = []

        def read(context):
            captured.append(('read', self.service.supported))
            if not self.service.supported:
                raise ValueError('Missing metadata')
            return read_context

        def pin(context, folder_id):
            captured.append(('pin', self.service.supported))
            if not self.service.supported:
                raise ValueError('Missing metadata')
            return pin_context

        self.service.capture_conversation_read = read
        self.service.capture_conversation_pin = pin
        self.service.get_conversation_read_state = lambda ctx: {'context': ctx}
        self.service.is_current_conversation_pin = lambda ctx: ctx is pin_context
        self.service.conversation_pin_status = lambda ctx: {'context': ctx, 'available': False}
        self.popup()
        self.assertIsNone(self.window._row_read_context)
        self.assertIsNone(self.window._row_pin_context)
        self.case._run(self.queue.pop())
        self.assertIs(self.window._row_read_context, read_context)
        self.assertIs(self.window._row_pin_context, pin_context)
        self.assertEqual(captured, [('read', False), ('pin', False), ('read', True), ('pin', True)])

    def test_early_pin_completion_cannot_erase_loading_placeholder(self):
        pin_context = object()
        self.window._folders = {'all': {'id': 'all.chat.folder'}}
        self.service.capture_conversation_pin = lambda *a: pin_context
        self.service.is_current_conversation_pin = lambda ctx: ctx is pin_context
        self.service.conversation_pin_status = lambda ctx: {'context': ctx, 'available': False}
        self.service.get_conversation_pin_state = lambda ctx: {'context': ctx, 'available': False}
        menu = self.popup()
        self.assertEqual(len(self.queue), 2)
        self.case._run(self.queue.pop())  # Pin returns before metadata.
        self.assertEqual([a.text() for a in menu.actions()], ['Обновление меню…'])
        self.assertFalse(any(a.isEnabled() for a in menu.actions()))
        self.case._run(self.queue.pop())
        self.assertTrue(any('уведомления' in a.text() for a in menu.actions()))

    def test_pending_mutation_is_not_cleared_or_retried_by_metadata_refresh(self):
        self.service.pending = True
        menu = self.popup()
        self.case._run(self.queue.pop())
        self.assertTrue(self.service.pending)
        enabled = [row['action'] for action, row, child in menu._rows if action.isEnabled()]
        self.assertFalse(any(action in enabled for action in ('block', 'unblock', 'mute', 'clear', 'delete')))

    def test_capture_failure_logs_only_fixed_category_and_class(self):
        def fail(*args, **kwargs):
            raise ValueError('private identifier token payload')
        self.service.conversation_context = fail
        self.assertIsNone(self.window._build_chat_row_menu(1))
        self.assertIn('chat-row-menu: category=capture_failed; error=ValueError', self.service.logs)
        self.assertFalse(any('private' in line for line in self.service.logs))


if __name__ == '__main__':
    unittest.main()
