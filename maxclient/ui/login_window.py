"""Окно входа: по SMS (как в оригинальном MAX) и по auth-токену (web.max.ru).

SMS-флоу: телефон → код из SMS → (опционально) пароль 2FA.
Токен-флоу: вставить auth-token; сервер принимает веб-токен только при WEB.
"""
from __future__ import annotations

from enum import Enum, auto
from typing import Optional

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QButtonGroup,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..core.config import Settings
from ..core.service import MaxService
from .assets import logo_pixmap
from .async_runner import run_async


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
        self._mode_token = False
        self._step = _Step.PHONE
        self._verify_token: Optional[str] = None
        self._track_id: Optional[str] = None
        self._busy = False

        # Сторож: если вход висит дольше таймаута — снять "…" и не молчать.
        self._watchdog = QTimer(self)
        self._watchdog.setSingleShot(True)
        self._watchdog.setInterval(45000)
        self._watchdog.timeout.connect(self._on_watchdog)

        self.setWindowTitle("Вход в MAX")
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
            logo.setText("MAX")
            logo.setFixedSize(76, 76)
            logo.setStyleSheet(
                f"background:{self.settings.accent}; color:white; border-radius:22px;"
                "font-size:22px; font-weight:800; letter-spacing:1px;"
            )
        logo.setAlignment(Qt.AlignmentFlag.AlignCenter)
        col.addWidget(logo, 0, Qt.AlignmentFlag.AlignHCenter)

        title = QLabel("MAX Desktop")
        title.setStyleSheet("font-size:22px; font-weight:700;")
        title.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        col.addWidget(title)

        # Переключатель способа входа.
        seg = QHBoxLayout()
        seg.setSpacing(0)
        self.btn_sms = QPushButton("По SMS")
        self.btn_token = QPushButton("По токену")
        self.btn_sms.setCheckable(True)
        self.btn_token.setCheckable(True)
        self.btn_sms.setChecked(True)
        group = QButtonGroup(self)
        group.addButton(self.btn_sms)
        group.addButton(self.btn_token)
        group.setExclusive(True)
        self.btn_sms.clicked.connect(lambda: self._set_mode(False))
        self.btn_token.clicked.connect(lambda: self._set_mode(True))
        seg.addWidget(self.btn_sms)
        seg.addWidget(self.btn_token)
        col.addLayout(seg)

        self.hint = QLabel()
        self.hint.setWordWrap(True)
        self.hint.setObjectName("ChatSubtitle")
        col.addWidget(self.hint)

        self.error = QLabel()
        self.error.setWordWrap(True)
        self.error.setStyleSheet(
            "background: rgba(229,72,77,0.12); color:#E5484D;"
            "border-radius:8px; padding:8px 10px;"
        )
        self.error.hide()
        col.addWidget(self.error)

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

        self.token = QPlainTextEdit()
        self.token.setPlaceholderText("Вставьте auth-token из web.max.ru…")
        self.token.setFixedHeight(110)
        col.addWidget(self.token)

        self.primary = QPushButton("Получить код")
        self.primary.setObjectName("Primary")
        self.primary.clicked.connect(self._primary)
        col.addWidget(self.primary)

        self.resend = QPushButton("Запросить SMS заново")
        self.resend.setObjectName("Ghost")
        self.resend.clicked.connect(self._resend)
        col.addWidget(self.resend)

        center.addWidget(card)
        center.addStretch(1)
        root.addLayout(center)
        root.addStretch(2)

    # ───────────────────────── state rendering ─────────────────────────

    def _set_mode(self, token_mode: bool) -> None:
        self._mode_token = token_mode
        self._step = _Step.PHONE
        self._set_error(None)
        self._render()

    def _render(self) -> None:
        token = self._mode_token
        self.token.setVisible(token)
        self.phone.setVisible(not token and self._step == _Step.PHONE)
        self.code.setVisible(not token and self._step == _Step.CODE)
        self.pw.setVisible(not token and self._step == _Step.TWOFA)
        self.resend.setVisible(not token and self._step in (_Step.CODE, _Step.TWOFA))

        if token:
            self.hint.setText(
                "Вставьте auth-token аккаунта MAX (web.max.ru → DevTools → "
                "Application → хранилище). SMS не нужен."
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
        self.primary.setText("…" if busy else self.primary.text())
        if not busy:
            self._render()

    def _set_error(self, text: Optional[str]) -> None:
        if text:
            self.error.setText(text)
            self.error.show()
        else:
            self.error.hide()

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
        token = self.token.toPlainText().strip()
        if len(token) < 20:
            self._set_error("Токен выглядит слишком коротким.")
            return
        self._set_busy(True)
        run_async(
            self.service.login_with_token, token,
            on_done=lambda _: self._emit_ok(), on_error=self._on_fail,
        )

    def _emit_ok(self) -> None:
        self._watchdog.stop()
        self.service.log("login_window: emit logged_in")
        self._set_busy(False)
        self.logged_in.emit()

    def _on_watchdog(self) -> None:
        if not self._busy:
            return
        self.service.log("login_window: WATCHDOG fired (login chain stuck)")
        self._set_busy(False)
        self._set_error(
            "Вход занял слишком долго и был прерван. Проверьте сеть/VPN и "
            "попробуйте снова."
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
        self.service.log(f"login_window: login failed: {message}")
        self._set_busy(False)
        self._set_error(message)
