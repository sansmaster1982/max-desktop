"""Stage an explicitly authorized FILE88 URL; never open or execute a file.

No MAX credentials are accepted. The caller owns the download intent and must
make a final context check before commit(). Network cancellation/time budgets
are cooperative; the system DNS resolver itself has its OS-managed deadline.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import ipaddress
import os
from pathlib import Path
import re
import socket
import stat
import tempfile
from threading import RLock
import time
import unicodedata
from urllib.parse import urljoin, urlsplit, urlunsplit

from .media_fetch import image_url


MAX_FILE_BYTES = 1024 ** 3
DOWNLOAD_SECONDS = 600
_REPARSE_POINT = getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400)
_RESERVED = {'CON', 'PRN', 'AUX', 'NUL', 'CONIN$', 'CONOUT$', 'CLOCK$'} | {
    f'{prefix}{index}' for prefix in ('COM', 'LPT') for index in range(1, 10)}


class FileDownloadError(ValueError):
    """A safe user-facing failure; never includes the signed URL."""


class FileDownloadCancelled(FileDownloadError):
    pass


def suggested_filename(value):
    """Local Windows dialog suggestion, not a source-derived destination path."""
    name = unicodedata.normalize('NFKC', value[:1024]) if isinstance(value, str) else ''
    name = re.split(r'[/\\]', name)[-1]
    name = ''.join('_' if char in '<>:"/\\|?*' or unicodedata.category(char) in ('Cc', 'Cf', 'Cs')
                   else char for char in name).strip(' .')
    if not name:
        return 'download.bin'
    if name.split('.', 1)[0].rstrip(' .').upper() in _RESERVED:
        name = '_' + name
    # Windows component limits use UTF-16 code units. Keep a bounded extension
    # visible when shortening an untrusted long metadata name.
    stem, extension = os.path.splitext(name)
    if len(extension.encode('utf-16-le', errors='replace')) > 48:
        stem, extension = name, ''
    while len((stem + extension).encode('utf-16-le', errors='replace')) > 360:
        stem = stem[:-1]
    return (stem + extension).rstrip(' .') or 'download.bin'


def _file_stat(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size,
            info.st_mtime_ns, info.st_ctime_ns)


def _regular(info):
    return stat.S_ISREG(info.st_mode) and not getattr(info, 'st_file_attributes', 0) & _REPARSE_POINT


def _destination(value):
    path = Path(value)
    name = path.name
    if (not path.is_absolute() or not name or name in ('.', '..') or name.endswith((' ', '.'))
            or any(char in '<>:"/\\|?*' or ord(char) < 32 for char in name)
            or name.split('.', 1)[0].rstrip(' .').upper() in _RESERVED):
        raise FileDownloadError('Недопустимое имя выбранного файла.')
    parent = path.parent.resolve(strict=True)
    parent_info = parent.lstat()
    if (not stat.S_ISDIR(parent_info.st_mode)
            or getattr(parent_info, 'st_file_attributes', 0) & _REPARSE_POINT):
        raise FileDownloadError('Папка сохранения недоступна.')
    return parent / name, (parent_info.st_dev, parent_info.st_ino)


@dataclass(frozen=True)
class TargetFingerprint:
    exists: bool
    parent_identity: tuple[int, int]
    file_stat: tuple | None
    _path: str = field(repr=False)


def capture_target(path):
    """Capture an existing regular destination or its confirmed absence."""
    try:
        target, parent = _destination(path)
        try:
            info = target.lstat()
        except FileNotFoundError:
            info = None
        if info is not None and not _regular(info):
            raise FileDownloadError('Выбранный путь не является обычным файлом.')
        return TargetFingerprint(info is not None, parent,
            _file_stat(info) if info is not None else None, os.path.normcase(str(target)))
    except FileDownloadError:
        raise
    except Exception:
        raise FileDownloadError('Не удалось проверить выбранный файл.') from None


def _active(cancelled, is_current, deadline=None):
    try:
        valid = not cancelled.is_set() and is_current() is True
    except Exception:
        valid = False
    if not valid:
        raise FileDownloadCancelled('Загрузка отменена: карточка или сессия изменилась.')
    if deadline is not None and time.monotonic() >= deadline:
        raise FileDownloadError('Время загрузки файла истекло.')


def _remove_owned(path, identity):
    try:
        info = path.lstat()
        if not _regular(info) or (info.st_dev, info.st_ino) != identity:
            return False
        path.unlink()
        return True
    except FileNotFoundError:
        return True
    except OSError:
        return False


class StagedFile:
    """An owned .part; commit is explicit, guarded and never automatically retried."""
    def __init__(self, part, target, expected, part_stat, cancelled, is_current, received, total):
        self._part, self._target = part, target
        self._expected, self._part_stat = expected, part_stat
        self._cancelled, self._is_current = cancelled, is_current
        self.received, self.total = received, total
        self._state, self._lock = 'staged', RLock()

    def __repr__(self):
        return f'StagedFile(received={self.received}, total={self.total}, state={self._state!r})'

    def discard(self):
        """Remove only our randomized temporary file; never remove a final target."""
        with self._lock:
            if self._state != 'staged':
                return True
            removed = _remove_owned(self._part, self._part_stat[:2])
            if removed:
                self._state = 'discarded'
            return removed

    def commit(self):
        """Replace once after current/cancel, parent, destination and .part checks.

        The fingerprint comparison detects intervening target changes. It is not
        a cross-process filesystem compare-and-swap transaction.
        """
        with self._lock:
            if self._state == 'committed':
                return self._target
            if self._state != 'staged':
                raise FileDownloadError('Временный файл уже недоступен.')
            try:
                _active(self._cancelled, self._is_current)
                if capture_target(self._target) != self._expected:
                    raise FileDownloadError('Файл назначения изменился. Выберите место сохранения заново.')
                if _file_stat(self._part.lstat()) != self._part_stat:
                    raise FileDownloadError('Временный файл изменился. Сохранение отменено.')
                _active(self._cancelled, self._is_current)
                if self._expected.exists:
                    os.replace(self._part, self._target)
                elif os.name == 'nt':
                    # Unlike replace(), Windows rename fails atomically if a
                    # destination appeared after our last fingerprint check.
                    os.rename(self._part, self._target)
                else:
                    # Same-directory link creation provides no-replace publish
                    # semantics for the portable offline-test path as well.
                    os.link(self._part, self._target)
                    _remove_owned(self._part, self._part_stat[:2])
                self._state = 'committed'
                return self._target
            except FileDownloadError:
                self.discard()
                raise
            except Exception:
                self.discard()
                raise FileDownloadError('Не удалось сохранить файл.') from None

    def __del__(self):
        try:
            self.discard()
        except Exception:
            pass


def stream_file(url, target, *, expected_target, cancelled, is_current,
                on_progress=None, max_bytes=MAX_FILE_BYTES):
    """Stream one explicit88 URL into a sibling .part and return it uncommitted.

    Progress is (actual_received_bytes, Content-Length_or_None). Compressed
    responses have an unknown decoded total; the actual byte cap still applies.
    Existing destinations remain intact until StagedFile.commit().
    """
    part = identity = None
    staged = None
    try:
        if (image_url(url) is None or urlsplit(url).fragment
                or type(max_bytes) is not int or not 0 < max_bytes <= MAX_FILE_BYTES
                or type(expected_target) is not TargetFingerprint
                or not callable(is_current) or on_progress is not None and not callable(on_progress)):
            raise FileDownloadError('Некорректные параметры загрузки файла.')
        _active(cancelled, is_current)
        target, _parent = _destination(target)
        if capture_target(target) != expected_target:
            raise FileDownloadError('Файл назначения изменился. Выберите место сохранения заново.')
        import requests

        class NoCredentials(requests.auth.AuthBase):
            def __call__(self, request):
                request.headers.pop('Authorization', None)
                request.headers.pop('Cookie', None)
                return request

        class PinnedFileAdapter(requests.adapters.HTTPAdapter):
            def __init__(self, hostname):
                self._hostname = hostname
                super().__init__(max_retries=0)

            def init_poolmanager(self, *args, **kwargs):
                kwargs.update(server_hostname=self._hostname, assert_hostname=self._hostname)
                return super().init_poolmanager(*args, **kwargs)

            def proxy_manager_for(self, proxy, **kwargs):
                kwargs.update(server_hostname=self._hostname, assert_hostname=self._hostname)
                return super().proxy_manager_for(proxy, **kwargs)

        deadline = time.monotonic() + DOWNLOAD_SECONDS
        previous = None
        with requests.Session() as session:
            session.auth = NoCredentials()
            for redirect in range(4):
                _active(cancelled, is_current, deadline)
                parsed = urlsplit(url)
                host = parsed.hostname.encode('idna').decode('ascii')
                addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
                if not addresses or any(not ipaddress.ip_address(row[4][0]).is_global
                                        or ipaddress.ip_address(row[4][0]).is_multicast for row in addresses):
                    raise FileDownloadError('Адрес файла недоступен для скачивания.')
                _active(cancelled, is_current, deadline)
                address = ipaddress.ip_address(addresses[0][4][0])
                authority = f'[{address}]' if address.version == 6 else str(address)
                destination = urlunsplit(('https', authority, parsed.path, parsed.query, ''))
                if previous is not None:
                    previous.close()
                previous = PinnedFileAdapter(host)
                session.mount('https://', previous)
                session.cookies.clear()
                _active(cancelled, is_current, deadline)
                with session.get(destination, stream=True, allow_redirects=False, timeout=(5, 15),
                    headers={'Host': host, 'Accept': '*/*', 'Accept-Encoding': 'identity'}) as response:
                    _active(cancelled, is_current, deadline)
                    if response.status_code in (301, 302, 303, 307, 308):
                        new_url = urljoin(url, response.headers.get('Location', ''))
                        if (redirect == 3 or new_url == url or image_url(new_url) is None
                                or urlsplit(new_url).fragment):
                            raise FileDownloadError('Недоступный адрес файла.')
                        url = new_url
                        continue
                    if response.status_code != 200:
                        raise FileDownloadError('Сервер не вернул файл для скачивания.')
                    length = response.headers.get('Content-Length')
                    declared = None
                    if length is not None:
                        if (not isinstance(length, str) or not length.isascii() or not length.isdigit()
                                or len(length) > 20):
                            raise FileDownloadError('Не подтверждён размер файла.')
                        declared = int(length)
                        if declared > max_bytes:
                            raise FileDownloadError('Файл превышает допустимый размер.')
                    encoded = response.headers.get('Content-Encoding', '').lower().strip()
                    total = declared if encoded in ('', 'identity') else None
                    if capture_target(target) != expected_target:
                        raise FileDownloadError('Файл назначения изменился. Выберите место сохранения заново.')
                    _active(cancelled, is_current, deadline)
                    descriptor, name = tempfile.mkstemp(prefix='.max-download-', suffix='.part', dir=target.parent)
                    part = Path(name)
                    info = os.fstat(descriptor)
                    identity = info.st_dev, info.st_ino
                    received = 0
                    with os.fdopen(descriptor, 'wb') as output:
                        if on_progress is not None:
                            on_progress(0, total)
                        for chunk in response.iter_content(chunk_size=64 * 1024):
                            _active(cancelled, is_current, deadline)
                            if not isinstance(chunk, bytes):
                                raise FileDownloadError('Сервер вернул неподдерживаемые данные файла.')
                            if received + len(chunk) > max_bytes:
                                raise FileDownloadError('Файл превышает допустимый размер.')
                            if not chunk:
                                continue
                            output.write(chunk)
                            received += len(chunk)
                            if on_progress is not None:
                                on_progress(received, total)
                        _active(cancelled, is_current, deadline)
                        if total is not None and received != total:
                            raise FileDownloadError('Файл получен не полностью.')
                        output.flush()
                        os.fsync(output.fileno())
                        # The descriptor identifies our actual file. Never
                        # adopt a new path's inode after releasing this handle.
                        completed_stat = _file_stat(os.fstat(output.fileno()))
                    _active(cancelled, is_current, deadline)
                    part_info = part.lstat()
                    if not _regular(part_info) or _file_stat(part_info) != completed_stat:
                        raise FileDownloadError('Временный файл изменился. Загрузка отменена.')
                    staged = StagedFile(part, target, expected_target, completed_stat,
                                        cancelled, is_current, received, total)
                    return staged
        raise FileDownloadError('Не удалось получить файл для скачивания.')
    except FileDownloadError:
        raise
    except Exception:
        raise FileDownloadError('Не удалось загрузить файл.') from None
    finally:
        if staged is None and part is not None and identity is not None:
            _remove_owned(part, identity)
