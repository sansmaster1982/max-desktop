"""FILE intents bound to a timeline view, separate from detached Info cards."""
from __future__ import annotations

from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass, field
from threading import Lock, RLock
from weakref import WeakSet
import json

from ..protocol.client import AuthenticatedSession
from ..protocol.file_download_contracts import FileDownloadReply, parse_file_download
from ..protocol.search_contracts import _integer, _number
from .conversation_file_service import ConversationFileDescriptor, ConversationFileDownload
from .models import Attach, Message, parse_message_link, forwarded_to_data


@dataclass(frozen=True, eq=False)
class TimelineFileContext:
    owner_id: int
    source_client: object = field(repr=False)
    session_context: AuthenticatedSession = field(repr=False)
    chat_id: int


def _signature(message):
    if (not isinstance(message, Message) or not _integer(message.chat_id, 64)
            or not _integer(message.id, 64) or not _number(message.time_ms)
            or message.status != 'sent' or not isinstance(message.text, str)
            or not isinstance(message.attaches, list) or len(message.attaches) > 1000
            or any(not isinstance(attach, Attach) for attach in message.attaches)
            or not isinstance(message.display_attaches, list) or len(message.display_attaches) > 1000
            or any(not isinstance(attach, Attach) for attach in message.display_attaches)):
        raise ValueError('A current server message is required')
    link = parse_message_link(message.link)
    return (message.chat_id, message.id, message.time_ms, message.text, message.status,
            tuple(sorted(link.items())) if link else None,
            message.display_source,
            json.dumps(forwarded_to_data(message.forwarded), sort_keys=True, allow_nan=False),
            tuple((attach.type, type(attach.file_id), attach.file_id, attach.file_name,
                   attach.size, attach.file_metadata_confirmed, attach.deleted, attach.file_item_type,
                   attach.file_update_time)
                  for attach in message.attaches))


def _actions(message, *, trusted=False):
    if not isinstance(message, Message) or not isinstance(message.attaches, list):
        return {}
    result = {}
    for index, attach in enumerate(message.display_attaches):
        if not isinstance(attach, Attach) or attach.type != 'FILE':
            continue
        proof = trusted and attach.file_metadata_confirmed is True
        size_valid = attach.size is None or _number(attach.size) and attach.size >= 0
        collision = (message.display_source == 'forward' and any(
            other.type == 'FILE' and _number(other.file_id) and other.file_id == attach.file_id
            for other in message.attaches if isinstance(other, Attach)))
        allowed = (proof and not collision and _number(attach.file_id) and type(attach.deleted) is bool
                   and not attach.deleted and attach.file_item_type in ('REGULAR', 'DELAYED') and size_valid)
        result[index] = {'download_available': bool(allowed),
                         'file_name': attach.file_name if proof and isinstance(attach.file_name, str) else '',
                         'deleted': proof and attach.deleted is True}
    return result


