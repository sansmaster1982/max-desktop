"""The official InviteLinksModal copies supplied CONFIG.server['invite-long']."""
from __future__ import annotations


INVITE_TEXT_LIMIT = 16384  # Local clipboard/UI bound, not a server-wide limit.


def invite_link_config(server):
    """Retain only a supplied typed invitation; empty explicitly disables it."""
    if not isinstance(server, dict):
        raise ValueError('MAX вернул неверные параметры приглашения')
    if 'invite-long' not in server:
        return {}
    value = server['invite-long']
    if (not isinstance(value, str)
            or len(value.encode('utf-16-le', errors='surrogatepass')) // 2 > INVITE_TEXT_LIMIT
            or any((ord(char) < 32 and char not in '\t\n\r') or 0xD800 <= ord(char) <= 0xDFFF
                   for char in value)):
        raise ValueError('MAX вернул неподтверждённый текст приглашения')
    return {'invite-long': value}


def invite_link_text(server):
    """Return the exact source text, not a fabricated or profile-derived URL."""
    value = invite_link_config(server).get('invite-long', '')
    if not value.strip():
        raise ValueError('MAX пока не предоставил ссылку-приглашение для этой сессии.')
    return value
