"""Диалог контактов: список, поиск, импорт из файла (.vcf/.csv) отдельной
кнопкой, добавление по номеру. Тап по контакту открывает чат.
"""
from __future__ import annotations

from PySide6.QtCore import QObject, Qt, Signal
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
from ..core.service import MaxService
from .async_runner import run_async
from .widgets.avatar import Avatar


class _ProgressBridge(QObject):
    progress = Signal(int, int)


class ContactsDialog(QDialog):
    open_chat = Signal(int, str)

    def __init__(self, service: MaxService, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.service = service
        self.setWindowTitle("Контакты")
        self.resize(460, 640)
        self._build()
        self._refresh()

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(12)

        title = QLabel("Контакты")
        title.setStyleSheet("font-size:18px; font-weight:700;")
        root.addWidget(title)

        actions = QHBoxLayout()
        actions.setSpacing(8)
        self.import_btn = QPushButton("⤓  Импортировать контакты")
        self.import_btn.setObjectName("Primary")
        self.import_btn.setToolTip("Импорт из файла .vcf или .csv")
        self.import_btn.clicked.connect(self._import)
        actions.addWidget(self.import_btn, 1)

        self.add_btn = QPushButton("＋ По номеру")
        self.add_btn.clicked.connect(self._add_by_phone)
        actions.addWidget(self.add_btn)
        root.addLayout(actions)

        self.search = QLineEdit()
        self.search.setPlaceholderText("Поиск по имени или номеру")
        self.search.textChanged.connect(self._refresh)
        root.addWidget(self.search)

        self.list = QListWidget()
        self.list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self._context_menu)
        self.list.itemClicked.connect(self._on_click)
        root.addWidget(self.list, 1)

        self.empty = QLabel("Контактов нет.\nИмпортируйте файл или добавьте номер.")
        self.empty.setObjectName("EmptyHint")
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        root.addWidget(self.empty)

    # ───────────────────────── data ─────────────────────────

    def _refresh(self) -> None:
        query = self.search.text().strip()
        contacts = self.service.list_contacts(query)
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

    def _on_click(self, item: QListWidgetItem) -> None:
        c: Contact = item.data(Qt.ItemDataRole.UserRole)
        self.open_chat.emit(c.id, c.display)
        self.accept()

    def _context_menu(self, pos) -> None:
        item = self.list.itemAt(pos)
        if item is None:
            return
        c: Contact = item.data(Qt.ItemDataRole.UserRole)
        menu = QMenu(self)
        menu.addAction("Открыть чат", lambda: self._on_click(item))
        menu.addAction("Удалить", lambda: self._delete(c))
        menu.exec(self.list.mapToGlobal(pos))

    def _delete(self, c: Contact) -> None:
        self.service.delete_contact(c.id)
        self._refresh()

    # ───────────────────────── actions ─────────────────────────

    def _add_by_phone(self) -> None:
        phone, ok = QInputDialog.getText(self, "Добавить контакт", "Телефон:", text="+7")
        if not ok or not phone.strip():
            return
        self.add_btn.setEnabled(False)
        run_async(
            self.service.add_contact_by_phone, phone.strip(),
            on_done=self._on_added, on_error=self._on_add_error,
        )

    def _on_added(self, contact: Contact) -> None:
        self.add_btn.setEnabled(True)
        self._refresh()
        QMessageBox.information(self, "Контакт добавлен", f"Найден: {contact.display}")

    def _on_add_error(self, message: str) -> None:
        self.add_btn.setEnabled(True)
        QMessageBox.warning(self, "Не найдено", message)

    def _import(self) -> None:
        from PySide6.QtWidgets import QFileDialog

        path, _ = QFileDialog.getOpenFileName(
            self, "Выберите файл контактов", "",
            "Контакты (*.vcf *.csv);;vCard (*.vcf);;CSV (*.csv);;Все файлы (*.*)",
        )
        if not path:
            return

        dialog = QProgressDialog("Проверка контактов…", "Отмена", 0, 0, self)
        dialog.setWindowTitle("Импорт контактов")
        dialog.setWindowModality(Qt.WindowModality.WindowModal)
        dialog.setMinimumDuration(0)
        dialog.setValue(0)

        bridge = _ProgressBridge()

        def on_progress(done: int, total: int) -> None:
            bridge.progress.emit(done, total)

        def apply_progress(done: int, total: int) -> None:
            if total:
                dialog.setMaximum(total)
                dialog.setValue(done)
                dialog.setLabelText(f"Проверка: {done} из {total}")

        bridge.progress.connect(apply_progress)

        def done(found: int) -> None:
            dialog.close()
            self._refresh()
            QMessageBox.information(self, "Импорт завершён", f"Найдено в MAX: {found}")

        def fail(message: str) -> None:
            dialog.close()
            QMessageBox.warning(self, "Ошибка импорта", message)

        run_async(
            self.service.import_contacts_from_file, path,
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
        av.set_identity(contact.display, contact.id)
        lay.addWidget(av)
        col = QVBoxLayout()
        col.setSpacing(1)
        name = QLabel(contact.display)
        name.setStyleSheet("font-weight:600;")
        col.addWidget(name)
        if contact.phone:
            phone = QLabel(contact.phone)
            phone.setObjectName("ChatSubtitle")
            phone.setStyleSheet("color: palette(mid); font-size:12.5px;")
            col.addWidget(phone)
        lay.addLayout(col, 1)
        chat_icon = QLabel("💬")
        lay.addWidget(chat_icon)
