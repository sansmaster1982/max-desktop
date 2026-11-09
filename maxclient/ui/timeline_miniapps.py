"""Explicit OPEN_APP actions, bound to the selected message and live session."""
from copy import deepcopy

from PySide6.QtCore import QObject
from shiboken6 import isValid

from ..core.message_presentation import normalized_button_rows
from ..core.models import Message
from .async_runner import run_async
from .public_build import web_features_available

_DISPLAY_ERRORS = frozenset({
    'Обновите сообщение для загрузки параметров мини-приложения',
    'MAX не указал приложение для этой кнопки',
    'MAX не подтвердил бота мини-приложения',
    'Мини-приложение недоступно у этого бота',
    'MAX не подтвердил наличие мини-приложения у бота',
    'MAX не подтвердил диалог бота мини-приложения',
    'MAX не подтвердил адрес мини-приложения',
})


def create_window(url, context, is_current, parent):
    if not web_features_available():
        raise RuntimeError('Мини-приложения не включены в эту переносимую сборку MAXIM.')
    # Import WebEngine only after an explicit action and a successful server reply.
    from .miniapp_window import MiniAppWindow
    return MiniAppWindow(url, context, is_current, parent)


class TimelineMiniApps(QObject):
    def __init__(self, view):
        super().__init__(view)
        self.view = view
        self.pending = None
        self.window = None

    def reset(self):
        self.pending = None
        window, self.window = self.window, None
        if window is not None and isValid(window):
            window.close()

    def open(self, message, index, row_index, button_index, context):
        view = self.view
        if not web_features_available():
            if isValid(view):
                view.h_sub.setText('Мини-приложения не включены в эту переносимую сборку MAXIM.')
            return
        if (self.pending is not None or not isValid(view)
                or not view.file_context_current(context)
                or not isinstance(message, Message) or message.status == 'removed'
                or message.display_source != 'outer'
                or any(type(value) is not int or value < 0 for value in (index, row_index, button_index))
                or index >= len(message.display_attaches)):
            return
        record = next((row for row in view._rows if row['msg'] is message), None)
        if record is None or not isValid(record['bubble']):
            return
        attach = message.display_attaches[index]
        rows = normalized_button_rows(attach.inline_buttons)
        if (attach.type != 'INLINE_KEYBOARD' or attach.inline_metadata_confirmed is not True
                or rows is None or row_index >= len(rows) or button_index >= len(rows[row_index])
                or rows[row_index][button_index]['type'] != 'OPEN_APP'):
            return
        try:
            allowed = view.service.is_current_message_app(
                message, index, row_index, button_index, context=context) is True
        except Exception:
            allowed = False
        if not allowed:
            view.h_sub.setText('Обновите сообщение для загрузки параметров мини-приложения')
            return
        fingerprint = deepcopy((message.id, message.chat_id, message.time_ms, message.text, rows))
        bubble = record['bubble']
        previous_status = view.h_sub.text()
        ticket = object()
        self.pending = ticket

        def action_current(active_context):
            if not isValid(view) or not view.file_context_current(active_context):
                return False
            try:
                return view.service.is_current_message_app(
                    message, index, row_index, button_index, context=active_context) is True
            except Exception:
                return False

        def current():
            return (isValid(self) and isValid(view) and self.pending is ticket
                    and action_current(context) and isValid(bubble)
                    and message.status != 'removed' and message.display_source == 'outer'
                    and index < len(message.display_attaches) and message.display_attaches[index] is attach
                    and attach.inline_metadata_confirmed is True
                    and any(row['msg'] is message and row['bubble'] is bubble for row in view._rows)
                    and (message.id, message.chat_id, message.time_ms, message.text,
                         normalized_button_rows(attach.inline_buttons)) == fingerprint)

        def failed(_error=None):
            if current():
                # Only our finite static validation messages may reach the UI.
                # RPC/renderer errors could embed a signed URL or private data.
                view.h_sub.setText(_error if isinstance(_error, str) and _error in _DISPLAY_ERRORS
                                   else 'Не удалось открыть мини-приложение. Попробуйте ещё раз.')
            if isValid(self) and self.pending is ticket:
                self.pending = None

        def finished(result):
            if not current():
                if isValid(self) and self.pending is ticket:
                    self.pending = None
                return
            if not isinstance(result, dict) or not isinstance(result.get('url'), str):
                failed()
                return
            self.pending = None
            if self.window is not None and isValid(self.window):
                self.window.close()
            try:
                self.window = create_window(result['url'], context,
                                            action_current, view.window())
                def restore_status():
                    if (isValid(view) and view.file_context_current(context)
                            and view.h_sub.text() == 'Мини-приложение открыто'):
                        view.h_sub.setText(previous_status)
                closed = getattr(self.window, 'closed', None)
                if closed is not None:
                    closed.connect(restore_status)
                self.window.show()
                view.h_sub.setText('Мини-приложение открыто')
            except Exception:
                if self.window is not None and isValid(self.window):
                    self.window.close()
                self.window = None
                view.h_sub.setText('Не удалось открыть окно мини-приложения.')

        view.h_sub.setText('Открытие мини-приложения…')
        run_async(view.service.open_message_app, message, index, row_index, button_index,
                  context=context, on_done=finished, on_error=failed)
