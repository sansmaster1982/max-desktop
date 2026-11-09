"""One explicit file save, from a bound 88 reply to an atomic local commit."""
from threading import Event

from PySide6.QtCore import QObject, Signal, Qt
from shiboken6 import isValid

from ..core.i18n import normalize_language
from ..core.file_download_transport import stream_file
from .async_runner import run_async
from .conversation_file_dialog import FileDownloadDialog, confirm_download


class ConversationFileController(QObject):
    progress = Signal(object, object)
    state_changed = Signal(str)
    message_requested = Signal(object)

    def __init__(self, service, descriptor, destination, expected_target, parent=None, *, theme='light', language='ru'):
        super().__init__(parent)
        self.service = service
        self.descriptor = descriptor
        self.destination = destination
        self.expected_target = expected_target
        self._cancel = Event()
        self._active = True
        self._started = False
        self._worker_pending = False
        self._dialog_gone = False
        self._theme = theme
        self._language = normalize_language(language)
        self.dialog = FileDownloadDialog(descriptor, parent, theme=theme, language=self._language)
        self.progress.connect(self.dialog.set_progress, Qt.ConnectionType.QueuedConnection)
        self.state_changed.connect(self.dialog.set_status)
        self.dialog.cancel_requested.connect(self.cancel)
        self.dialog.message_requested.connect(self._message)
        self.dialog.destroyed.connect(self._dialog_destroyed)
        self.destroyed.connect(lambda: self._cancel.set())

    def set_language(self, language):
        if not isValid(self):
            return False
        self._language = normalize_language(language)
        if isValid(self.dialog):
            return self.dialog.set_language(self._language)
        return False

    retranslate = set_language

    @property
    def active(self):
        return isValid(self) and self._active

    def _current(self):
        if not isValid(self) or not self._active or self._cancel.is_set():
            return False
        try:
            return self.service.is_current_conversation_file(self.descriptor.context, self.descriptor) is True
        except (ValueError, AttributeError):
            return False

    def show(self):
        if isValid(self.dialog):
            self.dialog.show()
            self.dialog.raise_()

    def start(self):
        if self._started:
            return
        if not self._current():
            self.cancel()
            return
        self._started = True
        self.show()
        self._worker_pending = True
        try:
            run_async(self.service.resolve_conversation_file_download, self.descriptor.context, self.descriptor,
                      on_done=self._resolved, on_error=self._failed)
        except Exception:
            self._failed(None)

    def _resolved(self, reply):
        self._worker_pending = False
        if not self._current():
            self.cancel()
            return
        if (getattr(reply, 'descriptor', None) is not self.descriptor
                or type(getattr(reply, 'unsafe', None)) is not bool
                or not isinstance(getattr(reply, 'url', None), str)):
            self._failed(None)
            return
        if reply.unsafe:
            if not isValid(self.dialog) or not confirm_download(
                    self.descriptor, self.dialog, theme=self._theme, language=self._language):
                self.cancel()
                return
            # A modal warning runs an event loop: the card/session may close.
            if not self._current():
                self.cancel()
                return
        self.state_changed.emit('downloading')
        self._worker_pending = True
        try:
            run_async(stream_file, reply.url, self.destination, expected_target=self.expected_target,
                      cancelled=self._cancel, is_current=self._current, on_progress=self._report_progress,
                      on_done=self._downloaded, on_error=self._failed)
        except Exception:
            self._failed(None)

    def _report_progress(self, received, total=None):
        if self._current():
            try:
                self.progress.emit(received, total)
            except RuntimeError:
                self._cancel.set()

    def _downloaded(self, staged):
        self._worker_pending = False
        if not callable(getattr(staged, 'commit', None)) or not callable(getattr(staged, 'discard', None)):
            self._failed(None)
            return
        if not self._current():
            staged.discard()
            self.cancel()
            return
        self.state_changed.emit('saving')
        try:
            if not self._current():
                staged.discard()
                self.cancel()
                return
            staged.commit()  # Rechecks cancellation/context/target, no network here.
        except Exception:
            staged.discard()
            self._failed(None)
            return
        self._active = False
        self.state_changed.emit('done')
        self._maybe_dispose()

    def _failed(self, _error):
        self._worker_pending = False
        if not isValid(self):
            return
        if not self._active:
            self._maybe_dispose()
            return
        if self._cancel.is_set() or not self._current():
            self.cancel()
            return
        self._active = False
        self.state_changed.emit('error')  # No URL/token/path from the exception.
        self._maybe_dispose()

    def cancel(self, *_):
        self._cancel.set()
        if not isValid(self):
            return
        if not self._active:
            self._maybe_dispose()
            return
        if self._worker_pending:
            # Keep the single-download reservation until the worker has cleaned
            # its temporary file and its completion callback acknowledges it.
            self.state_changed.emit('cancelling')
            return
        self._active = False
        self.state_changed.emit('cancelled')
        self._maybe_dispose()

    def _dialog_destroyed(self, *_):
        self._dialog_gone = True
        self.cancel()
        self._maybe_dispose()

    def _maybe_dispose(self):
        if isValid(self) and self._dialog_gone and not self._worker_pending:
            self.deleteLater()

    def _message(self, descriptor):
        if isValid(self) and descriptor is self.descriptor:
            self.message_requested.emit(descriptor)
