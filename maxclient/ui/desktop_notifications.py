"""Non-activating desktop toast; explicit audio never delegates sound to the OS."""
import time
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, Qt, QUrl, Signal
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtWidgets import QApplication, QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout

from ..core.notification_settings import notification_settings
from ..core.notifications import NotificationPolicy


class MessageToast(QFrame):
    opened = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent, Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint |
                         Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setObjectName('MessageToast')
        self.setFixedWidth(360)
        self.chat_id = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 12, 14, 14)
        header = QHBoxLayout()
        self.title = QLabel()
        self.title.setTextFormat(Qt.TextFormat.PlainText)
        self.title.setWordWrap(True)
        self.title.setObjectName('NoticeTitle')
        header.addWidget(self.title, 1)
        close = QPushButton('×')
        close.setAccessibleName('Закрыть уведомление')
        close.setFixedSize(28, 28)
        close.clicked.connect(self.dismiss)
        header.addWidget(close)
        layout.addLayout(header)
        self.body = QLabel()
        self.body.setTextFormat(Qt.TextFormat.PlainText)
        self.body.setWordWrap(True)
        layout.addWidget(self.body)
        self.setStyleSheet('QFrame#MessageToast{background:#FFFFFF;border:1px solid #DDE0E7;border-radius:14px;}'
                           'QLabel{color:#17191D;background:transparent;border:0;font-size:14px;}'
                           '#NoticeTitle{font-weight:600;}'
                           'QPushButton{background:transparent;border:0;color:#656C77;font-size:20px;}')
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self.dismiss)

    def present(self, notice, *, show=True):
        self.chat_id = notice.chat_id
        self.title.setText(notice.title)
        self.body.setText(notice.body)
        self.adjustSize()
        screen = self.parentWidget().screen() if self.parentWidget() is not None else QApplication.primaryScreen()
        area = screen.availableGeometry()
        self.move(area.right() - self.width() - 18, area.bottom() - self.height() - 18)
        if show:
            self.show()
        self.timer.start(6000)

    def dismiss(self):
        self.timer.stop()
        self.hide()
        self.title.clear()
        self.body.clear()
        self.chat_id = None

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self.chat_id is not None:
            identity = self.chat_id
            self.dismiss()
            self.opened.emit(identity)
        super().mouseReleaseEvent(event)


class DesktopNotifications(QObject):
    def __init__(self, window, service, settings):
        super().__init__(window)
        self.window, self.service, self.settings = window, service, settings
        self.preferences = notification_settings(service)
        self.policy = NotificationPolicy(int(time.time() * 1000))
        self.toast = MessageToast(window)
        self.toast.opened.connect(self.open_chat)
        self.player = None
        self._toast_context = None
        self._toast_chat_id = None

    def receive(self, message):
        try:
            presenter = getattr(self.service, 'present_chat', None)
            context = self.service._history_read_context() if callable(presenter) else None
            chat = (presenter(message.chat_id, context=context) if context is not None
                    else self.service.store.get_chat(message.chat_id))
            preferences = (self.preferences.read(context=context) if context is not None
                           else self.preferences.read())
            active_chat = (self.window._current_chat_id
                           if self.window._workspace_pages.currentWidget() == self.window.splitter else None)
            notice = self.policy.decide(
                message, chat, preferences, now_ms=int(time.time() * 1000),
                local_enabled=bool(self.settings.notifications), opened_chat=active_chat,
                window_active=self.window.isActiveWindow(), viewer_id=self.service.my_id)
            if context is not None:
                self.service._require_history_context(context)
        except Exception:
            return  # Missing session/config never leaks a message preview.
        if notice is None:
            return
        self._toast_context, self._toast_chat_id = context, notice.chat_id
        self.toast.present(notice)
        if notice.sound:
            self._play_sound()

    def _play_sound(self):
        if self.player is None:
            self.audio = QAudioOutput(self)
            self.audio.setVolume(0.5)
            self.player = QMediaPlayer(self)
            self.player.setAudioOutput(self.audio)
            self.player.setSource(QUrl.fromLocalFile(str(Path(__file__).parents[1] / 'assets' / 'message.wav')))
        if self.player.playbackState() != QMediaPlayer.PlaybackState.PlayingState:
            self.player.setPosition(0)
            self.player.play()

    def open_chat(self, chat_id):
        context, identity = self._toast_context, self._toast_chat_id
        self._toast_context = self._toast_chat_id = None
        if context is None or type(chat_id) is not int or chat_id != identity:
            return
        try:
            self.service._require_history_context(context)
            if (self.window._bind_search_context() is not context[2]
                    or self.window._display_chat(chat_id) is None):
                return
            self.service._require_history_context(context)
        except (AttributeError, ValueError):
            return
        self.window.showNormal()
        self.window.raise_()
        self.window.activateWindow()
        try:
            self.service._require_history_context(context)
            if self.window._bind_search_context() is not context[2]:
                return
        except (AttributeError, ValueError):
            return
        # Reuse the shell's existing exact cached-chat navigation. It switches
        # workspaces and rechecks the session around its presentation lookup.
        self.window._open_search_result({'chat_id': chat_id})

    def clear(self):
        self._toast_context = self._toast_chat_id = None
        self.toast.dismiss()
        if self.player is not None:
            self.player.stop()
