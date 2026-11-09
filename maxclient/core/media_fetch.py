"""Bounded public-image reads. No MAX tokens, API cookies or local paths."""
from __future__ import annotations

import ipaddress
import socket
import time
from urllib.parse import urljoin, urlsplit, urlunsplit


THUMBNAIL_MAX_BYTES = 8 * 1024 * 1024
IMAGE_MAX_BYTES = 24 * 1024 * 1024


class MediaFetchError(ValueError):
    pass


def image_url(value):
    """Local preview policy, not a claim about MAX's complete CDN inventory."""
    if not isinstance(value, str) or not value or len(value) > 8192:
        return None
    if any(ord(char) <= 32 or ord(char) == 127 for char in value):
        return None
    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or '').lower().rstrip('.')
        if (parsed.scheme != 'https' or parsed.username is not None or parsed.password is not None
                or not host or '.' not in host or parsed.port not in (None, 443)
                or host.endswith(('.localhost', '.local', '.internal'))):
            return None
        try:
            ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            return None
    except ValueError:
        return None
    return value


def fetch_image(url, *, cancelled, max_bytes=THUMBNAIL_MAX_BYTES):
    import requests
    try:
        return _fetch_image(url, cancelled=cancelled, max_bytes=max_bytes)
    except (requests.RequestException, OSError):
        raise MediaFetchError('Не удалось загрузить изображение.') from None


def _fetch_image(url, *, cancelled, max_bytes):
    """Fetch one explicit image URL with finite size/time and cancellation.

    Redirects are checked individually; proxy configuration is preserved.
    The request intentionally supplies no MAX authorization or netrc identity.
    Cancellation is cooperative between chunks (bounded network timeout).
    """
    if image_url(url) is None or type(max_bytes) is not int or not 0 < max_bytes <= IMAGE_MAX_BYTES:
        raise MediaFetchError('Недоступный адрес изображения.')
    import requests

    class NoCredentials(requests.auth.AuthBase):
        def __call__(self, request):
            request.headers.pop('Authorization', None)
            request.headers.pop('Cookie', None)
            return request

    class PinnedImageAdapter(requests.adapters.HTTPAdapter):
        def __init__(self, hostname):
            self._image_hostname = hostname
            super().__init__(max_retries=0)

        def init_poolmanager(self, *args, **kwargs):
            kwargs.update(server_hostname=self._image_hostname, assert_hostname=self._image_hostname)
            return super().init_poolmanager(*args, **kwargs)

        def proxy_manager_for(self, proxy, **kwargs):
            kwargs.update(server_hostname=self._image_hostname, assert_hostname=self._image_hostname)
            return super().proxy_manager_for(proxy, **kwargs)

    with requests.Session() as session:
        session.auth = NoCredentials()
        deadline = time.monotonic() + 45
        previous_adapter = None
        for redirect in range(4):
            if cancelled.is_set() or time.monotonic() >= deadline:
                raise MediaFetchError('Загрузка отменена.')
            parsed = urlsplit(url)
            host = parsed.hostname.encode('idna').decode('ascii')
            try:
                addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
                if not addresses or any(not ipaddress.ip_address(row[4][0]).is_global for row in addresses):
                    raise MediaFetchError('Адрес изображения недоступен для просмотра.')
            except (OSError, ValueError):
                raise MediaFetchError('Адрес изображения недоступен для просмотра.') from None
            if cancelled.is_set() or time.monotonic() >= deadline:
                raise MediaFetchError('Загрузка отменена.')
            # Connect (including CONNECT through the configured proxy) to the
            # validated address; keep the original TLS SNI/certificate/Host.
            # A second DNS lookup must not change the destination to a LAN host.
            address = ipaddress.ip_address(addresses[0][4][0])
            authority = f'[{address}]' if address.version == 6 else str(address)
            destination = urlunsplit(('https', authority, parsed.path, parsed.query, ''))
            if previous_adapter is not None:
                previous_adapter.close()
            previous_adapter = PinnedImageAdapter(host)
            session.mount('https://', previous_adapter)
            session.cookies.clear()
            with session.get(destination, stream=True, allow_redirects=False, timeout=(5, 15),
                             headers={'Accept': 'image/*', 'Host': host}) as response:
                if response.status_code in (301, 302, 303, 307, 308):
                    target = urljoin(url, response.headers.get('Location', ''))
                    if redirect == 3 or target == url or image_url(target) is None:
                        raise MediaFetchError('Недоступный адрес изображения.')
                    url = target
                    continue
                if response.status_code != 200:
                    raise MediaFetchError('Не удалось загрузить изображение.')
                mime = response.headers.get('Content-Type', '').split(';', 1)[0].strip().lower()
                if mime not in ('image/jpeg', 'image/png', 'image/webp', 'image/gif', 'image/avif'):
                    raise MediaFetchError('MAX не вернул изображение поддерживаемого формата.')
                length = response.headers.get('Content-Length')
                if length is not None:
                    try:
                        declared = int(length)
                    except (ValueError, OverflowError):
                        raise MediaFetchError('Не подтверждён размер изображения.') from None
                    if declared < 0 or declared > max_bytes:
                        raise MediaFetchError('Изображение превышает предел просмотра.')
                data = bytearray()
                for chunk in response.iter_content(chunk_size=64 * 1024):
                    if cancelled.is_set() or time.monotonic() >= deadline:
                        raise MediaFetchError('Загрузка отменена.')
                    if len(data) + len(chunk) > max_bytes:
                        raise MediaFetchError('Изображение превышает предел просмотра.')
                    data.extend(chunk)
                if cancelled.is_set():
                    raise MediaFetchError('Загрузка отменена.')
                if not data:
                    raise MediaFetchError('Получено пустое изображение.')
                return bytes(data)
    raise MediaFetchError('Не удалось загрузить изображение.')
