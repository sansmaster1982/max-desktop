"""Timeline FILE gestures and shared save UI with no network, file or clipboard IO."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication
from shiboken6 import isValid

from maxclient.core.config import Settings
from maxclient.core.models import Attach, Chat, Message
from maxclient.ui.chat_view import ChatView
from maxclient.ui.main_window import MainWindow
from test_conversation_info_shell import FakeService


class TimelineGestureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.message = Message(chat_id=303, id=7, time_ms=150,
                               attaches=[Attach(type='FILE', file_name='display.txt')])
        self.service = Mock()
        self.service.local_history.return_value = [self.message]
        self.service.timeline_file_context.side_effect = self.new_context
        self.service.is_current_timeline_file_context.side_effect = lambda c: c is self.context
        self.service.timeline_file_actions.return_value = {
            0: {'download_available': True, 'file_name': '<b>literal.txt</b>', 'deleted': False}}
        self.service.is_current_conversation_file.return_value = True
        self.patch_worker = patch('maxclient.ui.chat_view.run_async')
        self.jobs = self.patch_worker.start()
        self.view = ChatView(self.service, Settings())
        self.view.open_chat(Chat(id=303))
        self.descriptor = SimpleNamespace(context=self.context, message_id=7, file_name='literal.txt')
        self.service.get_timeline_file_descriptor.return_value = self.descriptor
        self.saved = []
        self.view.file_save_requested.connect(self.saved.append)

    def new_context(self, chat_id):
        self.context = SimpleNamespace(chat_id=chat_id)
        return self.context

    def tearDown(self):
        if isValid(self.view):
            self.view.close()
            self.view.deleteLater()
            QCoreApplication.sendPostedEvents(self.view, QEvent.Type.DeferredDelete)
        self.app.processEvents()
        self.patch_worker.stop()

    def emit(self, action='save', message=None, index=0, bubble=None):
        (bubble or self.view._rows[0]['bubble']).file_action_requested.emit(
            action, message or self.message, index)

    def test_current_gesture_uses_exact_loaded_model_and_bound_descriptor(self):
        self.emit()
        self.service.get_timeline_file_descriptor.assert_called_once_with(self.context, self.message, 0)
        self.assertEqual(self.saved, [self.descriptor])
        self.service.resolve_attach_url.assert_not_called()

    def test_equivalent_foreign_message_object_cannot_authorize_save(self):
        clone = Message(chat_id=303, id=7, time_ms=150, attaches=self.message.attaches)
        self.emit(message=clone)
        self.assertEqual(self.saved, [])

    def test_replaced_row_cannot_receive_old_bubble_action(self):
        old = self.view._rows[0]['bubble']
        self.view._replace_row(0, self.message)
        self.emit(bubble=old)
        self.assertEqual(self.saved, [])

    def test_reopen_same_chat_revokes_old_generation_and_rebinds_rows(self):
        old, owner = self.view._rows[0]['bubble'], self.context
        closed = []
        self.view.file_scope_closed.connect(closed.append)
        self.view.open_chat(Chat(id=303))
        self.assertEqual(closed, [owner])
        self.assertIsNot(self.context, owner)
        self.emit(bubble=old)
        self.assertEqual(self.saved, [])
        self.service.clear_timeline_files.assert_any_call(owner)

    def test_changed_session_rejects_gesture(self):
        self.service.is_current_timeline_file_context.return_value = False
        self.service.is_current_timeline_file_context.side_effect = None
        self.emit()
        self.assertEqual(self.saved, [])

    def test_deleted_or_unconfirmed_capability_does_not_resolve_descriptor(self):
        self.service.timeline_file_actions.return_value = {0: {'download_available': False}}
        self.emit()
        self.service.get_timeline_file_descriptor.assert_not_called()

    def test_copy_name_uses_fresh_literal_backend_name_without_download(self):
        clipboard = Mock()
        with patch('PySide6.QtWidgets.QApplication.clipboard', return_value=clipboard):
            self.emit('copy_name')
        clipboard.setText.assert_called_once_with('<b>literal.txt</b>')
        self.service.get_timeline_file_descriptor.assert_not_called()
        self.assertEqual(self.saved, [])

    def test_old_generic_FILE_callback_also_uses_guarded_save(self):
        with patch.object(self.view, '_show_media') as media:
            self.view._open_media(self.message, self.message.attaches[0])
        self.assertEqual(self.saved, [self.descriptor])
        media.assert_not_called()
        self.service.resolve_attach_url.assert_not_called()

    def test_invalid_index_is_rejected(self):
        self.emit(index=-1)
        self.emit(index=1)
        self.service.get_timeline_file_descriptor.assert_not_called()

    def test_close_revokes_scope_once(self):
        owner = self.context
        closed = []
        self.view.file_scope_closed.connect(closed.append)
        self.view.close()
        self.view.close_file_scope()
        self.assertEqual(closed, [owner])
        self.assertIsNone(self.view._file_context)

    def test_delete_without_close_revokes_scope(self):
        owner = self.context
        self.view.deleteLater()
        QCoreApplication.sendPostedEvents(self.view, QEvent.Type.DeferredDelete)
        self.service.clear_timeline_files.assert_called_once_with(owner)

    def test_return_to_message_does_not_start_history_request(self):
        self.jobs.reset_mock()
        with patch.object(self.view.scroll, 'ensureWidgetVisible') as scroll:
            self.view.show_file_message(self.descriptor)
        self.assertEqual(self.view._spotlight_id, 7)
        scroll.assert_called_once()
        self.jobs.assert_not_called()


class TimelineSaveWindowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.service = FakeService()
        self.service.is_current_conversation_file = Mock(return_value=True)
        self.service.clear_timeline_files = Mock()
        self.service.is_current_timeline_file_context = Mock(return_value=True)
        self.patches = [patch('maxclient.ui.main_window.run_async'),
                        patch('maxclient.ui.main_window.DesktopNotifications'),
                        patch('maxclient.ui.main_window.QFileDialog.getSaveFileName', return_value=('synthetic.txt', '')),
                        patch('maxclient.core.file_download_transport.capture_target', return_value=object()),
                        patch('maxclient.ui.conversation_file_controller.ConversationFileController')]
        _, _, self.choose, self.capture, self.create = [p.start() for p in self.patches]
        settings = Settings(); settings.save = Mock()
        self.window = MainWindow(self.service, settings)
        self.context = SimpleNamespace(chat_id=303)
        self.window.chat_view.chat = self.service.chats[303]
        self.window.chat_view._file_context = self.context
        self.descriptor = SimpleNamespace(context=self.context, file_name='../CON.txt', message_id=7)

    def tearDown(self):
        if isValid(self.window):
            self.window.close(); self.window.deleteLater()
        self.app.processEvents()
        for p in reversed(self.patches): p.stop()

    def test_timeline_save_uses_shared_dialog_and_controller(self):
        self.window._timeline_file_save(self.descriptor)
        self.assertIs(self.create.call_args.args[1], self.descriptor)
        self.assertEqual(self.create.call_args.args[2], 'synthetic.txt')
        self.create.return_value.start.assert_called_once()
        self.assertNotIn('/', self.choose.call_args.args[2])

    def test_chat_change_during_path_dialog_prevents_save(self):
        def change(*_, **__):
            self.window.chat_view.close_file_scope()
            return ('synthetic.txt', '')
        self.choose.side_effect = change
        self.window._timeline_file_save(self.descriptor)
        self.capture.assert_not_called()
        self.create.assert_not_called()

    def test_reentrant_save_picker_keeps_single_controller(self):
        active = Mock(active=True, descriptor=self.descriptor)
        def changed(*_, **__):
            self.window._info_file_controller = active
            return ('synthetic.txt', '')
        self.choose.side_effect = changed
        self.window._timeline_file_save(self.descriptor)
        active.show.assert_called_once()
        self.create.assert_not_called()

    def test_active_info_download_blocks_timeline_save_without_showing_old_window(self):
        active = Mock(active=True, descriptor=SimpleNamespace(context=object()))
        self.window._info_file_controller = active
        self.window._timeline_file_save(self.descriptor)
        self.choose.assert_not_called()
        active.show.assert_not_called()

    def test_closing_info_does_not_cancel_independent_timeline_download(self):
        active = Mock(active=True, descriptor=self.descriptor)
        self.window._info_file_controller = active
        self.window._close_conversation_info()
        active.cancel.assert_not_called()
        self.assertIs(self.window.chat_view._file_context, self.context)

    def test_closing_timeline_scope_cancels_matching_download(self):
        active = Mock(active=True, descriptor=self.descriptor)
        self.window._info_file_controller = active
        self.window.chat_view.close_file_scope()
        active.cancel.assert_called_once()

    def test_stopping_window_revokes_timeline_scope(self):
        active = Mock(active=True, descriptor=self.descriptor)
        self.window._info_file_controller = active
        self.window._stop_search()
        self.service.clear_timeline_files.assert_called_once_with(self.context)
        active.cancel.assert_called_once()

    def test_revoked_descriptor_does_not_open_path_picker(self):
        self.service.is_current_conversation_file.return_value = False
        self.window._timeline_file_save(self.descriptor)
        self.choose.assert_not_called()


if __name__ == '__main__': unittest.main()
