"""Official server-issued, expiring QR challenge and ordinary 2FA prompt."""
from __future__ import annotations

import math
import time
from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QColor, QImage, QPainter, QPixmap
from PySide6.QtWidgets import QLabel, QLineEdit, QPushButton, QVBoxLayout, QWidget

from ..core.qr_login import QrLoginController
from .async_runner import run_async


def qr_pixmap(qr_link: str) -> QPixmap:
    """Encode only the server-returned challenge, with a four-module quiet zone."""
    import qrcode
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, border=4)
    qr.add_data(qr_link)
    qr.make(fit=True)
    matrix = qr.get_matrix()
    scale = max(1, 280 // len(matrix))
    image = QImage(len(matrix) * scale, len(matrix) * scale, QImage.Format.Format_RGB32)
    image.fill(Qt.GlobalColor.white)
    painter = QPainter(image)
    for y, row in enumerate(matrix):
        for x, black in enumerate(row):
            if black:
                painter.fillRect(x * scale, y * scale, scale, scale, QColor('#000000'))
    painter.end()
    return QPixmap.fromImage(image)


class QrLoginPanel(QWidget):
    authorized = Signal(object)

    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.settings = settings
        self._controller = None
        self._active = False
        self._busy = False
        self._generation = 0
        self._expires_at_ms = 0.0
        self._poll_interval = 1000
        self._poll_timer = QTimer(self)
        self._poll_timer.setSingleShot(True)
        self._poll_timer.timeout.connect(self._poll)
        self._clock = QTimer(self)
        self._clock.setInterval(1000)
        self._clock.timeout.connect(self._tick)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.code = QLabel()
        self.code.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.code.setMinimumSize(280, 280)
        layout.addWidget(self.code)
        self.status = QLabel('Создайте QR-код для входа на этот компьютер.')
        self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.status)
        self.instructions = QLabel('Сканируйте QR сканером в приложении MAX на устройстве, где уже выполнен вход.')
        self.instructions.setWordWrap(True)
        self.instructions.setObjectName('ChatSubtitle')
        layout.addWidget(self.instructions)
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.password.setPlaceholderText('Пароль 2FA этого аккаунта')
        self.password.returnPressed.connect(self._check_password)
        self.password.hide()
        layout.addWidget(self.password)
        self.password_button = QPushButton('Подтвердить пароль')
        self.password_button.clicked.connect(self._check_password)
        self.password_button.hide()
        layout.addWidget(self.password_button)
        self.refresh_button = QPushButton('Создать новый QR-код')
        self.refresh_button.clicked.connect(self.refresh)
        layout.addWidget(self.refresh_button)

    def activate(self):
        if not self._active:
            self._active = True
            self.refresh()

    def deactivate(self):
        self._active = False
        self._generation += 1
        self._poll_timer.stop()
        self._clock.stop()
        self.code.clear()
        self.password.clear()
        controller, self._controller = self._controller, None
        self._busy = False
        if controller is not None:
            controller.cancel()
            run_async(controller.close)

    def refresh(self):
        if not self._active or self._busy:
            return
        self._generation += 1
        generation = self._generation
        self._poll_timer.stop()
        self._clock.stop()
        self.code.clear()
        self.password.clear()
        self.password.hide()
        self.password_button.hide()
        self.refresh_button.setEnabled(False)
        self.status.setText('Создание QR-кода…')
        self._busy = True
        old = self._controller
        if old is not None:
            old.cancel()
        controller = QrLoginController(self.settings.web_app_version, self.settings.locale)
        self._controller = controller
        def start():
            if old is not None:
                old.close()
            return controller.start()
        run_async(start, on_done=lambda value: self._created(generation, value),
                  on_error=lambda error: self._failed(generation, error))

    def _created(self, generation, challenge):
        if not self._alive(generation):
            return
        self._busy = False
        self._expires_at_ms = challenge.expires_at_ms
        self._poll_interval = challenge.polling_interval_ms
        try:
            self.code.setPixmap(qr_pixmap(challenge.qr_link))
        except Exception:
            self._failed(generation, 'Не удалось отобразить QR-код. Создайте новый.')
            return
        self.refresh_button.setEnabled(True)
        if not self._tick():
            return
        self._clock.start()
        self._poll_timer.start(self._poll_interval)

    def _poll(self):
        if not self._active or self._busy or self._controller is None:
            return
        self._busy = True
        generation = self._generation
        run_async(self._controller.poll, on_done=lambda value: self._received(generation, value),
                  on_error=lambda error: self._failed(generation, error))

    def _received(self, generation, result):
        if not self._alive(generation):
            return
        self._busy = False
        state = result.get('state')
        if state == 'waiting':
            self._expires_at_ms = result['expires_at_ms']
            if not self._tick():
                return
            self._poll_timer.start(self._poll_interval)
        elif state == 'password_required':
            self._clock.stop()
            self._poll_timer.stop()
            self.code.clear()
            hint = result.get('hint')
            self.status.setText('Подтвердите пароль 2FA.' + (f' Подсказка: {hint}' if hint else ''))
            self.password.show()
            self.password_button.show()
            self.password_button.setEnabled(True)
            self.password.setFocus()
        elif state == 'authorized':
            self._clock.stop()
            self._poll_timer.stop()
            self.code.clear()
            self.password.clear()
            self.password.hide()
            self.password_button.hide()
            self.refresh_button.setEnabled(False)
            self.status.setText('Вход подтверждён. Открываем профиль…')
            self.authorized.emit(result['authorization'])
        else:
            self._failed(generation, 'Срок действия QR-кода истёк. Создайте новый.')

    def _check_password(self):
        if not self._active or self._busy or self._controller is None:
            return
        password = self.password.text()
        if not password:
            self.status.setText('Введите пароль 2FA этого аккаунта.')
            return
        self.password.clear()
        self._busy = True
        self.password_button.setEnabled(False)
        generation = self._generation
        run_async(self._controller.check_password, password,
                  on_done=lambda value: self._received(generation, value),
                  on_error=lambda error: self._failed(generation, error))

    def _tick(self):
        # A completed worker may still have its callback queued behind a timer
        # event. Wait for that result (including passwordChallenge or a newer
        # server TTL) before applying the old local expiry.
        if self._busy:
            return True
        if self._controller is not None and self._controller.state == 'finishing':
            self._clock.stop()
            self.code.clear()
            self.status.setText('Получаем подтверждение входа…')
            return True
        remaining = math.ceil((self._expires_at_ms - time.time() * 1000) / 1000)
        if remaining <= 0:
            self._failed(self._generation, 'Срок действия QR-кода истёк. Создайте новый.')
            return False
        else:
            self.status.setText(f'Сканируйте QR и подтвердите вход на телефоне. Осталось {remaining} с.')
            return True

    def _failed(self, generation, error):
        if not self._alive(generation):
            return
        self._generation += 1
        self._poll_timer.stop()
        self._clock.stop()
        self._busy = False
        self.code.clear()
        self.password.clear()
        self.password.hide()
        self.password_button.hide()
        self.refresh_button.setEnabled(True)
        self.status.setText(str(error) or 'Не удалось завершить QR-вход. Создайте новый код.')
        controller, self._controller = self._controller, None
        if controller is not None:
            controller.cancel()
            run_async(controller.close)

    def _alive(self, generation):
        return self._active and generation == self._generation

    def authentication_failed(self, error):
        self._failed(self._generation, error)

    def closeEvent(self, event):
        self.deactivate()
        super().closeEvent(event)
