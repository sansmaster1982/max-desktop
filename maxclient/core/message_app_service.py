"""Ordinary OPEN_APP launch, scoped to a current timeline and literal button."""
from __future__ import annotations

import copy
import json

from ..protocol.miniapp_contracts import MiniAppReply, parse_miniapp_launch
from ..protocol.search_contracts import _integer, _number
from .message_presentation import normalized_button_rows
from .models import Attach, Message


def _selection(message, attachment_index, row_index, button_index):
    if (not isinstance(message, Message) or not _integer(message.chat_id, 64)
            or not _integer(message.id, 64) or not _number(message.time_ms)
            or message.status != 'sent' or message.display_source != 'outer'
            or not isinstance(message.text, str) or not isinstance(message.attaches, list)
            or any(type(index) is not int or index < 0 for index in (attachment_index, row_index, button_index))
            or attachment_index >= len(message.attaches)):
        raise ValueError('Выберите доступную кнопку текущего сообщения')
    attach = message.attaches[attachment_index]
    if (not isinstance(attach, Attach) or attach.type != 'INLINE_KEYBOARD'
            or attach.inline_metadata_confirmed is not True
            or attach.inline_open_app_metadata_confirmed is not True):
        raise ValueError('Обновите сообщение для загрузки параметров мини-приложения')
    rows = normalized_button_rows(attach.inline_buttons)
    if rows is None or row_index >= len(rows) or button_index >= len(rows[row_index]):
        raise ValueError('Кнопка мини-приложения больше недоступна')
    button = rows[row_index][button_index]
    if button['type'] != 'OPEN_APP' or not _integer(button.get('contactId'), 64) or button['contactId'] == 0:
        raise ValueError('MAX не указал приложение для этой кнопки')
    signature = (message.chat_id, message.id, message.time_ms, message.sender,
                 message.text, message.update_time_ms, message.status, message.display_source,
                 tuple(a.type for a in message.attaches if isinstance(a, Attach)),
                 json.dumps(rows, sort_keys=True, ensure_ascii=False))
    return signature, button


def _contact(data, identity):
    values = data.get('contacts') if isinstance(data, dict) else None
    if not isinstance(values, list):
        values = [data['contact']] if isinstance(data, dict) and isinstance(data.get('contact'), dict) else []
    matches = [value for value in values if isinstance(value, dict)
               and type(value.get('id')) is int and value['id'] == identity]
    if len(matches) != 1:
        raise ValueError('MAX не подтвердил бота мини-приложения')
    contact = matches[0]
    flags = contact.get('flags')
    if (type(flags) is not int or not 0 <= flags < 2**31 or flags & 18 != 18
            or contact.get('status') in ('REMOVED', 'BLOCKED', 'HIDDEN')
            or contact.get('accountStatus', 0) != 0):
        raise ValueError('Мини-приложение недоступно у этого бота')
    if not isinstance(contact.get('webApp'), str) or not contact['webApp'] or len(contact['webApp']) > 65536:
        raise ValueError('MAX не подтвердил наличие мини-приложения у бота')
    return contact


def _dialog_ids(data, owner, bot_id):
    # Reuse the existing authoritative participant-map projection, not a title
    # or the selected conversation's ID (a button may open another bot).
    from .service import _extract_chats
    return [chat.id for chat in _extract_chats(data, my_id=owner)
            if chat.chat_type == 'DIALOG' and chat.peer_user_id == bot_id]


class MessageAppServiceMixin:
    def is_current_message_app(self, message, attachment_index, row_index, button_index, *, context):
        try:
            with self._timeline_files_adapter()._local(context):
                selected, _ = _selection(message, attachment_index, row_index, button_index)
                current = self.store.message(context.chat_id, message.id)
                return (message.chat_id == context.chat_id
                        and _selection(current, attachment_index, row_index, button_index)[0] == selected)
        except (ValueError, TypeError, AttributeError):
            return False

    def open_message_app(self, message, attachment_index, row_index, button_index, *, context):
        adapter = self._timeline_files_adapter()
        selected, button = _selection(message, attachment_index, row_index, button_index)
        bot_id, start_param = button['contactId'], button.get('payload')

        def current():
            # Called inside adapter._local: metadata/store/view locks are held.
            if message.chat_id != context.chat_id:
                raise ValueError('Изменился выбранный чат')
            stored = self.store.message(context.chat_id, message.id)
            if (_selection(message, attachment_index, row_index, button_index)[0] != selected
                    or _selection(stored, attachment_index, row_index, button_index)[0] != selected):
                raise ValueError('Сообщение или кнопка изменились во время открытия')

        # Use one reservation for this view; never repeat an ambiguous request.
        with adapter._local(context) as binding:
            current()
            if not hasattr(self, '_message_app_inflight'):
                self._message_app_inflight = set()
            if context in self._message_app_inflight:
                raise ValueError('Мини-приложение уже открывается')
            self._message_app_inflight.add(context)
        try:
            client, session = binding[1], binding[2]
            contact_data = client.contact_info([bot_id], expected_session=session)
            with adapter._local(context):
                current()
                _contact(contact_data, bot_id)
                source_chats = copy.deepcopy(list(self._server_chats.values()))
                dialogs = list(dict.fromkeys(_dialog_ids({'chats': source_chats}, binding[0], bot_id)))
            if len(dialogs) > 1:
                raise ValueError('MAX вернул неоднозначный диалог мини-приложения')
            if dialogs:
                chat_id = dialogs[0]
            else:
                # Exact native U.resolveChatId(viewerId, botId) candidate. It is
                # never used as permission until CHAT_INFO proves its peer.
                candidate = binding[0] ^ bot_id
                info = client.chat_info([candidate], expected_session=session)
                with adapter._local(context):
                    current()
                    confirmed = _dialog_ids(info, binding[0], bot_id)
                    if confirmed != [candidate]:
                        raise ValueError('MAX не подтвердил диалог бота мини-приложения')
                chat_id = candidate
            with adapter._local(context):
                current()
            reply = client.open_bot_app(bot_id, chat_id, start_param, expected_session=session)
            if not isinstance(reply, MiniAppReply):
                raise ValueError('MAX не подтвердил адрес мини-приложения')
            validated = parse_miniapp_launch({'url': reply.url, 'query_id': reply.query_id})
            with adapter._local(context):
                current()
                return {'url': validated.url, 'origin': validated.origin, 'query_id': validated.query_id}
        finally:
            with adapter._lock:
                self._message_app_inflight.discard(context)
