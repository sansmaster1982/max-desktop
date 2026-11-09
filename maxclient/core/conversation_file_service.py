"""Bound loaded-file descriptors and read-only88 resolution. No file/network IO."""
from __future__ import annotations

from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass, field
from threading import Lock, RLock
from weakref import WeakSet

from ..protocol.file_download_contracts import FileDownloadReply, parse_file_download
from ..protocol.search_contracts import _integer, _number


@dataclass(frozen=True, eq=False)
class ConversationFileDescriptor:
    context: object = field(repr=False)
    key: str = field(repr=False)
    file_id: int | float = field(repr=False)
    chat_id: int = field(repr=False)
    message_id: int = field(repr=False)
    time_ms: int | float = field(repr=False)
    item_type: str
    file_name: str = field(repr=False)
    size: int | float | None


@dataclass(frozen=True, eq=False)
class ConversationFileDownload:
    descriptor: ConversationFileDescriptor = field(repr=False)
    url: str = field(repr=False)
    unsafe: bool


def _descriptor(context, row):
    if (not isinstance(row, dict) or row.get('kind') != 'FILE'
            or row.get('download_available') is not True or row.get('deleted') is True):
        return None
    index = row.get('attachment_index')
    if (not _number(row.get('file_id')) or not _integer(row.get('chat_id'), 64)
            or row['chat_id'] != context.chat_id or not _integer(row.get('message_id'), 64)
            or not _number(row.get('time_ms')) or type(index) is not int or not 0 <= index < 1000
            or row.get('key') != f"attachment:{context.chat_id}:{row['message_id']}:{index}"
            or row.get('item_type') not in ('REGULAR', 'DELAYED')
            or not isinstance(row.get('file_name'), str)):
        raise ValueError('MAX did not confirm the loaded file descriptor')
    size = row.get('size')
    if size is not None and (not _number(size) or size < 0):
        raise ValueError('MAX did not confirm the loaded file size')
    return ConversationFileDescriptor(context, row['key'], row['file_id'], context.chat_id,
        row['message_id'], row['time_ms'], row['item_type'], row['file_name'], size)


def _same_descriptor(first, second):
    return (first.context is second.context and first.key == second.key
            and type(first.file_id) is type(second.file_id) and first.file_id == second.file_id
            and first.chat_id == second.chat_id and first.message_id == second.message_id
            and first.time_ms == second.time_ms and first.item_type == second.item_type
            and first.file_name == second.file_name and first.size == second.size)


