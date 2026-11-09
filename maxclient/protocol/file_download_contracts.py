"""Exact current FILE_DOWNLOAD88 request/ACK; no fetching or path handling."""
from __future__ import annotations

from dataclasses import dataclass, field

from .conversation_contracts import explicit_media_url
from .search_contracts import _integer, _number


@dataclass(frozen=True)
class FileDownloadReply:
    url: str = field(repr=False)
    unsafe: bool


def build_file_download(file_id, chat_id, message_id, *, item_type='REGULAR'):
    # Zae uses WEBnumber for fileId, not int64. Never truncate/generate it.
    if not _number(file_id) or not _integer(chat_id, 64) or not _integer(message_id, 64):
        raise ValueError('Actual file/message/chat identifiers are required')
    if not isinstance(item_type, str) or item_type not in ('REGULAR', 'DELAYED'):
        raise ValueError('Unsupported file item type')
    return {'fileId': file_id, 'chatId': chat_id, 'messageId': message_id, 'itemType': item_type}


def parse_file_download(data):
    if not isinstance(data, dict) or type(data.get('unsafe')) is not bool:
        raise ValueError('MAX did not confirm the file safety result')
    url = explicit_media_url(data.get('url'))
    if url is None:
        raise ValueError('MAX did not supply a supported HTTPS file address')
    return FileDownloadReply(url, data['unsafe'])
