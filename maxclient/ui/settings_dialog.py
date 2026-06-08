"""Настройки — минимум, как в оригинальном MAX: профиль, внешний вид,
уведомления, аккаунт/сессии, версия, выход.
"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from .. import __version__
from ..core.config import Settings
from ..core.service import MaxService
from ..protocol.client import PROTO_VER
from .async_runner import run_async
from .widgets.avatar import Avatar

_ACCENTS = ["#2B7FFF", "#7C5CFF", "#23B26D", "#FF7043", "#E5484D", "#00A3A3"]


class SettingsDialog(QDialog):
    theme_changed = Signal()
    logout_requested = Signal()

    def __init__(self, service: MaxService, settings: Settings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.service = service
        self.settings = settings
        self.setWindowTitle("Настройки")
        self.resize(440, 640)
        self._build()

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 20, 20, 20)
        root.setSpacing(16)

        # Профиль.
        prof = self.service.my_profile()
        head = QHBoxLayout()
        av = Avatar(56)
        av.set_identity(prof.name or "MAX", prof.id or "me")
        head.addWidget(av)
        pcol = QVBoxLayout()
        pcol.setSpacing(2)
        self._name_label = QLabel(prof.name or "Мой профиль")
        self._name_label.setStyleSheet("font-size:17px; font-weight:700;")
        pcol.addWidget(self._name_label)
        sub = QLabel(prof.phone or (f"id {prof.id}" if prof.id else "—"))
        sub.setObjectName("ChatSubtitle")
        sub.setStyleSheet("color: palette(mid);")
        pcol.addWidget(sub)
        head.addLayout(pcol, 1)
        edit_name = QPushButton("Изменить имя")
        edit_name.clicked.connect(self._edit_name)
        head.addWidget(edit_name, 0, Qt.AlignmentFlag.AlignVCenter)
        root.addLayout(head)
        root.addWidget(_divider())

        # Внешний вид.
        root.addWidget(_section("Внешний вид"))
        theme_row = QHBoxLayout()
        theme_row.addWidget(QLabel("Тема"))
        theme_row.addStretch(1)
        self.theme_combo = QComboBox()
        self.theme_combo.addItem("Тёмная", "dark")
        self.theme_combo.addItem("Светлая", "light")
        self.theme_combo.setCurrentIndex(0 if self.settings.theme == "dark" else 1)
        self.theme_combo.currentIndexChanged.connect(self._on_theme)
        theme_row.addWidget(self.theme_combo)
        root.addLayout(theme_row)

        accent_row = QHBoxLayout()
        accent_row.addWidget(QLabel("Акцент"))
        accent_row.addStretch(1)
        for color in _ACCENTS:
            sw = _Swatch(color, color == self.settings.accent)
            sw.clicked.connect(lambda _=False, c=color: self._on_accent(c))
            accent_row.addWidget(sw)
        self._accent_row = accent_row
        root.addLayout(accent_row)
        root.addWidget(_divider())

        # Уведомления и поведение.
        root.addWidget(_section("Уведомления и ввод"))
        self.notif = QCheckBox("Уведомления о новых сообщениях")
        self.notif.setChecked(self.settings.notifications)
        self.notif.toggled.connect(self._on_notif)
        root.addWidget(self.notif)

        self.enter = QCheckBox("Отправка по Enter (Shift+Enter — перенос)")
        self.enter.setChecked(self.settings.send_on_enter)
        self.enter.toggled.connect(self._on_enter)
        root.addWidget(self.enter)
        root.addWidget(_divider())

        # Безопасность.
        root.addWidget(_section("Безопасность"))
        self.twofa_status = QLabel("2FA: загрузка статуса…")
        self.twofa_status.setObjectName("ChatSubtitle")
        self.twofa_status.setWordWrap(True)
        self.twofa_status.setStyleSheet("color: palette(mid);")
        root.addWidget(self.twofa_status)

        self._twofa_enabled = None  # None=неизвестно, пока не загрузили статус
        self.twofa_btn = QPushButton("Сменить пароль 2FA")
        self.twofa_btn.setEnabled(False)
        self.twofa_btn.clicked.connect(self._twofa_primary)
        root.addWidget(self.twofa_btn)

        self.email_btn = QPushButton("Recovery email")
        self.email_btn.setEnabled(False)
        self.email_btn.clicked.connect(self._change_email)
        root.addWidget(self.email_btn)

        sessions_btn = QPushButton("Активные сессии и устройства")
        sessions_btn.clicked.connect(self._show_sessions)
        root.addWidget(sessions_btn)

        kick_btn = QPushButton("Завершить все другие устройства")
        kick_btn.setObjectName("Danger")
        kick_btn.clicked.connect(self._terminate_others)
        root.addWidget(kick_btn)
        root.addWidget(_divider())

        # О приложении.
        root.addWidget(_section("О приложении"))
        about = QLabel(
            f"MAX Desktop {__version__}\n"
            f"Протокол MAX: app {self.settings.app_version}, proto v{PROTO_VER}\n"
            f"api.oneme.ru · неофициальный клиент"
        )
        about.setObjectName("ChatSubtitle")
        about.setStyleSheet("color: palette(mid); font-size:12.5px;")
        root.addWidget(about)

        root.addStretch(1)
        logout = QPushButton("Выйти из аккаунта")
        logout.setObjectName("Danger")
        logout.clicked.connect(self._logout)
        root.addWidget(logout)

        self._load_2fa()

    # ───────────────────────── handlers ─────────────────────────

    def _on_theme(self) -> None:
        self.settings.theme = self.theme_combo.currentData()
        self.settings.save()
        self.theme_changed.emit()

    def _on_accent(self, color: str) -> None:
        self.settings.accent = color
        self.settings.save()
        for i in range(self._accent_row.count()):
            w = self._accent_row.itemAt(i).widget()
            if isinstance(w, _Swatch):
                w.set_selected(w.color == color)
        self.theme_changed.emit()

    def _on_notif(self, value: bool) -> None:
        self.settings.notifications = value
        self.settings.save()

    def _on_enter(self, value: bool) -> None:
        self.settings.send_on_enter = value
        self.settings.save()

    def _edit_name(self) -> None:
        new_name, ok = QInputDialog.getText(
            self, "Изменить имя", "Имя в MAX (видно собеседникам):",
            text=self._name_label.text(),
        )
        if not ok or not new_name.strip():
            return
        run_async(
            self.service.update_my_name, new_name.strip(),
            on_done=self._on_name_changed,
            on_error=lambda e: QMessageBox.warning(self, "Не удалось", str(e)),
        )

    def _on_name_changed(self, name: str) -> None:
        self._name_label.setText(name)
        QMessageBox.information(self, "Готово", "Имя профиля изменено.")

    def _show_sessions(self) -> None:
        run_async(
            self.service.list_sessions,
            on_done=self._on_sessions,
            on_error=lambda e: QMessageBox.warning(self, "Сессии", str(e)),
        )

    def _on_sessions(self, sessions: list) -> None:
        if not sessions:
            QMessageBox.information(self, "Сессии", "Сервер не вернул данные о сессиях.")
            return
        lines = []
        for s in sessions:
            mark = "  ● текущая" if s.get("current") else ""
            loc = f"\n    {s['location']}" if s.get("location") else ""
            lines.append(f"• {s.get('label', 'Сессия')}{mark}{loc}")
        QMessageBox.information(self, "Активные сессии", "\n".join(lines))

    def _load_2fa(self) -> None:
        run_async(
            self.service.get_2fa_status,
            on_done=self._on_2fa_status,
            on_error=self._on_2fa_error,
        )

    def _on_2fa_status(self, status: dict) -> None:
        self._twofa_enabled = bool(status.get("enabled"))
        email = status.get("email")
        hint = status.get("hint")
        self.twofa_btn.setEnabled(True)
        if self._twofa_enabled:
            parts = ["2FA включён"]
            if email:
                parts.append(f"recovery email: {email}")
            if hint:
                parts.append(f"подсказка: {hint}")
            self.twofa_status.setText(" · ".join(parts))
            self.twofa_btn.setText("Сменить пароль 2FA (без текущего)")
            self.email_btn.setEnabled(True)
            self.email_btn.setText("Сменить recovery email" if email else "Привязать recovery email")
        else:
            self.twofa_status.setText("2FA выключен")
            self.twofa_btn.setText("Включить 2FA")
            self.email_btn.setEnabled(False)
            self.email_btn.setText("Recovery email (нужен 2FA)")

    def _on_2fa_error(self, _e: object) -> None:
        # Статус не загрузился — НЕ угадываем enable/change (иначе на 2FA-OFF
        # аккаунте «Сменить пароль» выдал бы ложное «неверный пароль»). Кнопка
        # перечитывает статус.
        self._twofa_enabled = None
        self.twofa_status.setText("2FA: не удалось загрузить статус (нажмите, чтобы повторить)")
        self.twofa_btn.setText("Обновить статус 2FA")
        self.twofa_btn.setEnabled(True)
        self.email_btn.setEnabled(False)
        self.email_btn.setText("Recovery email (нужен 2FA)")

    def _twofa_primary(self) -> None:
        # Ветвимся ТОЛЬКО на известном булеве. None (статус не загружен) —
        # перечитываем статус, не открываем диалог наугад.
        if self._twofa_enabled is None:
            self._load_2fa()
            return
        dlg = (
            Enable2FADialog(self.service, self)
            if self._twofa_enabled is False
            else Change2FADialog(self.service, self)
        )
        dlg.exec()
        self._load_2fa()

    def _change_email(self) -> None:
        if not self._twofa_enabled:
            QMessageBox.information(
                self, "Recovery email",
                "Сначала включите 2FA — recovery email привязывается к нему.",
            )
            return
        dlg = RecoveryEmailDialog(self.service, self)
        dlg.exec()
        self._load_2fa()

    def _terminate_others(self) -> None:
        confirm = QMessageBox.question(
            self, "Завершить другие устройства?",
            "Все другие сессии будут завершены. Самый надёжный способ выкинуть "
            "другие устройства — сменить пароль 2FA (тогда они потребуют повторный "
            "вход). Продолжить попытку завершения сессий?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        run_async(
            self.service.terminate_other_sessions,
            on_done=self._on_terminated,
            on_error=lambda e: QMessageBox.warning(self, "Сессии", str(e)),
        )

    def _on_terminated(self, ok: bool) -> None:
        if ok:
            QMessageBox.information(self, "Готово", "Запрос на завершение сессий отправлен.")
        else:
            QMessageBox.warning(
                self, "Не удалось",
                "Сервер не дал завершить сессии по этому протоколу (у сессий нет "
                "идентификатора). Смените пароль 2FA — это гарантированно "
                "выкинет другие устройства.",
            )

    def _logout(self) -> None:
        confirm = QMessageBox.question(
            self, "Выйти?",
            "Локальная история и контакты останутся, но потребуется повторный вход.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if confirm == QMessageBox.StandardButton.Yes:
            self.logout_requested.emit()
            self.accept()


def _section(title: str) -> QLabel:
    lbl = QLabel(title.upper())
    lbl.setObjectName("FieldLabel")
    return lbl


def _divider() -> QFrame:
    line = QFrame()
    line.setFrameShape(QFrame.Shape.HLine)
    line.setStyleSheet("color: rgba(127,127,127,0.2);")
    line.setFixedHeight(1)
    return line


class _Swatch(QPushButton):
    def __init__(self, color: str, selected: bool) -> None:
        super().__init__()
        self.color = color
        self.setFixedSize(28, 28)
        self.set_selected(selected)

    def set_selected(self, selected: bool) -> None:
        border = "#FFFFFF" if selected else "rgba(0,0,0,0)"
        ring = "3px" if selected else "0px"
        self.setStyleSheet(
            f"QPushButton {{ background:{self.color}; border-radius:14px;"
            f"border:{ring} solid {border}; }}"
        )


class Change2FADialog(QDialog):
    """Смена пароля 2FA БЕЗ ввода текущего (сервисная модель MAX, by design):
    новый -> подтверждение (+ подсказка). Работает только при уже включённом 2FA."""

    def __init__(self, service: MaxService, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.service = service
        self.setWindowTitle("Сменить пароль 2FA")
        self.resize(380, 340)
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 20, 20, 20)
        root.setSpacing(10)

        title = QLabel("Смена пароля 2FA")
        title.setStyleSheet("font-size:16px; font-weight:700;")
        root.addWidget(title)

        info = QLabel(
            "Текущий пароль не требуется. Не жмите повторно при ошибке — "
            "серия попыток 2FA может временно ограничить аккаунт."
        )
        info.setWordWrap(True)
        info.setObjectName("ChatSubtitle")
        info.setStyleSheet("color: palette(mid);")
        root.addWidget(info)

        self.new = _pw_field("Новый пароль")
        self.confirm = _pw_field("Повторите новый пароль")
        root.addWidget(self.new)
        root.addWidget(self.confirm)

        self.hint = QLineEdit()
        self.hint.setPlaceholderText("Подсказка (необязательно)")
        root.addWidget(self.hint)

        self.error = QLabel()
        self.error.setWordWrap(True)
        self.error.setStyleSheet("background: rgba(229,72,77,0.12); color:#E5484D; border-radius:8px; padding:8px 10px;")
        self.error.hide()
        root.addWidget(self.error)

        root.addStretch(1)
        self.btn = QPushButton("Сменить пароль")
        self.btn.setObjectName("Primary")
        self.btn.clicked.connect(self._submit)
        root.addWidget(self.btn)

    def _submit(self) -> None:
        new = self.new.text()
        confirm = self.confirm.text()
        if not new:
            self._err("Введите новый пароль.")
            return
        if new != confirm:
            self._err("Новые пароли не совпадают.")
            return
        self.error.hide()
        self.btn.setEnabled(False)
        self.btn.setText("…")
        run_async(
            self.service.change_2fa, new, self.hint.text().strip() or None,
            on_done=lambda _r: self._ok(),
            on_error=self._fail,
        )

    def _ok(self) -> None:
        QMessageBox.information(
            self, "Готово",
            "Пароль 2FA изменён. Другие устройства потребуют повторного входа.",
        )
        self.accept()

    def _fail(self, message: str) -> None:
        self.btn.setEnabled(True)
        self.btn.setText("Сменить пароль")
        self._err(message)

    def _err(self, text: str) -> None:
        self.error.setText(text)
        self.error.show()


def _pw_field(placeholder: str) -> QLineEdit:
    f = QLineEdit()
    f.setPlaceholderText(placeholder)
    f.setEchoMode(QLineEdit.EchoMode.Password)
    return f


class Enable2FADialog(QDialog):
    """Включение 2FA, когда он выключен: только новый пароль (+ подсказка),
    без текущего — авторизует токен. CREATE_TRACK -> SET_2FA."""

    def __init__(self, service: MaxService, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.service = service
        self.setWindowTitle("Включить 2FA")
        self.resize(380, 320)
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 20, 20, 20)
        root.setSpacing(10)

        title = QLabel("Включение 2FA")
        title.setStyleSheet("font-size:16px; font-weight:700;")
        root.addWidget(title)

        info = QLabel(
            "Пароль 2FA будет запрашиваться при входе на новых устройствах. "
            "Запомните его — без него (и без recovery email) вход не восстановить."
        )
        info.setWordWrap(True)
        info.setObjectName("ChatSubtitle")
        info.setStyleSheet("color: palette(mid);")
        root.addWidget(info)

        self.new = _pw_field("Пароль 2FA")
        self.confirm = _pw_field("Повторите пароль")
        root.addWidget(self.new)
        root.addWidget(self.confirm)

        self.hint = QLineEdit()
        self.hint.setPlaceholderText("Подсказка (необязательно)")
        root.addWidget(self.hint)

        self.error = QLabel()
        self.error.setWordWrap(True)
        self.error.setStyleSheet(
            "background: rgba(229,72,77,0.12); color:#E5484D; border-radius:8px; padding:8px 10px;"
        )
        self.error.hide()
        root.addWidget(self.error)

        root.addStretch(1)
        self.btn = QPushButton("Включить 2FA")
        self.btn.setObjectName("Primary")
        self.btn.clicked.connect(self._submit)
        root.addWidget(self.btn)

    def _submit(self) -> None:
        new = self.new.text()
        confirm = self.confirm.text()
        if not new:
            self._err("Введите пароль 2FA.")
            return
        if new != confirm:
            self._err("Пароли не совпадают.")
            return
        self.error.hide()
        self.btn.setEnabled(False)
        self.btn.setText("…")
        run_async(
            self.service.enable_2fa, new, self.hint.text().strip() or None,
            on_done=lambda _r: self._ok(),
            on_error=self._fail,
        )

    def _ok(self) -> None:
        QMessageBox.information(self, "Готово", "2FA включён.")
        self.accept()

    def _fail(self, message: str) -> None:
        self.btn.setEnabled(True)
        self.btn.setText("Включить 2FA")
        self._err(str(message))

    def _err(self, text: str) -> None:
        self.error.setText(text)
        self.error.show()


class RecoveryEmailDialog(QDialog):
    """Привязка/смена recovery email БЕЗ ввода пароля 2FA (сервисная модель).
    Двухшаговый поток: 1) email -> VERIFY_EMAIL (код на почту); 2) код -> CHECK_EMAIL."""

    def __init__(self, service: MaxService, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.service = service
        self._track_id: str | None = None
        self.setWindowTitle("Recovery email")
        self.resize(380, 320)
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 20, 20, 20)
        root.setSpacing(10)

        title = QLabel("Recovery email для 2FA")
        title.setStyleSheet("font-size:16px; font-weight:700;")
        root.addWidget(title)

        info = QLabel(
            "Текущий пароль 2FA не требуется. На указанную почту придёт код "
            "подтверждения."
        )
        info.setWordWrap(True)
        info.setObjectName("ChatSubtitle")
        info.setStyleSheet("color: palette(mid);")
        root.addWidget(info)

        # Шаг 1.
        self.email = QLineEdit()
        self.email.setPlaceholderText("Email (на него придёт код)")
        root.addWidget(self.email)
        self.send_btn = QPushButton("Отправить код")
        self.send_btn.setObjectName("Primary")
        self.send_btn.clicked.connect(self._send)
        root.addWidget(self.send_btn)

        # Шаг 2 (появляется после отправки кода).
        self.code = QLineEdit()
        self.code.setPlaceholderText("Код из письма")
        self.code.hide()
        root.addWidget(self.code)
        self.confirm_btn = QPushButton("Подтвердить")
        self.confirm_btn.setObjectName("Primary")
        self.confirm_btn.clicked.connect(self._confirm)
        self.confirm_btn.hide()
        root.addWidget(self.confirm_btn)

        self.error = QLabel()
        self.error.setWordWrap(True)
        self.error.setStyleSheet(
            "background: rgba(229,72,77,0.12); color:#E5484D; border-radius:8px; padding:8px 10px;"
        )
        self.error.hide()
        root.addWidget(self.error)
        root.addStretch(1)

    def _send(self) -> None:
        self.error.hide()
        self.send_btn.setEnabled(False)
        self.send_btn.setText("…")
        run_async(
            self.service.start_set_recovery_email, self.email.text(),
            on_done=self._code_sent,
            on_error=self._send_fail,
        )

    def _code_sent(self, track_id: str) -> None:
        self._track_id = track_id
        self.email.setEnabled(False)
        self.send_btn.setText("Код отправлен")
        self.code.show()
        self.confirm_btn.show()
        QMessageBox.information(
            self, "Код отправлен",
            f"Код подтверждения отправлен на {self.email.text().strip()}. "
            "Введите его из письма.",
        )

    def _send_fail(self, message: str) -> None:
        self.send_btn.setEnabled(True)
        self.send_btn.setText("Отправить код")
        self._err(str(message))

    def _confirm(self) -> None:
        if not self._track_id:
            self._err("Сначала отправьте код.")
            return
        self.error.hide()
        self.confirm_btn.setEnabled(False)
        self.confirm_btn.setText("…")
        run_async(
            self.service.confirm_recovery_email, self._track_id, self.code.text(),
            on_done=lambda _r: self._ok(),
            on_error=self._confirm_fail,
        )

    def _ok(self) -> None:
        QMessageBox.information(self, "Готово", "Recovery email привязан.")
        self.accept()

    def _confirm_fail(self, message: str) -> None:
        self.confirm_btn.setEnabled(True)
        self.confirm_btn.setText("Подтвердить")
        self._err(str(message))

    def _err(self, text: str) -> None:
        self.error.setText(text)
        self.error.show()
