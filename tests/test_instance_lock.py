"""Real isolated Qt processes prove exclusion/release without application data."""
from __future__ import annotations

import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from PySide6.QtCore import QLockFile

from maxclient.core.instance_lock import AlreadyRunning, DataDirectoryLock, InstanceLockError, LOCK_FILENAME


CHILD = r'''
import os, sys
sys.dont_write_bytecode = True
sys.path[:0] = [sys.argv[1], sys.argv[2]]
def audit(event, args):
    if event in ('socket.connect', 'socket.connect_ex', 'socket.sendto', 'socket.getaddrinfo', 'socket.gethostbyname'):
        raise AssertionError('network denied')
    if event == 'open' and args and isinstance(args[0], (str, bytes, os.PathLike)):
        if '.local' in os.fsdecode(args[0]).replace('\\', '/').lower().split('/'):
            raise AssertionError('account data denied')
sys.addaudithook(audit)
from PySide6.QtCore import QCoreApplication
from maxclient.core.instance_lock import AlreadyRunning, DataDirectoryLock, InstanceLockError
app = QCoreApplication([])
try:
    retained = DataDirectoryLock.acquire(sys.argv[3])
except AlreadyRunning:
    print('ALREADY', flush=True)
    raise SystemExit(2)
except InstanceLockError:
    print('ERROR', flush=True)
    raise SystemExit(3)
print('LOCKED', flush=True)
for command in sys.stdin:
    if command.strip() == 'crash':
        os._exit(17)
    if command.strip() == 'close':
        retained.close()
        retained.close()
        print('CLOSED', flush=True)
        raise SystemExit(0)
    if command.strip() == 'alive':
        print('ALIVE', flush=True)
'''


