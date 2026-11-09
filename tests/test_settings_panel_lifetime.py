"""Destroy real Qt widgets before synthetic replies, including real queued tasks."""
import contextlib
import io
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import sys
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication, QWidget
from shiboken6 import isValid

from maxclient.ui import async_runner
from maxclient.ui.folders_panel import FoldersPanel
from maxclient.ui.notification_settings_panel import NotificationSettingsPanel


FOLDER_PARTIAL = {'folderSync': 0}
FOLDER_FULL = {'folderSync': 0, 'foldersOrder': [], 'orderedFolders': []}
NOTIFICATION_RESULT = {'available': True, 'pending': False, 'settings': {'PUSH_DETAILS': True}}


class _CapturedAsync:
    def __init__(self):
        self.calls = []
    def __call__(self, method, *args, on_done=None, on_error=None, **kwargs):
        self.calls.append((method, args, kwargs, on_done, on_error))


class SettingsPanelLifetimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.host = QWidget()
        self.service = SimpleNamespace(list_chats=lambda: [], get_folders=Mock(return_value=FOLDER_FULL))
        self.service.owner = 123
        self.service.client = object()
        self.service.auth_session = object()
        self.service._history_read_context = lambda: (
            self.service.owner, self.service.client, self.service.auth_session)
        def require_context(context):
            if (context[0] != self.service.owner or context[1] is not self.service.client
                    or context[2] is not self.service.auth_session):
                raise ValueError('Synthetic stale notification session')
            return self.service.client
        self.service._require_history_context = require_context
        self.adapter = SimpleNamespace(read=Mock(return_value=NOTIFICATION_RESULT),
                                       update=Mock(return_value=NOTIFICATION_RESULT))
        self.service._desktop_notification_settings = self.adapter
        self.settings = SimpleNamespace(theme='light', notifications=True, save=Mock())

    def tearDown(self):
        if isValid(self.host):
            self.host.deleteLater()
            QCoreApplication.sendPostedEvents(self.host, QEvent.Type.DeferredDelete)
        self.settings.save.assert_not_called()

    def destroy(self, panel):
        self.host.deleteLater()
        QCoreApplication.sendPostedEvents(self.host, QEvent.Type.DeferredDelete)
        self.assertFalse(isValid(panel))

    def test_folder_read_success_error_and_queued_refresh_after_parent_deletion(self):
        queue = _CapturedAsync()
        panel = FoldersPanel(self.service, self.host)
        emitted = Mock()
        panel.folders_changed.connect(emitted)
        with patch('maxclient.ui.folders_panel.run_async', queue):
            panel.refresh()
            panel.refresh()
            self.assertEqual(len(queue.calls), 1)
            self.destroy(panel)
            queue.calls[0][3](FOLDER_PARTIAL)  # Previously accessed a deleted QLabel.
            queue.calls[0][3](FOLDER_FULL)
            queue.calls[0][4]('synthetic failure')
            panel._populate()
            panel._apply_enabled()
            panel.refresh()
            panel.ensure_loaded()
        self.assertEqual(len(queue.calls), 1)
        self.assertFalse(panel._loaded)
        emitted.assert_not_called()
        self.service.get_folders.assert_not_called()

    def test_folder_write_completion_after_deletion_cannot_confirm_or_read_again(self):
        queue = _CapturedAsync()
        panel = FoldersPanel(self.service, self.host)
        method = Mock(side_effect=AssertionError('No account write in this test'))
        intent = {'kind': 'create', 'id': None, 'patch': {'title': 'Synthetic'}}
        with patch('maxclient.ui.folders_panel.run_async', queue):
            panel._start_write(intent, method)
            panel.refresh()
            self.destroy(panel)
            queue.calls[0][3]({'accepted': True, 'pending': False, 'folderId': 'synthetic',
                               'snapshot': FOLDER_FULL})
            queue.calls[0][4]('synthetic unknown write')
            panel._start_write(intent, method)
            panel._drain_refresh()
        self.assertEqual(len(queue.calls), 1)
        self.assertIs(panel._pending, intent)
        self.assertIsNone(intent['id'])
        method.assert_not_called()
        self.service.get_folders.assert_not_called()

    def test_notification_reply_error_population_and_queued_refresh_after_deletion(self):
        queue = _CapturedAsync()
        panel = NotificationSettingsPanel(self.service, self.settings, self.host)
        with patch('maxclient.ui.notification_settings_panel.run_async', queue):
            panel.refresh()
            panel.refresh()
            self.destroy(panel)
            queue.calls[0][3](NOTIFICATION_RESULT)
            queue.calls[0][4]('synthetic failure')  # Previously created QSignalBlocker on deleted controls.
            panel._populate()
            panel._change('preview', False)
            panel._local_changed(False)
            panel._drain()
            panel.apply_theme('dark')
            panel.ensure_loaded()
        self.assertEqual(len(queue.calls), 1)
        self.assertFalse(panel._loaded)
        self.assertFalse(panel._result['available'])
        self.adapter.update.assert_not_called()
        self.adapter.read.assert_not_called()

    def test_notification_write_result_after_deletion_cannot_schedule_a_followup(self):
        queue = _CapturedAsync()
        panel = NotificationSettingsPanel(self.service, self.settings, self.host)
        panel._done(NOTIFICATION_RESULT)
        with patch('maxclient.ui.notification_settings_panel.run_async', queue):
            panel._change('preview', False)
            panel.refresh()
            self.destroy(panel)
            queue.calls[0][3]({'available': True, 'pending': False, 'settings': {'PUSH_DETAILS': False}})
            queue.calls[0][4]('synthetic unknown write')
        self.assertEqual(len(queue.calls), 1)
        self.assertEqual(panel._result, NOTIFICATION_RESULT)
        self.adapter.update.assert_not_called()
        self.adapter.read.assert_not_called()

    def assert_queued_task_safe(self, kind, fail):
        entered, release = threading.Event(), threading.Event()
        result = FOLDER_PARTIAL if kind == 'folders' else NOTIFICATION_RESULT
        def delayed(**kwargs):
            if kind == 'notifications':
                self.service._require_history_context(kwargs['context'])
            else:
                self.assertEqual(kwargs, {})
            entered.set()
            if not release.wait(timeout=3):
                raise AssertionError('Synthetic worker was not released')
            if fail:
                raise ValueError('Synthetic delayed failure')
            return result
        if kind == 'folders':
            self.service.get_folders = Mock(side_effect=delayed)
            panel = FoldersPanel(self.service, self.host)
        else:
            self.adapter.read = Mock(side_effect=delayed)
            panel = NotificationSettingsPanel(self.service, self.settings, self.host)
        before = set(async_runner._pending)
        errors, captured_stderr, tasks = [], io.StringIO(), set()
        with patch.object(sys, 'excepthook', lambda *error: errors.append(error)), contextlib.redirect_stderr(captured_stderr):
            panel.refresh()  # Actual QRunnable, Qt signals and dispatcher; fake service only.
            try:
                self.assertTrue(entered.wait(timeout=2))
                tasks = set(async_runner._pending) - before
                self.assertEqual(len(tasks), 1)
                self.destroy(panel)
            finally:
                release.set()
            deadline = time.monotonic() + 3
            while tasks & async_runner._pending and time.monotonic() < deadline:
                self.app.processEvents()
                time.sleep(0.002)
            self.app.processEvents()
        self.assertFalse(tasks & async_runner._pending, 'Completed task must be released even when UI died')
        self.assertEqual(errors, [], 'Qt callback exceptions must fail the test')
        self.assertNotIn('Traceback', captured_stderr.getvalue())
        self.assertNotIn('already deleted', captured_stderr.getvalue())
        self.assertFalse(panel._loaded)

    def test_real_queued_folder_success_ignores_destroyed_receiver_and_releases_task(self):
        self.assert_queued_task_safe('folders', False)

    def test_real_queued_folder_error_ignores_destroyed_receiver_and_releases_task(self):
        self.assert_queued_task_safe('folders', True)

    def test_real_queued_notification_success_ignores_destroyed_receiver_and_releases_task(self):
        self.assert_queued_task_safe('notifications', False)

    def test_real_queued_notification_error_ignores_destroyed_receiver_and_releases_task(self):
        self.assert_queued_task_safe('notifications', True)


if __name__ == '__main__':
    unittest.main()
