"""Native chat-row menu presentation, separate from the Info → More menu.

Only descriptors with existing guarded service handlers are enabled. Unsupported
operations remain visible, with no guessed protocol or optimistic local state.
See research/CHAT_ROW_MENU_SOURCE_20261104.md for native and WEB observations.
"""
from __future__ import annotations

from ..core.i18n import tr
from .conversation_bot_delete_dialog import bot_delete_available
from .conversation_owner_transfer_dialog import owner_transfer_available


ROW_MUTE_CHOICES = (
    ('На 1 час', 3_600_000), ('На 4 часа', 14_400_000),
    ('На 24 часа', 86_400_000), ('Навсегда', -1),
)


def chat_row_entries(snapshot, *, folders=None, language='ru', busy=False,
                     read_state=None, pin_state=None, preview_available=False):
    if not isinstance(snapshot, dict):
        return []
    capabilities = snapshot.get('action_capabilities', {})
    if not isinstance(capabilities, dict) or capabilities.get('supported') is not True:
        return []
    kind = capabilities.get('kind')
    if kind not in ('dialog', 'saved', 'bot', 'group', 'channel'):
        return []
    supplied_actions = snapshot.get('more_actions', [])
    supplied_actions = supplied_actions if isinstance(supplied_actions, list) else []
    source = {row['action']: row for row in supplied_actions
              if isinstance(row, dict) and isinstance(row.get('action'), str)}
    state = snapshot.get('row_menu', {})
    state = state if isinstance(state, dict) else {}
    read_state = read_state if isinstance(read_state, dict) else {}
    pin_state = pin_state if isinstance(pin_state, dict) else {}
    pending = (busy or snapshot.get('action_pending') is True
               or read_state.get('pending') is True or pin_state.get('pending') is True)
    blocked = state.get('chat_status') == 'BLOCKED'

    def entry(action, label, *, available=False, value=None, icon=None, **extra):
        reasons = {'preview': 'Не удалось подтвердить возможность предпросмотра.',
                   'report': 'Жалоба недоступна для текущих прав или состояния чата.',
                   'leave': 'Выход недоступен для текущих прав или состояния чата.',
                   'delete': 'Удаление этого вида чата пока не реализовано в этой сборке.',
                   'toggle_read': 'Не удалось подтвердить текущее состояние прочтения.',
                   'toggle_pin': 'Не удалось подтвердить закрепление в выбранной папке.'}
        if not available:
            if 'unavailable_reason' not in extra:
                reason = ('Сначала дождитесь подтверждения текущего действия.' if pending else
                          reasons.get(action, 'Состояние этого действия ещё не подтверждено.'))
                extra['unavailable_reason_source'] = reason
                extra['unavailable_reason'] = tr(reason, language)
            elif isinstance(extra.get('unavailable_reason_source'), str):
                extra['unavailable_reason'] = tr(extra['unavailable_reason_source'], language)
        return dict(action=action, label=tr(label, language), available=available,
                    value=value, icon=icon, **extra)

    def existing(action, label, *, icon=None, **extra):
        offered = source.get(action)
        if offered is None:
            return None
        implemented = action in ('block', 'unblock', 'stop_bot', 'clear', 'delete', 'leave', 'report')
        if action == 'leave':
            implemented = (kind in ('group', 'channel') and capabilities.get('owner') is False
                           and capabilities.get('participant') is True
                           and state.get('chat_status') in ('ACTIVE', 'BLOCKED'))
        if offered.get('unavailable_reason') and 'unavailable_reason' not in extra:
            extra['unavailable_reason'] = offered['unavailable_reason']
            if isinstance(offered.get('unavailable_reason_source'), str):
                extra['unavailable_reason_source'] = offered['unavailable_reason_source']
        return entry(action, label, available=implemented and offered.get('available') is True and not pending,
                     value=offered.get('value'), write=True, icon=icon, **extra)

    rows = []
    if not blocked:
        rows.append(entry('preview', 'Предпросмотр', icon='preview', write=False,
                          available=preview_available is True and not pending))
        if 'folder' in source:
            folder_rows = [dict(row, available=row.get('available') is True and
                                not (pending and row.get('write') is True))
                           for row in folders if isinstance(row, dict)] if isinstance(folders, list) else [
                entry('folder_loading', 'Загрузка папок…', write=False)]
            folder_rows.append(entry('folder_create', 'Создать папку', available=not busy,
                                     write=False, separator_before=True, icon='plus'))
            rows.append(entry('folder', 'Добавить в папку',
                              available=source['folder'].get('available') is True,
                              write=False, icon='folder', children=folder_rows))
        if capabilities.get('last_message') is True:
            unread = read_state.get('unread', state.get('unread'))
            label = ('Отметить непрочитанным' if unread is False else 'Прочитать всё'
                     if unread is True else 'Отметка о прочтении')
            rows.append(entry('toggle_read', label, write=True, icon='unread',
                              value=unread if type(unread) is bool else None,
                              available=type(unread) is bool and read_state.get('available') is True and not pending))
    pinned = pin_state.get('pinned', state.get('pinned'))
    pin_label = ('Открепить' if pinned is True else 'Закрепить'
                 if pinned is False else 'Закрепление чата')
    rows.append(entry('toggle_pin', pin_label, write=True, icon='pin',
                      value=not pinned if type(pinned) is bool else None,
                      available=type(pinned) is bool and pin_state.get('available') is True and not pending))
    if capabilities.get('mute') is True:
        available = snapshot.get('availability', {}).get('mute') is True and not pending
        if snapshot.get('muted') is True:
            rows.append(entry('mute', 'Включить уведомления', available=available,
                              value=0, write=True, icon='bell'))
        else:
            children = [entry('mute', label, available=available, value=value, write=True, danger=value == -1)
                        for label, value in ROW_MUTE_CHOICES]
            rows.append(entry('mute_choices', 'Отключить уведомления', available=available,
                              write=False, icon='muted', children=children))

    trailing = []
    if not blocked and 'clear' in source:
        label = 'Стереть переписку' if kind == 'dialog' else 'Очистить историю'
        trailing.append(existing('clear', label, icon='history'))
    # These variants were observed in the native app. Info's hide-stories and
    # share-contact actions are intentionally not transplanted into this menu.
    if not blocked and kind in ('bot', 'channel') and 'report' in source:
        trailing.append(existing('report', 'Пожаловаться', icon='report', danger=True))
    if kind in ('group', 'channel') and 'leave' in source and 'transfer_owner_leave' not in source:
        trailing.append(existing('leave', 'Отписаться от канала' if kind == 'channel' else 'Выйти из чата',
                                 icon='logout', danger=True))
    if kind in ('group', 'channel'):
        for action, continuation in (('transfer_owner_leave', False), ('continue_owner_leave', True)):
            if action in source:
                trailing.append(entry(action, 'Выйти из чата' if continuation else 'Передать права и выйти',
                    available=not busy and read_state.get('pending') is not True
                    and pin_state.get('pending') is not True
                    and owner_transfer_available(snapshot, snapshot.get('context'), continuation=continuation),
                    write=True, icon='logout', danger=True))
    if not blocked and kind == 'bot' and 'stop_bot' in source:
        trailing.append(existing('stop_bot', 'Остановить', icon='stop', danger=True))
    if not blocked and kind == 'dialog':
        action = 'unblock' if capabilities.get('recipient_blocked') is True else 'block'
        if action in source:
            trailing.append(existing(action, 'Разблокировать' if action == 'unblock' else 'Заблокировать',
                                     icon='block', danger=action == 'block'))
    if 'delete' in source:
        trailing.append(existing('delete', 'Удалить и остановить'
                                 if kind == 'bot' and capabilities.get('suspended') is not True else 'Удалить чат',
                                 icon='trash', danger=True))
    if kind == 'bot' and 'continue_bot_delete' in source:
        trailing.append(entry('continue_bot_delete', 'Удалить чат',
                              available=not busy and read_state.get('pending') is not True
                              and pin_state.get('pending') is not True
                              and bot_delete_available(snapshot, snapshot.get('context'), continuation=True),
                              write=True, icon='trash', danger=True))
    trailing = [row for row in trailing if row is not None]
    if trailing:
        trailing[0]['separator_before'] = True
    rows.extend(trailing)
    if snapshot.get('action_pending') is True and 'refresh_actions' in source:
        row = dict(source['refresh_actions'])
        row.update(label=tr('Проверить состояние', language), available=not busy,
                   separator_before=True, write=False)
        rows.append(row)
    for action, control in (('refresh_row_read', read_state), ('refresh_row_pin', pin_state)):
        if control.get('pending') is True:
            rows.append(entry(action, 'Проверить состояние', available=not busy,
                              write=False, separator_before=True))
    return rows


def flatten_entries(rows):
    for row in rows:
        yield row
        yield from flatten_entries(row.get('children', []))
