"""Owner-bound folder/contact actions with authoritative readback, no retries."""
from __future__ import annotations

import copy
import threading
import uuid

from ..protocol.client import AuthenticatedSession, MaxError, MaxWriteNotStarted, MaxWriteOutcomeUnknown
from ..protocol.folder_contracts import (
    ALL_FOLDER, contact_dto, folder_dto, folder_ids, folder_payload, folder_title, int64,
)


class FolderBlockServiceMixin:
    def _feature_action_lock(self, name):
        with self._metadata_lock:
            locks = getattr(self, '_folder_block_locks', None)
            if locks is None:
                self._folder_block_locks = locks = {}
            return locks.setdefault(name, threading.Lock())

    def _assert_action_owner(self, owner):
        if self._confirmed_profile_id() != owner:
            raise ValueError('Аккаунт изменился во время операции')

    def _folder_pending(self, owner):
        with self._metadata_lock:
            pending = getattr(self, '_pending_folder_write', None)
            return pending if pending and pending['owner'] == owner else None

    def _reconcile_folder_snapshot(self, owner, snapshot):
        pending = self._folder_pending(owner)
        if not pending:
            return
        expected = pending['expected']
        actual = {folder['id']: folder for folder in snapshot.get('folders', [])}
        if pending['kind'] == 'upsert':
            folder = actual.get(expected['id'])
            def matches(key, value):
                actual_value = folder.get(key, [] if isinstance(value, list) else None)
                return set(actual_value) == set(value) if key in ('include', 'filters', 'options') else actual_value == value
            matched = folder is not None and all(matches(key, value) for key, value in expected.items())
        elif pending['kind'] == 'delete':
            matched = ('foldersOrder' in snapshot and expected not in snapshot['foldersOrder'] and expected not in actual)
        else:
            matched = snapshot.get('foldersOrder') == expected
        if matched:
            with self._metadata_lock:
                if getattr(self, '_pending_folder_write', None) is pending:
                    self._pending_folder_write = None

    def create_folder(self, title, *, include=None, filters=None, context=None):
        try:
            payload = folder_payload({'id': str(uuid.uuid4()), 'title': title,
                                      'include': [] if include is None else include,
                                      'filters': [] if filters is None else filters, 'options': []})
            return self._folder_action('upsert', create=payload, editor_context=context)
        except ValueError as error:
            if context is None:
                raise
            raise MaxWriteNotStarted(str(error)) from None

    def update_folder(self, folder_id, *, title=None, include=None, filters=None, favorites=None, context=None):
        try:
            identity = folder_ids([folder_id])[0]
            changes = copy.deepcopy({key: value for key, value in (('title', title), ('include', include),
                       ('filters', filters), ('favorites', favorites)) if value is not None})
            return self._folder_action('upsert', folder_id=identity, changes=changes, editor_context=context)
        except ValueError as error:
            if context is None:
                raise
            raise MaxWriteNotStarted(str(error)) from None

    def delete_folder(self, folder_id, *, context=None):
        try:
            return self._folder_action('delete', folder_id=folder_ids([folder_id])[0], editor_context=context)
        except ValueError as error:
            if context is None:
                raise
            raise MaxWriteNotStarted(str(error)) from None

    def reorder_folders(self, folder_ids, *, context=None, expected_order=None):
        """Move a complete order, optionally bound to the UI gesture's snapshot.

        The bound path requires both the captured authenticated context and the
        previous complete order. Neither guard is an additional wire field.
        Bound preflight validation failures are typed as not started; failures
        after entering the write wrapper are classified inside _folder_action.
        """
        from ..protocol.folder_contracts import folder_ids as validate_ids
        bound = context is not None or expected_order is not None
        try:
            if (context is None) != (expected_order is None):
                raise ValueError('Нужно подтвердить сессию и исходный порядок папок')
            if context is not None:
                self._require_folder_order_context(context)
                if not isinstance(expected_order, (list, tuple)):
                    raise ValueError('Нужен полный исходный порядок папок')
                expected_order = validate_ids(list(expected_order))
                if expected_order[0] != ALL_FOLDER:
                    raise ValueError('Нужно оставить папку «Все» первой')
            return self._folder_action('order', order=validate_ids(folder_ids),
                                       order_context=context, expected_order=expected_order)
        except MaxWriteNotStarted:
            raise
        except ValueError as error:
            if not bound:
                raise  # Legacy callers retain their established exception type.
            raise MaxWriteNotStarted(str(error)) from None

    def _require_folder_order_context(self, context):
        if (not isinstance(context, tuple) or len(context) != 3
                or not isinstance(context[2], AuthenticatedSession)
                or context[2].owner_id != context[0]):
            raise ValueError('Не подтверждена сессия изменения порядка папок')
        return self._require_history_context(context)

    def _get_reorder_folders(self, context):
        """Detached, context-bound 272/273 read; no inferred empty delta fields."""
        from .service import _folder_from_data
        client = self._require_folder_order_context(context)
        result = client.get_folders(folder_sync=None, expected_session=context[2])
        self._require_folder_order_context(context)
        if not isinstance(result, dict) or not int64(result.get('folderSync')):
            raise ValueError('MAX не вернул подтверждённый список папок')
        output = {'folderSync': result['folderSync']}
        folders = []
        if 'folders' in result:
            if not isinstance(result['folders'], list):
                raise ValueError('MAX вернул неверные сведения о папках')
            folders = [_folder_from_data(folder) for folder in result['folders']]
            if len({folder['id'] for folder in folders}) != len(folders):
                raise ValueError('MAX вернул повторные сведения о папке')
            output['folders'] = folders
        for field in ('foldersOrder', 'allFilterExcludeFolders'):
            if field in result:
                output[field] = folder_ids(result[field], allow_empty=True)
        if 'foldersOrder' in output:
            by_id = {folder['id']: folder for folder in folders}
            missing = [identity for identity in output['foldersOrder'] if identity not in by_id]
            if missing:
                self._require_folder_order_context(context)
                response = client.get_folder_details(missing, expected_session=context[2])
                self._require_folder_order_context(context)
                if not isinstance(response, dict) or not isinstance(response.get('folders'), list):
                    raise ValueError('MAX не подтвердил сведения о выбранных папках')
                resolved = [_folder_from_data(folder) for folder in response['folders']]
                if (any(folder['id'] not in missing for folder in resolved)
                        or len({folder['id'] for folder in resolved}) != len(resolved)):
                    raise ValueError('MAX вернул сведения о другой папке')
                by_id.update((folder['id'], folder) for folder in resolved)
                output['folders'] = folders + resolved
            output['orderedFolders'] = [by_id.get(identity) for identity in output['foldersOrder']]
            output['missingFolderIds'] = [identity for identity in missing if identity not in by_id]
        self._require_folder_order_context(context)
        self._reconcile_folder_snapshot(context[0], output)
        output['writePending'] = self._folder_pending(context[0]) is not None
        self._require_folder_order_context(context)
        return output

    def _require_conversation_folder(self, context):
        from .conversation_action_capabilities import conversation_action_capabilities
        cached = self._require_conversation(context)
        binding, _chat, raw, peer, contact, _cached_contact, config = cached
        capabilities = conversation_action_capabilities(
            raw, contact, binding[0], config.get('server', {}), peer_id=peer)
        if 'folder' not in capabilities.get('more', ()):
            raise ValueError('Папки для этой карточки сейчас недоступны')
        return cached

    def get_conversation_folders(self, context):
        """One context-bound detached folder read; never changes inclusion."""
        from .service import _folder_from_data
        self._require_conversation_folder(context)
        client = context.source_client
        result = client.get_folders(folder_sync=None, expected_session=context.session_context)
        self._require_conversation_folder(context)
        if not isinstance(result, dict) or not int64(result.get('folderSync')):
            raise ValueError('MAX не вернул подтверждённый список папок')
        output = {'folderSync': result['folderSync'], 'context': context}
        folders = []
        if 'folders' in result:
            if not isinstance(result['folders'], list):
                raise ValueError('MAX вернул неверные сведения о папках')
            folders = [_folder_from_data(folder) for folder in result['folders']]
            if len({folder['id'] for folder in folders}) != len(folders):
                raise ValueError('MAX вернул повторные сведения о папке')
            output['folders'] = folders
        for field in ('foldersOrder', 'allFilterExcludeFolders'):
            if field in result:
                values = folder_ids(result[field], allow_empty=True)
                output[field] = values
        if 'foldersOrder' in output:
            by_id = {folder['id']: folder for folder in folders}
            missing = [identity for identity in output['foldersOrder'] if identity not in by_id]
            if missing:
                self._require_conversation_folder(context)
                response = client.get_folder_details(missing, expected_session=context.session_context)
                self._require_conversation_folder(context)
                if not isinstance(response, dict) or not isinstance(response.get('folders'), list):
                    raise ValueError('MAX не подтвердил сведения о выбранных папках')
                resolved = [_folder_from_data(folder) for folder in response['folders']]
                if (any(folder['id'] not in missing for folder in resolved)
                        or len({folder['id'] for folder in resolved}) != len(resolved)):
                    raise ValueError('MAX вернул сведения о другой папке')
                by_id.update((folder['id'], folder) for folder in resolved)
                output['folders'] = folders + resolved
            output['orderedFolders'] = [by_id.get(identity) for identity in output['foldersOrder']]
            output['missingFolderIds'] = [identity for identity in missing if identity not in by_id]
        self._require_conversation_folder(context)
        self._reconcile_folder_snapshot(context.owner_id, output)
        output['writePending'] = self._folder_pending(context.owner_id) is not None
        self._require_conversation_folder(context)
        return output

    def set_conversation_folder_included(self, context, folder_id, included):
        """Apply one explicit membership delta to fresh data under the folder lock."""
        identity = folder_ids([folder_id])[0]
        if type(included) is not bool or identity == ALL_FOLDER:
            raise ValueError('Нужно выбрать обычную папку и состояние включения чата')
        self._require_conversation_folder(context)
        return self._folder_action('upsert', folder_id=identity, changes={},
                                   context=context, membership=included)

    def _folder_action(self, kind, *, create=None, folder_id=None, changes=None, order=None,
                       context=None, membership=None, order_context=None, expected_order=None,
                       editor_context=None):
        if sum(value is not None for value in (context, order_context, editor_context)) > 1:
            raise ValueError('Неоднозначный контекст изменения папки')
        if editor_context is not None and kind not in ('upsert', 'delete'):
            raise ValueError('Неподтверждённое действие редактора папок')
        binding = order_context if order_context is not None else editor_context
        envelope = {'editor_context': editor_context} if editor_context is not None else {}
        def read_snapshot():
            snapshot = (self._get_reorder_folders(binding) if binding is not None else
                        self.get_folders() if context is None else self.get_conversation_folders(context))
            if editor_context is not None:
                snapshot['editor_context'] = editor_context
            return snapshot
        if context is not None and (kind != 'upsert' or create is not None
                or folder_id == ALL_FOLDER or type(membership) is not bool):
            raise ValueError('Неподтверждённое изменение папки из карточки')
        lock = self._feature_action_lock('folders')
        if not lock.acquire(blocking=False):
            raise ValueError('Дождитесь текущего изменения папок')
        try:
            if context is not None:
                self._require_conversation_folder(context)
            if binding is not None:
                self._require_folder_order_context(binding)
            owner = self._confirmed_profile_id()
            if binding is not None:
                with self._metadata_lock:
                    previous = getattr(self, '_pending_folder_write', None)
                    if previous is not None and previous.get('owner') != owner:
                        raise MaxWriteNotStarted('Результат изменения папок другого профиля ещё не подтверждён')
            snapshot = read_snapshot()
            if context is not None:
                self._require_conversation_folder(context)
            if self._folder_pending(owner):
                raise MaxWriteOutcomeUnknown('Предыдущее изменение папок ещё не подтверждено. Обновите список.')
            actual = {folder['id']: folder for folder in snapshot.get('folders', [])}
            if kind == 'upsert':
                if create is not None:
                    payload = create
                else:
                    if folder_id not in actual:
                        raise ValueError('MAX не вернул выбранную папку')
                    folder = folder_dto(actual[folder_id])
                    if context is not None:
                        view = self.get_folder_view(folder)
                        self._require_conversation_folder(context)
                        if isinstance(view, dict) and view.get('hidden') is True:
                            raise ValueError('Эта папка сейчас скрыта в MAX')
                        # Calculate from this read, not the menu's older snapshot.
                        # Source checkmarks use explicit include, not filter matches.
                        include = list(folder.get('include', []))
                        if membership and context.chat_id not in include:
                            include.append(context.chat_id)
                        elif membership is False:
                            include = [identity for identity in include if identity != context.chat_id]
                        changes = {'include': include}
                    options = folder.get('options', [])
                    if ('title' in changes and (folder_id == ALL_FOLDER or 2 in options)
                            and folder_title(changes['title']) != folder['title']):
                        raise ValueError('MAX запретил изменение названия этой папки')
                    if 'filters' in changes and 3 in options and changes['filters'] != folder.get('filters', []):
                        raise ValueError('MAX запретил изменение фильтров этой папки')
                    # These [] defaults are explicit in qA and its real274 producer.
                    payload = {'id': folder_id, 'title': folder['title'],
                               **{key: folder.get(key, []) for key in ('include', 'filters', 'options', 'favorites')}}
                    original = copy.deepcopy(payload)
                    payload.update(changes)
                    payload = folder_payload(payload)
                    if context is not None and payload['title'] != original['title']:
                        raise ValueError('Формат названия папки не позволяет сохранить только состав чатов')
                    if payload == original:
                        if context is not None:
                            self._require_conversation_folder(context)
                        if binding is not None:
                            self._require_folder_order_context(binding)
                        return {'accepted': False, 'pending': False, 'unchanged': True,
                                'folderId': folder_id, 'snapshot': snapshot,
                                **envelope,
                                **({'context': context} if context is not None else {})}
                expected = payload
                identity = payload['id']
                if editor_context is not None:
                    operation = lambda: editor_context[1].update_folder(payload, expected_session=editor_context[2])
                else:
                    operation = (lambda: self.client.update_folder(payload)) if context is None else (
                        lambda: context.source_client.update_folder(payload, expected_session=context.session_context))
            elif kind == 'delete':
                if folder_id == ALL_FOLDER or folder_id not in actual or 1 in actual[folder_id].get('options', []):
                    raise ValueError('MAX не разрешил удаление выбранной папки')
                expected, identity = folder_id, folder_id
                operation = (lambda: self.client.delete_folders([folder_id])) if editor_context is None else (
                    lambda: editor_context[1].delete_folders([folder_id], expected_session=editor_context[2]))
            else:
                current = snapshot.get('foldersOrder')
                if order_context is not None and current != expected_order:
                    raise ValueError('Порядок папок изменился. Обновите список перед перемещением.')
                if (not isinstance(current, list) or not order or order[0] != ALL_FOLDER
                        or set(order) != set(current) or len(order) != len(current)):
                    raise ValueError('Нужно сохранить все папки и оставить «Все» первой')
                if order == current:
                    return {'accepted': False, 'pending': False, 'unchanged': True, 'snapshot': snapshot}
                expected, identity = order, None
                operation = (lambda: self.client.reorder_folders(order)) if order_context is None else (
                    lambda: order_context[1].reorder_folders(order, expected_session=order_context[2]))
            self._assert_action_owner(owner)
            if context is not None:
                self._require_conversation_folder(context)
            if binding is not None:
                self._require_folder_order_context(binding)
            pending = {'owner': owner, 'kind': kind, 'expected': copy.deepcopy(expected)}
            with self._metadata_lock:
                self._pending_folder_write = pending
            operation_started = False
            try:
                if context is not None:
                    self._require_conversation_folder(context)
                if binding is not None:
                    self._require_folder_order_context(binding)
                operation_started = True
                operation()  # Wrapper validates ACK. Its input is never installed as a readback.
            except MaxWriteNotStarted:
                with self._metadata_lock:
                    if getattr(self, '_pending_folder_write', None) is pending:
                        self._pending_folder_write = None
                raise
            except MaxWriteOutcomeUnknown:
                raise
            except MaxError:
                with self._metadata_lock:
                    if getattr(self, '_pending_folder_write', None) is pending:
                        self._pending_folder_write = None  # Explicit negative ACK, no hidden retry.
                raise
            except Exception:
                if (context is not None or binding is not None) and not operation_started:
                    with self._metadata_lock:
                        if getattr(self, '_pending_folder_write', None) is pending:
                            self._pending_folder_write = None
                    raise  # Context/capability changed before entering a write wrapper.
                raise MaxWriteOutcomeUnknown('Результат изменения папок неизвестен. Обновите список.') from None
            try:
                self._assert_action_owner(owner)
                if binding is not None:
                    self._require_folder_order_context(binding)
                refreshed = read_snapshot()
                self._assert_action_owner(owner)
                if context is not None:
                    self._require_conversation_folder(context)
                if binding is not None:
                    self._require_folder_order_context(binding)
            except Exception:
                raise MaxWriteOutcomeUnknown('Изменение папок принято, но чтение результата не завершилось. Обновите список.') from None
            result = {'accepted': True, 'pending': self._folder_pending(owner) is not None,
                      'snapshot': refreshed, **envelope}
            if identity is not None:
                result['folderId'] = identity
            if context is not None:
                result['context'] = context
            return result
        finally:
            lock.release()

    def _require_conversation_contact(self, context):
        from .conversation_action_capabilities import conversation_action_capabilities
        cached = self._require_conversation(context)
        binding, _chat, raw, peer, contact, _cached_contact, config = cached
        capabilities = conversation_action_capabilities(
            raw, contact, binding[0], config.get('server', {}), peer_id=peer)
        if (peer is None or peer == binding[0]
                or not {'block', 'unblock'} & set(capabilities.get('more', ()))):
            raise ValueError('Блокировка для этой карточки сейчас недоступна')
        return cached

    def set_conversation_contact_blocked(self, context, blocked):
        if type(blocked) is not bool:
            raise ValueError('Нужно указать состояние блокировки')
        self._require_conversation_contact(context)
        return self._contact_block_action(context.peer_id, blocked, context=context)

    def list_blocked_contacts(self, *, context=None):
        cached = self._require_conversation_contact(context) if context is not None else None
        owner = self._confirmed_profile_id()
        result = (self.client.list_blocked_contacts() if context is None else
                  context.source_client.list_blocked_contacts(expected_session=context.session_context))
        if context is not None:
            self._require_conversation_contact(context)
        if not isinstance(result, dict) or not isinstance(result.get('contacts'), list):
            raise ValueError('MAX не вернул список заблокированных контактов')
        contacts = [contact_dto(item) for item in result['contacts']]
        if any('status' in item and item['status'] != 'BLOCKED' for item in contacts):
            raise ValueError('MAX вернул контакт с другим статусом')
        self._assert_action_owner(owner)
        if context is None:
            self._remember_contact_info({'contacts': contacts})
        else:
            self._history_cache_write(cached[0], lambda: self._remember_contact_info({'contacts': contacts}))
            self._require_conversation_contact(context)
        return {'contacts': contacts, 'from': 0, 'count': 100, 'pagination': 'first_page_only'}

    def get_contact_block_status(self, contact_id, *, context=None):
        if not int64(contact_id):
            raise ValueError('Нужен идентификатор контакта MAX')
        cached = self._require_conversation_contact(context) if context is not None else None
        if context is not None and contact_id != context.peer_id:
            raise ValueError('Идентификатор контакта не совпадает с карточкой')
        owner = self._confirmed_profile_id()
        result = (self.client.contact_info([contact_id]) if context is None else
                  context.source_client.contact_info([contact_id], expected_session=context.session_context))
        if context is not None:
            self._require_conversation_contact(context)
        values = result.get('contacts') if isinstance(result, dict) else None
        if not isinstance(values, list):
            raise ValueError('MAX не вернул запрошенный контакт')
        contacts = [contact_dto(item) for item in values]
        matching = [item for item in contacts if item['id'] == contact_id]
        if len(matching) != 1:
            raise ValueError('MAX не подтвердил идентификатор контакта')
        contact = matching[0]
        self._assert_action_owner(owner)
        if context is None:
            self._remember_contact_info({'contacts': [contact]})
        else:
            self._history_cache_write(cached[0], lambda: self._remember_contact_info({'contacts': [contact]}))
            self._require_conversation_contact(context)
        # Current U.status maps an absent optional status to null/normal.
        blocked = contact.get('status') == 'BLOCKED'
        with self._metadata_lock:
            pending = getattr(self, '_pending_contact_write', None)
            if pending and pending == {'owner': owner, 'id': contact_id, 'blocked': blocked}:
                self._pending_contact_write = None
        return {'contactId': contact_id, 'blocked': blocked, 'contact': contact}

    def block_contact(self, contact_id):
        return self._contact_block_action(contact_id, True)

    def unblock_contact(self, contact_id):
        return self._contact_block_action(contact_id, False)

    def _contact_block_action(self, contact_id, blocked, *, context=None):
        if not int64(contact_id):
            raise ValueError('Нужен идентификатор контакта MAX')
        lock = self._feature_action_lock('blocked')
        if not lock.acquire(blocking=False):
            raise ValueError('Дождитесь текущего изменения блокировки')
        try:
            if context is not None:
                self._require_conversation_contact(context)
                if contact_id != context.peer_id:
                    raise ValueError('Идентификатор контакта не совпадает с карточкой')
            owner = self._confirmed_profile_id()
            if contact_id == owner:
                raise ValueError('Нельзя изменить блокировку собственного профиля')
            profile_pending = getattr(self, '_pending_contact_profile_write', None)
            if profile_pending and profile_pending.get('owner') == owner:
                raise MaxWriteOutcomeUnknown('Предыдущее изменение контакта ещё не подтверждено. Откройте его карточку и проверьте состояние.')
            state = (self.get_contact_block_status(contact_id) if context is None else
                     self.get_contact_block_status(contact_id, context=context))
            pending = getattr(self, '_pending_contact_write', None)
            if pending and pending['owner'] == owner:
                raise MaxWriteOutcomeUnknown('Предыдущее изменение блокировки ещё не подтверждено. Обновите статус.')
            if state['blocked'] == blocked:
                snapshot = self.list_blocked_contacts() if context is None else self.list_blocked_contacts(context=context)
                if context is not None and not blocked and any(item['id'] == contact_id for item in snapshot['contacts']):
                    with self._metadata_lock:
                        self._pending_contact_write = {'owner': owner, 'id': contact_id, 'blocked': blocked}
                    return {'accepted': False, 'pending': True, 'contactId': contact_id,
                            'blocked': None, 'snapshot': snapshot, 'context': context}
                return {'accepted': False, 'pending': False, 'unchanged': True,
                        'contactId': contact_id, 'blocked': blocked,
                        'snapshot': snapshot,
                        **({'context': context} if context is not None else {})}
            self._assert_action_owner(owner)
            if context is not None:
                self._require_conversation_contact(context)
            pending = {'owner': owner, 'id': contact_id, 'blocked': blocked}
            with self._metadata_lock:
                self._pending_contact_write = pending
            operation_started = False
            try:
                if context is not None:
                    self._require_conversation_contact(context)
                operation_started = True
                if context is None:
                    (self.client.block_contact if blocked else self.client.unblock_contact)(contact_id)
                else:
                    method = context.source_client.block_contact if blocked else context.source_client.unblock_contact
                    method(contact_id, expected_session=context.session_context)
            except MaxWriteNotStarted:
                with self._metadata_lock:
                    if getattr(self, '_pending_contact_write', None) is pending:
                        self._pending_contact_write = None
                raise
            except MaxWriteOutcomeUnknown:
                raise
            except MaxError:
                with self._metadata_lock:
                    if getattr(self, '_pending_contact_write', None) is pending:
                        self._pending_contact_write = None
                raise
            except Exception:
                if context is not None and not operation_started:
                    with self._metadata_lock:
                        if getattr(self, '_pending_contact_write', None) is pending:
                            self._pending_contact_write = None
                    raise
                raise MaxWriteOutcomeUnknown('Результат изменения блокировки неизвестен. Обновите статус.') from None
            try:
                self._assert_action_owner(owner)
                state = (self.get_contact_block_status(contact_id) if context is None else
                         self.get_contact_block_status(contact_id, context=context))
                snapshot = self.list_blocked_contacts() if context is None else self.list_blocked_contacts(context=context)
                self._assert_action_owner(owner)
                if context is not None:
                    self._require_conversation_contact(context)
            except Exception:
                raise MaxWriteOutcomeUnknown('Изменение блокировки принято, но статус не прочитан. Обновите список.') from None
            conflict = not blocked and any(item['id'] == contact_id for item in snapshot['contacts'])
            matched = state['blocked'] == blocked and not conflict
            if not matched:
                with self._metadata_lock:
                    self._pending_contact_write = pending
            return {'accepted': True, 'pending': not matched, 'contactId': contact_id,
                    'blocked': state['blocked'] if not conflict else None, 'snapshot': snapshot,
                    **({'context': context} if context is not None else {})}
        finally:
            lock.release()
