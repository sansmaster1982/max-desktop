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
from shiboken6 import isValid

from .. import __app_name__, __version__
from ..core.config import Settings
from ..core.i18n import normalize_language, tr
from ..core.service import MaxService
from ..protocol.client import PROTO_VER, MaxWriteOutcomeUnknown
from .async_runner import run_async
from .widgets.avatar import Avatar

_ACCENTS = ["#2B7FFF", "#7C5CFF", "#23B26D", "#FF7043", "#E5484D", "#00A3A3"]


class _UnconfirmedWrite:
    """Carry only a category through the shared string-error async runner."""


class _LocalizedDialog(QDialog):
    """Translate owned labels only; retain inputs, request state and raw replies."""

    def __init__(self, service, parent=None, settings=None):
        super().__init__(parent)
        self.service = service
        self._language_settings = settings or getattr(parent, 'settings', None) or getattr(service, 'settings', None)
        self._lang = normalize_language(getattr(self._language_settings, 'ui_language', 'ru'))
        self._bindings = {}
        self._closed = False
        self._uncertain = False
        self._jobs = {}
        # Real MaxService exposes the established exact owner/client/session guard.
        # Do not mistake arbitrary Mock attributes for an authenticated context API.
        self._has_context = callable(getattr(type(service), '_history_read_context', None))
        self._context = None
        if self._has_context:
            try:
                self._context = service._history_read_context()
            except Exception:
                pass

    def _tr(self, source, **values):
        return tr(source, self._lang, **values)

    def _bind(self, widget, source, setter='setText', *, upper=False, **values):
        self._bindings[(widget, setter)] = (source, upper, values)
        text = self._tr(source, **values)
        getattr(widget, setter)(text.upper() if upper else text)
        return widget

    def _widget(self, widget_type, source, *, upper=False):
        widget = widget_type()
        if isinstance(widget, QLabel):
            widget.setTextFormat(Qt.TextFormat.PlainText)
        return self._bind(widget, source, upper=upper)

    def _placeholder(self, widget, source):
        self._bind(widget, source, 'setPlaceholderText')
        self._bind(widget, source, 'setAccessibleName')
        return widget

    def retranslate(self, language=None):
        if not isValid(self) or self._closed:
            return
        self._lang = normalize_language(language if isinstance(language, str) else
                                        getattr(self._language_settings, 'ui_language', 'ru'))
        for (widget, setter), (source, upper, values) in tuple(self._bindings.items()):
            if isValid(widget):
                text = self._tr(source, **values)
                getattr(widget, setter)(text.upper() if upper else text)
        if hasattr(self, 'theme_combo'):
            blocked = self.theme_combo.blockSignals(True)
            try:
                for index, source in enumerate(('Системная', 'Тёмная', 'Светлая')):
                    self.theme_combo.setItemText(index, self._tr(source))
            finally:
                self.theme_combo.blockSignals(blocked)
        if hasattr(self, '_status_parts'):
            self._render_status()

    def _current(self):
        if self._closed or not isValid(self):
            return False
        if not self._has_context:
            return True  # Small legacy/test services have no session API.
        if self._context is None:
            return False
        try:
            self.service._require_history_context(self._context)
            return True
        except Exception:
            return False

    def _run(self, method, *args, on_done, on_error, **kwargs):
        if not self._current():
            return
        key = getattr(method, '__name__', 'operation')
        request = object()
        self._jobs[key] = request

        def current():
            return self._jobs.get(key) is request and self._current()

        def worker(*values, **options):
            if not current():
                raise RuntimeError('Settings dialog context is no longer current')
            try:
                return method(*values, **options)
            except MaxWriteOutcomeUnknown:
                return _UnconfirmedWrite()

        def deliver(callback, result):
            if current():
                self._jobs.pop(key, None)
                if isinstance(result, _UnconfirmedWrite):
                    if hasattr(self, 'error'):
                        self._unknown()
                    else:
                        on_error('')
                else:
                    callback(result)

        run_async(worker, *args, on_done=lambda result: deliver(on_done, result),
                  on_error=lambda error: deliver(on_error, error), **kwargs)

    def _err(self, text, *, raw=False):
        if not self._current():
            return
        self.error.setTextFormat(Qt.TextFormat.PlainText)
        if raw:
            self._bindings.pop((self.error, 'setText'), None)
            self.error.setText(str(text))
        else:
            self._bind(self.error, text)
        self.error.show()

    def _unknown(self):
        if not self._current():
            return
        self._uncertain = True
        for name in ('btn', 'send_btn', 'confirm_btn'):
            button = getattr(self, name, None)
            if button is not None:
                if button.text() == '…':
                    self._bind(button, 'Результат не подтверждён')
                button.setEnabled(False)
        self._err('Результат изменения пока не подтверждён. Повторная отправка в этом окне отключена. Закройте окно и проверьте состояние в настройках MAX.')

    def _raw_warning(self, title, text):
        if not self._current():
            return
        box = QMessageBox(QMessageBox.Icon.Warning, self._tr(title), '', parent=self)
        box.setTextFormat(Qt.TextFormat.PlainText)
        box.setText(str(text))
        box.exec()

    def _plain_information(self, title, text):
        if not self._current():
            return
        box = QMessageBox(QMessageBox.Icon.Information, self._tr(title), '', parent=self)
        box.setTextFormat(Qt.TextFormat.PlainText)
        box.setText(text)
        box.exec()

    def done(self, result):
        self._closed = True
        self._jobs.clear()
        super().done(result)

    def closeEvent(self, event):
        self._closed = True
        self._jobs.clear()
        super().closeEvent(event)


