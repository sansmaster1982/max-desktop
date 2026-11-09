"""Explicit non-owner leave confirmation. This module has no transport access."""
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QDialog, QDialogButtonBox, QLabel, QVBoxLayout

from ..core.i18n import tr


def simple_leave_available(capabilities):
    return (isinstance(capabilities, dict) and capabilities.get('supported') is True
            and capabilities.get('kind') in ('group', 'channel')
            and capabilities.get('owner') is False
            and capabilities.get('participant') is True
            and isinstance(capabilities.get('more'), list)
            and 'leave' in capabilities.get('more', []))


class ConversationLeaveDialog(QDialog):
    def __init__(self, title, *, channel=False, private=True, language='ru',
                 context_is_current=None, parent=None):
        super().__init__(parent)
        self._current = context_is_current or (lambda: True)
        self.setObjectName('ConversationLeaveDialog')
        self.setWindowModality(Qt.WindowModality.WindowModal)
        caption = tr('Отписаться от канала' if channel else 'Выйти из чата', language)
        self.setWindowTitle(caption)
        self.setMinimumWidth(380)
        self.resize(440, 190)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(16)
        text = QLabel(f'{caption} «{title}»?')
        text.setTextFormat(Qt.TextFormat.PlainText)
        text.setWordWrap(True)
        layout.addWidget(text)
        if private:
            warning = QLabel(tr('Вы сможете вернуться только по\xa0ссылке‑приглашению', language))
            warning.setTextFormat(Qt.TextFormat.PlainText)
            warning.setWordWrap(True)
            layout.addWidget(warning)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok |
                                        QDialogButtonBox.StandardButton.Cancel)
        self.leave_button = self.buttons.button(QDialogButtonBox.StandardButton.Ok)
        self.leave_button.setText(tr('Отписаться' if channel else 'Выйти', language))
        self.leave_button.setAutoDefault(False)
        self.leave_button.setDefault(False)
        cancel = self.buttons.button(QDialogButtonBox.StandardButton.Cancel)
        cancel.setText(tr('Не отписываться' if channel else 'Остаться', language))
        cancel.setDefault(True)
        cancel.setFocus()
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        self._scope_timer = QTimer(self)
        self._scope_timer.setInterval(150)
        self._scope_timer.timeout.connect(self._check_scope)

    def _valid_scope(self):
        try:
            return self._current() is True
        except (AttributeError, RuntimeError, ValueError):
            return False

    def _check_scope(self):
        if not self._valid_scope():
            self.reject()

    def showEvent(self, event):
        super().showEvent(event)
        self._scope_timer.start()
        self._check_scope()

    def accept(self):
        if self._valid_scope():
            super().accept()
        else:
            self.reject()

    def done(self, result):
        self._scope_timer.stop()
        super().done(result)


def confirm_conversation_leave(parent, snapshot, *, language='ru', context_is_current=None):
    if not isinstance(snapshot, dict) or not simple_leave_available(snapshot.get('action_capabilities')):
        return False
    if snapshot.get('action_pending') is True:
        return False
    capabilities = snapshot['action_capabilities']
    dialog = ConversationLeaveDialog(
        str(snapshot.get('title', '')), channel=capabilities['kind'] == 'channel',
        private=snapshot.get('leave_private') is not False, language=language,
        context_is_current=context_is_current, parent=parent)
    accepted = dialog.exec() == QDialog.DialogCode.Accepted and dialog._valid_scope()
    dialog.deleteLater()
    return accepted