class InstanceLockTests(unittest.TestCase):
    def setUp(self):
        self.project = Path(__file__).resolve().parents[1]
        base = self.project / '.test-tmp'
        base.mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix='instance-lock-', dir=base)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.data = self.root / 'data'
        self.data.mkdir()
        self.children = []
        self.reader_threads = []
        self.addCleanup(self._close_children)

    def _close_children(self):
        for process, _lines in self.children:
            if process.poll() is None:
                try:
                    process.stdin.write('close\n')
                    process.stdin.flush()
                    process.wait(timeout=5)
                except (OSError, subprocess.TimeoutExpired):
                    process.kill()
                    process.wait(timeout=5)
        for thread in self.reader_threads:
            thread.join(timeout=5)
        for process, _lines in self.children:
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None:
                    stream.close()

    def child(self, path=None):
        child_env = self.root / f'env-{len(self.children)}'
        child_env.mkdir()
        env = os.environ.copy()
        for key in ('APPDATA', 'LOCALAPPDATA', 'MAX_DESKTOP_DATA_DIR', 'TEMP', 'TMP'):
            env[key] = str(child_env)
        env['QT_QPA_PLATFORM'] = 'offscreen'
        env['PYTHONIOENCODING'] = 'utf-8'
        process = subprocess.Popen(
            [sys.executable, '-u', '-c', CHILD, str(self.project.parent / '.deps'),
             str(self.project), str(self.data if path is None else path)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding='utf-8', env=env, cwd=self.root,
        )
        lines = queue.Queue()
        def read_lines():
            for line in process.stdout:
                lines.put(line.rstrip('\r\n'))
        thread = threading.Thread(target=read_lines, daemon=True)
        thread.start()
        self.reader_threads.append(thread)
        self.children.append((process, lines))
        return process, lines

    def line(self, child):
        _process, lines = child
        try:
            return lines.get(timeout=10)
        except queue.Empty:
            self.fail('Synthetic lock process did not complete its handshake')

    def send(self, child, command):
        process, _lines = child
        process.stdin.write(command + '\n')
        process.stdin.flush()

    def locked(self, path=None):
        child = self.child(path)
        self.assertEqual(self.line(child), 'LOCKED')
        return child

    def release(self, child):
        self.send(child, 'close')
        self.assertEqual(self.line(child), 'CLOSED')
        self.assertEqual(child[0].wait(timeout=10), 0)

    def test_second_process_fails_while_retained_owner_remains_alive(self):
        owner = self.locked()
        contender = self.child()
        self.assertEqual(self.line(contender), 'ALREADY')
        self.assertEqual(contender[0].wait(timeout=10), 2)
        # Destruction of the failed contender's QLockFile must not release the
        # owner's lock; changing only its age must not authorize takeover.
        self.assertTrue((self.data / LOCK_FILENAME).is_file())
        os.utime(self.data / LOCK_FILENAME, (1, 1))
        another = self.child()
        self.assertEqual(self.line(another), 'ALREADY')
        self.assertEqual(another[0].wait(timeout=10), 2)
        self.send(owner, 'alive')
        self.assertEqual(self.line(owner), 'ALIVE')
        self.assertIsNone(owner[0].poll())
        self.release(owner)

    def test_two_different_data_directories_are_allowed_simultaneously(self):
        other = self.root / 'other'
        other.mkdir()
        a, b = self.locked(), self.locked(other)
        self.assertTrue((self.data / LOCK_FILENAME).is_file())
        self.assertTrue((other / LOCK_FILENAME).is_file())
        self.release(a)
        self.release(b)

    def test_canonical_relative_and_dotdot_aliases_contend_on_same_lock(self):
        owner = self.locked()
        child_dir = self.data / 'child'
        child_dir.mkdir()
        alias = self.child(Path('data') / 'child' / '..')
        self.assertEqual(self.line(alias), 'ALREADY')
        self.assertEqual(alias[0].wait(timeout=10), 2)
        self.release(owner)

    def test_normal_close_allows_next_process_and_removes_own_lock(self):
        owner = self.locked()
        self.release(owner)
        self.assertFalse((self.data / LOCK_FILENAME).exists())
        replacement = self.locked()
        self.release(replacement)

    def test_crashed_owner_is_recovered_by_qt_without_manual_removal(self):
        owner = self.locked()
        self.send(owner, 'crash')
        self.assertEqual(owner[0].wait(timeout=10), 17)
        self.assertTrue((self.data / LOCK_FILENAME).is_file())
        replacement = self.locked()
        self.release(replacement)
        self.assertFalse((self.data / LOCK_FILENAME).exists())

    def test_existing_directory_required_and_errors_do_not_reveal_paths(self):
        missing = self.root / 'synthetic-private-marker'
        file_path = self.root / 'plain-file'
        file_path.write_text('synthetic', encoding='utf-8')
        for path in (missing, file_path, '', b'bytes', None, True, 'bad\0value'):
            with self.subTest(kind=type(path).__name__):
                with self.assertRaises(InstanceLockError) as caught:
                    DataDirectoryLock.acquire(path)
                self.assertNotIsInstance(caught.exception, AlreadyRunning)
                self.assertEqual(str(caught.exception), 'The data directory could not be locked.')
        self.assertFalse(missing.exists())
        self.assertEqual(file_path.read_text(encoding='utf-8'), 'synthetic')

    def test_qt_configuration_is_nonblocking_no_age_timeout_and_no_forced_removal(self):
        fake = Mock()
        fake.tryLock.return_value = True
        with patch('maxclient.core.instance_lock.QLockFile', return_value=fake) as factory:
            lock = DataDirectoryLock.acquire(self.data)
        fake.setStaleLockTime.assert_called_once_with(0)
        fake.tryLock.assert_called_once_with(0)
        fake.removeStaleLockFile.assert_not_called()
        self.assertEqual(Path(factory.call_args.args[0]).name, LOCK_FILENAME)
        self.assertIs(lock._lock_file, fake)
        fake.unlock.assert_not_called()
        lock.close()
        lock.close()
        fake.unlock.assert_called_once_with()

    def test_lock_error_distinction_and_exception_redaction_fail_closed(self):
        for error, expected in ((QLockFile.LockError.LockFailedError, AlreadyRunning),
                                (QLockFile.LockError.PermissionError, InstanceLockError),
                                (QLockFile.LockError.UnknownError, InstanceLockError),
                                (QLockFile.LockError.NoError, InstanceLockError)):
            fake = Mock()
            fake.tryLock.return_value = False
            fake.error.return_value = error
            with self.subTest(error=error), patch('maxclient.core.instance_lock.QLockFile') as factory:
                factory.return_value = fake
                factory.LockError = QLockFile.LockError
                with self.assertRaises(expected) as caught:
                    DataDirectoryLock.acquire(self.data)
                self.assertIs(type(caught.exception), expected)
                fake.removeStaleLockFile.assert_not_called()
        with patch('maxclient.core.instance_lock.QLockFile', side_effect=OSError('synthetic secret path')):
            with self.assertRaises(InstanceLockError) as caught:
                DataDirectoryLock.acquire(self.data)
            self.assertNotIn('secret', str(caught.exception))

    def test_context_manager_retains_then_releases_even_when_body_raises(self):
        fake = Mock()
        fake.tryLock.return_value = True
        with patch('maxclient.core.instance_lock.QLockFile', return_value=fake):
            with self.assertRaisesRegex(ValueError, '^synthetic body$'):
                with DataDirectoryLock.acquire(self.data) as retained:
                    self.assertIs(retained._lock_file, fake)
                    fake.unlock.assert_not_called()
                    raise ValueError('synthetic body')
        fake.unlock.assert_called_once_with()

    def test_unlock_failure_is_typed_and_retains_lock_for_safe_cleanup(self):
        fake = Mock()
        fake.tryLock.return_value = True
        fake.unlock.side_effect = RuntimeError('synthetic private detail')
        with patch('maxclient.core.instance_lock.QLockFile', return_value=fake):
            retained = DataDirectoryLock.acquire(self.data)
        with self.assertRaises(InstanceLockError) as caught:
            retained.close()
        self.assertEqual(str(caught.exception), 'The data-directory lock could not be released.')
        self.assertIs(retained._lock_file, fake)
