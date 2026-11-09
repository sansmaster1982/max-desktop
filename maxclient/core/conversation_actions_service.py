"""Account/session-bound Info actions; no action runs during information reads."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from threading import Lock, RLock
import time

from ..protocol.client import MaxError, MaxWriteNotStarted, MaxWriteOutcomeUnknown
from ..protocol.conversation_actions_contracts import (
    mute_deadline, mute_is_active, parse_chat_mute_ack, supplied_mute,
    parse_story_visibility_ack, parse_action_contact_readback, story_hidden_from_contact,
    parse_stop_bot_ack, parse_action_chat_readback, bot_suspended_from_chat,
)


def _now_ms():
    return time.time_ns() // 1_000_000


def _capabilities(raw, contact, owner, server, peer):
    from .conversation_action_capabilities import conversation_action_capabilities
    return conversation_action_capabilities(raw, contact, owner, server, peer_id=peer)


@dataclass
class _PendingAction:
    expected: int | float | bool
    inflight: bool = True
    peer_id: int | None = None


class _ConversationActions:
    def __init__(self, service):
        self.service = service
        self._lock = RLock()
        self._pending = {}

    @contextmanager
    def _local(self, context):
        # Use the service's established metadata -> owner-cache lock order.
        # Neither lock is held over a request, and no read starts authentication.
        service = self.service
        with service._metadata_lock:
            binding = service._history_read_context()
            with service.store.account_scope(binding[0]):
                cached = service._require_conversation(context)
                service._require_history_context(cached[0])
                with self._lock:
                    yield cached
                    service._require_history_context(cached[0])

    def _state(self, context, cached):
        binding, _chat, raw, peer, contact, _cached_contact, config = cached
        capabilities = _capabilities(raw, contact, binding[0], config.get('server', {}), peer)
        actual = supplied_mute(config, context.chat_id)
        story_hidden = story_hidden_from_contact(contact)
        suspended = bot_suspended_from_chat(raw)
        pending = {}
        # WEB's lastEventTime==0 shortcut is a display/model default, not
        # acknowledgement of an unknown119. Retain the stop reservation until
        # an actual DTO explicitly supplies the expected suspension boolean.
        confirmed_suspension = raw.get('isSuspended') if type(raw.get('isSuspended')) is bool else None
        for family, observed in (('mute', actual), ('stories', story_hidden), ('stop_bot', confirmed_suspension)):
            key = (binding[0], context.chat_id, family)
            record = self._pending.get(key)
            if record is not None and not record.inflight and observed is not None and observed == record.expected:
                del self._pending[key]
                record = None
            pending[family] = record is not None
        available = capabilities.get('mute') is True and callable(getattr(binding[1], 'update_chat_mute', None))
        more = capabilities.get('more', [])
        return {'context': context,
                'available': available,
                'mute_transport_available': callable(getattr(binding[1], 'update_chat_mute', None)),
                'availability': {'mute': available,
                    'hide_stories': any(action in more for action in ('hide_stories', 'show_stories'))
                        and callable(getattr(binding[1], 'set_contact_stories_hidden', None)),
                    'stop_bot': 'stop_bot' in more and callable(getattr(binding[1], 'stop_bot', None))},
                'muted': mute_is_active(actual, _now_ms()),
                'dont_disturb_until': actual, 'story_hidden': story_hidden, 'suspended': suspended,
                'pending_actions': pending, 'pending': any(pending.values())}

    def read(self, context):
        with self._local(context) as cached:
            return self._state(context, cached)

    def update_mute(self, context, duration_ms):
        # Validate even an otherwise harmless/no-op input before registering work.
        expected = mute_deadline(duration_ms, _now_ms())
        if not self.read(context)['available']:
            refresh = getattr(self.service, 'refresh_conversation_menu_metadata', None)
            if callable(refresh):
                refresh(context)
        key = record = None
        try:
            with self._local(context) as cached:
                lifecycle = self.service.store.conversation_action_journal(context.owner_id, context.chat_id)
                if lifecycle and lifecycle.get('action') in ('owner_transfer', 'conversation_edit') and lifecycle.get('stage') != 'complete':
                    raise ValueError('Передача прав и выход ещё не завершены')
                before = self._state(context, cached)
                if not before['available']:
                    raise ValueError('Conversation notifications are unavailable in this context')
                if before['pending_actions']['mute']:
                    raise ValueError('The previous conversation change is not confirmed')
                if before['dont_disturb_until'] == expected:
                    return {'accepted': False, 'confirmed': True, 'unchanged': True, **before}
                binding = cached[0]
                key = (binding[0], context.chat_id, 'mute')
                record = _PendingAction(expected)
                self._pending[key] = record
        except Exception:
            with self._lock:
                if record is not None and self._pending.get(key) is record:
                    del self._pending[key]
            raise

        request_started = False
        try:
            # Repeat all owner/client/session/peer/capability guards immediately
            # before the one request, after releasing the local transaction.
            with self._local(context) as cached:
                lifecycle = self.service.store.conversation_action_journal(context.owner_id, context.chat_id)
                if lifecycle and lifecycle.get('action') in ('owner_transfer', 'conversation_edit') and lifecycle.get('stage') != 'complete':
                    raise ValueError('Передача прав и выход ещё не завершены')
                if not self._state(context, cached)['available']:
                    raise ValueError('Conversation notifications are no longer available')
            request_started = True
            result = binding[1].update_chat_mute(
                context.chat_id, expected, expected_session=binding[2])
        except MaxWriteOutcomeUnknown:
            with self._lock:
                record.inflight = False
            raise
        except (MaxError, MaxWriteNotStarted):
            # A structured negative server ACK is the only request failure that
            # establishes no effect and unlocks another explicit action.
            with self._lock:
                if self._pending.get(key) is record:
                    del self._pending[key]
            raise
        except Exception:
            with self._lock:
                if not request_started and self._pending.get(key) is record:
                    del self._pending[key]
                else:
                    record.inflight = False
            if not request_started:
                raise
            raise MaxWriteOutcomeUnknown('The conversation notification result is unknown.') from None

        try:
            result = parse_chat_mute_ack(result)
            with self._local(context) as cached:
                # Only the server's supplied CONFIG goes into the owner cache.
                # Never synthesize a CONFIG from `expected`, even for empty ACK.
                if result is not None:
                    self.service._remember_config(result, owner_id=binding[0])
                record.inflight = False
                current = self._state(context, self.service._require_conversation(context))
            return {'accepted': True, 'confirmed': not current['pending_actions']['mute'],
                    'unchanged': False, **current}
        except Exception:
            with self._lock:
                record.inflight = False
            raise MaxWriteOutcomeUnknown('MAX did not confirm the conversation notification state.') from None

    def _remember_observation(self, context, family, observed):
        """Merge only a validated matching observation under its original context."""
        with self._local(context) as cached:
            if family == 'stories':
                record = {**cached[4], **observed}
                self.service._remember_contact_info({'contact': record})
            else:
                #48 is a complete validated ChatDTO. Optional model fields such
                # as isSuspended/hasBots must not inherit stale true values.
                self.service._remember_chat_info({'chats': [observed]}, my_id=cached[0][0])

    def _readback(self, context, family):
        with self._local(context) as cached:
            client, session, peer = cached[0][1], cached[0][2], cached[3]
        if family == 'stories':
            observed = parse_action_contact_readback(client.contact_info([peer], expected_session=session), peer)
        else:
            observed = parse_action_chat_readback(client.chat_info([context.chat_id], expected_session=session), context.chat_id)
        self._remember_observation(context, family, observed)
        return observed

    def refresh(self, context):
        """One ordinary read per pending contact/bot family; never resend a write."""
        before = self.read(context)
        for family in ('stories', 'stop_bot'):
            if before['pending_actions'][family]:
                self._readback(context, family)
        return self.read(context)

    def update_family(self, context, family, expected):
        if type(expected) is not bool or family not in ('stories', 'stop_bot'):
            raise ValueError('A supported explicit conversation action is required')
        feature = 'hide_stories' if family == 'stories' else 'stop_bot'
        observed_key = 'story_hidden' if family == 'stories' else 'suspended'
        key = record = None
        try:
            with self._local(context) as cached:
                before = self._state(context, cached)
                if before['pending_actions'][family]:
                    raise ValueError('The previous conversation action is not confirmed')
                binding, peer = cached[0], cached[3]
                if (peer is None or peer == binding[0] or cached[2].get('type') != 'DIALOG'
                        or (family == 'stop_bot' and cached[2].get('hasBots') is not True)
                        or (family == 'stories' and cached[2].get('hasBots') is True)):
                    raise ValueError('This conversation action has no confirmed recipient')
                if family == 'stop_bot' and self.service.store.bot_delete_for_peer(binding[0], peer):
                    raise ValueError('Bot deletion is pending; check its state before another stop')
                key = (binding[0], context.chat_id, family)
                record = _PendingAction(expected, peer_id=peer)
                self._pending[key] = record
            # Info refresh is intentionally detached from persistent account
            # state. Read the actual target state under our reservation before
            # using any cached no-op shortcut or mutation capability.
            observed = self._readback(context, family)
            fresh_value = (story_hidden_from_contact(observed) if family == 'stories'
                           else bot_suspended_from_chat(observed))
            if fresh_value is None:
                raise ValueError('MAX did not supply the current conversation action state')
            with self._local(context) as cached:
                before = self._state(context, cached)
                if before[observed_key] is expected:
                    if self._pending.get(key) is record:
                        del self._pending[key]
                    return {'accepted': False, 'confirmed': True, 'unchanged': True,
                            **self._state(context, cached)}
                if not before['availability'][feature]:
                    raise ValueError('This conversation action is unavailable')
        except Exception:
            with self._lock:
                if record is not None and self._pending.get(key) is record:
                    del self._pending[key]
            raise

        started = False
        try:
            with self._local(context) as cached:
                if not self._state(context, cached)['availability'][feature]:
                    raise ValueError('This conversation action is no longer available')
                if family == 'stop_bot' and self.service.store.bot_delete_for_peer(binding[0], peer):
                    raise ValueError('Bot deletion is pending; no competing stop was sent')
            started = True
            if family == 'stories':
                ack = binding[1].set_contact_stories_hidden(peer, expected, expected_session=binding[2])
            else:
                ack = binding[1].stop_bot(peer, expected_session=binding[2])
        except MaxWriteOutcomeUnknown:
            with self._lock:
                record.inflight = False
            raise
        except (MaxError, MaxWriteNotStarted):
            with self._lock:
                if self._pending.get(key) is record:
                    del self._pending[key]
            raise
        except Exception:
            with self._lock:
                if not started and self._pending.get(key) is record:
                    del self._pending[key]
                else:
                    record.inflight = False
            if not started:
                raise
            raise MaxWriteOutcomeUnknown('The conversation action result is unknown.') from None

        readback_failed = False
        try:
            with self._local(context):
                if family == 'stories':
                    ack = parse_story_visibility_ack(ack, peer)
                    if 'contact' in ack:
                        self._remember_observation(context, family, ack['contact'])
                else:
                    parse_stop_bot_ack(ack)
            # Optional34 contact may already supply the matching flags.119
            # VOID supplies no state at all; only an actual48 observation can.
            current = self.read(context)
            if current[observed_key] is not expected:
                try:
                    self._readback(context, family)
                except Exception:
                    # A successful write plus failed ordinary read is accepted
                    # but remains unresolved. A changed context cannot return UI
                    # data or merge its late read into a new owner.
                    self.service._require_conversation(context)
                    readback_failed = True
            with self._local(context) as cached:
                record.inflight = False
                current = self._state(context, cached)
            return {'accepted': True, 'confirmed': not current['pending_actions'][family],
                    'unchanged': False, 'readback_failed': readback_failed, **current}
        except Exception:
            with self._lock:
                record.inflight = False
            raise MaxWriteOutcomeUnknown('MAX did not confirm the conversation action state.') from None


_ADAPTER_LOCK = Lock()


class ConversationActionsServiceMixin:
    def _conversation_actions_adapter(self):
        with _ADAPTER_LOCK:
            adapter = getattr(self, '_conversation_actions_state', None)
            if adapter is None:
                adapter = _ConversationActions(self)
                self._conversation_actions_state = adapter
            return adapter

    def get_conversation_action_state(self, context):
        """Actual supplied state only; does not request data or alter settings."""
        return self._conversation_actions_adapter().read(context)

    def set_conversation_mute(self, context, duration_ms):
        """One user gesture, one write, and no optimistic authoritative state."""
        return self._conversation_actions_adapter().update_mute(context, duration_ms)

    def set_conversation_stories_hidden(self, context, hidden):
        return self._conversation_actions_adapter().update_family(context, 'stories', hidden)

    def stop_conversation_bot(self, context):
        return self._conversation_actions_adapter().update_family(context, 'stop_bot', True)

    def refresh_conversation_action_state(self, context):
        return self._conversation_actions_adapter().refresh(context)
