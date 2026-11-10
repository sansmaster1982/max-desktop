"""Detached, account-bound conversation information. Read-only RPCs only."""
from __future__ import annotations

import copy
import math
from dataclasses import dataclass, field

from ..protocol.client import AuthenticatedSession
from ..protocol.conversation_contracts import parse_common_chats, explicit_media_url
from .models import Chat, file_message_schedule
from .conversation_action_capabilities import conversation_action_capabilities


class ConversationUnavailable(ValueError):
    pass


@dataclass(frozen=True, eq=False)
class ConversationContext:
    owner_id: int
    source_client: object = field(repr=False)
    session_context: AuthenticatedSession = field(repr=False)
    chat_id: int
    peer_id: int | None


_TABS = {'media': 'PHOTO_VIDEO', 'files': 'FILE', 'voice': 'AUDIO', 'links': 'SHARE'}
_LABELS = {'media': 'Фото или видео', 'files': 'Файл', 'voice': 'Аудио', 'links': 'Ссылка'}
_ACTION_LABELS = {'share_contact': 'Поделиться контактом', 'folder': 'Добавить в папку',
                  'search': 'Найти в чате', 'clear': 'Очистить историю',
                  'hide_stories': 'Не показывать истории', 'show_stories': 'Показывать истории',
                  'leave': 'Выйти из чата', 'report': 'Пожаловаться', 'stop_bot': 'Остановить бота',
                  'block': 'Заблокировать', 'unblock': 'Разблокировать', 'delete': 'Удалить чат'}


def _id(value):
    return type(value) is int and -(2**63) <= value < 2**63


def _finite(value):
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except (ValueError, OverflowError):
        return False


def _text(value):
    return value if isinstance(value, str) else ''


def _participant_id(value):
    if _id(value):
        return value
    if (isinstance(value, str) and value and value.lstrip('-').isascii()
            and value.lstrip('-').isdigit()):
        parsed = int(value)
        return parsed if _id(parsed) else None
    return None


def _peer(chat, raw, owner):
    kind = raw.get('type', chat.chat_type if chat is not None else None)
    if kind != 'DIALOG':
        return None
    cached = chat.peer_user_id if chat is not None else None
    cached = cached if _id(cached) else None
    saved = raw.get('id', chat.id if chat is not None else None) == 0
    if 'participants' not in raw:
        if saved:
            if cached is not None and cached != owner:
                raise ConversationUnavailable('Не подтверждён владелец Избранного.')
            return owner
        return cached  # An explicit owner-scoped cached mapping, never XOR/equal-ID.
    participants = raw['participants']
    if not isinstance(participants, dict):
        raise ConversationUnavailable('Не подтверждён собеседник выбранного чата.')
    if any(not _finite(value) for value in participants.values()):
        raise ConversationUnavailable('Не подтверждены данные участников чата.')
    identities = {_participant_id(value) for value in participants}
    peers = identities - {owner}
    if None in identities or len(peers) > 1:
        raise ConversationUnavailable('Не подтверждён собеседник выбранного чата.')
    # The official DIALOG model uses the viewer when no other participant is
    # present (saved/self conversation); this never derives a chat ID.
    peer = next(iter(peers)) if peers else owner
    if saved and peer != owner:
        raise ConversationUnavailable('Не подтверждён владелец Избранного.')
    if cached is not None and cached != peer:
        raise ConversationUnavailable('Данные собеседника изменились. Откройте карточку заново.')
    return peer


def _contact_name(raw):
    # The same confirmed CUSTOM alias must be shown in list/header/Info.
    # Import locally: MaxService composes this mixin while loading service.py.
    from .service import _person_name
    return _person_name(raw) or ''