class SettingsDialog(_LocalizedDialog):
    theme_changed = Signal()
    logout_requested = Signal()

    def __init__(self, service: MaxService, settings: Settings, parent: QWidget | None = None) -> None:
        super().__init__(service, parent, settings)
        self.settings = settings
        self._bind(self, "Настройки", 'setWindowTitle')
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
        if not prof.name:
            self._bind(self._name_label, "Мой профиль")
        self._name_label.setTextFormat(Qt.TextFormat.PlainText)
        self._name_label.setStyleSheet("font-size:17px; font-weight:700;")
        pcol.addWidget(self._name_label)
        sub = QLabel(prof.phone or (f"id {prof.id}" if prof.id else "—"))
        sub.setTextFormat(Qt.TextFormat.PlainText)
        sub.setObjectName("ChatSubtitle")
        sub.setStyleSheet("color: palette(mid);")
        pcol.addWidget(sub)
        head.addLayout(pcol, 1)
        edit_name = self._widget(QPushButton, "Изменить имя")
        edit_name.clicked.connect(self._edit_name)
        head.addWidget(edit_name, 0, Qt.AlignmentFlag.AlignVCenter)
        root.addLayout(head)
        root.addWidget(_divider())

        # Внешний вид.
        root.addWidget(self._bind(_section("Внешний вид"), "Внешний вид", upper=True))
        theme_row = QHBoxLayout()
        theme_row.addWidget(self._widget(QLabel, "Тема"))
        theme_row.addStretch(1)
        self.theme_combo = QComboBox()
        self.theme_combo.addItem(self._tr("Системная"), "system")
        self.theme_combo.addItem(self._tr("Тёмная"), "dark")
        self.theme_combo.addItem(self._tr("Светлая"), "light")
        self.theme_combo.setCurrentIndex(self.theme_combo.findData(self.settings.theme))
        self.theme_combo.currentIndexChanged.connect(self._on_theme)
        theme_row.addWidget(self.theme_combo)
        root.addLayout(theme_row)

        accent_row = QHBoxLayout()
        accent_row.addWidget(self._widget(QLabel, "Акцент"))
        accent_row.addStretch(1)
        for color in _ACCENTS:
            sw = _Swatch(color, color == self.settings.accent)
            sw.clicked.connect(lambda _=False, c=color: self._on_accent(c))
            accent_row.addWidget(sw)
        self._accent_row = accent_row
        root.addLayout(accent_row)
        root.addWidget(_divider())

        # Уведомления и поведение.
        root.addWidget(self._bind(_section("Уведомления и ввод"), "Уведомления и ввод", upper=True))
        self.notif = self._widget(QCheckBox, "Уведомления о новых сообщениях")
        self.notif.setChecked(self.settings.notifications)
        self.notif.toggled.connect(self._on_notif)
        root.addWidget(self.notif)

        self.enter = self._widget(QCheckBox, "Отправка по Enter (Shift+Enter — перенос)")
        self.enter.setChecked(self.settings.send_on_enter)
        self.enter.toggled.connect(self._on_enter)
        root.addWidget(self.enter)
        root.addWidget(_divider())

        # Безопасность.
        root.addWidget(self._bind(_section("Безопасность"), "Безопасность", upper=True))
        self.twofa_status = self._widget(QLabel, "2FA: загрузка статуса…")
        self.twofa_status.setTextFormat(Qt.TextFormat.PlainText)
        self.twofa_status.setObjectName("ChatSubtitle")
        self.twofa_status.setWordWrap(True)
        self.twofa_status.setStyleSheet("color: palette(mid);")
        root.addWidget(self.twofa_status)

        self._twofa_enabled = None  # None=неизвестно, пока не загрузили статус
        self.twofa_btn = self._widget(QPushButton, "Сменить пароль 2FA")
        self.twofa_btn.setEnabled(False)
        self.twofa_btn.clicked.connect(self._twofa_primary)
        root.addWidget(self.twofa_btn)

        self.email_btn = self._widget(QPushButton, "Recovery email")
        self.email_btn.setEnabled(False)
        self.email_btn.clicked.connect(self._change_email)
        root.addWidget(self.email_btn)

        sessions_btn = self._widget(QPushButton, "Активные сессии и устройства")
        sessions_btn.clicked.connect(self._show_sessions)
        root.addWidget(sessions_btn)

        kick_btn = self._widget(QPushButton, "Завершить все другие устройства")
        kick_btn.setObjectName("Danger")
        kick_btn.setEnabled(False)
        self._bind(kick_btn, "Завершение других устройств доступно в официальном приложении MAX", "setToolTip")
        kick_btn.clicked.connect(self._terminate_others)
        root.addWidget(kick_btn)
        root.addWidget(_divider())

        # О приложении.
        root.addWidget(self._bind(_section("О приложении"), "О приложении", upper=True))
        about = self._bind(QLabel(), '{app} {version}\nПротокол MAX: app {app_version}, proto v{protocol}\napi.oneme.ru · неофициальный клиент',
                           app=__app_name__, version=__version__, app_version=self.settings.app_version, protocol=PROTO_VER)
        about.setTextFormat(Qt.TextFormat.PlainText)
        about.setObjectName("ChatSubtitle")
        about.setStyleSheet("color: palette(mid); font-size:12.5px;")
        root.addWidget(about)

        root.addStretch(1)
        logout = self._widget(QPushButton, "Выйти из аккаунта")
        logout.setObjectName("Danger")
        logout.clicked.connect(self._logout)
        root.addWidget(logout)

        self._load_2fa()

    # ───────────────────────── handlers ─────────────────────────

    def _on_theme(self) -> None:
        if not self._current():
            return
        self.settings.theme = self.theme_combo.currentData()
        self.settings.save()
        self.theme_changed.emit()

    def _on_accent(self, color: str) -> None:
        if not self._current():
            return
        self.settings.accent = color
        self.settings.save()
        for i in range(self._accent_row.count()):
            w = self._accent_row.itemAt(i).widget()
            if isinstance(w, _Swatch):
                w.set_selected(w.color == color)
        self.theme_changed.emit()

    def _on_notif(self, value: bool) -> None:
        if not self._current():
            return
        self.settings.notifications = value
        self.settings.save()

    def _on_enter(self, value: bool) -> None:
        if not self._current():
            return
        self.settings.send_on_enter = value
        self.settings.save()

    def _edit_name(self) -> None:
        if not self._current():
            return
        new_name, ok = QInputDialog.getText(
            self, self._tr('Изменить имя'), self._tr('Имя в MAX (видно собеседникам):'),
            text=self._name_label.text(),
        )
        if not ok or not new_name.strip():
            return
        self._run(
            self.service.update_my_name, new_name.strip(),
            on_done=self._on_name_changed,
            on_error=lambda e: self._raw_warning("Не удалось", e),
        )

    def _on_name_changed(self, name: str) -> None:
        if not self._current():
            return
        self._bindings.pop((self._name_label, 'setText'), None)
        self._name_label.setText(name)
        QMessageBox.information(self, self._tr('Готово'), self._tr('Имя профиля изменено.'))

    def _show_sessions(self) -> None:
        if not self._current():
            return
        self._run(
            self.service.list_sessions,
            on_done=self._on_sessions,
            on_error=lambda e: self._raw_warning("Сессии", e),
        )

    def _on_sessions(self, sessions: list) -> None:
        if not self._current():
            return
        if not sessions:
            QMessageBox.information(self, self._tr('Сессии'), self._tr('Сервер не вернул данные о сессиях.'))
            return
        lines = []
        for s in sessions:
            mark = "  ● " + self._tr('текущая') if s.get("current") else ""
            loc = f"\n    {s['location']}" if s.get("location") else ""
            lines.append(f"• {s.get('label', self._tr('Сессия'))}{mark}{loc}")
        box = QMessageBox(QMessageBox.Icon.Information, self._tr('Активные сессии'), '', parent=self)
        box.setTextFormat(Qt.TextFormat.PlainText)
        box.setText('\n'.join(lines))
        box.exec()

    def _load_2fa(self) -> None:
        if not self._current():
            return
        self._twofa_enabled = None
        self._status_parts = [('2FA: загрузка статуса…', {})]
        self._render_status()
        self.twofa_btn.setEnabled(False)
        self.email_btn.setEnabled(False)
        self._run(
            self.service.get_2fa_status,
            on_done=self._on_2fa_status,
            on_error=self._on_2fa_error,
            **({'context': self._context} if self._has_context else {}),
        )

    def _render_status(self):
        self._bindings.pop((self.twofa_status, 'setText'), None)
        self.twofa_status.setText(' · '.join(self._tr(source, **values) for source, values in self._status_parts))

    def _on_2fa_status(self, status: dict) -> None:
        if not self._current():
            return
        enabled = status.get('enabled') if isinstance(status, dict) else None
        if type(enabled) is not bool:
            self._on_2fa_error(None)
            return
        self._twofa_enabled = enabled
        email = status.get("email")
        hint = status.get("hint")
        self.twofa_btn.setEnabled(True)
        if self._twofa_enabled:
            parts = [("2FA включён", {})]
            if email:
                parts.append(('Почта для восстановления: {email}', {'email': email}))
            if hint:
                parts.append(('Подсказка: {hint}', {'hint': hint}))
            self._status_parts = parts
            self._bind(self.twofa_btn, "Сменить пароль 2FA")
            self.email_btn.setEnabled(True)
            self._bind(self.email_btn, "Сменить recovery email" if email else "Привязать recovery email")
        else:
            self._status_parts = [('2FA выключен', {})]
            self._bind(self.twofa_btn, "Включить 2FA")
            self.email_btn.setEnabled(False)
            self._bind(self.email_btn, "Recovery email (нужен 2FA)")
        self._render_status()

    def _on_2fa_error(self, _e: object) -> None:
        if not self._current():
            return
        # Статус не загрузился — НЕ угадываем enable/change (иначе на 2FA-OFF
        # аккаунте «Сменить пароль» выдал бы ложное «неверный пароль»). Кнопка
        # перечитывает статус.
        self._twofa_enabled = None
        self._status_parts = [('2FA: не удалось загрузить статус (нажмите, чтобы повторить)', {})]
        self._render_status()
        self._bind(self.twofa_btn, "Обновить статус 2FA")
        self.twofa_btn.setEnabled(True)
        self.email_btn.setEnabled(False)
        self._bind(self.email_btn, "Recovery email (нужен 2FA)")

    def _twofa_primary(self) -> None:
        if not self._current():
            return
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
        if not self._current():
            return
        if not self._twofa_enabled:
            QMessageBox.information(
                self, self._tr('Recovery email'),
                self._tr('Сначала включите 2FA — recovery email привязывается к нему.'),
            )
            return
        dlg = RecoveryEmailDialog(self.service, self)
        dlg.exec()
        self._load_2fa()

    def _terminate_others(self) -> None:
        if not self._current():
            return
        confirm = QMessageBox.question(
            self, self._tr('Завершить другие устройства?'),
            self._tr('Все другие сессии будут завершены. Самый надёжный способ выкинуть другие устройства — сменить пароль 2FA (тогда они потребуют повторный вход). Продолжить попытку завершения сессий?'),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        self._run(
            self.service.terminate_other_sessions,
            on_done=self._on_terminated,
            on_error=lambda e: self._raw_warning("Сессии", e),
        )

    def _on_terminated(self, ok: bool) -> None:
        if not self._current():
            return
        if ok:
            QMessageBox.information(self, self._tr('Готово'), self._tr('Запрос на завершение сессий отправлен.'))
        else:
            QMessageBox.warning(
                self, self._tr('Не удалось'),
                self._tr('Сервер не дал завершить сессии по этому протоколу (у сессий нет идентификатора). Проверьте список устройств в официальном приложении MAX.'),
            )

    def _logout(self) -> None:
        if not self._current():
            return
        confirm = QMessageBox.question(
            self, self._tr('Выйти?'),
            self._tr('Локальная история и контакты останутся, но потребуется повторный вход.'),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if confirm == QMessageBox.StandardButton.Yes and self._current():
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


class Change2FADialog(_LocalizedDialog):
    """Смена пароля после обычной проверки текущего пароля на сервере."""

    def __init__(self, service: MaxService, parent: QWidget | None = None) -> None:
        super().__init__(service, parent)
        self._bind(self, "Сменить пароль 2FA", 'setWindowTitle')
        self.resize(380, 390)
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 20, 20, 20)
        root.setSpacing(10)

        title = self._widget(QLabel, "Смена пароля 2FA")
        title.setStyleSheet("font-size:16px; font-weight:700;")
        root.addWidget(title)

        info = self._widget(QLabel, 'Введите текущий и новый пароль. Изменение выполнится после проверки текущего пароля сервером MAX.')
        info.setWordWrap(True)
        info.setObjectName("ChatSubtitle")
        info.setStyleSheet("color: palette(mid);")
        root.addWidget(info)

        self.current = self._placeholder(_pw_field("Текущий пароль"), "Текущий пароль")
        self.new = self._placeholder(_pw_field("Новый пароль"), "Новый пароль")
        self.confirm = self._placeholder(_pw_field("Повторите новый пароль"), "Повторите новый пароль")
        root.addWidget(self.current)
        root.addWidget(self.new)
        root.addWidget(self.confirm)
        self._bind(_add_show_toggle(root, self.current, self.new, self.confirm), "Показать пароль")

        self.hint = QLineEdit()
        self._placeholder(self.hint, "Подсказка (необязательно)")
        root.addWidget(self.hint)

        self.error = QLabel()
        self.error.setTextFormat(Qt.TextFormat.PlainText)
        self.error.setWordWrap(True)
        self.error.setStyleSheet("background: rgba(229,72,77,0.12); color:#E5484D; border-radius:8px; padding:8px 10px;")
        self.error.hide()
        root.addWidget(self.error)

        root.addStretch(1)
        self.btn = self._widget(QPushButton, "Сменить пароль")
        self.btn.setObjectName("Primary")
        self.btn.clicked.connect(self._submit)
        root.addWidget(self.btn)

    def _submit(self) -> None:
        if not self._current() or self._uncertain or not self.btn.isEnabled():
            return
        current = self.current.text()
        new = self.new.text()
        confirm = self.confirm.text()
        if not current:
            self._err("Введите текущий пароль.")
            return
        if not new:
            self._err("Введите новый пароль.")
            return
        if new != confirm:
            self._err("Новые пароли не совпадают.")
            return
        self.error.hide()
        self.btn.setEnabled(False)
        self._bind(self.btn, "…")
        self._run(
            self.service.change_2fa, new, self.hint.text().strip() or None,
            current_password=current,
            on_done=lambda _r: self._ok(),
            on_error=self._fail,
            **({'context': self._context} if self._has_context else {}),
        )

    def _ok(self) -> None:
        if not self._current():
            return
        QMessageBox.information(
            self, self._tr('Готово'),
            self._tr('Пароль 2FA изменён. Список устройств можно проверить в настройках MAX.'),
        )
        self.accept()

    def _fail(self, message: str) -> None:
        if not self._current():
            return
        if isinstance(message, MaxWriteOutcomeUnknown):
            self._unknown()
            return
        self.btn.setEnabled(True)
        self._bind(self.btn, "Сменить пароль")
        self._err(message, raw=True)


def _pw_field(placeholder: str) -> QLineEdit:
    f = QLineEdit()
    f.setPlaceholderText(placeholder)
    f.setEchoMode(QLineEdit.EchoMode.Password)
    return f


def _add_show_toggle(layout, *fields: QLineEdit) -> QCheckBox:
    """Чекбокс «Показать пароль» — переключает видимость переданных полей."""
    cb = QCheckBox("Показать пароль")

    def _t(on: bool) -> None:
        mode = QLineEdit.EchoMode.Normal if on else QLineEdit.EchoMode.Password
        for f in fields:
            f.setEchoMode(mode)

    cb.toggled.connect(_t)
    layout.addWidget(cb)
    return cb


class Enable2FADialog(_LocalizedDialog):
    """Включение 2FA, когда он выключен: только новый пароль (+ подсказка),
    без текущего — авторизует токен. CREATE_TRACK -> SET_2FA."""

    def __init__(self, service: MaxService, parent: QWidget | None = None) -> None:
        super().__init__(service, parent)
        self._bind(self, "Включить 2FA", 'setWindowTitle')
        self.resize(380, 320)
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 20, 20, 20)
        root.setSpacing(10)

        title = self._widget(QLabel, "Включение 2FA")
        title.setStyleSheet("font-size:16px; font-weight:700;")
        root.addWidget(title)

        info = self._widget(QLabel, 'Пароль 2FA будет запрашиваться при входе на новых устройствах. Запомните его — без него (и без recovery email) вход не восстановить.')
        info.setWordWrap(True)
        info.setObjectName("ChatSubtitle")
        info.setStyleSheet("color: palette(mid);")
        root.addWidget(info)

        self.new = self._placeholder(_pw_field("Пароль 2FA"), "Пароль 2FA")
        self.confirm = self._placeholder(_pw_field("Повторите пароль"), "Повторите пароль")
        root.addWidget(self.new)
        root.addWidget(self.confirm)
        self._bind(_add_show_toggle(root, self.new, self.confirm), "Показать пароль")

        self.hint = QLineEdit()
        self._placeholder(self.hint, "Подсказка (необязательно)")
        root.addWidget(self.hint)

        self.error = QLabel()
        self.error.setTextFormat(Qt.TextFormat.PlainText)
        self.error.setWordWrap(True)
        self.error.setStyleSheet(
            "background: rgba(229,72,77,0.12); color:#E5484D; border-radius:8px; padding:8px 10px;"
        )
        self.error.hide()
        root.addWidget(self.error)

        root.addStretch(1)
        self.btn = self._widget(QPushButton, "Включить 2FA")
        self.btn.setObjectName("Primary")
        self.btn.clicked.connect(self._submit)
        root.addWidget(self.btn)
        # The compact initial dialog must not squeeze a styled single-line button.
        self.btn.ensurePolished()
        self.btn.setMinimumHeight(self.btn.sizeHint().height())

    def _submit(self) -> None:
        if not self._current() or self._uncertain or not self.btn.isEnabled():
            return
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
        self._bind(self.btn, "…")
        self._run(
            self.service.enable_2fa, new, self.hint.text().strip() or None,
            on_done=lambda _r: self._ok(),
            on_error=self._fail,
            **({'context': self._context} if self._has_context else {}),
        )

    def _ok(self) -> None:
        if not self._current():
            return
        QMessageBox.information(self, self._tr('Готово'), self._tr('2FA включён.'))
        self.accept()

    def _fail(self, message: str) -> None:
        if not self._current():
            return
        if isinstance(message, MaxWriteOutcomeUnknown):
            self._unknown()
            return
        self.btn.setEnabled(True)
        self._bind(self.btn, "Включить 2FA")
        self._err(message, raw=True)


