"""Detached, account-bound chat previews; no cache or read-receipt mutation."""
from __future__ import annotations

import copy
from dataclasses import dataclass

from .conversation_info_service import (
    ConversationContext, ConversationUnavailable, _contact_name, _finite, _id, _peer, _text,
)
from .models import Message


@dataclass(frozen=True, eq=False)
class ConversationPreviewResult:
    """One fetched window, detached from the normal timeline and local cache.

    Consumers must retain their captured context and discard this result if
    that context is no longer current. Message objects belong to this result,
    never to the service cache. Opening/closing this window sends no receipt.
    """

    context: ConversationContext
    title: str
    messages: tuple[Message, ...]
    anchor: int | float | None


def _metadata(context, cached, raw):
    if (not isinstance(raw, dict) or not _id(raw.get('id'))
            or raw['id'] != context.chat_id
            or 'type' in raw and raw['type'] not in ('DIALOG', 'CHAT', 'CHANNEL')):
        raise ConversationUnavailable('MAX не подтвердил чат для предпросмотра.')
    if _peer(cached[1], raw, context.owner_id) != context.peer_id:
        raise ConversationUnavailable('Собеседник изменился. Предпросмотр закрыт.')
    return copy.deepcopy(raw)


def _anchor(raw):
    last = raw.get('lastMessage')
    return last.get('time') if isinstance(last, dict) and _finite(last.get('time')) else None


def _title(context, cached, raw):
    _binding, chat, _raw, peer, contact, saved_contact, _config = cached
    if (context.chat_id == 0 and peer == context.owner_id
            and raw.get('type', chat.chat_type if chat is not None else None) == 'DIALOG'):
        return 'Избранное'
    return (_text(chat.title) if chat is not None and chat.title_locked else '') or (
        _contact_name(contact) or _text(raw.get('title'))
        or _text(saved_contact.name if saved_contact is not None else None)
        or _text(chat.title if chat is not None else None) or f'Чат {context.chat_id}')


def _messages(context, value):
    if not isinstance(value, list) or len(value) > 1000:
        raise ConversationUnavailable('MAX не вернул подтверждённую историю предпросмотра.')
    result, seen = [], set()
    for raw in copy.deepcopy(value):
        if (not isinstance(raw, dict) or not _id(raw.get('id')) or not _finite(raw.get('time'))
                or raw.get('type') not in ('USER', 'CHANNEL', 'CHANNEL_ADMIN')
                or raw['id'] in seen
                or 'chatId' in raw and (not _id(raw['chatId']) or raw['chatId'] != context.chat_id)
                or 'sender' in raw and not _id(raw['sender'])
                or 'text' in raw and not isinstance(raw['text'], str)
                or 'updateTime' in raw and not _finite(raw['updateTime'])):
            raise ConversationUnavailable('MAX вернул неподтверждённое сообщение предпросмотра.')
        seen.add(raw['id'])
        if raw.get('deleted') is True or raw.get('removed') is True or raw.get('status') == 'REMOVED':
            continue
        for field in ('attaches', 'attachments'):
            if field in raw and (not isinstance(raw[field], list) or len(raw[field]) > 1000
                                 or any(not isinstance(item, dict) for item in raw[field])):
                raise ConversationUnavailable('MAX вернул неподтверждённые вложения предпросмотра.')
        try:
            message = Message.from_server(context.chat_id, raw, context.owner_id)
        except (TypeError, ValueError, OverflowError):
            raise ConversationUnavailable('MAX вернул неподтверждённое сообщение предпросмотра.') from None
        if message.has_display_content:
            result.append(message)
    return sorted(result, key=lambda message: message.time_ms)


class ConversationPreviewServiceMixin:
    def load_conversation_preview(self, context, *, count=20):
        """GET48 if needed, then at most one GET49 on the captured session.

        The anchor is the selected chat's confirmed lastMessage.time, exactly
        as in ordinary latest-history reads. This does not call open_chat,
        hydrate contacts, activate files, update SQL or issue opcode50.
        """
        if type(count) is not int or not 1 <= count <= 50:
            raise ValueError('Предпросмотр поддерживает от 1 до 50 сообщений.')
        cached = self._require_conversation(context)
        raw = cached[2]
        anchor = _anchor(raw)
        if anchor is None:
            response = context.source_client.chat_info(
                [context.chat_id], expected_session=context.session_context)
            cached = self._require_conversation(context)
            rows = response.get('chats') if isinstance(response, dict) else None
            if rows is None and isinstance(response, dict) and isinstance(response.get('chat'), dict):
                rows = [response['chat']]
            matches = ([row for row in rows if isinstance(row, dict)
                        and _id(row.get('id')) and row['id'] == context.chat_id]
                       if isinstance(rows, list) and len(rows) <= 1000 else [])
            if len(matches) != 1:
                raise ConversationUnavailable('MAX не подтвердил выбранный чат для предпросмотра.')
            raw = _metadata(context, cached, matches[0])
            anchor = _anchor(raw)
            if 'lastMessage' in raw and anchor is None:
                raise ConversationUnavailable('MAX не передал время последнего сообщения.')
        else:
            raw = _metadata(context, cached, raw)
        messages = []
        if anchor is not None:
            self._require_conversation(context)
            response = context.source_client.chat_history_window(
                context.chat_id, from_cursor=anchor, forward=0, backward=count,
                get_chat=True, expected_session=context.session_context)
            cached = self._require_conversation(context)
            if not isinstance(response, dict):
                raise ConversationUnavailable('MAX не вернул историю предпросмотра.')
            if ('chatId' in response
                    and (not _id(response['chatId']) or response['chatId'] != context.chat_id)):
                raise ConversationUnavailable('MAX вернул историю другого чата.')
            if 'chat' in response:
                raw = _metadata(context, cached, response['chat'])
            messages = _messages(context, response.get('messages'))
        # Keep the owner/boundary stable through the detached result. A clear
        # completed while GET49 was running must not resurrect its old history.
        with self._metadata_lock, self.store.account_scope(context.owner_id):
            cached = self._require_conversation(context)
            _metadata(context, cached, raw)
            messages = tuple(message for message in messages
                             if not self.store.history_was_cleared(context.chat_id, message.time_ms))[-count:]
            result = ConversationPreviewResult(context, _title(context, cached, raw), messages, anchor)
            self._require_conversation(context)
            return result