class ConversationInfoServiceMixin:
    def conversation_action_status(self, context):
        """Cached control state, including existing folder/contact pending writes."""
        self._require_conversation(context)
        reader = getattr(self, 'get_conversation_action_state', None)
        state = dict(reader(context)) if callable(reader) else {'context': context, 'pending': False}
        with self._metadata_lock:
            folder = getattr(self, '_pending_folder_write', None)
            contact = getattr(self, '_pending_contact_write', None)
            extra = {'folders': bool(folder and folder.get('owner') == context.owner_id),
                     'block': bool(contact and contact.get('owner') == context.owner_id)}
            pending_elsewhere = extra['block'] and contact.get('id') != context.peer_id
        state['menu_pending'] = extra
        state['pending_elsewhere'] = pending_elsewhere
        if pending_elsewhere:
            state['action_status'] = 'Откройте карточку контакта, для которого ожидается подтверждение.'
        state['pending'] = state.get('pending') is True or any(extra.values())
        lifecycle = getattr(self, 'conversation_lifecycle_status', None)
        if callable(lifecycle):
            lifecycle = lifecycle(context)
            state['pending'] = state['pending'] or lifecycle['pending']
            state['lifecycle_pending'] = lifecycle['pending']
            if lifecycle.get('lifecycle_kind') == 'bot_delete':
                state['bot_delete_state'] = lifecycle
            if lifecycle.get('lifecycle_kind') == 'owner_transfer':
                state['owner_transfer_state'] = lifecycle
            if lifecycle.get('lifecycle_kind') == 'conversation_edit':
                state['conversation_edit_state'] = lifecycle
            if lifecycle['pending']:
                state['action_status'] = (lifecycle.get('action_status') or
                                         'Предыдущее действие ещё не подтверждено. Можно проверить состояние.')
        self._require_conversation(context)
        return state

    def refresh_conversation_controls(self, context):
        """Read unresolved outcomes once; never repeat a submitted write."""
        before = self.conversation_action_status(context)
        refresh = getattr(self, 'refresh_conversation_action_state', None)
        if callable(refresh):
            refresh(context)
        lifecycle_result = (self.refresh_conversation_lifecycle(context)
                            if before.get('lifecycle_pending') else None)
        if before['menu_pending']['folders']:
            self.get_conversation_folders(context)
        # Other readbacks may have settled/replaced an owner-wide contact
        # record. Only its actual target can establish that write's outcome.
        current = self.conversation_action_status(context)
        if current['menu_pending']['block'] and not current['pending_elsewhere']:
            self.get_contact_block_status(context.peer_id, context=context)
        self._require_conversation(context)
        result = self.conversation_action_status(context)
        if lifecycle_result is not None:
            result['lifecycle_result'] = lifecycle_result
        return result

    def _conversation_cache(self, context=None, *, chat_id=None):
        try:
            binding = self._history_read_context()
            owner, client, session = binding
            if context is not None:
                if (not isinstance(context, ConversationContext) or not _id(context.owner_id)
                        or (context.peer_id is not None and not _id(context.peer_id)) or context.owner_id != owner
                        or context.source_client is not client or context.session_context is not session):
                    raise ConversationUnavailable('Сессия карточки изменилась.')
                chat_id = context.chat_id
            if not _id(chat_id):
                raise ConversationUnavailable('Не подтверждён выбранный чат.')
            with self._metadata_lock:
                with self.store.account_scope(owner):
                    self._require_history_context(binding)
                    chat = self.store.get_chat(chat_id)
                    raw = copy.deepcopy(self._server_chats.get(chat_id, {}))
                    if (not isinstance(raw, dict) or (raw and (not _id(raw.get('id')) or raw['id'] != chat_id))
                            or ('type' in raw and raw['type'] not in ('DIALOG', 'CHAT', 'CHANNEL'))):
                        raise ConversationUnavailable('Не подтверждены сведения выбранного чата.')
                    if chat is None and not raw:
                        raise ConversationUnavailable('Выбранного чата нет в подтверждённом кэше.')
                    peer = _peer(chat, raw, owner)
                    if context is not None and peer != context.peer_id:
                        raise ConversationUnavailable('Собеседник карточки изменился.')
                    contact = copy.deepcopy(self._server_contacts.get(peer, {})) if peer is not None else {}
                    if not isinstance(contact, dict) or contact and contact.get('id') != peer:
                        raise ConversationUnavailable('Не подтверждены сведения собеседника.')
                    # Every currency check lands here (hundreds per chat-row
                    # popup): read the one keyed row a full scan would find.
                    cached_contact = self.store.get_contact(peer) if peer is not None else None
                    config = self.store.load_config(owner)
                    self._require_history_context(binding)
            return binding, chat, raw, peer, contact, cached_contact, config
        except ConversationUnavailable:
            raise
        except (ValueError, AttributeError, TypeError):
            raise ConversationUnavailable('Сведения в текущей сессии MAX недоступны.') from None

    def conversation_context(self, chat_id, *, activate_files=True):
        if type(activate_files) is not bool:
            raise ValueError('activate_files must be a boolean')
        binding, _chat, _raw, peer, *_rest = self._conversation_cache(chat_id=chat_id)
        context = ConversationContext(binding[0], binding[1], binding[2], chat_id, peer)
        self._require_conversation(context)
        file_scope = getattr(self, '_activate_conversation_files', None)
        if activate_files and callable(file_scope):
            file_scope(context)
        return context

    def _require_conversation(self, context):
        return self._conversation_cache(context)

    def is_current_conversation(self, context):
        try:
            self._require_conversation(context)
            return True
        except ConversationUnavailable:
            return False

    def _conversation_snapshot(self, context, cached, *, raw=None, contact=None):
        binding, chat, stored_raw, peer, stored_contact, cached_contact, config = cached
        fresh_contact = contact is not None
        raw = stored_raw if raw is None else raw
        contact = stored_contact if contact is None else contact
        if _peer(chat, raw, binding[0]) != peer:
            raise ConversationUnavailable('MAX вернул другого собеседника. Карточка не обновлена.')
        locked_title = _text(chat.title) if chat is not None and chat.title_locked else ''
        title = (locked_title or _contact_name(contact) or _text(raw.get('title'))
                 or _text(cached_contact.name if cached_contact is not None else None)
                 or _text(chat.title if chat is not None else None) or f'Чат {context.chat_id}')
        saved = (context.chat_id == 0 and peer == context.owner_id
                 and raw.get('type', chat.chat_type if chat is not None else None) == 'DIALOG')
        own_profile = None
        if saved:
            title = 'Избранное'
            # This is an explicitly requested local extension: public WEB hides
            # the phone in Saved Info. Use only this confirmed cache owner's
            # profile, never the session's last-entered phone or another account.
            with self.store.account_scope(context.owner_id):
                own_profile = self.store.load_profile()
            if own_profile is not None and own_profile.id != context.owner_id:
                own_profile = None
        display_chat = copy.deepcopy(chat) if chat is not None else Chat(id=context.chat_id)
        display_chat.chat_type = raw.get('type', display_chat.chat_type)
        display_chat.peer_user_id = peer
        calls = False
        available = getattr(self, 'calls_available', None)
        if callable(available):
            calls = available(display_chat) is True
        capabilities = conversation_action_capabilities(
            raw, contact, binding[0], config.get('server', {}), peer_id=peer)
        state_reader = getattr(self, 'get_conversation_action_state', None)
        action_state = self.conversation_action_status(context) if callable(state_reader) else {}
        report_reader = getattr(self, 'conversation_report_status', None)
        report_state = report_reader(context) if callable(report_reader) and 'report' in capabilities['more'] else {}
        handlers = {'search': True, 'folder': callable(getattr(self, 'get_conversation_folders', None)),
                    'block': callable(getattr(self, 'set_conversation_contact_blocked', None)),
                    'unblock': callable(getattr(self, 'set_conversation_contact_blocked', None)),
                    'hide_stories': callable(getattr(self, 'set_conversation_stories_hidden', None)),
                    'show_stories': callable(getattr(self, 'set_conversation_stories_hidden', None)),
                    'stop_bot': callable(getattr(self, 'stop_conversation_bot', None))}
        handlers.update(report=(report_state.get('available') is True
                               and callable(getattr(self, 'get_conversation_report_reasons', None))
                               and callable(getattr(self, 'submit_conversation_report', None))),
                        clear=callable(getattr(self, 'clear_conversation_history', None)),
                        delete=capabilities.get('kind') != 'saved' and
                            callable(getattr(self, 'delete_conversation', None)),
                        leave=(capabilities.get('kind') in ('group', 'channel')
                               and raw.get('status') in ('ACTIVE', 'BLOCKED')
                               and capabilities.get('owner') is False
                               and capabilities.get('participant') is True
                               and callable(getattr(self, 'leave_conversation', None))),
                        share_contact=callable(getattr(self, 'share_conversation_contact', None)))
        if capabilities.get('kind') == 'bot':
            delete_gate = getattr(self, 'bot_delete_available', None)
            handlers['delete'] = callable(delete_gate) and delete_gate(context) is True
        menu = [{'action': action, 'label': _ACTION_LABELS[action],
                 'available': handlers.get(action, False), 'write': action not in ('search', 'folder'),
                 'value': action == 'hide_stories' if action in ('hide_stories', 'show_stories') else None}
                for action in capabilities['more']]
        for row in menu:
            if row['action'] == 'delete' and capabilities.get('kind') == 'bot':
                row['label'] = 'Удалить чат' if capabilities.get('suspended') else 'Удалить и остановить'
            if row['action'] == 'report':
                if report_state.get('pending') is True:
                    row['unavailable_reason'] = 'Результат отправки не подтверждён. Повторная отправка остановлена.'
                elif report_state.get('already_reported') is True:
                    row['unavailable_reason'] = 'Жалоба на этот чат уже отправлена.'
            if row['action'] == 'leave':
                if capabilities.get('kind') == 'channel':
                    row['label'] = 'Отписаться от канала'
                if capabilities.get('owner') is True:
                    owner_gate = getattr(self, 'owner_transfer_available', None)
                    row.update(action='transfer_owner_leave', label='Передать права и выйти',
                               available=callable(owner_gate) and owner_gate(context) is True)
                    if not row['available']:
                        row['unavailable_reason'] = 'MAX пока не подтвердил возможность передачи прав.'
        if action_state.get('pending') is True:
            menu.append({'action': 'refresh_actions', 'label': 'Проверить состояние',
                         'available': callable(getattr(self, 'refresh_conversation_controls', None)),
                         'write': False, 'separator_before': True})
        bot_delete_state = action_state.get('bot_delete_state')
        if bot_delete_state and bot_delete_state.get('can_continue_delete') is True:
            menu.append({'action': 'continue_bot_delete', 'label': 'Удалить чат',
                         'available': True, 'write': True, 'separator_before': True})
        owner_transfer_state = action_state.get('owner_transfer_state')
        if owner_transfer_state and owner_transfer_state.get('can_continue_leave') is True:
            menu.append({'action': 'continue_owner_leave', 'label': 'Выйти из чата',
                         'available': True, 'write': True, 'separator_before': True})
        # These descriptors are produced from local labels, never server names.
        # Preserve their source separately so UI locale changes need no reload.
        for row in menu:
            row['label_source'] = row['label']
            if 'unavailable_reason' in row:
                row['unavailable_reason_source'] = row['unavailable_reason']
        edit_mode = None
        edit_available = False
        if capabilities.get('kind') in ('group', 'channel'):
            edit_gate = getattr(self, 'conversation_edit_available', None)
            edit_mode = 'server'
            edit_available = callable(edit_gate) and edit_gate(context) is True
        elif capabilities.get('kind') in ('dialog', 'bot'):
            edit_mode = 'local_contact'
            edit_available = callable(getattr(self, 'rename_conversation', None))
        result = {'context': context, 'chat_id': context.chat_id, 'peer_id': peer, 'title': title,
                  'edit_mode': edit_mode,
                  'availability': {'chat': True, 'find': True, 'call': calls, 'video': calls,
                                   'edit': edit_available,
                                   'mute': capabilities['mute'] and (action_state.get('mute_transport_available') is True
                                                                    or action_state.get('available') is True),
                                   'more': bool(menu)},
                  'tabs': {'common_chats': peer is not None and callable(getattr(binding[1], 'common_chats', None)),
                           **{tab: callable(getattr(binding[1], 'conversation_media', None)) for tab in _TABS}}}
        result.update(more_actions=menu, action_capabilities=capabilities,
                      leave_private=raw.get('access') != 'PUBLIC',
                      action_pending=action_state.get('pending') is True,
                      action_status=(action_state.get('action_status') or 'MAX пока не подтвердил изменение. Можно проверить состояние.'
                                     if action_state.get('pending') is True else ''))
        if action_state.get('pending') is True and not action_state.get('action_status'):
            result['action_status_source'] = 'MAX пока не подтвердил изменение. Можно проверить состояние.'
        if bot_delete_state:
            result['bot_delete_state'] = bot_delete_state
        if owner_transfer_state:
            result['owner_transfer_state'] = owner_transfer_state
        if action_state.get('conversation_edit_state'):
            result['conversation_edit_state'] = action_state['conversation_edit_state']
        # Use the same three-state read projection as folder filtering. A row
        # popup must not turn an incomplete cached zero count into known read.
        # Pin state belongs to a selected folder, so it is not guessed here.
        from .folder_view import _properties
        flags, _read, _muted = _properties(display_chat, raw, {}, binding[0], None, None, {})
        result['row_menu'] = {
            'chat_status': raw.get('status') if raw.get('status') in
                ('ACTIVE', 'LEFT', 'REMOVED', 'CLOSED', 'HIDDEN', 'BLOCKED') else None,
            'unread': flags[0],
        }
        description = contact.get('description') if peer is not None else raw.get('description')
        if saved and own_profile is not None:
            result['profile_name'] = _text(own_profile.name)
            if 'description' not in contact:
                description = own_profile.description
        if isinstance(description, str):
            result['description'] = description
        phone = contact.get('phone') if fresh_contact else contact.get(
            'phone', cached_contact.phone if cached_contact is not None else None)
        if saved and own_profile is not None and 'phone' not in contact:
            phone = own_profile.phone
        if type(phone) is int or isinstance(phone, str):
            result['phone'] = str(phone)
        # No presence is fabricated: CONTACT_INFO32 returns Vh, not presence.
        if callable(state_reader):
            result['muted'] = action_state.get('muted')
            result['dont_disturb_until'] = action_state.get('dont_disturb_until')
        else:
            settings = config.get('chats', {}).get(str(context.chat_id), {})
            mute = settings.get('dontDisturbUntil') if isinstance(settings, dict) else None
            if type(mute) is int and mute in (-1, 0):
                result['muted'] = mute == -1
        self._require_conversation(context)
        return result

    def conversation_snapshot(self, context):
        return self._conversation_snapshot(context, self._require_conversation(context))

    def refresh_conversation(self, context):
        cached = self._require_conversation(context)
        client = context.source_client
        self._require_conversation(context)
        response = client.chat_info([context.chat_id])
        self._require_conversation(context)
        entries = response.get('chats') if isinstance(response, dict) else None
        if entries is None and isinstance(response, dict) and isinstance(response.get('chat'), dict):
            entries = [response['chat']]
        matches = ([row for row in entries if isinstance(row, dict) and
                    type(row.get('id')) is int and row['id'] == context.chat_id]
                   if isinstance(entries, list) else [])
        if len(matches) != 1:
            raise ConversationUnavailable('MAX не подтвердил выбранный чат для карточки.')
        raw = copy.deepcopy(matches[0])
        if 'type' in raw and raw['type'] not in ('DIALOG', 'CHAT', 'CHANNEL'):
            raise ConversationUnavailable('MAX не подтвердил тип выбранного чата.')
        if _peer(cached[1], raw, context.owner_id) != context.peer_id:
            raise ConversationUnavailable('MAX вернул другого собеседника.')
        contact = {}
        if context.peer_id is not None:
            self._require_conversation(context)
            response = client.contact_info([context.peer_id])
            self._require_conversation(context)
            contacts = response.get('contacts') if isinstance(response, dict) else None
            matches = ([row for row in contacts if isinstance(row, dict) and
                        type(row.get('id')) is int and row['id'] == context.peer_id]
                       if isinstance(contacts, list) else [])
            if len(matches) != 1 or not isinstance(matches[0].get('names'), list):
                raise ConversationUnavailable('MAX не подтвердил сведения собеседника.')
            contact = copy.deepcopy(matches[0])
        current_cache = self._require_conversation(context)
        return self._conversation_snapshot(context, current_cache, raw=raw, contact=contact)

    def load_conversation_tab(self, context, tab, marker=None):
        cached = self._require_conversation(context)
        client = context.source_client
        if tab == 'common_chats':
            if context.peer_id is None or not callable(getattr(client, 'common_chats', None)):
                raise ConversationUnavailable('Общие чаты для этого собеседника недоступны.')
            if marker is not None and not _finite(marker):
                raise ConversationUnavailable('Не подтверждён курсор общих чатов.')
            self._require_conversation(context)
            data = client.common_chats(context.peer_id, marker=marker)
            self._require_conversation(context)
            data = parse_common_chats(data)
            items = [{'key': f"chat:{row['id']}", 'title': _text(row.get('title')) or f"Чат {row['id']}",
                      'subtitle': '', 'chat_id': row['id'], 'action': 'chat', 'available': True}
                     for row in data['commonChats']]
            next_marker = data.get('marker')
            complete = not data['hasMore']
            status = ''
            if complete:
                next_marker = None
            if not complete and (next_marker is None or next_marker == marker):
                next_marker = None
                status = 'MAX не передал следующий курсор общих чатов.'
        elif tab in _TABS and callable(getattr(client, 'conversation_media', None)):
            initial = marker is None
            anchor = cached[2].get('lastMessage', {})
            if (not isinstance(anchor, dict) or not _id(anchor.get('id'))
                    or not _finite(anchor.get('time'))
                    or anchor.get('type') not in ('USER', 'CHANNEL', 'CHANNEL_ADMIN')):
                anchor = {}
            request_marker = anchor.get('id') if initial and _id(anchor.get('id')) else marker
            if request_marker is not None and not _id(request_marker):
                raise ConversationUnavailable('Не подтверждён курсор сообщений вкладки.')
            self._require_conversation(context)
            data = client.conversation_media(context.chat_id, _TABS[tab], marker=request_marker, initial=initial)
            self._require_conversation(context)
            items, next_marker, complete, status = self._conversation_media_page(
                context, tab, data, marker=request_marker, initial=initial, anchor=anchor)
        else:
            raise ConversationUnavailable('Эта вкладка сведений пока недоступна.')
        self._require_conversation(context)
        remember_files = getattr(self, '_remember_conversation_files', None)
        if tab == 'files' and callable(remember_files):
            remember_files(context, items, append=marker is not None)
        self._require_conversation(context)
        return {'context': context, 'tab': tab, 'items': items, 'marker': next_marker,
                'complete': complete, 'status': status, 'status_source': status}

    @staticmethod
    def _conversation_media_page(context, tab, data, *, marker, initial, anchor):
        rows = data.get('messages') if isinstance(data, dict) else None
        if not isinstance(rows, list) or len(rows) > 1000:
            raise ConversationUnavailable('MAX не вернул подтверждённые сообщения вкладки.')
        items, identities, times = [], set(), []
        for row in rows:
            if (not isinstance(row, dict) or not _id(row.get('id')) or not _finite(row.get('time'))
                    or row.get('type') not in ('USER', 'CHANNEL', 'CHANNEL_ADMIN') or row['id'] in identities):
                raise ConversationUnavailable('MAX вернул неподтверждённую строку вкладки.')
            identities.add(row['id'])
            times.append(row['time'])
            items.extend(_conversation_attachment_rows(context, tab, row))
            if len(items) > 4000:  # Local UI resource cap, never a server count.
                raise ConversationUnavailable('Слишком много вложений для одной страницы сведений.')
        if not rows:
            return items, None, True, ''
        # The source reverses the returned rows before using its last item as
        # backward marker. Verify chronological order instead of sorting IDs or
        # guessing direction for an unexpected/partial server response.
        if times != sorted(times):
            return items, None, False, 'Порядок сообщений MAX не подтверждён; продолжение недоступно.'
        if initial:
            if not _finite(anchor.get('time')):
                return items, None, False, 'Нет подтверждённого времени начального сообщения.'
            backward_count = sum(timestamp <= anchor['time'] for timestamp in times)
        else:
            backward_count = len(rows)
        complete = backward_count < 25
        next_marker = rows[0]['id'] if not complete else None
        if next_marker == marker:
            return items, None, False, 'MAX не передал новый курсор сообщений.'
        return items, next_marker, complete, ''


