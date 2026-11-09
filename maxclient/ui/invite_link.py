"""Explicit local copy of the invitation supplied by the current MAX session."""
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QApplication, QFrame, QHBoxLayout, QLabel
from shiboken6 import isValid

from .widgets.line_icon import line_icon


def copy_invitation(service, *, context):
    # No RPC and no fallback to a fabricated profile URL. Recheck after reading
    # so that a reconnect/account switch cannot put another owner's data in it.
    if context is None or service.search_context() != context:
        raise ValueError('Сессия MAX изменилась. Повторите действие.')
    text = service.invite_link_text(context=context)
    if service.search_context() != context:
        raise ValueError('Сессия MAX изменилась. Повторите действие.')
    if not isinstance(text, str) or not text.strip():
        raise ValueError('MAX пока не передал текст приглашения. Переподключитесь и попробуйте снова.')
    QApplication.clipboard().setText(text)


def show_menu_notice(parent, text):
    """Nonmodal local toast; never adds an item to a chat or a notification log."""
    if not isValid(parent):
        return
    window = parent.window()
    old = getattr(window, '_menu_notice', None)
    if old is not None and isValid(old):
        old.hide()
        old.deleteLater()
    frame = QFrame(window)
    frame.setObjectName('MenuNotice')
    frame.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
    frame.setStyleSheet('QFrame#MenuNotice {background:#66696E;border-radius:14px;}'
                        'QLabel {color:white;background:transparent;border:none;}')
    layout = QHBoxLayout(frame)
    layout.setContentsMargins(18, 15, 20, 15)
    layout.setSpacing(12)
    icon = QLabel(frame)
    icon.setPixmap(line_icon('link', '#FFFFFF', 22).pixmap(22, 22))
    layout.addWidget(icon)
    label = QLabel(text, frame)
    label.setObjectName('MenuNoticeText')
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    layout.addWidget(label)
    frame.ensurePolished()
    label.ensurePolished()
    # QLabel's wrapped sizeHint can shrink a short notice to a narrow column.
    # Size the text explicitly, keeping a short success sentence on one line.
    limit = max(180, min(400, window.width() - 100))
    label.setFixedWidth(min(limit, max(220, label.fontMetrics().horizontalAdvance(text) + 16)))
    frame.adjustSize()
    frame.move(16, max(0, window.height() - frame.height() - 16))
    frame.show()
    frame.raise_()
    timer = QTimer(frame)
    timer.setSingleShot(True)
    timer.timeout.connect(frame.hide)
    timer.start(5000)
    window._menu_notice = frame
