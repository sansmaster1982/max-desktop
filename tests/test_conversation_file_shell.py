"""File menu/save-dialog wiring, fake descriptor and no file/network effects."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from PySide6.QtWidgets import QApplication
from shiboken6 import isValid

from maxclient.core.config import Settings
from maxclient.ui.main_window import MainWindow
from test_conversation_info_shell import FakeService


class FileShellTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.service = FakeService()
        self.service.clear_conversation_files = Mock()
        self.service.get_conversation_file_descriptor = Mock()
        self.service.is_current_conversation_file = Mock(return_value=True)
        self.patches = [patch('maxclient.ui.main_window.run_async'),
                        patch('maxclient.ui.main_window.DesktopNotifications'),
                        patch('maxclient.ui.main_window.QFileDialog.getSaveFileName', return_value=('synthetic.txt', '')),
                        patch('maxclient.core.file_download_transport.capture_target', return_value=object()),
                        patch('maxclient.ui.conversation_file_controller.ConversationFileController')]
        self.jobs, _, self.choose, self.capture, self.create = [item.start() for item in self.patches]
        settings = Settings(theme='light')
        settings.save = Mock()
        self.window = MainWindow(self.service, settings)
        self.window.chat_view.chat = self.service.chats[303]
        self.window.chat_view.open_chat = Mock()
        self.window._open_conversation_info(self.service.chats[303])
        self.context = self.window._info_context
        self.item = {'key': 'attachment:303:7:0', 'kind': 'FILE', 'chat_id': 303,
                     'message_id': 7, 'time_ms': 123, 'file_id': 22, 'file_name': '../CON.txt',
                     'download_available': True, 'available': True, 'action': 'message'}
        self.window._info_pages['files'] = {'items': [self.item]}
        self.descriptor = SimpleNamespace(context=self.context, key=self.item['key'],
                                        file_name=self.item['file_name'], size=500)
        self.service.get_conversation_file_descriptor.return_value = self.descriptor

    def tearDown(self):
        if isValid(self.window):
            self.window.close()
            self.window.deleteLater()
        self.app.processEvents()
        for item in reversed(self.patches): item.stop()

    def test_save_uses_actual_loaded_key_and_selected_destination(self):
        self.window._info_file_action('save', {**self.item, 'file_id': 999}, self.context)
        self.service.get_conversation_file_descriptor.assert_called_once_with(self.context, self.item['key'])
        self.assertIs(self.create.call_args.args[1], self.descriptor)
        self.assertEqual(self.create.call_args.args[2], 'synthetic.txt')
        self.create.return_value.start.assert_called_once()
        suggested = self.choose.call_args.args[2]
        self.assertNotIn('/', suggested)
        self.assertNotEqual(suggested.upper(), 'CON.TXT')

    def test_unknown_or_deleted_row_does_not_open_save_flow(self):
        self.window._info_file_action('save', {**self.item, 'key': 'unknown'}, self.context)
        self.item['deleted'] = True
        self.window._info_file_action('save', self.item, self.context)
        self.choose.assert_not_called()
        self.create.assert_not_called()

    def test_cancelled_destination_does_not_resolve_or_download(self):
        self.choose.return_value = ('', '')
        self.window._info_file_action('save', self.item, self.context)
        self.capture.assert_not_called()
        self.create.assert_not_called()

    def test_session_changed_inside_save_dialog_does_not_start_download(self):
        def changed(*_, **__):
            self.service.context = object()
            return ('synthetic.txt', '')
        self.choose.side_effect = changed
        self.window._info_file_action('save', self.item, self.context)
        self.create.assert_not_called()

    def test_stale_descriptor_after_save_dialog_does_not_start_download(self):
        self.service.is_current_conversation_file.return_value = False
        self.window._info_file_action('save', self.item, self.context)
        self.create.assert_not_called()

    def test_active_download_is_shown_without_duplicate_request(self):
        active = Mock(active=True, descriptor=self.descriptor)
        self.window._info_file_controller = active
        self.window._info_file_action('save', self.item, self.context)
        active.show.assert_called_once()
        self.choose.assert_not_called()

    def test_old_context_pending_cancel_blocks_save_without_reopening_old_dialog(self):
        active = Mock(active=True, descriptor=SimpleNamespace(context=object()))
        self.window._info_file_controller = active
        self.window._info_file_action('save', self.item, self.context)
        active.show.assert_not_called()
        self.choose.assert_not_called()
        self.assertEqual(self.window.statusBar().currentMessage(), 'Дождитесь завершения предыдущей загрузки.')

    def test_close_card_cancels_download_and_revokes_file_descriptors(self):
        active = Mock(active=True, descriptor=self.descriptor)
        self.window._info_file_controller = active
        self.window._close_conversation_info()
        active.cancel.assert_called_once()
        self.service.clear_conversation_files.assert_called_once_with(self.context)

    def test_file_message_action_uses_outer_message_without_save(self):
        self.window._info_file_action('message', self.item, self.context)
        self.window.chat_view.open_chat.assert_called_once_with(self.service.chats[303], message_id=7, time_ms=123)
        self.choose.assert_not_called()


if __name__ == '__main__': unittest.main()
