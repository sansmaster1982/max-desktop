"""Explicit two-phase bot removal choices; no transport or automatic retries."""
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QDialog, QDialogButtonBox, QLabel, QVBoxLayout

from ..core.i18n import tr
from ..protocol.folder_contracts import int64


def bot_delete_result(result, context, requested='delete'):
    candidate = (result.get('lifecycle_result') if requested == 'refresh_actions'
                 and isinstance(result, dict) else result)
    if (not isinstance(candidate, dict) or context is None
            or candidate.get('context') is not context
            or candidate.get('action') != 'delete'
            or candidate.get('lifecycle_kind') != 'bot_delete'
            or not int64(candidate.get('chat_id')) or not int64(candidate.get('peer_id'))
            or candidate['chat_id'] != getattr(context, 'chat_id', None)
            or candidate['peer_id'] != getattr(context, 'peer_id', None)):
        return None
    return candidate


def bot_delete_state(snapshot, context):
    if not isinstance(snapshot, dict) or snapshot.get('context') is not context:
        return None
    return bot_delete_result(snapshot.get('bot_delete_state'), context)


def bot_delete_available(snapshot, context, *, continuation=False):
    if (not isinstance(snapshot, dict) or context is None
            or snapshot.get('context') is not context
            or not int64(snapshot.get('chat_id')) or not int64(snapshot.get('peer_id'))
            or snapshot['chat_id'] != getattr(context, 'chat_id', None)
            or snapshot['peer_id'] != getattr(context, 'peer_id', None)):
        return False
    caps = snapshot.get('action_capabilities')
    if (not isinstance(caps, dict) or caps.get('supported') is not True
            or caps.get('kind') != 'bot' or caps.get('service') is True
            or 'delete' not in caps.get('more', [])):
        return False
    action = 'continue_bot_delete' if continuation else 'delete'
    if not any(isinstance(row, dict) and row.get('action') == action
               and row.get('available') is True for row in snapshot.get('more_actions', [])):
        return False
    if continuation:
        state = bot_delete_state(snapshot, context)
        return (state is not None and state.get('can_continue_delete') is True
                and state.get('stop_confirmed') is True
                and state.get('delete_confirmed') is False and state.get('stage') == 'stopped')
    return snapshot.get('action_pending') is not True


def bot_delete_notice(state, language='ru'):
    """Only present the exact validated target's durable phase projection."""
    if not isinstance(state, dict):
        return ''
    if (state.get('confirmed') is True and state.get('pending') is False
            and state.get('stop_confirmed') is True and state.get('delete_confirmed') is True):
        return tr('Бот остановлен. Чат удалён.', language)
    if state.get('stop_confirmed') is True:
        if state.get('stage') == 'stopped':
            text = ('Бот остановлен. Чат не удалён. Удаление можно продолжить отдельно.'
                    if state.get('can_continue_delete') is True else
                    'Бот остановлен. Чат не удалён. Обновите сведения перед продолжением.')
        else:
            text = 'Бот остановлен. Удаление чата не подтверждено. Проверка состояния не повторяет удаление.'
    else:
        text = 'Остановка бота не подтверждена. Удаление чата не запускалось. Проверьте состояние.'
    return tr(text, language)


class ConversationBotDeleteDialog(QDialog):
    def __init__(self, title, *, suspended=False, continuation=False, language='ru',
                 context_is_current=None, parent=None):
        super().__init__(parent)
        self._current = context_is_current or (lambda: False)
        self.setObjectName('ConversationBotDeleteDialog')
        self.setWindowModality(Qt.WindowModality.WindowModal)
        caption = tr('Удалить чат' if suspended or continuation else 'Удалить и остановить', language)
        self.setWindowTitle(caption)
        self.setMinimumWidth(380)
        self.resize(460, 230)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(16)
        heading = QLabel(f'{caption} «{title}»?')
        heading.setTextFormat(Qt.TextFormat.PlainText)
        heading.setWordWrap(True)
        layout.addWidget(heading)
        explanation = ('Бот уже остановлен. Будет выполнено только удаление чата у вас.'
                       if continuation else
                       'Сначала приложение подтвердит остановку бота, затем удалит чат только у вас. '
                       'Если удаление не завершится, бот останется остановленным.')
        body = QLabel(tr(explanation, language))
        body.setTextFormat(Qt.TextFormat.PlainText)
        body.setWordWrap(True)
        layout.addWidget(body)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok |
                                        QDialogButtonBox.StandardButton.Cancel)
        self.delete_button = self.buttons.button(QDialogButtonBox.StandardButton.Ok)
        self.delete_button.setText(caption)
        self.delete_button.setAutoDefault(False)
        self.delete_button.setDefault(False)
        cancel = self.buttons.button(QDialogButtonBox.StandardButton.Cancel)
        cancel.setText(tr('Отмена', language))
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


def confirm_bot_conversation_delete(parent, snapshot, context, *, continuation=False,
                                    language='ru', context_is_current=None):
    if not bot_delete_available(snapshot, context, continuation=continuation):
        return False
    dialog = ConversationBotDeleteDialog(
        str(snapshot.get('title', '')),
        suspended=snapshot['action_capabilities'].get('suspended') is True,
        continuation=continuation, language=language,
        context_is_current=context_is_current, parent=parent)
    accepted = dialog.exec() == QDialog.DialogCode.Accepted and dialog._valid_scope()
    dialog.deleteLater()
    return accepted
