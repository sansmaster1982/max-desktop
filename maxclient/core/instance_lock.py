"""One retained Qt lock per canonical, already-existing application data root."""
from __future__ import annotations

import os

from PySide6.QtCore import QDir, QFileInfo, QLockFile


LOCK_FILENAME = 'maxim.instance.lock'


class InstanceLockError(RuntimeError):
    """The data-directory lock could not be established or released safely."""


class AlreadyRunning(InstanceLockError):
    """Qt could not acquire a lock currently held by another instance."""


class DataDirectoryLock:
    """Retain this object for the entire lifetime of data-directory access.

    Qt may recover a lock whose owner process has ended. Age alone never makes
    a lock stale here, and no caller-controlled removal or fallback is used.
    """

    def __init__(self, lock_file):
        self._lock_file = lock_file

    @classmethod
    def acquire(cls, path):
        """Lock an existing directory without waiting, or raise a fixed error."""
        try:
            raw_path = os.fspath(path)
            if not isinstance(raw_path, str) or not raw_path or '\0' in raw_path:
                raise ValueError('invalid directory')
            if not QFileInfo(raw_path).isDir():
                raise ValueError('directory does not exist')
            canonical = QDir(raw_path).canonicalPath()
            if not canonical:
                raise ValueError('directory cannot be resolved')
            lock_file = QLockFile(QDir(canonical).filePath(LOCK_FILENAME))
            lock_file.setStaleLockTime(0)
            if lock_file.tryLock(0):
                return cls(lock_file)
            error = lock_file.error()
        except Exception:
            raise InstanceLockError('The data directory could not be locked.') from None
        if error == QLockFile.LockError.LockFailedError:
            raise AlreadyRunning('Another instance is using this data directory.')
        raise InstanceLockError('The data directory could not be locked.')

    def close(self):
        """Release only this retained lock; repeated close calls are harmless."""
        lock_file = self._lock_file
        if lock_file is None:
            return
        try:
            lock_file.unlock()
        except Exception:
            raise InstanceLockError('The data-directory lock could not be released.') from None
        self._lock_file = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
