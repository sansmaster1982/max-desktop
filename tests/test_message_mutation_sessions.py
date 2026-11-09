"""Menu-action owner/transport races against synthetic requests and memory Store.

No network, login, account mutation, invitations or real message contents.
"""
import copy
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from maxclient.core.models import Chat, Message
from maxclient.core.service import MaxService
from maxclient.core.store import Store
from maxclient.protocol.client import (
    AuthenticatedSession, ConnectionState, MaxClient, MaxError, MaxFrame,
    MaxTimeout, MaxWriteNotStarted, MaxWriteOutcomeUnknown,
)


DEVICE = '00112233-4455-6677-8899-aabbccddeeff'
STAMP = 1_700_000_000_000
KINDS = ('edit', 'delete', 'forward', 'react', 'unreact')


class MessageMutationSessionTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(':memory:')
        self.addCleanup(self.store.close)
        self.store.bind_owner(101)
        self.service = MaxService.__new__(MaxService)
        self.service.store = self.store
        self.service.session = SimpleNamespace(my_user_id=101, device_id=DEVICE)
        self.service._metadata_lock = threading.RLock()
        self.service._server_metadata_owner = 101
        self.service._server_chats = {}
        self.service._server_contacts = {}
        self.service._contacts_cache = None
        self.service._chat_fetch_anchor = {}
        self.service.on_chat_changed = Mock()
        self.client = MaxClient(device_id=DEVICE, auto_reconnect=False)
        self.client._device_type = 'WEB'
        self.client._state = ConnectionState.CONNECTED
        self.client._sock = Mock()
        self.context = AuthenticatedSession(101, DEVICE, 'WEB', self.client._sock)
        self.client._auth_session = self.context
        self.client.request = Mock(side_effect=self.reply)
        self.service.client = self.client
        self.seed()

    def seed(self):
        for identity in (42, 43):
            self.store.upsert_chat(Chat(id=identity, title='Synthetic', is_group=True, chat_type='CHAT'))
        self.store.insert_message(Message(chat_id=42, id=100, sender=101,
            text='Original synthetic text', time_ms=STAMP, outgoing=True,
            reaction_info={'yourReaction': 'synthetic', 'totalCount': 1}))

    def reply(self, opcode, payload, **kwargs):
        self.assertIs(kwargs.get('expected_session'), self.context)
        if opcode == 67:
            data = {'chatId': 42, 'message': {'id': 100, 'sender': 101,
                     'text': 'Confirmed synthetic edit', 'time': STAMP}}
        elif opcode == 66:
            data = {'chatId': 42, 'messageIds': [100]}
        elif opcode == 64:
            data = {'chatId': 43, 'message': {'id': 200, 'sender': 101,
                     'text': 'Confirmed synthetic forward', 'time': STAMP + 1}}
        elif opcode in (178, 179):
            data = {'reactionInfo': {'totalCount': 2 if opcode == 178 else 0}}
        else:
            raise AssertionError('Unexpected synthetic opcode')
        return MaxFrame(1, 1, opcode, b'', data)

    def act(self, kind, context='captured'):
        kwargs = {'context': self.context if context == 'captured' else context}
        if kind == 'edit':
            return self.service.edit_message(42, 100, 'Requested synthetic edit', **kwargs)
        if kind == 'delete':
            return self.service.delete_messages(42, [100], **kwargs)
        if kind == 'forward':
            return self.service.forward_messages(42, [100], 43, **kwargs)
        if kind == 'react':
            return self.service.set_message_reaction(42, 100, 'synthetic', **kwargs)
        return self.service.remove_message_reaction(42, 100, **kwargs)

    def snapshot(self):
        return (copy.deepcopy(self.store.messages(42)), copy.deepcopy(self.store.messages(43)),
                copy.deepcopy(self.service._server_chats))

    def switch(self, owner=101):
        if owner != 101:
            self.store.bind_owner(owner)
            self.service.session.my_user_id = owner
            self.service._server_metadata_owner = owner
            self.service._server_chats.clear()
            self.service._server_contacts.clear()
            self.seed()
        self.client._auth_session = AuthenticatedSession(owner, DEVICE, 'WEB', self.client._sock)

    def test_missing_or_reconnected_context_never_queues_rpc(self):
        for kind in KINDS:
            for context in (None, AuthenticatedSession(101, DEVICE, 'WEB', self.client._sock)):
                with self.subTest(kind=kind, context_missing=context is None):
                    with self.assertRaises(ValueError):
                        self.act(kind, context)
        self.client.request.assert_not_called()
        self.service.on_chat_changed.assert_not_called()

    def test_different_client_is_rejected_even_with_old_owner(self):
        replacement = MaxClient(device_id=DEVICE, auto_reconnect=False)
        replacement._device_type = 'WEB'
        replacement._state = ConnectionState.CONNECTED
        replacement._sock = Mock()
        replacement._auth_session = AuthenticatedSession(101, DEVICE, 'WEB', replacement._sock)
        self.service.client = replacement
        for kind in KINDS:
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                self.act(kind)
        self.client.request.assert_not_called()
        replacement._sock.sendall.assert_not_called()

    def test_all_actions_propagate_captured_transport_and_commit_once(self):
        for kind in KINDS:
            with self.subTest(kind=kind):
                # A confirmed deletion permanently tombstones this ID for its
                # owner. Each independent action therefore needs a fresh cache.
                case = MessageMutationSessionTests()
                case.setUp()
                try:
                    case.act(kind)
                    case.client.request.assert_called_once()
                    self.assertIs(case.client.request.call_args.kwargs['expected_session'], case.context)
                    case.service.on_chat_changed.assert_called_once_with(43 if kind == 'forward' else 42)
                    if kind == 'edit':
                        self.assertEqual(case.store.messages(42)[0].text, 'Confirmed synthetic edit')
                    elif kind == 'delete':
                        self.assertEqual(case.store.messages(42), [])
                    elif kind == 'forward':
                        self.assertEqual(case.store.messages(43)[0].id, 200)
                    else:
                        self.assertEqual(case.store.messages(42)[0].reaction_info['totalCount'],
                                         2 if kind == 'react' else 0)
                finally:
                    case.doCleanups()

    def test_same_owner_reconnect_while_waiting_discards_late_ack_and_callback(self):
        for kind in KINDS:
            with self.subTest(kind=kind):
                self.client._auth_session = self.context
                before = self.snapshot()
                def late(opcode, payload, **kwargs):
                    frame = self.reply(opcode, payload, **kwargs)
                    self.switch()
                    return frame
                self.client.request.side_effect = late
                with self.assertRaises(MaxWriteOutcomeUnknown):
                    self.act(kind)
                self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.client.request.call_count, len(KINDS))
        self.service.on_chat_changed.assert_not_called()

    def test_switched_owner_cache_receives_no_late_edits_deletes_forwards_or_reactions(self):
        for kind in KINDS:
            with self.subTest(kind=kind):
                self.store.bind_owner(101)
                self.service.session.my_user_id = 101
                self.service._server_metadata_owner = 101
                self.client._auth_session = self.context
                self.seed()
                expected = []
                def late(opcode, payload, **kwargs):
                    frame = self.reply(opcode, payload, **kwargs)
                    self.switch(999)
                    expected.append(self.snapshot())
                    return frame
                self.client.request.side_effect = late
                with self.assertRaises(MaxWriteOutcomeUnknown):
                    self.act(kind)
                self.assertEqual(self.snapshot(), expected[0])
        self.service.on_chat_changed.assert_not_called()

    def test_session_change_between_ack_and_cache_scope_is_still_rejected(self):
        real_commit = self.service._message_mutation_commit
        for kind in KINDS:
            with self.subTest(kind=kind):
                self.client._auth_session = self.context
                before = self.snapshot()
                def replace(binding, apply, chat_id, **kwargs):
                    self.switch()
                    return real_commit(binding, apply, chat_id, **kwargs)
                with patch.object(self.service, '_message_mutation_commit', side_effect=replace):
                    with self.assertRaises(MaxWriteOutcomeUnknown):
                        self.act(kind)
                self.assertEqual(self.snapshot(), before)
        self.service.on_chat_changed.assert_not_called()

    def test_atomic_send_guard_rechecks_every_action_inside_send_lock(self):
        for kind in KINDS:
            with self.subTest(kind=kind):
                self.client._auth_session = self.context
                class ReplaceLock:
                    def __enter__(lock):
                        self.switch()
                    def __exit__(lock, *_):
                        return False
                self.client._send_lock = ReplaceLock()
                self.client.request = MaxClient.request.__get__(self.client, MaxClient)
                with self.assertRaises(MaxWriteNotStarted):
                    self.act(kind)
                self.client._sock.sendall.assert_not_called()
                self.assertEqual(self.client._pending, {})
        self.service.on_chat_changed.assert_not_called()

    def test_unknown_wire_outcome_is_not_retried_and_cache_stays_unchanged(self):
        self.client.request.side_effect = MaxTimeout('Synthetic timeout')
        before = self.snapshot()
        for kind in KINDS:
            with self.subTest(kind=kind), self.assertRaises(MaxWriteOutcomeUnknown):
                self.act(kind)
        self.assertEqual(self.client.request.call_count, len(KINDS))
        self.assertEqual(self.snapshot(), before)
        self.service.on_chat_changed.assert_not_called()

    def test_wrong_chat_or_reaction_message_ack_never_commits(self):
        for kind in KINDS:
            with self.subTest(kind=kind):
                before = self.snapshot()
                def wrong(opcode, payload, **kwargs):
                    frame = self.reply(opcode, payload, **kwargs)
                    frame.decoded['chatId'] = 999
                    return frame
                self.client.request.side_effect = wrong
                with self.assertRaises(MaxWriteOutcomeUnknown):
                    self.act(kind)
                self.assertEqual(self.snapshot(), before)
        for kind in ('react', 'unreact'):
            def wrong_message(opcode, payload, **kwargs):
                frame = self.reply(opcode, payload, **kwargs)
                frame.decoded['messageId'] = 999
                return frame
            self.client.request.side_effect = wrong_message
            with self.subTest(kind=kind), self.assertRaises(MaxWriteOutcomeUnknown):
                self.act(kind)
        self.service.on_chat_changed.assert_not_called()

    def test_forward_batch_stops_after_confirmed_prefix_when_session_changes(self):
        def callback(_):
            self.switch()
        self.service.on_chat_changed.side_effect = callback
        with self.assertRaises((ValueError, MaxWriteOutcomeUnknown)):
            self.service.forward_messages(42, [100, 101], 43, context=self.context)
        self.client.request.assert_called_once()
        self.assertEqual([message.id for message in self.store.messages(43)], [200])


if __name__ == '__main__':
    unittest.main()