class _ConversationFiles:
    LIMIT = 2048  # Local memory policy, not a server count or TTL.

    def __init__(self, service):
        self.service = service
        self._lock = RLock()
        self._context = None
        self._rows = OrderedDict()
        self._inflight = set()
        self._revoked = WeakSet()

    @contextmanager
    def _local(self, context):
        service = self.service
        with service._metadata_lock:
            binding = service._history_read_context()
            with service.store.account_scope(binding[0]):
                cached = service._require_conversation(context)
                service._require_history_context(cached[0])
                with self._lock:
                    yield cached[0]
                    service._require_history_context(cached[0])

    def clear(self, context=None):
        with self._lock:
            target = self._context if context is None else context
            if target is not None:
                try:
                    self._revoked.add(target)
                except TypeError:
                    pass  # Only a real typed ConversationContext can be loaded.
            if context is None or self._context is context:
                self._rows.clear()
                self._context = None
                # An already-running88 retains its reservation until completion;
                # clearing membership makes its result invalid immediately.

    def activate(self, context):
        with self._local(context):
            if context in self._revoked:
                raise ValueError('This conversation file context was closed')
            if self._context is not context:
                if self._context is not None:
                    self._revoked.add(self._context)
                self._rows.clear()
                self._context = context

    def remember(self, context, rows, *, append=False):
        if not isinstance(rows, list) or len(rows) > 4000 or type(append) is not bool:
            raise ValueError('A bounded actual files page is required')
        proposed = []
        seen = set()
        for row in rows:
            descriptor = _descriptor(context, row)
            if descriptor is not None:
                if descriptor.key in seen:
                    raise ValueError('Duplicate loaded file identity')
                seen.add(descriptor.key)
                proposed.append(descriptor)
        with self._local(context):
            if self._context is not context or context in self._revoked:
                raise ValueError('The files page belongs to a closed conversation')
            prior = self._rows
            updated = OrderedDict(prior) if append else OrderedDict()
            # Explicitly deleted/unavailable rows invalidate their old descriptor.
            for row in rows:
                if isinstance(row, dict) and row.get('kind') == 'FILE' and row.get('key') not in seen:
                    updated.pop(row.get('key'), None)
            for descriptor in proposed:
                old = prior.get(descriptor.key)
                updated[descriptor.key] = old if old is not None and _same_descriptor(old, descriptor) else descriptor
                updated.move_to_end(descriptor.key)
            while len(updated) > self.LIMIT:
                updated.popitem(last=False)
            self._context, self._rows = context, updated

    def _require_descriptor(self, context, descriptor):
        if (not isinstance(descriptor, ConversationFileDescriptor) or descriptor.context is not context
                or self._context is not context or self._rows.get(descriptor.key) is not descriptor):
            raise ValueError('The selected file is no longer in the current loaded files page')

    def get(self, context, key):
        if not isinstance(key, str):
            raise ValueError('An actual loaded file key is required')
        with self._local(context):
            descriptor = self._rows.get(key)
            self._require_descriptor(context, descriptor)
            self._rows.move_to_end(key)
            return descriptor

    def current(self, context, descriptor):
        try:
            with self._local(context):
                self._require_descriptor(context, descriptor)
            return True
        except (ValueError, AttributeError, TypeError):
            return False

    def resolve(self, context, descriptor):
        reserved = False
        try:
            with self._local(context) as binding:
                self._require_descriptor(context, descriptor)
                if descriptor in self._inflight:
                    raise ValueError('The file address is already being requested')
                self._inflight.add(descriptor)
                reserved = True
            # The transport guard binds the packet itself, not only its response.
            reply = binding[1].conversation_file_download(
                descriptor.file_id, descriptor.chat_id, descriptor.message_id,
                item_type=descriptor.item_type, expected_session=binding[2])
            if isinstance(reply, FileDownloadReply):
                # Do not trust a caller-created dataclass to bypass URL/bool rules.
                reply = parse_file_download({'url': reply.url, 'unsafe': reply.unsafe})
            else:
                raise ValueError('MAX did not return a typed file address')
            with self._local(context):
                self._require_descriptor(context, descriptor)
                return ConversationFileDownload(descriptor, reply.url, reply.unsafe)
        finally:
            if reserved:
                with self._lock:
                    self._inflight.discard(descriptor)


_ADAPTER_LOCK = Lock()


class ConversationFileServiceMixin:
    def _conversation_files_adapter(self):
        with _ADAPTER_LOCK:
            adapter = getattr(self, '_conversation_files_state', None)
            if adapter is None:
                adapter = _ConversationFiles(self)
                self._conversation_files_state = adapter
            return adapter

    def _remember_conversation_files(self, context, rows, *, append=False):
        self._conversation_files_adapter().remember(context, rows, append=append)

    def _activate_conversation_files(self, context):
        self._conversation_files_adapter().activate(context)

    def get_conversation_file_descriptor(self, context, key):
        return self._conversation_files_adapter().get(context, key)

    def is_current_conversation_file(self, context, descriptor):
        from .timeline_file_service import TimelineFileContext
        if isinstance(context, TimelineFileContext):
            return self._timeline_files_adapter().current(context, descriptor)
        return self._conversation_files_adapter().current(context, descriptor)

    def resolve_conversation_file_download(self, context, descriptor):
        from .timeline_file_service import TimelineFileContext
        if isinstance(context, TimelineFileContext):
            return self._timeline_files_adapter().resolve(context, descriptor)
        return self._conversation_files_adapter().resolve(context, descriptor)

    def clear_conversation_files(self, context=None):
        adapter = getattr(self, '_conversation_files_state', None)
        if adapter is not None:
            adapter.clear(context)
