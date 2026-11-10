"""Foreground viewport read receipts; never mark an entire loaded history."""
from collections import OrderedDict

from PySide6.QtCore import QObject, QEvent, QTimer
from PySide6.QtWidgets import QApplication
from shiboken6 import isValid

from ..core.models import Message
from ..core.service import _message_time
from ..protocol.client import AuthenticatedSession
from ..protocol.folder_contracts import int64
from .async_runner import run_async


class VisibleReadReceipts(QObject):
    """One bounded, session-bound receipt after 200ms of visible reading.

    A failed/unknown request is not automatically repeated for the same message.
    Core applies server counters only; this controller never resets badges.
    """
    def __init__(self, view):
        super().__init__(view)
        self.view = view
        self._scope = None
        self._attempted = OrderedDict()
        self._pending = None
        self._visibility_epoch = 0
        self._foreground_visible = False
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(200)
        self.timer.timeout.connect(self.flush)
        view.scroll.verticalScrollBar().valueChanged.connect(self.schedule)
        view.scroll.verticalScrollBar().rangeChanged.connect(self.schedule)
        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)

    def eventFilter(self, watched, event):  # noqa: N802
        if not isValid(self.view) or not isValid(self.view.scroll):
            return False
        if (event.type() in (QEvent.Type.WindowActivate, QEvent.Type.WindowDeactivate,
                            QEvent.Type.ApplicationStateChange)
                or watched in (self.view, self.view.window(), self.view.scroll.viewport())
                and event.type() in (QEvent.Type.Show, QEvent.Type.Hide,
                                     QEvent.Type.Resize, QEvent.Type.WindowStateChange)):
            self.schedule()
        return False

    def schedule(self, *_args):
        if not isValid(self) or not isValid(self.view):
            return
        self.timer.stop()
        self._visibility_epoch += 1
        self._foreground_visible = self._foreground()
        if self._foreground_visible:
            self.timer.start()

    def suspend_for_overlay(self):
        """Invalidate queued reads before showing a modal over the timeline."""
        self.timer.stop()
        self._visibility_epoch += 1
        self._foreground_visible = False

    def _foreground(self):
        view = self.view
        if (not isValid(view) or not isValid(view.scroll)
                or not view.isVisible() or view.chat is None):
            return False
        window = view.window()
        return (isValid(window) and window.isVisible() and not window.isMinimized()
                and window.isActiveWindow() and QApplication.activeModalWidget() is None
                and view._history_target is None and not view._history_target_pending)

    def _candidate(self):
        view = self.view
        if not self._foreground():
            return None
        binding = view.chat_metadata_binding()
        if binding is None:
            return None
        try:
            context = view.service.search_context()
            if not isinstance(context, AuthenticatedSession):
                return None
            state = view.service.read_receipt_state(view.chat.id, context=context)
        except Exception:  # Disconnect/closed cache is not a reason to send.
            return None
        if (not view.chat_metadata_binding_current(binding)
                or state['unread'] <= 0 and not state['markedAsUnread']):
            return None
        viewport = view.scroll.viewport()
        candidates = []
        covered = []
        self._count_reconcile = False
        for row in view._rows:
            message, bubble = row['msg'], row['bubble']
            if (not isinstance(message, Message) or message.chat_id != view.chat.id
                    or not int64(message.id) or _message_time({'time': message.time_ms}) is None
                    or message.status in ('pending', 'failed', 'unknown', 'removed')
                    or not isValid(bubble) or not bubble.isVisible()):
                continue
            bottom = bubble.mapTo(viewport, bubble.rect().bottomLeft()).y()
            top = bubble.mapTo(viewport, bubble.rect().topLeft()).y()
            # Seeing the lower edge completes even a message taller than the
            # viewport; messages below the viewport are never marked as read.
            if bottom < 0 or bottom >= viewport.height() or top >= viewport.height():
                continue
            if (_message_time({'time': state['mark']}) is not None
                    and message.time_ms <= state['mark']):
                covered.append(message)
                continue
            candidates.append(message)
        if not candidates:
            # The badge is nonzero although our confirmed marker already covers
            # the newest server message and that message is fully on screen
            # (a receipt that arrived without a count, or a count that a late
            # ACK could not apply). Nothing visible is newer than the marker, so
            # the normal path would never ask again. One explicit request for
            # this exact frontier obtains the server's count; nothing is reset
            # locally and the request is never repeated for the same frontier.
            frontier = getattr(view.service, 'read_receipt_frontier', None)
            if (not covered or state['unread'] <= 0 or state['markedAsUnread']
                    or not callable(frontier)):
                return None
            selected = max(covered, key=lambda message: (message.time_ms, message.id))
            try:
                latest = frontier(view.chat.id, context=context)
            except Exception:
                return None
            if (not isinstance(latest, tuple) or latest != (selected.id, selected.time_ms)
                    or not view.chat_metadata_binding_current(binding)):
                return None
            self._count_reconcile = True
            return binding, context, selected, state
        selected = max(candidates, key=lambda message: (message.time_ms, message.id))
        return binding, context, selected, state

    def flush(self):
        if not isValid(self) or not isValid(self.view):
            return
        candidate = self._candidate()
        if candidate is None or self._pending is not None:
            return
        binding, context, message, state = candidate
        # A candidate is selected only on the GUI thread. Workers inspect this
        # immutable generation/boolean, never QWidget foreground properties.
        self._foreground_visible = True
        visibility_epoch = self._visibility_epoch
        if context is not self._scope:
            self._scope = context
            self._attempted.clear()
        key = (message.chat_id, message.id, message.time_ms)
        if getattr(self, '_count_reconcile', False):
            # Separate single-use slot: at most one count request per frontier
            # and session, whatever its outcome.
            key = key + ('count',)
            if key in self._attempted:
                return
        if key in self._attempted:
            acknowledged = self._attempted[key]
            current_mark = _message_time({'time': state['mark']})
            # A confirmed later SET_AS_UNREAD is a new state, not a retry of
            # the earlier successful read. Unknown/failed outcomes stay held.
            if acknowledged is None or current_mark is None or current_mark >= acknowledged:
                return
        self._attempted[key] = None
        while len(self._attempted) > 256:
            self._attempted.popitem(last=False)
        request = self._pending = object()

        def finished(result):
            if not isValid(self) or not isValid(self.view) or self._pending is not request:
                return
            self._pending = None
            if isinstance(result, dict) and result.get('cancelled') is True:
                # No request was sent. A later deliberate reopen may select the
                # same message again; this is not a retry of an unknown write.
                self._attempted.pop(key, None)
                self.schedule()
                return
            if (context is self._scope and isinstance(result, dict)
                    and result.get('success') is not False
                    and _message_time({'time': result.get('mark')}) is not None
                    and type(result.get('unread')) is int and 0 <= result['unread'] < 2**31):
                self._attempted[key] = result['mark']
            # The core already checked account/session identity. Only schedule
            # a freshly selected visible candidate; do not repaint old rows.
            self.schedule()

        def send_if_current(chat_id, *, message_id, mark, context):
            if (not isValid(self) or not isValid(self.view) or self._pending is not request
                    or not self.view.chat_metadata_binding_current(binding)
                    or visibility_epoch != self._visibility_epoch or not self._foreground_visible):
                return {'success': False, 'cancelled': True}
            try:
                if self.view.service.search_context() is not context:
                    return {'success': False, 'cancelled': True}
            except (AttributeError, ValueError, RuntimeError):
                return {'success': False, 'cancelled': True}
            return self.view.service.mark_chat_read(chat_id, message_id=message_id, mark=mark, context=context)

        run_async(send_if_current, message.chat_id,
                  message_id=message.id, mark=message.time_ms, context=context,
                  on_done=finished, on_error=finished)
