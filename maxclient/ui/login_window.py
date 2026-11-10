"""Окно входа: по SMS (как в оригинальном MAX) и по auth-токену (web.max.ru).

SMS-флоу: телефон → код из SMS → (опционально) пароль 2FA.
Токен-флоу: вставить auth-token; сервер принимает веб-токен только при WEB.
"""
from __future__ import annotations

from enum import Enum, auto
from typing import Optional
import uuid

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QButtonGroup,
    QFrame,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from .. import __app_name__
from ..core.config import Settings
from ..core.service import MaxService
from ..core.token_import import read_web_session
from ..protocol.errors import MaxLoginFailed
from .assets import logo_pixmap
from .async_runner import run_async
from .qr_login_panel import QrLoginPanel

# Why the saved sign-in did not open. The saved credential is never removed here.
SESSION_ENDED_TEXT = (
    "MAX завершил эту сессию MAXIM: например, вход завершили с другого устройства "
    "или истёк её срок. Сообщения и настройки на этом компьютере сохранены. "
    "Войдите снова по SMS или QR-коду."
)
RESTORE_UNREACHABLE_TEXT = (
    "Не удалось связаться с MAX: сервер не ответил или нет подключения к интернету. "
    "Сохранённый вход не удалён. Нажмите «Повторить», когда связь восстановится."
)


def session_ended_text(code: Optional[str] = None) -> str:
    """MAX ended or refused this session; its error code is appended when known."""
    if isinstance(code, str) and code:
        return f"{SESSION_ENDED_TEXT} (Код ошибки MAX: {code})"
    return SESSION_ENDED_TEXT


def restore_failure_text(error) -> tuple[Optional[str], bool]:
    """Text for a failed saved-session restore and whether «Повторить» is offered."""
    if error is None:
        return None, False
    if isinstance(error, MaxLoginFailed):
        return session_ended_text(getattr(error, "server_code", None)), False
    return RESTORE_UNREACHABLE_TEXT, True


class _Step(Enum):
    PHONE = auto()
    CODE = auto()
    TWOFA = auto()


