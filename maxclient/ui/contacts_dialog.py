"""Локальные контакты: список, импорт файла и поиск номера в MAX.
Поиск сохраняет найденный профиль локально, без серверного добавления.
"""
from __future__ import annotations

from threading import Event

from PySide6.QtCore import QObject, Qt, Signal
from shiboken6 import isValid
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QProgressDialog,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..core.models import Contact
from ..core.i18n import normalize_language, tr
from ..core.service import MaxService, _contact_display_name
from ..protocol.folder_contracts import int64
from .async_runner import run_async
from .phone_lookup_dialog import lookup_phone_input
from .widgets.avatar import Avatar


class _ProgressBridge(QObject):
    progress = Signal(int, int)


class ContactsDialog(QDialog):
    open_chat = Signal(object, str)  # Actual contact int64; not a chat ID or Qt int32.

    def __init__(self, service: MaxService, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.service = service
        self._language = normalize_language(getattr(getattr(parent, 'settings', None), 'ui_language', 'ru'))
        self._result_source = None
        self._empty_source = 'Контактов нет.\nИмпортируйте файл или найдите номер в MAX.'
        self._import_dialog = None
        self._import_progress = None
        self._closed = False
        self._lookup_request = None
        self._found_contact = None
        self._found_context = None
        self.navigate_contact = None
        self._import_request = None
        self._import_cancel = None
        try:
            self._dialog_context = self.service.search_context()
        except (AttributeError, ValueError):
            self._dialog_context = None
        self.finished.connect(self._finished)
        self.setWindowTitle("Локальные контакты")
        self.resize(460, 640)
        self._build()
        self.set_language(self._language)
        self._refresh()

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(12)

        self.heading = QLabel("Локальные контакты")
        self.heading.setStyleSheet("font-size:18px; font-weight:700;")
        root.addWidget(self.heading)

        actions = QHBoxLayout()
        actions.setSpacing(8)
        self.import_btn = QPushButton("⤓  Импортировать контакты")
        self.import_btn.setObjectName("Primary")
        self.import_btn.setToolTip("Импорт из файла .vcf или .csv")
        self.import_btn.clicked.connect(self._import)
        actions.addWidget(self.import_btn, 1)

        self.add_btn = QPushButton("Найти по номеру")
        self.add_btn.setToolTip("Поиск в MAX с сохранением в локальном списке")
        self.add_btn.clicked.connect(self._add_by_phone)
        actions.addWidget(self.add_btn)
        root.addLayout(actions)

        self.search = QLineEdit()
        self.search.setPlaceholderText("Поиск по имени или номеру")
        self.search.textChanged.connect(self._refresh)
        root.addWidget(self.search)

        self.result_label = QLabel()
        self.result_label.setTextFormat(Qt.TextFormat.PlainText)
        self.result_label.setWordWrap(True)
        self.result_label.hide()
        root.addWidget(self.result_label)
        self.open_result_btn = QPushButton("Открыть чат")
        self.open_result_btn.clicked.connect(self._open_found)
        self.open_result_btn.hide()
        root.addWidget(self.open_result_btn)

        self.list = QListWidget()
        self.list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self._context_menu)
        self.list.itemClicked.connect(self._on_click)
        root.addWidget(self.list, 1)

        self.empty = QLabel("Контактов нет.\nИмпортируйте файл или найдите номер в MAX.")
        self.empty.setObjectName("EmptyHint")
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        root.addWidget(self.empty)

    def _tr(self, source, **values):
        return tr(source, self._language, **values)

    def set_language(self, language):
        self._language = normalize_language(language)
        self.setWindowTitle(self._tr('Локальные контакты'))
        self.heading.setText(self.windowTitle())
        self.import_btn.setText('⤓  ' + self._tr('Импортировать контакты'))
        self.import_btn.setToolTip(self._tr('Импорт из файла .vcf или .csv'))
        self.add_btn.setText(self._tr('Найти по номеру'))
        self.add_btn.setToolTip(self._tr('Поиск в MAX с сохранением в локальном списке'))
        self.search.setPlaceholderText(self._tr('Поиск по имени или номеру'))
        self.search.setAccessibleName(self._tr('Поиск по имени или номеру'))
        self.empty.setText(self._tr(self._empty_source))
        own = isinstance(self._found_contact, Contact) and self._found_contact.id == self.service.my_id
        self.open_result_btn.setText(self._tr('Открыть Избранное' if own else 'Открыть чат'))
        self._render_result()
        if self._import_dialog is not None and isValid(self._import_dialog):
            self._import_dialog.setWindowTitle(self._tr('Импорт контактов'))
            self._import_dialog.setCancelButtonText(self._tr('Отмена'))
            self._import_dialog.setLabelText(self._tr('Проверка: {done} из {total}',
                done=self._import_progress[0], total=self._import_progress[1]) if self._import_progress else
                self._tr('Проверка контактов…'))

    retranslate = set_language

    def _message(self, text, *, app_owned=True, **values):
        self._result_source = (text, app_owned, values)
        self._render_result()
        self.result_label.show()

    def _render_result(self):
        if self._result_source == 'found' and isinstance(self._found_contact, Contact):
            contact = self._found_contact
            own = contact.id == self.service.my_id
            details = [self._tr('Это ваш профиль' if own else 'Контакт найден'), _contact_display_name(contact)]
            if isinstance(contact.phone, str) and contact.phone:
                details.append(contact.phone)
            if own:
                details.append(self._tr('Сообщения себе сохраняются в «Избранном».'))
            self.result_label.setText('\n'.join(details))
        elif isinstance(self._result_source, tuple):
            source, owned, values = self._result_source
            self.result_label.setText(self._tr(source, **values) if owned else source)

    # ───────────────────────── data ─────────────────────────

    def _refresh(self) -> None:
        if not self._context_current():
            self._show_expired()
            return
        query = self.search.text().strip()
        try:
            contacts = self.service.list_contacts(query)
        except ValueError:
            self._show_expired()
            return
        if not self._context_current():
            self._show_expired()
            return
        self.list.clear()
        for c in contacts:
            item = QListWidgetItem(self.list)
            item.setData(Qt.ItemDataRole.UserRole, c)
            row = _ContactRow(c)
            item.setSizeHint(row.sizeHint())
            self.list.addItem(item)
            self.list.setItemWidget(item, row)
        self.empty.setVisible(self.list.count() == 0)
        self.list.setVisible(self.list.count() > 0)

    def _show_expired(self) -> None:
        if self._import_cancel is not None:
            self._import_cancel.set()
        self._lookup_request = self._dialog_context = None
        self._found_contact = self._found_context = None
        if not isValid(self) or self._closed:
            return
        self.list.clear()
        self.list.hide()
        self._empty_source = 'Сессия изменилась. Откройте контакты заново.'
        self.empty.setText(self._tr(self._empty_source))
        self.empty.show()
        self.add_btn.setEnabled(False)
        self.import_btn.setEnabled(False)
        self.result_label.clear()
        self._result_source = None
        self.result_label.hide()
        self.open_result_btn.hide()

    def _on_click(self, item: QListWidgetItem) -> None:
        if not self._context_current() or item.listWidget() is not self.list:
            return
        c: Contact = item.data(Qt.ItemDataRole.UserRole)
        if not isinstance(c, Contact) or not int64(c.id):
            return
        self._navigate(c)

    def _context_menu(self, pos) -> None:
        if not self._context_current():
            return
        item = self.list.itemAt(pos)
        if item is None:
            return
        c: Contact = item.data(Qt.ItemDataRole.UserRole)
        menu = QMenu(self)
        menu.addAction(self._tr("Открыть чат"), lambda: self._on_click(item))
        menu.addAction(self._tr("Переименовать"), lambda: self._rename(c))
        menu.addAction(self._tr("Удалить"), lambda: self._delete(c))
        menu.exec(self.list.mapToGlobal(pos))

    def _rename(self, c: Contact) -> None:
        if not self._context_current():
            return
        new_name, ok = QInputDialog.getText(
            self, self._tr("Переименовать контакт"), self._tr("Имя:"), text=_contact_display_name(c)
        )
        if not ok or not new_name.strip() or not self._context_current():
            return
        try:
            self.service.rename_contact(c.id, new_name.strip(), context=self._dialog_context)
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, self._tr("Не удалось"), str(e))
            return
        self._refresh()

    def _delete(self, c: Contact) -> None:
        if not self._context_current():
            self._show_expired()
            return
        try:
            self.service.delete_contact(c.id, context=self._dialog_context)
        except Exception as error:  # noqa: BLE001
            if not self._context_current():
                self._show_expired()
            else:
                QMessageBox.warning(self, self._tr("Не удалось удалить контакт"), str(error))
            return
        self._refresh()

    # ───────────────────────── actions ─────────────────────────

    def _add_by_phone(self) -> None:
        if not self._context_current():
            self._show_expired()
            return
        if self._lookup_request is not None or self._import_request is not None:
            return
        phone, ok = QInputDialog.getText(self, self._tr("Найти по номеру"), self._tr("Телефон:"), text="+7")
        context = self._dialog_context
        if not self._context_current():
            self._show_expired()
            return
        if not ok:
            return
        phone = lookup_phone_input(phone)
        if phone is None:
            self._on_add_error("Введите полный номер телефона с кодом страны.", app_owned=True)
            return
        request = self._lookup_request = object()
        self._found_contact = self._found_context = None
        self._message("Поиск…")
        self.open_result_btn.hide()
        self.add_btn.setEnabled(False)

        def done(contact):
            if self._lookup_current(request, context):
                self._lookup_request = None
                self._found_context = context
                self._on_added(contact)

        def failed(message):
            if self._lookup_current(request, context):
                self._lookup_request = None
                self._on_add_error(message)

        run_async(
            self.service.add_contact_by_phone, phone, context=context,
            on_done=done, on_error=failed,
        )

    def _on_added(self, contact: Contact) -> None:
        self.add_btn.setEnabled(True)
        self._refresh()
        if not self._context_current():
            return
        if not isinstance(contact, Contact) or not int64(contact.id):
            self._on_add_error("MAX не подтвердил сведения о контакте.", app_owned=True)
            return
        self._found_contact = contact
        self._result_source = 'found'
        self.set_language(self._language)
        self.result_label.show()
        self.open_result_btn.show()

    def _on_add_error(self, message: str, *, app_owned=False) -> None:
        if not self._context_current():
            self._show_expired()
            return
        self._found_contact = self._found_context = None
        self.add_btn.setEnabled(True)
        self._message(message, app_owned=app_owned)
        self.open_result_btn.hide()

    def _lookup_current(self, request, context) -> bool:
        if not isValid(self) or self._closed or self._lookup_request is not request:
            return False
        if not self._context_current() or self._dialog_context is not context:
            self._show_expired()
            return False
        return True

    def _context_current(self) -> bool:
        try:
            return (isValid(self) and not self._closed and self._dialog_context is not None
                    and self.service.search_context() is self._dialog_context)
        except (AttributeError, ValueError):
            return False

    def _finished(self, _result) -> None:
        if self._import_cancel is not None:
            self._import_cancel.set()
        self._closed = True
        self._lookup_request = None
        self._found_contact = self._found_context = None

    def _open_found(self) -> None:
        if not self._context_current():
            self._show_expired()
            return
        if (self._found_contact is None or self._found_context is None
                or self._dialog_context is not self._found_context):
            return
        contact = self._found_contact
        if not isinstance(contact, Contact) or not int64(contact.id):
            return
        self._navigate(contact)

    def _navigate(self, contact: Contact) -> None:
        if not self._context_current():
            self._show_expired()
            return
        if callable(self.navigate_contact):
            opened = self.navigate_contact(contact.id, _contact_display_name(contact))
            if not self._context_current():
                self._show_expired()
                return
            if not opened:
                self._message(
                    'Контакт найден. Диалог с ним пока не загружен.\n'
                    'Контакт доступен в списке; новое сообщение не отправлено.')
                return
        else:
            self.open_chat.emit(contact.id, _contact_display_name(contact))
        self.accept()

    def _import(self) -> None:
        from PySide6.QtWidgets import QFileDialog

        if (not self._context_current() or self._import_request is not None
                or self._lookup_request is not None):
            return
        path, _ = QFileDialog.getOpenFileName(
            self, self._tr("Выберите файл контактов"), "",
            self._tr('Контакты') + ' (*.vcf *.csv);;vCard (*.vcf);;CSV (*.csv);;' + self._tr('Все файлы') + ' (*.*)',
        )
        if not path or not self._context_current():
            return

        request = self._import_request = object()
        cancel_event = self._import_cancel = Event()
        self.destroyed.connect(lambda *_: cancel_event.set())
        self.import_btn.setEnabled(False)
        self.add_btn.setEnabled(False)

        dialog = self._import_dialog = QProgressDialog(self._tr("Проверка контактов…"), self._tr("Отмена"), 0, 0, self)
        self._import_progress = None
        dialog.setWindowTitle(self._tr("Импорт контактов"))
        dialog.setWindowModality(Qt.WindowModality.WindowModal)
        dialog.setMinimumDuration(0)
        dialog.setValue(0)

        bridge = _ProgressBridge()

        def on_progress(done: int, total: int) -> None:
            if not cancel_event.is_set() and isValid(bridge):
                bridge.progress.emit(done, total)

        def cancel() -> None:
            if self._import_request is not request:
                return
            cancel_event.set()
            if self._context_current():
                self._message('Импорт остановлен. Завершается текущая проверка.')

        dialog.canceled.connect(cancel)

        def apply_progress(done: int, total: int) -> None:
            if (self._import_request is not request or cancel_event.is_set()
                    or not self._context_current() or not isValid(dialog)):
                return
            if total:
                dialog.setMaximum(total)
                dialog.setValue(done)
                self._import_progress = (done, total)
                dialog.setLabelText(self._tr('Проверка: {done} из {total}', done=done, total=total))

        bridge.progress.connect(apply_progress)

        def finish() -> bool:
            if self._import_request is not request:
                return False
            self._import_request = self._import_cancel = None
            self._import_dialog = self._import_progress = None
            if isValid(dialog):
                dialog.close()
                dialog.deleteLater()
            if isValid(bridge):
                bridge.deleteLater()
            if not self._context_current():
                self._show_expired()
                return False
            self.import_btn.setEnabled(True)
            self.add_btn.setEnabled(True)
            return True

        def done(found: int) -> None:
            if not finish():
                return
            self._refresh()
            if cancel_event.is_set():
                self._message('Импорт отменён. Уже найдено в MAX: {found}.', found=found)
                return
            QMessageBox.information(self, self._tr("Импорт завершён"), self._tr('Найдено в MAX: {found}', found=found))

        def fail(message: str) -> None:
            if not finish():
                return
            if cancel_event.is_set():
                self._message('Импорт отменён.')
                return
            QMessageBox.warning(self, self._tr("Ошибка импорта"), message)

        run_async(
            self.service.import_contacts_from_file, path,
            context=self._dialog_context, cancel_event=cancel_event,
            on_progress=on_progress, on_done=done, on_error=fail,
        )
        dialog.show()


class _ContactRow(QWidget):
    def __init__(self, contact: Contact) -> None:
        super().__init__()
        lay = QHBoxLayout(self)
        lay.setContentsMargins(8, 6, 8, 6)
        lay.setSpacing(12)
        av = Avatar(42)
        av.setObjectName('ContactAvatar')
        display = _contact_display_name(contact)
        av.set_identity(display, contact.id)
        av.set_photo(contact.avatar_url if isinstance(contact.avatar_url, str) else None)
        lay.addWidget(av)
        col = QVBoxLayout()
        col.setSpacing(1)
        name = QLabel()
        name.setObjectName('ContactName')
        name.setTextFormat(Qt.TextFormat.PlainText)
        name.setText(display)
        name.setStyleSheet("font-weight:600;")
        col.addWidget(name)
        if isinstance(contact.phone, str) and contact.phone:
            phone = QLabel()
            phone.setTextFormat(Qt.TextFormat.PlainText)
            phone.setText(contact.phone)
            phone.setObjectName('ContactPhone')
            phone.setStyleSheet("color: palette(mid); font-size:12.5px;")
            col.addWidget(phone)
        lay.addLayout(col, 1)
        chat_icon = QLabel("💬")
        lay.addWidget(chat_icon)
