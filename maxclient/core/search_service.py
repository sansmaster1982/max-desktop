"""Owner-bound search reads. Results are detached from joined-chat history/cache."""
import re

from ..protocol.client import AuthenticatedSession


class SearchUnavailable(ValueError):
    pass


def _text(value):
    return value if isinstance(value, str) else ''


def _fold(value):
    """Case-insensitive comparison key; the Cyrillic yo/ye pair is one letter."""
    return _text(value).casefold().replace('ё', 'е')


def _phone_needles(query, minimum=1):
    """Digit variants of a query typed as a phone (+7/8, spaces, dashes, brackets)."""
    if not isinstance(query, str) or re.search(r'[^\d\s()+.\-]', query):
        return ()
    digits = re.sub(r'\D', '', query)
    if len(digits) < minimum:
        return ()
    variants = [digits]
    if digits[0] == '8' and len(digits) > 1 and not query.lstrip().startswith('+'):
        variants.append('7' + digits[1:])  # Domestic 8 prefix of a stored +7 number.
    return tuple(variants)


def contact_matches(query, display, phone, *, minimum_digits=1):
    """Return 'name', 'phone' or None for one address-book row; no I/O."""
    needle = _fold(query).strip()
    if needle and needle in _fold(display):
        return 'name'
    digits = re.sub(r'\D', '', phone) if isinstance(phone, str) else ''
    if digits and any(value in digits for value in _phone_needles(query, minimum_digits)):
        return 'phone'
    return None


def _contact_name(contact):
    names = contact.get('names', [])
    ordered = sorted(names, key=lambda row: 0 if isinstance(row, dict) and row.get('type') == 'CUSTOM' else 1)
    for row in ordered:
        if isinstance(row, dict):
            value = _text(row.get('name')) or ' '.join(
                part for key in ('firstName', 'lastName') if (part := _text(row.get(key))))
            if value.strip():
                return value
    return _text(contact.get('name'))


def _item(*, section, chat_id=None, message=None, contact=None, chat=None, cached=None):
    """Only actual returned IDs are navigation targets; no userId/chatId XOR."""
    contact = contact or {}
    chat = chat or {}
    message = message or {}
    user_id = contact.get('id')
    chat_id = chat_id if chat_id is not None else chat.get('id')
    message_id = message.get('id')
    if message_id is not None and chat_id is not None:
        key = f'message:{chat_id}:{message_id}'
    elif chat_id is not None:
        key = f'chat:{chat_id}'
    elif user_id is not None:
        key = f'contact:{user_id}'
    else:
        return None
    title = (_text(getattr(cached, 'title', None)) or _text(chat.get('title')) or
             _contact_name(contact) or _text(getattr(cached, 'display', None)) or
             (f'Чат {chat_id}' if chat_id is not None else f'Контакт {user_id}'))
    last_message = chat.get('lastMessage') if isinstance(chat.get('lastMessage'), dict) else {}
    preview = _text(message.get('text')) or _text(last_message.get('text'))
    return {'key': key, 'section': section, 'title': title, 'preview': preview,
            'chat_id': chat_id, 'message_id': message_id, 'user_id': user_id,
            'time_ms': message.get('time')}


