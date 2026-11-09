"""Actual widget destruction before fake read replies; no account/network IO."""
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
from maxclient.ui.settings_read_panels import SettingsReadPanel
from test_settings_panel_lifetime import _CapturedAsync


FOLDERS = {'orderedFolders': [{'id': 'synthetic', 'title': 'Тестовая папка'}]}
STORIES = {'stories': [{'id': 1, 'time': 123000}], 'marker': 123}


class SettingsReadLifetimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.host = QWidget()
        self.service = SimpleNamespace(get_folders=Mock(return_value=FOLDERS),
                                       list_own_stories=Mock(return_value=STORIES))

    def tearDown(self):
        if isValid(self.host):
            self.host.deleteLater()
            QCoreApplication.sendPostedEvents(self.host, QEvent.Type.DeferredDelete)
        self.app.processEvents()

    def destroy(self, receiver):
        receiver.deleteLater()
        QCoreApplication.sendPostedEvents(receiver, QEvent.Type.DeferredDelete)
        self.assertFalse(isValid(receiver))

    def assert_captured_reply_safe(self, kind, result):
        panel = SettingsReadPanel(self.service, kind, self.host)
        queue = _CapturedAsync()
        with patch('maxclient.ui.settings_read_panels.run_async', queue):
            panel.refresh()
            self.assertEqual(len(queue.calls), 1)
            self.destroy(self.host)
            self.assertFalse(isValid(panel))
            queue.calls[0][3](result)
            queue.calls[0][3]({})
            queue.calls[0][4]('synthetic private exception not displayed')
            panel._done()
            panel.ensure_loaded()
            panel.refresh()
            panel._load(more=True)
        self.assertFalse(panel._loaded)
        self.assertEqual(len(queue.calls), 1)
        self.service.get_folders.assert_not_called()
        self.service.list_own_stories.assert_not_called()

    def test_late_story_lambda_and_error_ignore_deleted_parent(self):
        self.assert_captured_reply_safe('stories', STORIES)

    def test_late_folder_success_and_error_ignore_deleted_parent(self):
        self.assert_captured_reply_safe('folders', FOLDERS)

    def test_live_panel_with_deleted_owned_list_ignores_reply_and_cannot_queue_more(self):
        panel = SettingsReadPanel(self.service, 'stories', self.host)
        queue = _CapturedAsync()
        with patch('maxclient.ui.settings_read_panels.run_async', queue):
            panel.refresh()
            self.destroy(panel.items)
            self.assertTrue(isValid(panel))
            queue.calls[0][3](STORIES)
            queue.calls[0][4]('synthetic')
            panel._folders(FOLDERS)
            panel.ensure_loaded()
            panel.refresh()
        self.assertFalse(panel._loaded)
        self.assertEqual(panel._story_ids, set())
        self.assertEqual(len(queue.calls), 1)

    def test_actual_queued_success_and_failure_release_task_after_receiver_destruction(self):
        for kind, result in (('stories', STORIES), ('folders', FOLDERS)):
            for fail in (False, True):
                with self.subTest(kind=kind, fail=fail):
                    entered, release = threading.Event(), threading.Event()
                    def delayed():
                        entered.set()
                        if not release.wait(timeout=3):
                            raise AssertionError('Synthetic worker not released')
                        if fail:
                            raise ValueError('Synthetic read error')
                        return result
                    service = SimpleNamespace(get_folders=delayed, list_own_stories=lambda **_kw: delayed())
                    owner = QWidget(self.host)
                    panel = SettingsReadPanel(service, kind, owner)
                    before = set(async_runner._pending)
                    errors, stderr, tasks = [], io.StringIO(), set()
                    with patch.object(sys, 'excepthook', lambda *error: errors.append(error)), contextlib.redirect_stderr(stderr):
                        panel.refresh()  # Real QRunnable/Qt dispatcher, synthetic service only.
                        try:
                            self.assertTrue(entered.wait(timeout=2))
                            tasks = set(async_runner._pending) - before
                            self.assertEqual(len(tasks), 1)
                            self.destroy(owner)
                            self.assertFalse(isValid(panel))
                        finally:
                            release.set()
                        deadline = time.monotonic() + 3
                        while tasks & async_runner._pending and time.monotonic() < deadline:
                            self.app.processEvents()
                            time.sleep(0.002)
                        self.app.processEvents()
                    self.assertFalse(tasks & async_runner._pending)
                    self.assertEqual(errors, [])
                    self.assertNotIn('already deleted', stderr.getvalue())
                    self.assertFalse(panel._loaded)

    def test_unrelated_live_widget_failure_is_not_swallowed(self):
        panel = SettingsReadPanel(self.service, 'stories', self.host)
        with patch.object(panel.items, 'clear', side_effect=RuntimeError('synthetic live failure')):
            with self.assertRaisesRegex(RuntimeError, 'synthetic live failure'):
                panel._stories(STORIES)


if __name__ == '__main__':
    unittest.main()