def _conversation_attachment_rows(context, tab, row):
    """Detached typed rows; media references never request or construct a URL."""
    source, source_kind = row, 'message'
    link = row.get('link')
    if isinstance(link, dict) and link.get('type') == 'FORWARD':
        source = link.get('message')
        if (not isinstance(source, dict) or not _id(link.get('chatId'))
                or not _id(source.get('id')) or not _finite(source.get('time'))
                or source.get('type') not in ('USER', 'CHANNEL', 'CHANNEL_ADMIN')):
            raise ConversationUnavailable('MAX не подтвердил источник пересланного вложения.')
        source_kind = 'forward'
    attaches = source.get('attaches', [])
    if (not isinstance(attaches, list) or len(attaches) > 1000
            or any(not isinstance(item, dict) or not isinstance(item.get('_type'), str) for item in attaches)):
        raise ConversationUnavailable('MAX вернул неподтверждённые данные вложений.')
    kinds = {'media': ('PHOTO', 'VIDEO'), 'files': ('FILE',), 'voice': ('AUDIO',), 'links': ('SHARE',)}[tab]
    selected = [(index, item) for index, item in enumerate(attaches) if item['_type'] in kinds]
    if tab == 'media':
        selected.reverse()  # Actual mL producer reverses photosVideos per message.
    common = {'chat_id': context.chat_id, 'message_id': row['id'], 'time_ms': row['time'],
              'action': 'message', 'available': True}
    if not selected:
        # Missing/unknown media stays a real message-navigation row. Do not
        # manufacture a photo, thumbnail or attachment identity from its text.
        return [{'key': f"message:{context.chat_id}:{row['id']}",
                 'title': _text(row.get('text')) or _LABELS[tab], 'subtitle': '', **common}]
    result = []
    for index, attach in selected:
        kind = attach['_type']
        labels = {'PHOTO': 'Фото', 'VIDEO': 'Видео', 'FILE': 'Файл', 'AUDIO': 'Аудио', 'SHARE': 'Ссылка'}
        item = {'key': f"attachment:{context.chat_id}:{row['id']}:{index}", 'kind': kind,
                'attachment_index': index, 'attachment_source': source_kind,
                'title': _text(attach.get('name')) or _text(attach.get('title')) or labels[kind],
                'subtitle': '', **common}
        for field in ('width', 'height'):
            if field in attach:
                value = attach[field]
                if type(value) is not int or not 0 <= value < 2**31:
                    raise ConversationUnavailable('MAX не подтвердил размер изображения.')
                item[field] = value
        if 'size' in attach:
            value = attach['size']
            if not _finite(value) or value < 0 or kind == 'VIDEO' and not _id(value):
                raise ConversationUnavailable('MAX не подтвердил размер вложения.')
            item['size'] = value
        if kind in ('AUDIO', 'VIDEO') and 'duration' in attach:
            value = attach['duration']
            if not _finite(value) or value < 0 or kind == 'AUDIO' and (type(value) is not int or value >= 2**31):
                raise ConversationUnavailable('MAX не подтвердил длительность вложения.')
            item['duration_ms'] = value  # Proven wire milliseconds, no rounding.
        if type(attach.get('deleted')) is bool:
            item['deleted'] = attach['deleted']
        if kind == 'FILE':
            if 'fileId' in attach and not _finite(attach['fileId']):
                raise ConversationUnavailable('MAX не подтвердил идентификатор файла.')
            schedule, schedule_valid, _supplied = file_message_schedule(row)
            if not schedule_valid:
                raise ConversationUnavailable('MAX не подтвердил тип сообщения файла.')
            if 'fileId' in attach:
                item['file_id'] = attach['fileId']
            item['file_name'] = _text(attach.get('name'))
            item['item_type'] = 'DELAYED' if schedule > 0 else 'REGULAR'
            item['download_available'] = _finite(attach.get('fileId')) and attach.get('deleted') is not True
        if attach.get('deleted') is not True:
            if kind == 'PHOTO':
                url = explicit_media_url(attach.get('url'))
                if url is not None:
                    item['thumbnail_url'] = item['open_url'] = url
            elif kind == 'VIDEO':
                if (url := explicit_media_url(attach.get('thumbnail'))) is not None:
                    item['thumbnail_url'] = url
            else:
                preview = attach.get('preview' if kind == 'FILE' else 'image') if kind in ('FILE', 'SHARE') else None
                if isinstance(preview, dict) and preview.get('deleted') is not True:
                    url = explicit_media_url(preview.get('url') if preview.get('_type') == 'PHOTO'
                                             else preview.get('thumbnail') if preview.get('_type') == 'VIDEO' else None)
                    if url is not None:
                        item['thumbnail_url'] = url
        result.append(item)
    return result
