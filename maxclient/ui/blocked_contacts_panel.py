"""First-page blocked contacts with explicit, confirmed block/unblock actions."""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QHBoxLayout, QListWidget, QListWidgetItem, QPushButton, QVBoxLayout, QWidget

from .async_runner import run_async
from .management_widgets import apply_panel_theme, confirm_action, is_identity, label


def contact_name(contact):
    names = contact.get('names') if isinstance(contact, dict) else None
    if not isinstance(names, list):
        return None
    # Both are real server name records. Prefer the user's custom label.
    ordered = sorted((record for record in names if isinstance(record, dict)),
                     key=lambda record: {'CUSTOM': 0, 'ONEME': 1}.get(record.get('type'), 2))
    for record in ordered:
        value = record.get('name')
        if isinstance(value, str) and value:
            return value
        parts = [record.get(key) for key in ('firstName', 'lastName')]
        value = ' '.join(part for part in parts if isinstance(part, str) and part)
        if value:
            return value
    return None


def _block_snapshot(value):
    if (not isinstance(value, dict) or not isinstance(value.get('contacts'), list)
            or value.get('pagination') != 'first_page_only'
            or type(value.get('from')) is not int or value['from'] != 0
            or type(value.get('count')) is not int or value['count'] != 100):
        raise ValueError('Invalid block snapshot')
    ids = []
    for contact in value['contacts']:
        if (not isinstance(contact, dict) or not is_identity(contact.get('id'))
                or not isinstance(contact.get('names'), list)):
            raise ValueError('Invalid blocked contact')
        ids.append(contact['id'])
    if len(set(ids)) != len(ids):
        raise ValueError('Duplicate contact identities')
    return value