class RecoveryEmailDialog(_LocalizedDialog):
    """Проверка текущего пароля, затем подтверждение email кодом из письма."""

    def __init__(self, service: MaxService, parent: QWidget | None = None) -> None:
        super().__init__(service, parent)
        self._track_id: str | None = None
        self._bind(self, "Recovery email", 'setWindowTitle')
        self.resize(380, 320)
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 20, 20, 20)
        root.setSpacing(10)

        title = self._widget(QLabel, "Recovery email для 2FA")
        title.setStyleSheet("font-size:16px; font-weight:700;")
        root.addWidget(title)

        info = self._widget(QLabel, 'Введите текущий пароль 2FA. После его проверки на указанную почту придёт код подтверждения.')
        info.setWordWrap(True)
        info.setObjectName("ChatSubtitle")
        info.setStyleSheet("color: palette(mid);")
        root.addWidget(info)

        # Шаг 1.
        self.current = self._placeholder(_pw_field("Текущий пароль 2FA"), "Текущий пароль 2FA")
        root.addWidget(self.current)
        self.email = QLineEdit()
        self._placeholder(self.email, "Email (на него придёт код)")
        root.addWidget(self.email)
        self.send_btn = self._widget(QPushButton, "Отправить код")
        self.send_btn.setObjectName("Primary")
        self.send_btn.clicked.connect(self._send)
        root.addWidget(self.send_btn)

        # Шаг 2 (появляется после отправки кода).
        self.code = QLineEdit()
        self._placeholder(self.code, "Код из письма")
        self.code.hide()
        root.addWidget(self.code)
        self.confirm_btn = self._widget(QPushButton, "Подтвердить")
        self.confirm_btn.setObjectName("Primary")
        self.confirm_btn.clicked.connect(self._confirm)
        self.confirm_btn.hide()
        root.addWidget(self.confirm_btn)

        self.error = QLabel()
        self.error.setTextFormat(Qt.TextFormat.PlainText)
        self.error.setWordWrap(True)
        self.error.setStyleSheet(
            "background: rgba(229,72,77,0.12); color:#E5484D; border-radius:8px; padding:8px 10px;"
        )
        self.error.hide()
        root.addWidget(self.error)
        root.addStretch(1)

    def _send(self) -> None:
        if not self._current() or self._uncertain or not self.send_btn.isEnabled():
            return
        if not self.current.text():
            self._err("Введите текущий пароль 2FA.")
            return
        self.error.hide()
        self.send_btn.setEnabled(False)
        self._bind(self.send_btn, "…")
        self._run(
            self.service.start_set_recovery_email, self.email.text(),
            current_password=self.current.text(),
            on_done=self._code_sent,
            on_error=self._send_fail,
            **({'context': self._context} if self._has_context else {}),
        )

    def _code_sent(self, track_id: str) -> None:
        if not self._current():
            return
        self._track_id = track_id
        self.current.clear()
        self.current.setEnabled(False)
        self.email.setEnabled(False)
        self._bind(self.send_btn, "Код отправлен")
        self.code.show()
        self.confirm_btn.show()
        self._plain_information(
            'Код отправлен',
            self._tr("Код подтверждения отправлен на {email}. Введите его из письма.", email=self.email.text().strip()),
        )

    def _send_fail(self, message: str) -> None:
        if not self._current():
            return
        if isinstance(message, MaxWriteOutcomeUnknown):
            self._unknown()
            return
        self.send_btn.setEnabled(True)
        self._bind(self.send_btn, "Отправить код")
        self._err(message, raw=True)

    def _confirm(self) -> None:
        if not self._current() or self._uncertain or not self.confirm_btn.isEnabled():
            return
        if not self._track_id:
            self._err("Сначала отправьте код.")
            return
        self.error.hide()
        self.confirm_btn.setEnabled(False)
        self._bind(self.confirm_btn, "…")
        self._run(
            self.service.confirm_recovery_email, self._track_id, self.code.text(),
            on_done=lambda _r: self._ok(),
            on_error=self._confirm_fail,
            **({'context': self._context} if self._has_context else {}),
        )

    def _ok(self) -> None:
        if not self._current():
            return
        QMessageBox.information(self, self._tr('Готово'), self._tr('Recovery email привязан.'))
        self.accept()

    def _confirm_fail(self, message: str) -> None:
        if not self._current():
            return
        if isinstance(message, MaxWriteOutcomeUnknown):
            self._unknown()
            return
        self.confirm_btn.setEnabled(True)
        self._bind(self.confirm_btn, "Подтвердить")
        self._err(message, raw=True)

