"""Иерархия ошибок протокола MAX."""


class MaxError(Exception):
    """Базовая ошибка клиента MAX."""


class MaxNotConnected(MaxError):
    """Сокет не подключён или порвался во время запроса."""


class MaxTimeout(MaxError):
    """Сервер не ответил на запрос за отведённое время."""


class MaxLoginFailed(MaxError):
    """Авторизация не удалась (неверный код, истёкший токен, 2FA и т.п.)."""


class MaxUploadError(MaxError):
    """Сбой на стадии загрузки медиа (нет URL, не-2xx, нет токена в ответе)."""
