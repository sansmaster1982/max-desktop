"""Captured row/folder pin intents over the established public274 contract.

No method starts authentication. All reads and the single write use the captured
session. A pin is a folder-favorites delta, never a message-pin request.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass

from .conversation_action_capabilities import conversation_action_capabilities
from .conversation_info_service import ConversationContext, ConversationUnavailable
from ..protocol.client import MaxError, MaxWriteNotStarted, MaxWriteOutcomeUnknown
from ..protocol.folder_contracts import folder_dto, folder_ids, folder_payload, int64


@dataclass(frozen=True, eq=False)
class ConversationPinContext:
    conversation: ConversationContext
    folder_id: str


def _same_intent_target(left, right):
    """A fresh read may resolve the same owner's intent after reconnect.

    This compares target identity only. The read's context is independently
    required to be current; it never authorizes reuse of the old session.
    """
    if not isinstance(left, ConversationPinContext) or not isinstance(right, ConversationPinContext):
        return False
    a, b = left.conversation, right.conversation
    return (left.folder_id == right.folder_id and a.owner_id == b.owner_id
            and a.chat_id == b.chat_id and a.peer_id == b.peer_id)


def _limit(cached):
    config = cached[6].get('server', {})
    if not isinstance(config, dict):
        return None
    # Retained WEB _se.get uses nullish fallback to dO; the shipped default
    # max-favorite-chats is 5. Explicit malformed values must not use it.
    value = config.get('max-favorite-chats')
    if value is None:
        return 5
    return value if type(value) is int and 0 <= value <= 1000 else None


def _wire_folder(folder):
    validated = folder_dto(folder)
    original = {'id': validated['id'], 'title': validated['title'],
                **{key: validated.get(key, []) for key in ('include', 'favorites', 'filters', 'options')}}
    payload = folder_payload(original)
    if payload != original:
        raise ValueError('Название папки не позволяет изменить только закрепление чата')
    return payload


def _matches(expected, folder):
    try:
        actual = _wire_folder(folder)
    except ValueError:
        return False
    return all(set(actual[key]) == set(value) if key in ('include', 'filters', 'options')
               else actual[key] == value for key, value in expected.items())


class ConversationPinServiceMixin:
    def capture_conversation_pin(self, context, folder_id):
        """Capture the caller's actual selected folder; no inferred All fallback."""
        if not isinstance(context, ConversationContext):
            raise ConversationUnavailable('Не подтверждён контекст строки чата')
        identity = folder_ids([folder_id])[0]
        result = ConversationPinContext(context, identity)
        self._require_conversation_pin(result)
        return result

    def _require_conversation_pin(self, context):
        if not isinstance(context, ConversationPinContext) or not isinstance(context.conversation, ConversationContext):
            raise ConversationUnavailable('Не подтверждён контекст закрепления')
        folder_ids([context.folder_id])
        cached = self._require_conversation(context.conversation)
        binding, _chat, raw, peer, contact, _cached_contact, config = cached
        capabilities = conversation_action_capabilities(
            raw, contact, binding[0], config.get('server', {}), peer_id=peer)
        # Pin is also available for BLOCKED chat rows; Info folder membership is
        # a different gate. A missing required ChatDTO status is not ACTIVE.
        if (capabilities.get('supported') is not True
                or capabilities.get('kind') not in ('dialog', 'saved', 'bot', 'group', 'channel')
                or raw.get('status') not in ('ACTIVE', 'LEFT', 'REMOVED', 'CLOSED', 'HIDDEN', 'BLOCKED')):
            raise ConversationUnavailable('Закрепление этого чата сейчас недоступно')
        return cached

    def is_current_conversation_pin(self, context):
        try:
            self._require_conversation_pin(context)
            return True
        except (ValueError, TypeError, AttributeError):
            return False

    def conversation_pin_status(self, context):
        """Local menu revalidation only: no folder request or guessed pin state."""
        cached = self._require_conversation_pin(context)
        client = context.conversation.source_client
        pending = self._folder_pending(context.conversation.owner_id)
        limit = _limit(cached)
        available = (pending is None and limit is not None and
                     all(callable(getattr(client, name, None)) for name in
                         ('get_folders', 'get_folder_details', 'update_folder')))
        self._require_conversation_pin(context)
        return {'context': context, 'folder_id': context.folder_id,
                'pending': pending is not None,
                'inflight': bool(pending and pending.get('inflight') is True),
                'available': available, 'limit': limit}

    def _read_conversation_pin_folder(self, context):
        """Read the exact captured folder, never silently replace a missing ID."""
        self._require_conversation_pin(context)
        source = context.conversation
        result = source.source_client.get_folders(folder_sync=None, expected_session=source.session_context)
        self._require_conversation_pin(context)
        if not isinstance(result, dict) or not int64(result.get('folderSync')):
            raise ValueError('MAX не подтвердил список папок')
        order = None
        if 'foldersOrder' in result:
            order = folder_ids(result['foldersOrder'], allow_empty=True)
            if context.folder_id not in order:
                raise ValueError('Выбранная папка больше недоступна')
        values = result.get('folders', [])
        if not isinstance(values, list):
            raise ValueError('MAX вернул неверный список папок')
        rows = [folder_dto(value) for value in values]
        if len({row['id'] for row in rows}) != len(rows):
            raise ValueError('MAX вернул повторные папки')
        folder = next((row for row in rows if row['id'] == context.folder_id), None)
        if folder is None:
            self._require_conversation_pin(context)
            details = source.source_client.get_folder_details(
                [context.folder_id], expected_session=source.session_context)
            self._require_conversation_pin(context)
            if not isinstance(details, dict) or not isinstance(details.get('folders'), list):
                raise ValueError('MAX не подтвердил выбранную папку')
            rows = [folder_dto(value) for value in details['folders']]
            if len(rows) != 1 or rows[0]['id'] != context.folder_id:
                raise ValueError('MAX не подтвердил выбранную папку')
            folder = rows[0]
        self._require_conversation_pin(context)
        return folder

    def _reconcile_conversation_pin(self, context, folder):
        with self._metadata_lock:
            self._require_conversation_pin(context)
            pending = self._folder_pending(context.conversation.owner_id)
            if (pending is not None and pending.get('kind') == 'pin'
                    and pending.get('inflight') is False
                    and _same_intent_target(pending.get('pin_context'), context)
                    and _matches(pending['expected'], folder)):
                self._require_conversation_pin(context)
                if getattr(self, '_pending_folder_write', None) is pending:
                    self._pending_folder_write = None

    def get_conversation_pin_state(self, context):
        """Fresh asynchronous read/readback. Use conversation_pin_status in paint."""
        folder = self._read_conversation_pin_folder(context)
        self._reconcile_conversation_pin(context, folder)
        status = self.conversation_pin_status(context)
        favorites = folder.get('favorites', [])
        pinned = context.conversation.chat_id in favorites
        limit = status['limit']
        can_pin = (status['available'] and limit is not None
                   and len(favorites) + (0 if pinned else 1) <= limit)
        can_unpin = (status['available'] and limit is not None
                     and len(favorites) - (1 if pinned else 0) <= limit)
        try:
            _wire_folder(folder)
        except ValueError:
            can_pin = can_unpin = False
        self._require_conversation_pin(context)
        return {**status, 'pinned': pinned, 'can_pin': can_pin, 'can_unpin': can_unpin,
                'available': can_unpin if pinned else can_pin, 'folder': copy.deepcopy(folder)}

    def set_conversation_pinned(self, context, pinned):
        """One fresh favorites delta and one274, sharing the global folder lock."""
        if type(pinned) is not bool:
            raise ValueError('Нужно указать состояние закрепления')
        self._require_conversation_pin(context)
        lock = self._feature_action_lock('folders')
        if not lock.acquire(blocking=False):
            raise ValueError('Дождитесь текущего изменения папок')
        record = None
        try:
            folder = self._read_conversation_pin_folder(context)
            # A matching post-flight read can settle an earlier uncertain intent;
            # a nonmatching read must never authorize a replacement write.
            self._reconcile_conversation_pin(context, folder)
            if self._folder_pending(context.conversation.owner_id) is not None:
                raise MaxWriteOutcomeUnknown('Предыдущее изменение папок ещё не подтверждено')
            cached = self._require_conversation_pin(context)
            payload = _wire_folder(folder)
            identity = context.conversation.chat_id
            favorites = payload['favorites']
            if (identity in favorites) is pinned:
                return {**self.conversation_pin_status(context), 'pinned': pinned,
                        'accepted': False, 'confirmed': True, 'unchanged': True,
                        'folder': copy.deepcopy(folder)}
            changed = [identity, *favorites] if pinned else [value for value in favorites if value != identity]
            limit = _limit(cached)
            if limit is None:
                raise ValueError('MAX вернул неверный лимит закреплённых чатов')
            if len(changed) > limit:
                raise ValueError('Достигнут лимит закреплённых чатов в этой папке')
            payload['favorites'] = changed
            payload = folder_payload(payload)
            self._require_conversation_pin(context)
            if not callable(getattr(context.conversation.source_client, 'update_folder', None)):
                raise ValueError('Изменение закрепления недоступно в текущем соединении')
            record = {'owner': context.conversation.owner_id, 'kind': 'pin',
                      'expected': copy.deepcopy(payload), 'pin_context': context, 'inflight': True}
            with self._metadata_lock:
                self._pending_folder_write = record
            entered = False
            try:
                self._require_conversation_pin(context)
                entered = True
                context.conversation.source_client.update_folder(
                    payload, expected_session=context.conversation.session_context)
            except MaxWriteOutcomeUnknown:
                with self._metadata_lock:
                    record['inflight'] = False
                raise
            except (MaxError, MaxWriteNotStarted):
                with self._metadata_lock:
                    if getattr(self, '_pending_folder_write', None) is record:
                        self._pending_folder_write = None
                raise
            except Exception as error:
                with self._metadata_lock:
                    record['inflight'] = False
                    if not entered and getattr(self, '_pending_folder_write', None) is record:
                        self._pending_folder_write = None
                if not entered or isinstance(error, MaxWriteOutcomeUnknown):
                    raise
                raise MaxWriteOutcomeUnknown('Результат закрепления неизвестен. Обновите состояние.') from None
            with self._metadata_lock:
                record['inflight'] = False
            try:
                self._require_conversation_pin(context)
                state = self.get_conversation_pin_state(context)
            except Exception:
                raise MaxWriteOutcomeUnknown('Закрепление принято, но чтение результата не завершилось') from None
            return {**state, 'accepted': True, 'confirmed': not state['pending'], 'unchanged': False}
        finally:
            lock.release()