class LoginWindow(QWidget):
    logged_in = Signal()

    def __init__(self, service: MaxService, settings: Settings) -> None:
        super().__init__()
        self.service = service
        self.settings = settings
        self._mode_token = True
        self._mode_qr = False
        self._step = _Step.PHONE
        self._verify_token: Optional[str] = None
        self._track_id: Optional[str] = None
        self._busy = False

        # Сторож: если вход висит дольше таймаута — снять "…" и не молчать.
        self._watchdog = QTimer(self)
        self._watchdog.setSingleShot(True)
        self._watchdog.setInterval(45000)
        self._watchdog.timeout.connect(self._on_watchdog)

        self.setWindowTitle(f"Вход в {__app_name__}")
        self.resize(560, 720)
        self._build()
        self._render()

    # ───────────────────────── layout ─────────────────────────

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.addStretch(1)

        center = QHBoxLayout()
        center.addStretch(1)

        card = QFrame()
        card.setFixedWidth(380)
        col = QVBoxLayout(card)
        col.setSpacing(14)
        col.setAlignment(Qt.AlignmentFlag.AlignTop)

        logo = QLabel()
        pix = logo_pixmap(80)
        if pix is not None:
            logo.setPixmap(pix)
        else:
            logo.setText(__app_name__)
            logo.setFixedSize(76, 76)
            logo.setStyleSheet(
                f"background:{self.settings.accent}; color:white; border-radius:22px;"
                "font-size:22px; font-weight:800; letter-spacing:1px;"
            )
        logo.setAlignment(Qt.AlignmentFlag.AlignCenter)
        col.addWidget(logo, 0, Qt.AlignmentFlag.AlignHCenter)

        title = QLabel(__app_name__)
        title.setStyleSheet("font-size:22px; font-weight:700;")
        title.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        col.addWidget(title)

        # Переключатель способа входа.
        seg = QHBoxLayout()
        seg.setSpacing(0)
        self.btn_sms = QPushButton("По SMS")
        self.btn_token = QPushButton("По токену")
        self.btn_qr = QPushButton("QR-код")
        self.btn_sms.setCheckable(True)
        self.btn_token.setCheckable(True)
        self.btn_qr.setCheckable(True)
        self.btn_token.setChecked(True)
        group = QButtonGroup(self)
        group.addButton(self.btn_sms)
        group.addButton(self.btn_token)
        group.addButton(self.btn_qr)
        group.setExclusive(True)
        self.btn_sms.clicked.connect(lambda: self._set_mode(False))
        self.btn_token.clicked.connect(lambda: self._set_mode(True))
        self.btn_qr.clicked.connect(self._set_qr_mode)
        seg.addWidget(self.btn_sms)
        seg.addWidget(self.btn_token)
        seg.addWidget(self.btn_qr)
        col.addLayout(seg)

        self.hint = QLabel()
        self.hint.setWordWrap(True)
        self.hint.setObjectName("ChatSubtitle")
        col.addWidget(self.hint)

        self.error = QLabel()
        self.error.setTextFormat(Qt.TextFormat.PlainText)
        self.error.setWordWrap(True)
        self.error.setStyleSheet(
            "background: rgba(229,72,77,0.12); color:#E5484D;"
            "border-radius:8px; padding:8px 10px;"
        )
        self.error.hide()
        col.addWidget(self.error)

        # Shown only with RESTORE_UNREACHABLE_TEXT: one restore per click.
        self.retry_restore = QPushButton("Повторить")
        self.retry_restore.setObjectName("RetryRestoreButton")
        self.retry_restore.clicked.connect(self._retry_restore)
        self.retry_restore.hide()
        col.addWidget(self.retry_restore)

        self.phone = QLineEdit()
        self.phone.setPlaceholderText("+7 999 123-45-67")
        self.phone.returnPressed.connect(self._primary)
        col.addWidget(self.phone)

        self.code = QLineEdit()
        self.code.setPlaceholderText("Код из SMS")
        self.code.returnPressed.connect(self._primary)
        col.addWidget(self.code)

        self.pw = QLineEdit()
        self.pw.setPlaceholderText("Пароль 2FA")
        self.pw.setEchoMode(QLineEdit.EchoMode.Password)
        self.pw.returnPressed.connect(self._primary)
        col.addWidget(self.pw)

        self.token = QLineEdit()
        self.token.setEchoMode(QLineEdit.EchoMode.Password)
        self.token.setPlaceholderText("Токен WEB-сессии MAX")
        self.token.setObjectName('WebSessionToken')
        self.token.returnPressed.connect(self._primary)
        col.addWidget(self.token)

        self.device_id = QLineEdit()
        self.device_id.setPlaceholderText('ID устройства из экспорта (UUID)')
        self.device_id.setObjectName('WebSessionDeviceId')
        col.addWidget(self.device_id)

        self.import_session = QPushButton('Импортировать файл сессии…')
        self.import_session.setObjectName('ImportWebSessionButton')
        self.import_session.clicked.connect(self._import_session)
        col.addWidget(self.import_session)

        self.primary = QPushButton("Получить код")
        self.primary.setObjectName("Primary")
        self.primary.clicked.connect(self._primary)
        col.addWidget(self.primary)

        self.resend = QPushButton("Запросить SMS заново")
        self.resend.setObjectName("Ghost")
        self.resend.clicked.connect(self._resend)
        col.addWidget(self.resend)

        self.qr_panel = QrLoginPanel(self.settings, self)
        self.qr_panel.authorized.connect(self._qr_authorized)
        self.qr_panel.hide()
        col.addWidget(self.qr_panel)

        center.addWidget(card)
        center.addStretch(1)
        root.addLayout(center)
        root.addStretch(2)

    # ───────────────────────── state rendering ─────────────────────────

    def _set_mode(self, token_mode: bool) -> None:
        self._mode_qr = False
        self.qr_panel.deactivate()
        self._mode_token = token_mode
        self._step = _Step.PHONE
        self._set_error(None)
        self._render()

    def _set_qr_mode(self) -> None:
        self._mode_qr = True
        self._set_error(None)
        self._render()
        self.qr_panel.activate()

    def _render(self) -> None:
        self.qr_panel.setVisible(self._mode_qr)
        self.hint.setVisible(not self._mode_qr)
        self.primary.setVisible(not self._mode_qr)
        if self._mode_qr:
            for widget in (self.token, self.device_id, self.import_session, self.phone,
                           self.code, self.pw, self.resend):
                widget.hide()
            return
        token = self._mode_token
        self.token.setVisible(token)
        self.device_id.setVisible(token)
        self.import_session.setVisible(token)
        self.phone.setVisible(not token and self._step == _Step.PHONE)
        self.code.setVisible(not token and self._step == _Step.CODE)
        self.pw.setVisible(not token and self._step == _Step.TWOFA)
        self.resend.setVisible(not token and self._step in (_Step.CODE, _Step.TWOFA))

        if token:
            self.hint.setText(
                "Импортируйте сохранённую WEB-сессию или укажите её токен и ID устройства. "
                "Для входа сервер должен принять токен."
            )
            self.primary.setText("Войти по токену")
        elif self._step == _Step.PHONE:
            self.hint.setText("Введите номер телефона в международном формате.")
            self.primary.setText("Получить код")
        elif self._step == _Step.CODE:
            self.hint.setText(f"Код отправлен на {self.service.session.phone or 'ваш номер'}.")
            self.primary.setText("Подтвердить код")
            self.code.setFocus()
        else:
            self.hint.setText("У аккаунта включён пароль 2FA. Введите его.")
            self.primary.setText("Войти")
            self.pw.setFocus()

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self.primary.setDisabled(busy)
        self.btn_sms.setDisabled(busy)
        self.btn_token.setDisabled(busy)
        self.btn_qr.setDisabled(busy)
        self.import_session.setDisabled(busy)
        self.resend.setDisabled(busy)
        self.retry_restore.setDisabled(busy)
        self.primary.setText("…" if busy else self.primary.text())
        if not busy:
            self._render()

    def _set_error(self, text: Optional[str]) -> None:
        if text:
            self.error.setText(text)
            self.error.show()
        else:
            self.error.hide()
            self.retry_restore.hide()

    def show_restore_error(self, error) -> None:
        """Explain why the saved sign-in did not open; network errors offer «Повторить»."""
        text, retry = restore_failure_text(error)
        self._set_error(text)
        self.retry_restore.setVisible(retry)

    # ───────────────────────── actions ─────────────────────────

    def _primary(self) -> None:
        if self._busy:
            return
        self._set_error(None)
        if self._mode_token:
            self._do_token()
        elif self._step == _Step.PHONE:
            self._do_request_sms()
        elif self._step == _Step.CODE:
            self._do_confirm_code()
        else:
            self._do_confirm_2fa()

    def _do_request_sms(self) -> None:
        phone = self.phone.text().strip()
        if len(phone) < 6:
            self._set_error("Введите корректный номер телефона.")
            return
        self._set_busy(True)
        run_async(
            self.service.request_sms, phone,
            on_done=self._on_sms_requested, on_error=self._on_fail,
        )

    def _on_sms_requested(self, verify_token: str) -> None:
        self._verify_token = verify_token
        self._step = _Step.CODE
        self._set_busy(False)

    def _do_confirm_code(self) -> None:
        code = self.code.text().strip()
        if not code:
            self._set_error("Введите код из SMS.")
            return
        self._set_busy(True)
        run_async(
            self.service.confirm_sms, self._verify_token, code,
            on_done=self._on_code_confirmed, on_error=self._on_fail,
        )

    def _on_code_confirmed(self, result) -> None:
        auth_token, track_id = result
        if auth_token:
            self.service.log("login_window: SMS ok, no 2FA -> finishing")
            self._finish_login(auth_token, "android")
        else:
            self.service.log("login_window: SMS ok, 2FA required")
            self._track_id = track_id
            self._step = _Step.TWOFA
            self._set_busy(False)

    def _do_confirm_2fa(self) -> None:
        pw = self.pw.text()
        if not pw:
            self._set_error("Введите пароль 2FA.")
            return
        self._set_busy(True)
        run_async(
            self.service.confirm_2fa, self._track_id, pw,
            on_done=lambda token: self._finish_login(token, "android"),
            on_error=self._on_fail,
        )

    def _finish_login(self, token: str, kind: str) -> None:
        self.service.log(f"login_window: finishing login (kind={kind})")
        self._set_busy(True)
        self._watchdog.start()
        run_async(
            self.service.complete_login, token, kind,
            on_done=lambda _: self._emit_ok(), on_error=self._on_fail,
        )

    def _do_token(self) -> None:
        token = self.token.text().strip()
        if len(token) < 100:
            self._set_error("Токен выглядит слишком коротким.")
            return
        device_id = self.device_id.text().strip() or None
        if device_id is not None:
            try:
                device_id = str(uuid.UUID(device_id))
            except ValueError:
                self._set_error('Укажите UUID устройства из исходного экспорта.')
                return
        self._set_busy(True)
        self._watchdog.start()
        run_async(
            self.service.login_with_token, token,
            device_id=device_id,
            on_done=lambda _: self._emit_ok(), on_error=self._on_fail,
        )

    def _qr_authorized(self, authorization) -> None:
        self._set_busy(True)
        self._watchdog.start()
        run_async(self.service.login_with_token, authorization.token,
                  device_id=authorization.device_id, expected_profile_id=authorization.profile_id,
                  on_done=lambda _: self._emit_ok(), on_error=self._on_fail)

    def closeEvent(self, event) -> None:
        self.qr_panel.deactivate()
        super().closeEvent(event)

    def _import_session(self) -> None:
        if self._busy:
            return
        path, _ = QFileDialog.getOpenFileName(self, 'Импорт WEB-сессии MAX', '', 'Сессия JSON (*.json)')
        if not path:
            return
        try:
            session = read_web_session(path)
        except (ValueError, OSError):
            self._set_error('Не удалось прочитать сессию. Нужен локальный JSON с токеном и deviceId.')
            return
        self.token.setText(session.token)
        self.device_id.setText(session.device_id)
        self._set_error(None)
        self.hint.setText('Файл сессии прочитан. Нажмите «Войти по токену».')

    def _emit_ok(self) -> None:
        self._watchdog.stop()
        self.service.log("login_window: emit logged_in")
        self._set_busy(False)
        self.logged_in.emit()

    def _retry_restore(self) -> None:
        """Restore the saved sign-in once per click; nothing retries automatically."""
        if self._busy:
            return
        self.service.log("login_window: restore retry")
        self._set_busy(True)
        self._watchdog.start()
        run_async(self.service.try_restore, on_done=self._on_restore_retry_done,
                  on_error=lambda _message: self._on_restore_retry_done(False))

    def _on_restore_retry_done(self, ok) -> None:
        if ok is True:
            self._set_error(None)
            self._emit_ok()
            return
        self._watchdog.stop()
        self._set_busy(False)
        self.show_restore_error(getattr(self.service, "last_restore_error", None))

    def _on_watchdog(self) -> None:
        if not self._busy:
            return
        self.service.log("login_window: WATCHDOG fired (login chain stuck)")
        self._set_error(
            "Ответ сервера задерживается. Ожидаем завершения текущей попытки; "
            "повторный вход пока недоступен."
        )

    def _resend(self) -> None:
        self._step = _Step.PHONE
        self._verify_token = None
        self.code.clear()
        self.pw.clear()
        self._set_error(None)
        self._render()

    def _on_fail(self, message: str) -> None:
        self._watchdog.stop()
        self.service.log('login_window: login failed')
        self._set_busy(False)
        self._set_error(message)
        if self._mode_qr:
            self.qr_panel.authentication_failed(message)