class _TimelineFiles:
    LIMIT = 2048

    def __init__(self, service):
        self.service = service
        self._lock = RLock()
        self._context = None
        self._revoked = WeakSet()
        self._rows = OrderedDict()
        self._inflight = set()

    def clear(self, context=None):
        with self._lock:
            target = self._context if context is None else context
            if isinstance(target, TimelineFileContext):
                self._revoked.add(target)
            if context is None or self._context is context:
                self._context = None
                self._rows.clear()

    def open(self, chat_id):
        if not _integer(chat_id, 64):
            raise ValueError('An actual timeline chat is required')
        service = self.service
        with service._metadata_lock:
            binding = service._history_read_context()
            with service.store.account_scope(binding[0]):
                service._require_history_context(binding)
                if service.store.get_chat(chat_id) is None:
                    raise ValueError('The timeline chat is not in this account cache')
                context = TimelineFileContext(binding[0], binding[1], binding[2], chat_id)
                with self._lock:
                    if self._context is not None:
                        self._revoked.add(self._context)
                    self._context, self._rows = context, OrderedDict()
                service._require_history_context(binding)
                return context

    @contextmanager
    def _local(self, context):
        if not isinstance(context, TimelineFileContext):
            raise ValueError('A typed timeline file context is required')
        service = self.service
        with service._metadata_lock:
            binding = service._history_read_context()
            if (binding[0] != context.owner_id or binding[1] is not context.source_client
                    or binding[2] is not context.session_context):
                raise ValueError('The timeline account or session changed')
            with service.store.account_scope(binding[0]):
                service._require_history_context(binding)
                with self._lock:
                    if self._context is not context or context in self._revoked:
                        raise ValueError('The timeline view was closed or replaced')
                    if service.store.get_chat(context.chat_id) is None:
                        raise ValueError('The timeline chat is no longer available')
                    yield binding
                    service._require_history_context(binding)

    def current_context(self, context):
        try:
            with self._local(context):
                pass
            return True
        except (ValueError, AttributeError, TypeError):
            return False

    def _stored_selection(self, context, message):
        selected = _signature(message)
        if message.chat_id != context.chat_id:
            raise ValueError('The selected message belongs to another timeline')
        current = self.service.store.message(context.chat_id, message.id)
        if current is None or _signature(current) != selected:
            raise ValueError('The selected timeline message changed')
        return current, selected

    def actions(self, context, message):
        try:
            with self._local(context):
                current, _snapshot = self._stored_selection(context, message)
                return _actions(current, trusted=True)
        except (ValueError, AttributeError, TypeError):
            return _actions(message)

    def descriptor(self, context, message, index):
        if type(index) is not int:
            raise ValueError('An actual normalized attachment index is required')
        with self._local(context):
            current, snapshot = self._stored_selection(context, message)
            actions = _actions(current, trusted=True)
            if actions.get(index, {}).get('download_available') is not True:
                raise ValueError('This timeline file has no confirmed download identity')
            attach = current.display_attaches[index]
            key = f'timeline-file:{context.chat_id}:{current.id}:{current.display_source}:{index}'
            previous = self._rows.get(key)
            if previous is not None and previous[1] == snapshot:
                self._rows.move_to_end(key)
                return previous[0]
            descriptor = ConversationFileDescriptor(context, key, attach.file_id, context.chat_id,
                current.id, current.time_ms, attach.file_item_type, actions[index]['file_name'], attach.size)
            self._rows[key] = (descriptor, snapshot)
            self._rows.move_to_end(key)
            while len(self._rows) > self.LIMIT:
                self._rows.popitem(last=False)
            return descriptor

    def _require_descriptor(self, context, descriptor):
        if not isinstance(descriptor, ConversationFileDescriptor) or descriptor.context is not context:
            raise ValueError('The timeline file descriptor is no longer valid')
        saved = self._rows.get(descriptor.key)
        if saved is None or saved[0] is not descriptor:
            raise ValueError('The timeline file descriptor expired')
        current = self.service.store.message(context.chat_id, descriptor.message_id)
        if current is None or _signature(current) != saved[1]:
            raise ValueError('The timeline file message changed or was removed')

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
                    raise ValueError('The timeline file address is already being requested')
                self._inflight.add(descriptor)
                reserved = True
            reply = binding[1].conversation_file_download(descriptor.file_id, descriptor.chat_id,
                descriptor.message_id, item_type=descriptor.item_type, expected_session=binding[2])
            if not isinstance(reply, FileDownloadReply):
                raise ValueError('MAX did not return a typed file address')
            reply = parse_file_download({'url': reply.url, 'unsafe': reply.unsafe})
            with self._local(context):
                self._require_descriptor(context, descriptor)
                return ConversationFileDownload(descriptor, reply.url, reply.unsafe)
        finally:
            if reserved:
                with self._lock:
                    self._inflight.discard(descriptor)


_ADAPTER_LOCK = Lock()


class TimelineFileServiceMixin:
    def _timeline_files_adapter(self):
        with _ADAPTER_LOCK:
            adapter = getattr(self, '_timeline_files_state', None)
            if adapter is None:
                adapter = _TimelineFiles(self)
                self._timeline_files_state = adapter
            return adapter

    def timeline_file_context(self, chat_id):
        return self._timeline_files_adapter().open(chat_id)

    def timeline_file_actions(self, context, message):
        return self._timeline_files_adapter().actions(context, message)

    def get_timeline_file_descriptor(self, context, message, index):
        return self._timeline_files_adapter().descriptor(context, message, index)

    def is_current_timeline_file_context(self, context):
        return self._timeline_files_adapter().current_context(context)

    def clear_timeline_files(self, context=None):
        adapter = getattr(self, '_timeline_files_state', None)
        if adapter is not None:
            adapter.clear(context)