class BlockedContactsPanel(QWidget):
    blocked_changed = Signal(object)  # Actual first-page read, never an optimistic row.

    def __init__(self, service, parent=None):
        super().__init__(parent)
        self.service = service
        self._busy = self._loaded = self._read_ok = self._refresh_queued = False
        self._pending = None
        self._records = {}
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(14)
        self.status = label('Обновите список заблокированных контактов.', secondary=True)
        layout.addWidget(self.status)
        self.items = QListWidget()
        self.items.setMinimumHeight(220)
        self.items.setAccessibleName('Заблокированные контакты · первая страница')
        self.items.currentItemChanged.connect(lambda _current, _old: self._apply_enabled())
        layout.addWidget(self.items)
        layout.addWidget(label('Показана первая страница списка MAX. Отсутствие контакта на этой странице '
                               'не подтверждает, что он разблокирован.', secondary=True))
        buttons = QHBoxLayout()
        self.refresh_button = QPushButton('Обновить')
        self.refresh_button.clicked.connect(self.refresh)
        buttons.addWidget(self.refresh_button)
        self.unblock_button = QPushButton('Разблокировать')
        self.unblock_button.clicked.connect(self._unblock_selected)
        buttons.addWidget(self.unblock_button)
        layout.addLayout(buttons)
        self._apply_enabled()
        self.apply_theme()

    def apply_theme(self, theme=None):
        apply_panel_theme(self, theme)

    def ensure_loaded(self):
        if not self._loaded:
            self.refresh()

    def refresh(self):
        if self._busy:
            self._refresh_queued = True
            return
        self._busy = True
        self.status.setText('Загрузка контактов…')
        self._apply_enabled()
        run_async(self.service.list_blocked_contacts, on_done=self._read_done, on_error=self._read_error)

    def _read_done(self, result, *, check_target=True):
        try:
            snapshot = _block_snapshot(result)
        except ValueError:
            self._read_error('invalid snapshot')
            return
        current = self.items.currentItem()
        selected = current.data(Qt.ItemDataRole.UserRole) if current is not None else None
        self._records = {contact['id']: contact for contact in snapshot['contacts']}
        self.items.blockSignals(True)
        self.items.clear()
        for identity, contact in self._records.items():
            item = QListWidgetItem(contact_name(contact) or 'Имя не передано MAX')
            item.setData(Qt.ItemDataRole.UserRole, identity)
            self.items.addItem(item)
            if identity == selected:
                self.items.setCurrentItem(item)
        self.items.blockSignals(False)
        self._loaded = self._read_ok = True
        self.blocked_changed.emit(snapshot)
        if self._pending is not None and check_target:
            self._busy = True
            self._apply_enabled()
            self.status.setText('Проверка результата действия…')
            run_async(self.service.get_contact_block_status, self._pending['id'],
                      on_done=self._target_done, on_error=self._target_error)
            return
        self._busy = False
        self.status.setText('Изменение ещё не подтверждено. Повтор отключён.' if self._pending
                            else f'Показано контактов: {len(self._records)}.' if self._records
                            else 'На этой странице контактов нет.')
        self._apply_enabled()
        self._drain_refresh()

    def _read_error(self, _error):
        self._busy = self._read_ok = False
        self._refresh_queued = False
        self.status.setText('Не удалось загрузить список. Попробуйте обновить позже.')
        self._apply_enabled()

    def _target_done(self, result):
        pending = self._pending
        if (pending is None or not isinstance(result, dict) or not is_identity(result.get('contactId'))
                or result['contactId'] != pending['id']
                or type(result.get('blocked')) is not bool
                or not isinstance(result.get('contact'), dict)
                or not is_identity(result['contact'].get('id')) or result['contact']['id'] != pending['id']):
            self._target_error('invalid status')
            return
        self._busy = False
        conflict = pending['blocked'] is False and pending['id'] in self._records
        if result['blocked'] is pending['blocked'] and not conflict:
            self._pending = None
            self.status.setText('Контакт заблокирован.' if result['blocked'] else 'Контакт разблокирован.')
        else:
            self.status.setText('MAX пока не подтвердил запрошенное изменение. Повтор отключён.')
        self._apply_enabled()
        self._drain_refresh()

    def _target_error(self, _error):
        self._busy = False
        self.status.setText('Список обновлён, но результат действия не подтверждён. Повтор отключён.')
        self._apply_enabled()
        self._drain_refresh()

    def _apply_enabled(self):
        self.refresh_button.setEnabled(not self._busy)
        self.items.setEnabled(not self._busy)
        self.unblock_button.setEnabled(self._read_ok and not self._busy and self._pending is None
                                       and self.items.currentItem() is not None)

    def request_block(self, contact_id, display_name=None):
        """Root calls this only for a contact ID explicitly selected from real data."""
        if not is_identity(contact_id) or self._busy or self._pending is not None:
            return False
        return self._confirm_and_write(contact_id, True, display_name)

    def _unblock_selected(self):
        if self.unblock_button.isEnabled():
            identity = self.items.currentItem().data(Qt.ItemDataRole.UserRole)
            self._confirm_and_write(identity, False, contact_name(self._records[identity]))

    def _confirm_and_write(self, identity, blocked, display_name):
        if not isinstance(display_name, str) or not display_name:
            display_name = contact_name(self._records.get(identity)) or f'контакт без имени (ID {identity})'
        verb = 'Заблокировать' if blocked else 'Разблокировать'
        if not confirm_action(self, verb + ' контакт?', verb + ' контакт «' + display_name + '»?'):
            return False
        self._pending = {'id': identity, 'blocked': blocked}
        self._busy = True
        self.status.setText('Сохранение…')
        self._apply_enabled()
        method = self.service.block_contact if blocked else self.service.unblock_contact
        run_async(method, identity, on_done=self._write_done, on_error=self._write_error)
        return True

    def _write_done(self, result):
        pending = self._pending
        if (pending is None or not isinstance(result, dict)
                or not (result.get('accepted') is True or (result.get('accepted') is False
                        and result.get('unchanged') is True and result.get('pending') is False))
                or type(result.get('pending')) is not bool or not is_identity(result.get('contactId'))
                or result['contactId'] != pending['id']
                or (not result['pending'] and result.get('blocked') is not pending['blocked'])):
            self._write_error('invalid result')
            return
        try:
            _block_snapshot(result.get('snapshot'))
        except ValueError:
            self._write_error('invalid snapshot')
            return
        if not result['pending']:
            self._pending = None
        self._read_done(result.get('snapshot'), check_target=False)
        if self._read_ok and self._pending is None:
            self.status.setText('Контакт заблокирован.' if pending['blocked'] else 'Контакт разблокирован.')

    def _write_error(self, _error):
        self._busy = False
        self.status.setText('Результат действия не подтверждён. Обновите список; повтор отключён.')
        self._apply_enabled()
        self._drain_refresh()

    def _drain_refresh(self):
        if self._refresh_queued:
            self._refresh_queued = False
            self.refresh()
