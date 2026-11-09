"""Chat-row menu regressions with synthetic sessions and a deferred local queue.

No account files, network requests, messages, or real settings are used.
"""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import unittest
from unittest.mock import patch

from PySide6.QtCore import QCoreApplication, QEvent, QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from shiboken6 import isValid

from maxclient.core.config import Settings
from maxclient.core.conversation_info_service import ConversationContext
from maxclient.ui.main_window import MainWindow
from test_settings_workspace import SyntheticService


class RowService(SyntheticService):
    def __init__(self):
        super().__init__()
        self.chats[0].chat_type = 'DIALOG'
        self.chats[0].peer_user_id = 7
        self.muted = True
        self.blocked = False
        self.pending = False
        self.allow_mute = True
        self.allow_block = True
        self.writes = []
        self.attempts = []
        self.contexts = []
        self.file_activations = []
        self.allow_lifecycle = False

    def search_context(self):
        return self.security_session

    def conversation_context(self, chat_id, *, activate_files=True):
        if self.get_chat(chat_id) is None:
            raise ValueError('Synthetic missing chat')
        context = ConversationContext(self.profile.id, self.client,
                                      self.security_session, chat_id, 7)
        self.contexts.append(context)
        self.file_activations.append(activate_files)
        return context

    def is_current_conversation(self, context):
        return (isinstance(context, ConversationContext)
                and context.owner_id == self.profile.id
                and context.source_client is self.client
                and context.session_context is self.security_session
                and self.get_chat(context.chat_id) is not None)

    def conversation_snapshot(self, context):
        if not self.is_current_conversation(context):
            raise ValueError('Synthetic stale context')
        block_action = 'unblock' if self.blocked else 'block'
        more = [dict(action='folder', label='Добавить в папку', available=True,
                     write=False, value=None)]
        if self.allow_block:
            more.append(dict(action=block_action,
                             label='Разблокировать' if self.blocked else 'Заблокировать',
                             available=True, write=True, value=None))
        more.extend([dict(action='clear', label='Очистить историю', available=self.allow_lifecycle,
                          write=True, value=None),
                     dict(action='delete', label='Удалить чат', available=self.allow_lifecycle,
                          write=True, value=None)])
        return dict(context=context, chat_id=context.chat_id, peer_id=7,
                    title='Синтетический контакт', muted=self.muted,
                    availability={'mute': self.allow_mute, 'more': True},
                    action_pending=self.pending, action_status='',
                    row_menu={'chat_status': 'ACTIVE', 'unread': False},
                    action_capabilities={'supported': True, 'kind': 'dialog',
                        'participant': True, 'mute': self.allow_mute,
                        'last_message': True, 'recipient_blocked': self.blocked,
                        'more': ['folder', 'clear', block_action, 'delete']},
                    more_actions=more)

    def get_conversation_folders(self, context):
        if not self.is_current_conversation(context):
            raise ValueError('Synthetic stale folders')
        return {'context': context, 'folderSync': 1, 'writePending': False,
                'orderedFolders': [dict(id='test-folder', title='Тестовая папка',
                                        include=[], filters=[], favorites=[])],
                'missingFolderIds': []}

    def get_folder_view(self, folder, chats=None):
        return {'hidden': False}

    def set_conversation_mute(self, context, value):
        self.attempts.append(('mute', context, value))
        if not self.is_current_conversation(context):
            raise ValueError('Synthetic stale write')
        self.writes.append(('mute', context, value))
        self.muted = value != 0
        return {'context': context, 'pending': False, 'confirmed': True}

    def set_conversation_contact_blocked(self, context, value):
        self.attempts.append(('block', context, value))
        if not self.is_current_conversation(context):
            raise ValueError('Synthetic stale write')
        self.writes.append(('block', context, value))
        self.blocked = value
        return {'context': context, 'pending': False, 'accepted': True}

    def set_conversation_folder_included(self, context, folder_id, included):
        self.attempts.append(('folder_include', context, folder_id, included))
        if not self.is_current_conversation(context):
            raise ValueError('Synthetic stale write')
        self.writes.append(('folder_include', context, folder_id, included))
        return {'context': context, 'pending': False, 'accepted': True}

    def clear_conversation_history(self, context, *, for_all=False):
        self.writes.append(('clear', context, for_all))
        return {'context': context, 'action': 'clear', 'confirmed': True, 'pending': False}

    def delete_conversation(self, context, *, for_all=False):
        self.writes.append(('delete', context, for_all))
        return {'context': context, 'action': 'delete', 'confirmed': True, 'pending': False}


class ChatRowMenuContextTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.service = RowService()
        self.queue = []
        self.patches = [patch('maxclient.ui.main_window.run_async', side_effect=self._queue),
                        patch('maxclient.ui.chat_view.run_async'),
                        patch('maxclient.ui.settings_workspace.run_async'),
                        patch.object(Settings, 'save')]
        for item in self.patches:
            item.start()
        self.window = MainWindow(self.service, Settings())
        self.window.show()
        self.app.processEvents()
        self.queue.clear()  # Startup reads are intentionally not executed.

    def tearDown(self):
        if isValid(self.window):
            self.window.close()
            self.window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()
        for item in reversed(self.patches):
            item.stop()

    def _queue(self, function, *args, on_done=None, on_error=None, **kwargs):
        self.queue.append((function, args, kwargs, on_done, on_error))

    def _menu(self):
        menu = self.window._build_chat_row_menu(1)
        self.assertIsNotNone(menu)
        menu.popup(self.window.mapToGlobal(QPoint(100, 100)))
        self.app.processEvents()
        # Permit only read-only folder jobs, preserving all mutation jobs for
        # explicit worker tests below.
        while self.queue and self.queue[0][0] == self.service.get_conversation_folders:
            self._run(self.queue.pop(0))
        return menu

    def _action(self, menu, word):
        return next(action for action in menu.actions()
                    if not action.isSeparator() and word in action.text())

    def _run(self, job, *, callbacks=True):
        function, args, kwargs, done, error = job
        try:
            result = function(*args, **kwargs)
        except Exception as exc:
            if callbacks and error:
                error(exc)
            elif not callbacks:
                return exc
            else:
                raise
        else:
            if callbacks and done:
                done(result)
            return result

    def _mute_job(self):
        menu = self._menu()
        self._action(menu, 'уведомления').trigger()
        self.assertEqual(len(self.queue), 1)
        return self.queue.pop()

    def test_menu_is_bound_at_creation_and_has_no_local_rename_or_automatic_writes(self):
        menu = self._menu()
        self.assertIs(self.window._row_menu_context, self.service.contexts[-1])
        self.assertEqual(self.service.file_activations, [False])
        self.assertEqual(self.window._current_chat_id, None)
        self.assertFalse(any('Переименовать' in action.text() for action in menu.actions()))
        self.assertEqual(self.service.writes, [])

    def test_native_mouse_dispatch_executes_one_mute_for_original_context(self):
        menu = self._menu()
        context = self.window._row_menu_context
        action = self._action(menu, 'уведомления')
        QTest.mouseClick(menu, Qt.MouseButton.LeftButton,
                         pos=menu.actionGeometry(action).center())
        self.assertEqual(len(self.queue), 1)
        self._run(self.queue.pop())
        self.assertEqual(self.service.writes, [('mute', context, 0)])

    def test_saved_action_cannot_bind_to_new_session(self):
        menu = self._menu()
        action = self._action(menu, 'уведомления')
        self.service.security_session = object()
        action.trigger()
        self.assertEqual(self.queue, [])
        self.assertEqual(self.service.writes, [])

    def test_hidden_menu_action_cannot_run(self):
        menu = self._menu()
        action = self._action(menu, 'уведомления')
        menu.close()
        action.trigger()
        self.assertEqual(self.queue, [])

    def test_replaced_menu_revokes_its_old_actions(self):
        old_menu = self._menu()
        action = self._action(old_menu, 'уведомления')
        self._menu()
        if isValid(action):
            action.trigger()
        self.assertEqual(self.queue, [])

    def test_replaced_menu_does_not_accumulate_parent_owned_actions(self):
        old_menu = self._menu()
        self._menu()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.assertFalse(isValid(old_menu))

    def test_changed_capability_prevents_saved_action_dispatch(self):
        menu = self._menu()
        action = self._action(menu, 'уведомления')
        self.service.allow_mute = False
        action.trigger()
        self.assertEqual(self.queue, [])

    def test_cancelled_block_confirmation_never_enqueues_write(self):
        menu = self._menu()
        with patch('maxclient.ui.main_window.confirm_action', return_value=False):
            self._action(menu, 'Заблокировать').trigger()
        self.assertEqual(self.queue, [])

    def test_row_clear_uses_explicit_scope_and_captured_context(self):
        self.service.allow_lifecycle = True
        menu = self._menu()
        context = self.window._row_menu_context
        with patch('maxclient.ui.main_window.confirm_conversation_removal', return_value=False):
            self._action(menu, 'Стереть').trigger()
        self.assertEqual(self.service.writes, [])
        self.assertEqual(len(self.queue), 1)
        self._run(self.queue.pop())
        self.assertEqual(self.service.writes, [('clear', context, False)])
        self.assertEqual(self.window.statusBar().currentMessage(), 'Переписка удалена.')

    def test_row_delete_cancel_does_not_queue(self):
        self.service.allow_lifecycle = True
        menu = self._menu()
        with patch('maxclient.ui.main_window.confirm_conversation_removal', return_value=None):
            self._action(menu, 'Удалить чат').trigger()
        self.assertEqual(self.queue, [])
        self.assertEqual(self.service.writes, [])

    def test_row_delete_queued_then_session_changed_is_not_sent(self):
        self.service.allow_lifecycle = True
        menu = self._menu()
        with patch('maxclient.ui.main_window.confirm_conversation_removal', return_value=False):
            self._action(menu, 'Удалить чат').trigger()
        job = self.queue.pop()
        self.service.security_session = object()
        self._run(job)
        self.assertEqual(self.service.writes, [])

    def test_session_change_inside_block_confirmation_rejects_original_action(self):
        menu = self._menu()
        def change_session(*args, **kwargs):
            self.service.security_session = object()
            return True
        with patch('maxclient.ui.main_window.confirm_action', side_effect=change_session):
            self._action(menu, 'Заблокировать').trigger()
        self.assertEqual(self.queue, [])
        self.assertEqual(self.service.writes, [])

    def test_queue_does_not_call_service_write_after_session_switch(self):
        job = self._mute_job()
        self.service.security_session = object()
        with patch.object(self.service, 'set_conversation_mute', wraps=self.service.set_conversation_mute) as write:
            self._run(job)
        write.assert_not_called()
        self.assertEqual(self.service.attempts, [])
        self.assertEqual(self.service.writes, [])

    def test_late_success_does_not_reload_a_different_session(self):
        job = self._mute_job()
        result = self._run(job, callbacks=False)
        self.service.security_session = object()
        with patch.object(self.window, '_schedule_reload') as reload_rows:
            if job[3]:
                job[3](result)
        reload_rows.assert_not_called()

    def test_late_failure_after_window_destruction_is_ignored(self):
        job = self._mute_job()
        self.window.close()
        self.window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.assertFalse(isValid(self.window))
        if job[4]:
            job[4](RuntimeError('synthetic failure'))
        self.assertEqual(self.service.writes, [])

    def test_queued_worker_never_calls_service_after_window_destruction(self):
        job = self._mute_job()
        self.window.close()
        self.window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self._run(job)
        self.assertEqual(self.service.attempts, [])

    def test_old_folder_read_cannot_replace_new_menus_contents(self):
        old_menu = self.window._build_chat_row_menu(1)
        old_menu.popup(QPoint(100, 100))
        self.app.processEvents()
        self.assertEqual(len(self.queue), 1)
        old_job = self.queue.pop()
        new_menu = self.window._build_chat_row_menu(1)
        new_menu.popup(QPoint(100, 100))
        self.app.processEvents()
        self.assertEqual(len(self.queue), 1)
        new_job = self.queue.pop()
        self._run(old_job)
        self.assertIsNone(self.window._row_folder_rows)
        self._run(new_job)
        self.assertEqual(self.window._row_folder_rows[0]['label'], 'Тестовая папка')

    def test_folder_completion_handles_a_now_unavailable_source(self):
        menu = self.window._build_chat_row_menu(1)
        menu.popup(QPoint(100, 100))
        self.app.processEvents()
        self.assertEqual(len(self.queue), 1)
        job = self.queue.pop()
        result = self._run(job, callbacks=False)
        with patch.object(self.service, 'conversation_snapshot',
                          side_effect=ValueError('Synthetic unavailable source')):
            job[3](result)
            if isValid(menu) and not menu._revoked:
                self._action(menu, 'уведомления').trigger()
        self.assertEqual(self.queue, [])
        self.assertEqual(self.service.writes, [])

    def test_folder_write_preserves_server_checkbox_until_worker(self):
        menu = self._menu()
        context = self.window._row_menu_context
        submenu = self._action(menu, 'Добавить в папку').menu()
        submenu.popup(menu.pos())
        self.app.processEvents()
        target = self._action(submenu, 'Тестовая папка')
        self.assertFalse(target.isChecked())
        QTest.mouseClick(submenu, Qt.MouseButton.LeftButton,
                         pos=submenu.actionGeometry(target).center())
        self.assertFalse(target.isChecked())
        self.assertEqual(self.service.writes, [])
        self.assertEqual(len(self.queue), 1)
        self._run(self.queue.pop())
        self.assertEqual(self.service.writes,
                         [('folder_include', context, 'test-folder', True)])

    def test_inflight_menu_disables_folder_writes_too(self):
        self._mute_job()
        menu = self._menu()
        submenu = self._action(menu, 'Добавить в папку').menu()
        self.assertFalse(self._action(submenu, 'Тестовая папка').isEnabled())

    def test_pending_operation_keeps_another_menu_from_sending_duplicate(self):
        job = self._mute_job()
        menu = self._menu()
        self._action(menu, 'уведомления').trigger()
        self.assertEqual(self.queue, [])
        self._run(job)
        self.assertEqual(len(self.service.writes), 1)

    def test_disconnect_revokes_open_action(self):
        menu = self._menu()
        action = self._action(menu, 'уведомления')
        self.window._stop_search()
        if isValid(action):
            action.trigger()
        self.assertEqual(self.queue, [])

    def test_unknown_outcome_preserves_pending_and_does_not_fabricate_success(self):
        menu = self._menu()
        self._action(menu, 'уведомления').trigger()
        self.assertEqual(len(self.queue), 1)
        job = self.queue.pop()
        self.service.pending = True
        if job[4]:
            job[4](RuntimeError('synthetic outcome unknown'))
        menu = self._menu()
        self.assertFalse(self._action(menu, 'уведомления').isEnabled())
        self.assertTrue(self.service.muted)
        self.assertEqual(self.service.writes, [])

    def test_mute_durations_do_not_guess_or_toggle_before_confirmation(self):
        self.service.muted = False
        menu = self._menu()
        context = self.window._row_menu_context
        submenu = self._action(menu, 'уведомления').menu()
        self.assertIsNotNone(submenu)
        self.assertEqual([action.text() for action in submenu.actions()],
                         ['На 1 час', 'На 4 часа', 'На 24 часа', 'Навсегда'])
        submenu.popup(menu.pos())
        self.app.processEvents()
        target = submenu.actions()[2]
        QTest.mouseClick(submenu, Qt.MouseButton.LeftButton,
                         pos=submenu.actionGeometry(target).center())
        self.assertFalse(self.service.muted)
        self.assertEqual(len(self.queue), 1)
        self._run(self.queue.pop())
        self.assertEqual(self.service.writes, [('mute', context, 86400000)])


if __name__ == '__main__':
    unittest.main()
