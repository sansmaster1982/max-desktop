"""Message-menu writes retain the session from popup creation and modal entry."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import unittest
from unittest.mock import Mock, patch

from PySide6.QtCore import QCoreApplication, QEvent, QTimer, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox

from maxclient.core.config import Settings
from maxclient.core.models import Chat, Message
from maxclient.protocol.client import AuthenticatedSession
from maxclient.ui.chat_view import ChatView, _ForwardChatDialog
from test_desktop_shell import _Service


class MessageMenuContextTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.service = _Service()
        self.service.my_id = 123
        self.service.client.authenticated_session = self.session()
        self.context = self.service.client.authenticated_session
        self.service.search_context = lambda: self.service.client.authenticated_session
        self.methods = ('edit_message', 'delete_messages', 'forward_messages',
                        'set_message_reaction', 'remove_message_reaction')
        for method in self.methods:
            setattr(self.service, method, Mock(return_value={}))
        self.runner_patch = patch('maxclient.ui.chat_view.run_async')
        self.runner = self.runner_patch.start()
        self.addCleanup(self.runner_patch.stop)
        self.view = ChatView(self.service, Settings())
        self.view.resize(700, 500)
        self.view.show()
        self.view.activateWindow()
        self.view.open_chat(Chat(1, title='Synthetic source'))
        self.message = Message(1, id=1234567890123, text='Synthetic original', outgoing=True)
        self.app.processEvents()
        self.jobs = []
        self.runner.reset_mock()
        self.runner.side_effect = self.queue
        self.menus = []
        self.actions = []

    def tearDown(self):
        self.view.close()
        self.view.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()

    def session(self):
        return AuthenticatedSession(123, 'synthetic-device', 'WEB', object())

    def switch(self):
        self.service.client.authenticated_session = self.session()

    def queue(self, method, *args, on_done, on_error=None, **kwargs):
        self.jobs.append((method, args, kwargs, on_done, on_error))

    def finish(self, index=0):
        method, args, kwargs, done, failed = self.jobs[index]
        try:
            result = method(*args, **kwargs)
        except Exception as exc:
            if failed:
                failed(str(exc))
        else:
            done(result)

    def menu_action(self, label):
        menu = self.view._message_menu(self.message)
        self.menus.append(menu)  # Keep the native submenu alive for delayed activation.
        action = next(action for action in menu.actions() if action.text() == label)
        self.actions.append(action)
        return action

    def assert_no_service_writes(self):
        for name in self.methods:
            getattr(self.service, name).assert_not_called()

    def test_retained_reaction_action_cannot_capture_a_replacement_session(self):
        action = self.menu_action('Реакция').menu().actions()[0]
        self.switch()
        action.trigger()
        self.assertEqual(self.jobs, [])
        self.assert_no_service_writes()

    def test_edit_modal_accepted_after_session_change_does_not_queue(self):
        def modal(*_args, **_kwargs):
            self.switch()
            return 'Synthetic edited', True
        with patch.object(self.view, '_edit_message_text', side_effect=modal):
            self.menu_action('Изменить…').trigger()
        self.assertEqual(self.jobs, [])
        self.assert_no_service_writes()

    def test_delete_actual_confirmation_click_after_session_change_is_ignored(self):
        def confirm():
            dialog = QApplication.activeModalWidget()
            self.assertIsInstance(dialog, QMessageBox)
            self.switch()
            button = next(b for b in dialog.buttons() if b.text() == 'Удалить у меня')
            QTest.mouseClick(button, Qt.MouseButton.LeftButton)
        QTimer.singleShot(0, confirm)
        self.menu_action('Удалить…').trigger()
        self.assertEqual(self.jobs, [])
        self.assert_no_service_writes()

    def test_forward_actual_destination_click_after_session_change_is_ignored(self):
        def confirm():
            dialog = QApplication.activeModalWidget()
            self.assertIsInstance(dialog, _ForwardChatDialog)
            dialog.chats.setCurrentRow(1)
            self.switch()
            QTest.mouseClick(dialog.send_button, Qt.MouseButton.LeftButton)
        QTimer.singleShot(0, confirm)
        self.menu_action('Переслать…').trigger()
        self.assertEqual(self.jobs, [])
        self.assert_no_service_writes()

    def test_context_is_passed_to_the_worker_and_duplicate_click_is_single_flight(self):
        menu = self.menu_action('Реакция').menu()
        menu.actions()[0].trigger()
        menu.actions()[0].trigger()
        self.assertEqual(len(self.jobs), 1)
        self.assert_no_service_writes()
        with patch.object(self.view, '_after_send') as refresh:
            self.finish()
            self.service.set_message_reaction.assert_called_once_with(
                1, self.message.id, '👍', context=self.context)
            refresh.assert_called_once_with(1)

    def test_session_change_before_worker_starts_never_calls_write(self):
        self.menu_action('Реакция').menu().actions()[0].trigger()
        self.switch()
        with patch('maxclient.ui.chat_view.QMessageBox.warning') as warning:
            self.finish()
            warning.assert_not_called()
        self.assert_no_service_writes()

    def test_edit_success_uses_original_session(self):
        with patch.object(self.view, '_edit_message_text', return_value=('Synthetic edited', True)):
            self.menu_action('Изменить…').trigger()
        with patch.object(self.view, '_after_send'):
            self.finish()
        self.service.edit_message.assert_called_once_with(1, self.message.id, 'Synthetic edited', context=self.context)

    def test_delete_success_uses_original_session_and_explicit_scope(self):
        def confirm():
            dialog = QApplication.activeModalWidget()
            button = next(b for b in dialog.buttons() if b.text() == 'Удалить у всех')
            QTest.mouseClick(button, Qt.MouseButton.LeftButton)
        QTimer.singleShot(0, confirm)
        self.menu_action('Удалить…').trigger()
        with patch.object(self.view, '_after_send'):
            self.finish()
        self.service.delete_messages.assert_called_once_with(1, [self.message.id], for_all=True, context=self.context)

    def test_forward_success_uses_selected_target_and_original_session(self):
        def confirm():
            dialog = QApplication.activeModalWidget()
            dialog.chats.setCurrentRow(1)
            QTest.mouseClick(dialog.send_button, Qt.MouseButton.LeftButton)
        QTimer.singleShot(0, confirm)
        self.menu_action('Переслать…').trigger()
        with patch.object(self.view, '_after_send') as refresh:
            self.finish()
            refresh.assert_called_once_with(2)
        self.service.forward_messages.assert_called_once_with(1, [self.message.id], 2, context=self.context)

    def test_reaction_removal_uses_original_session(self):
        self.menu_action('Реакция').menu().actions()[-1].trigger()
        with patch.object(self.view, '_after_send'):
            self.finish()
        self.service.remove_message_reaction.assert_called_once_with(1, self.message.id, context=self.context)

    def test_late_done_and_failure_do_not_refresh_or_warn_in_another_chat(self):
        self.menu_action('Реакция').menu().actions()[0].trigger()
        _method, _args, _kwargs, done, failed = self.jobs[0]
        self.view.open_chat(Chat(2, title='Synthetic destination'))
        with patch.object(self.view, '_after_send') as refresh, patch('maxclient.ui.chat_view.QMessageBox.warning') as warning:
            done({})
            failed('Synthetic late failure')
            refresh.assert_not_called()
            warning.assert_not_called()

    def test_late_callbacks_after_qt_destroy_are_ignored(self):
        self.menu_action('Реакция').menu().actions()[0].trigger()
        _method, _args, _kwargs, done, failed = self.jobs[0]
        self.view.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        with patch('maxclient.ui.chat_view.QMessageBox.warning') as warning:
            done({})
            failed('Synthetic late failure')
            warning.assert_not_called()
        # tearDown owns a different empty view after proving old callbacks inert.
        self.view = ChatView(self.service, Settings())

    def test_cancel_edit_and_delete_schedule_nothing(self):
        with patch.object(self.view, '_edit_message_text', return_value=('Synthetic edited', False)):
            self.menu_action('Изменить…').trigger()
        QTimer.singleShot(0, lambda: QApplication.activeModalWidget().reject())
        self.menu_action('Удалить…').trigger()
        self.assertEqual(self.jobs, [])
        self.assert_no_service_writes()


if __name__ == '__main__':
    unittest.main()