class SearchServiceMixin:
    def search_context(self):
        """Opaque in-memory context used by the UI to discard old query results."""
        client = self.client
        context = getattr(client, 'authenticated_session', None)
        try:
            if (not isinstance(context, AuthenticatedSession) or context.device_type != 'WEB' or
                    context.owner_id != self._confirmed_profile_id() or
                    self.store.cache_owner != context.owner_id or
                    self.session.device_id != context.device_id or
                    client is not self.client or getattr(client, 'authenticated_session', None) is not context):
                raise SearchUnavailable('Для поиска нужно подключение к подтверждённой сессии MAX.')
            return context
        except SearchUnavailable:
            raise
        except Exception:
            raise SearchUnavailable('Поиск в текущей сессии MAX недоступен.') from None

    def _check_search_context(self, client, context):
        if client is not self.client or self.search_context() is not context:
            raise SearchUnavailable('Сессия изменилась. Результаты поиска не применены.')

    def _local_contact_hits(self, query):
        """Address-book rows of this owner matched by saved name or phone.

        The server search knows neither a local alias nor a saved number, and a
        contact without a sidebar row has no chat title to match. This is a
        cache read: the hit keeps its actual user ID and gets a chat ID only
        from an existing confirmed dialog. No chat, contact or message is made.
        """
        lister = getattr(self, 'list_contacts', None)
        resolver = getattr(self, 'dialog_for_contact', None)
        if not callable(lister):
            return []
        try:
            contacts = lister()
        except (AttributeError, ValueError):
            return []
        rows = []
        for contact in contacts:
            identity = getattr(contact, 'id', None)
            if type(identity) is not int or not -(2**63) <= identity < 2**63:
                continue
            title = _text(getattr(contact, 'display', None))
            phone = _text(getattr(contact, 'phone', None))
            matched = contact_matches(query, title, phone, minimum_digits=4)
            if matched is None:
                continue
            try:
                dialog = resolver(identity) if callable(resolver) else None
            except (AttributeError, ValueError):
                dialog = None
            preview = phone if matched == 'phone' else ''
            if dialog is not None:
                row = _item(section='CHATS', chat_id=dialog.id, cached=dialog)
                row['preview'] = preview
            else:
                row = {'key': f'contact:{identity}', 'section': 'CONTACTS',
                       'title': title or f'Контакт {identity}', 'preview': preview,
                       'chat_id': None, 'message_id': None, 'user_id': identity, 'time_ms': None}
            rows.append(row)
            if len(rows) >= 20:
                break
        return rows

    def search_all(self, query, *, marker=None, context=None):
        """Actual WEB initial68+60; only68 cursor is followed on later pages."""
        if not isinstance(query, str):
            raise SearchUnavailable('Введите текст для поиска.')
        query = query.strip()
        client = self.client
        current = self.search_context()
        if context is not None and context is not current:
            raise SearchUnavailable('Сессия поиска изменилась.')
        context = current
        if not query:
            return {'query': '', 'context': context, 'items': [], 'total': 0,
                    'counters': {}, 'marker': None, 'unavailable_count': 0}
        # A search hit is not a joined chat and cannot overwrite local account
        # history, contacts or membership simply by appearing in this response.
        known = {chat.id: chat for chat in self.list_chats()}
        self._check_search_context(client, context)
        messages = client.search_messages(query, count=30, marker=marker)
        self._check_search_context(client, context)
        public = {'result': []}
        if marker is None:
            public = client.search_global(query, count=40, search_type='ALL')
            self._check_search_context(client, context)
        items = []
        if marker is None:
            folded = _fold(query)
            for chat in known.values():
                if folded in _fold(chat.display):
                    items.append(_item(section='CHATS', chat_id=chat.id, cached=chat))
            items.extend(self._local_contact_hits(query))
        unavailable = 0
        for row in messages['result']:
            if row.get('unavailable'):
                unavailable += 1
                continue
            identity = row['chatId']
            section = row.get('section') or ('MESSAGES' if row.get('message') else 'CHATS')
            items.append(_item(section=section, chat_id=identity, message=row.get('message'),
                               chat=row.get('chat'), cached=known.get(identity)))
        for row in public['result']:
            if row.get('unavailable'):
                unavailable += 1
                continue
            contact = row.get('contact', {}).get('contact') if isinstance(row.get('contact'), dict) else None
            chat = row.get('chat') or {}
            if contact:
                section = 'BOTS' if contact.get('flags', 0) & 2 else 'CONTACTS'
            else:
                section = 'PUBLIC_CHATS'
            items.append(_item(section=section, contact=contact, chat=chat, message=row.get('message'),
                               cached=known.get(chat.get('id'))))
        unique = {}
        for item in items:
            if item is not None and item['key'] not in unique:
                unique[item['key']] = item
        self._check_search_context(client, context)
        return {'query': query, 'context': context, 'items': list(unique.values()),
                'total': messages['total'], 'counters': messages.get('counters', {}),
                'marker': messages.get('marker'), 'unavailable_count': unavailable}
