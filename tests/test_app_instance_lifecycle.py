"""Startup ownership before account access; all services and directories synthetic."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from PySide6.QtWidgets import QApplication

from maxclient import app as entry
from maxclient.core.config import Settings
from maxclient.core.instance_lock import AlreadyRunning, DataDirectoryLock, InstanceLockError


class AppInstanceLifecycleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qt = QApplication.instance() or QApplication([])

    def setUp(self):
        base = Path('.test-tmp')
        base.mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix='instance_app_', dir=base)
        self.root = Path(self.temporary.name)
        self.original_locks = tuple(entry._PROCESS_DIRECTORY_LOCKS)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.addCleanup(self.cleanup_synthetic_ownership)
        self.paths = self.stack.enter_context(patch.object(entry, 'AppPaths', return_value=SimpleNamespace(dir=self.root)))
        self.qapp = Mock()
        self.stack.enter_context(patch.object(entry, 'QApplication', Mock(instance=Mock(return_value=self.qapp))))
        self.stack.enter_context(patch.object(entry, '_set_windows_app_identity'))
        self.stack.enter_context(patch.object(entry, 'app_icon', return_value=None))
        self.settings = self.stack.enter_context(patch.object(entry.Settings, 'load', return_value=Settings()))
        self.session = self.stack.enter_context(patch.object(entry.Session, 'load', return_value=SimpleNamespace(token='')))
        self.service = self.stack.enter_context(patch.object(entry, 'MaxService', return_value=Mock()))

    def cleanup_synthetic_ownership(self):
        # Production never releases here: tests must clean their own temp locks.
        for lock in tuple(entry._PROCESS_DIRECTORY_LOCKS):
            if lock not in self.original_locks:
                lock.close()
                entry._PROCESS_DIRECTORY_LOCKS.remove(lock)
        self.temporary.cleanup()

    def assert_locked(self):
        with self.assertRaises(AlreadyRunning):
            DataDirectoryLock.acquire(self.root)

    def test_every_sensitive_load_starts_only_with_directory_ownership(self):
        order = []
        def loaded(label, result):
            def call(*args):
                self.assert_locked()
                order.append(label)
                return result
            return call
        self.settings.side_effect = loaded('settings', Settings())
        self.session.side_effect = loaded('session', SimpleNamespace(token=''))
        self.service.side_effect = loaded('service', Mock())
        app = entry.MaxApp()
        self.assertEqual(order, ['settings', 'session', 'service'])
        self.assertIn(app._instance_lock, entry._PROCESS_DIRECTORY_LOCKS)

    def test_duplicate_constructor_never_loads_settings_session_or_service(self):
        first = entry.MaxApp()
        for operation in (self.settings, self.session, self.service):
            operation.reset_mock()
        with self.assertRaises(AlreadyRunning):
            entry.MaxApp()
        for operation in (self.settings, self.session, self.service):
            operation.assert_not_called()
        self.assertIn(first._instance_lock, entry._PROCESS_DIRECTORY_LOCKS)

    def test_cleanup_returns_and_even_failed_shutdown_keep_process_ownership(self):
        app = entry.MaxApp()
        app.service.shutdown.side_effect = [None, RuntimeError('synthetic shutdown')]
        app._cleanup()
        self.assert_locked()
        app._cleanup()
        self.assert_locked()
        self.assertEqual(app.service.shutdown.call_count, 2)

    def test_each_constructor_load_failure_stays_fail_closed_until_process_exit(self):
        for index, operation in enumerate((self.settings, self.session, self.service)):
            with self.subTest(step=index):
                self.paths.return_value = SimpleNamespace(dir=self.root / str(index))
                self.paths.return_value.dir.mkdir()
                operation.side_effect = RuntimeError('synthetic load')
                with self.assertRaisesRegex(RuntimeError, 'synthetic load'):
                    entry.MaxApp()
                with self.assertRaises(AlreadyRunning):
                    DataDirectoryLock.acquire(self.paths.return_value.dir)
                operation.side_effect = None

    def test_directory_failure_does_not_read_account_and_main_reports_safe_error(self):
        self.paths.side_effect = PermissionError('secret path must never reach dialog')
        with patch.object(entry.QMessageBox, 'critical') as notice, patch.object(entry.sys, 'argv', ['MAXIM']):
            self.assertEqual(entry.main(), 1)
        self.assertEqual(notice.call_count, 1)
        self.assertNotIn('secret path', str(notice.call_args))
        for operation in (self.settings, self.session, self.service):
            operation.assert_not_called()

    def test_duplicate_main_is_informational_and_never_starts_event_loop(self):
        entry.MaxApp()
        with patch.object(entry.QMessageBox, 'information') as notice, patch.object(entry.sys, 'argv', ['MAXIM']):
            self.assertEqual(entry.main(), 0)
        self.assertEqual(notice.call_count, 1)
        self.qapp.exec.assert_not_called()
        self.assert_locked()

    def test_non_contention_lock_failure_fails_closed(self):
        with patch.object(entry.DataDirectoryLock, 'acquire', side_effect=InstanceLockError('private detail')), \
                patch.object(entry.QMessageBox, 'critical') as notice, patch.object(entry.sys, 'argv', ['MAXIM']):
            self.assertEqual(entry.main(), 1)
        self.assertNotIn('private detail', str(notice.call_args))
        for operation in (self.settings, self.session, self.service):
            operation.assert_not_called()

    def test_selftest_cleanup_executes_even_when_window_construction_fails(self):
        app = Mock()
        with patch.object(entry, 'MaxApp', return_value=app), \
                patch.object(entry, 'LoginWindow', side_effect=RuntimeError('synthetic selftest')), \
                patch.dict(os.environ, {'MAXDESKTOP_SELFTEST_LOG': ''}), patch('builtins.print'):
            self.assertEqual(entry._selftest(), 1)
        app._cleanup.assert_called_once_with()


if __name__ == '__main__':
    unittest.main()
