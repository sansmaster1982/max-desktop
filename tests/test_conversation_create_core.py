"""Creation orchestration against fake wire replies and isolated SQLite."""
import copy
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from maxclient.core.models import Contact
from maxclient.core.service import MaxService
from maxclient.core.store import Store
from maxclient.protocol.client import (AuthenticatedSession, ConnectionState, MaxClient, MaxFrame,
    MaxError, MaxTimeout, MaxWriteNotStarted, MaxWriteOutcomeUnknown)
from test_conversation_create_contracts import chat_record

DEVICE = '00112233-4455-6677-8899-aabbccddeeff'


class ConversationCreateCoreTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(':memory:')
        self.addCleanup(self.store.close)
        self.store.bind_owner(101)
        self.service = self.make_service(self.store)
        self.client = self.service.client
        self.context = self.client.authenticated_session
        self.current = None
        self.client.request = Mock(side_effect=self.reply)

    def make_service(self, store):
        service = MaxService.__new__(MaxService)
        service.store = store
        service.session = SimpleNamespace(my_user_id=101, device_id=DEVICE)
        service._metadata_lock = threading.RLock()
        service._server_metadata_owner = 101
        service._server_chats = {}
        service._server_contacts = {}
        service._contacts_cache = None
        client = MaxClient(device_id=DEVICE, auto_reconnect=False)
        client._device_type = 'WEB'
        client._state = ConnectionState.CONNECTED
        client._sock = object()
        client._auth_session = AuthenticatedSession(101, DEVICE, 'WEB', client._sock)
        service.client = client
        return service

    def reply(self, opcode, payload, **kwargs):
        self.assertIs(kwargs['expected_session'], self.context)
        if opcode == 64:
            self.assertIsNotNone(self.store.load_conversation_creation(101))
            kind = 'group' if payload['message']['attaches'][0]['chatType'] == 'CHAT' else 'channel'
            self.current = chat_record(kind)
            data = {'chatId': self.current['id'], 'chat': copy.deepcopy(self.current),
                    'message': {'id': 800, 'time': 10, 'type': 'USER', 'cid': payload['message']['cid']}}
        elif opcode == 48:
            self.assertEqual(payload, {'chatIds': [-701]})
            data = {'chats': [copy.deepcopy(self.current)]}
        elif opcode == 55:
            self.assertEqual(set(payload), {'chatId', 'description'})
            self.current['description'] = payload['description']
            data = {'chat': copy.deepcopy(self.current)}
        else:
            raise AssertionError('Unexpected synthetic RPC')
        return MaxFrame(1, 1, opcode, b'', data)

    def create(self, kind='group', **kwargs):
        return self.service.create_conversation(kind, 'Synthetic title', context=self.context, **kwargs)

    def codes(self):
        return [call.args[0] for call in self.client.request.call_args_list]

    def test_group_creation_and_cache_only_from_confirmed_real_id(self):
        result = self.create()
        self.assertEqual(self.codes(), [64, 48])
        self.assertEqual(result.chat.id, -701)
        self.assertTrue(result.chat.is_group)
        self.assertIsNone(self.service.pending_conversation_creation(context=self.context))
        self.assertEqual(len(self.store.list_chats()), 1)

    def test_channel_description_separate55_then_actual_id_readback(self):
        result = self.create('channel', description='Info')
        self.assertEqual(self.codes(), [64, 48, 55, 48])
        self.assertTrue(result.chat.is_channel)
        self.assertTrue(result.description_confirmed)
        self.assertEqual(self.service._server_chats[-701]['description'], 'Info')

    def test_only_explicit_confirmed_contact_ids_are_sent(self):
        self.store.upsert_contact(Contact(202, 'Synthetic contact'))
        self.create(member_ids=[202])
        self.assertEqual(self.client.request.call_args_list[0].args[1]['message']['attaches'][0]['userIds'], [202])

    def test_unknown_or_self_member_fails_before_write(self):
        for ids in ([202], [101]):
            with self.assertRaises(ValueError):
                self.create(member_ids=ids)
        self.client.request.assert_not_called()

    def test_invalid_form_never_reserves_or_calls_server(self):
        with self.assertRaises(ValueError):
            self.service.create_conversation('group', '', context=self.context)
        self.assertIsNone(self.store.load_conversation_creation(101))
        self.client.request.assert_not_called()

    def test_stale_context_rejected_before_queue_execution(self):
        self.client._auth_session = AuthenticatedSession(101, DEVICE, 'WEB', self.client._sock)
        with self.assertRaises(ValueError):
            self.create()
        self.client.request.assert_not_called()

    def test_negative_creation_ack_releases_guard_without_caching(self):
        self.client.request.side_effect = lambda code, *args, **kwargs: MaxFrame(3, 1, code, b'', {'error': 'error'})
        with self.assertRaises(MaxError):
            self.create()
        self.assertIsNone(self.store.load_conversation_creation(101))
        self.assertEqual(self.store.list_chats(), [])

    def test_not_started_releases_reservation(self):
        self.client.request.side_effect = MaxWriteNotStarted('Synthetic pre-send failure')
        with self.assertRaises(MaxWriteNotStarted):
            self.create()
        self.assertIsNone(self.store.load_conversation_creation(101))

    def test_timeout_never_retries_or_uses_same_title_as_identity(self):
        self.client.request.side_effect = MaxTimeout('Synthetic timeout')
        with self.assertRaises(MaxWriteOutcomeUnknown):
            self.create()
        pending = self.service.pending_conversation_creation(context=self.context)
        self.assertFalse(pending['has_chat_id'])
        for action in (lambda: self.create('channel'), lambda: self.service.verify_conversation_creation(context=self.context)):
            with self.assertRaises(MaxWriteOutcomeUnknown):
                action()
        self.assertEqual(self.codes(), [64])

    def test_malformed_ack_no_synthetic_chat_and_no_repeat(self):
        self.client.request.side_effect = lambda code, *args, **kwargs: MaxFrame(1, 1, code, b'', {'chatId': -701})
        with self.assertRaises(MaxWriteOutcomeUnknown):
            self.create()
        self.assertEqual(self.store.list_chats(), [])
        with self.assertRaises(MaxWriteOutcomeUnknown):
            self.create()
        self.assertEqual(self.codes(), [64])

    def test_known_ack_failed_readback_is_recoverable_only_with48(self):
        def delayed(code, *args, **kwargs):
            if code == 48:
                raise MaxTimeout('Synthetic read timeout')
            return self.reply(code, *args, **kwargs)
        self.client.request.side_effect = delayed
        with self.assertRaises(MaxWriteOutcomeUnknown):
            self.create()
        self.assertTrue(self.service.pending_conversation_creation(context=self.context)['has_chat_id'])
        self.client.request.side_effect = self.reply
        result = self.service.verify_conversation_creation(context=self.context)
        self.assertEqual(result.chat.id, -701)
        self.assertEqual(self.codes(), [64, 48, 48])

    def test_description_unknown_returns_created_chat_and_readonly_recovery(self):
        def delayed(code, *args, **kwargs):
            frame = self.reply(code, *args, **kwargs)
            if code == 55:
                raise MaxTimeout('Synthetic lost update ACK')
            return frame
        self.client.request.side_effect = delayed
        result = self.create('channel', description='Info')
        self.assertEqual(result.chat.id, -701)
        self.assertFalse(result.description_confirmed)
        with self.assertRaises(MaxWriteOutcomeUnknown):
            self.create()
        self.client.request.side_effect = self.reply
        result = self.service.verify_conversation_creation(context=self.context)
        self.assertTrue(result.description_confirmed)
        self.assertEqual(self.codes(), [64, 48, 55, 48])

    def test_description_explicit_rejection_finishes_creation_without_retry(self):
        def rejected(code, *args, **kwargs):
            if code == 55:
                return MaxFrame(3, 1, code, b'', {'error': 'rejected'})
            return self.reply(code, *args, **kwargs)
        self.client.request.side_effect = rejected
        result = self.create('channel', description='Info')
        self.assertFalse(result.description_confirmed)
        self.assertIsNone(self.store.load_conversation_creation(101))
        self.assertEqual(self.codes(), [64, 48, 55])

    def test_crash_before_description_resolves_created_without_auto55(self):
        def delayed(code, *args, **kwargs):
            if code == 48:
                raise MaxTimeout('Synthetic interruption')
            return self.reply(code, *args, **kwargs)
        self.client.request.side_effect = delayed
        with self.assertRaises(MaxWriteOutcomeUnknown):
            self.create('channel', description='Info')
        self.client.request.side_effect = self.reply
        result = self.service.verify_conversation_creation(context=self.context)
        self.assertFalse(result.description_confirmed)
        self.assertEqual(self.codes(), [64, 48, 48])

    def test_owner_change_after64_leaves_guard_and_caches_nothing(self):
        def switched(code, *args, **kwargs):
            frame = self.reply(code, *args, **kwargs)
            self.store.bind_owner(303)
            self.service.session.my_user_id = 303
            self.service._server_metadata_owner = 303
            self.client._auth_session = AuthenticatedSession(303, DEVICE, 'WEB', self.client._sock)
            return frame
        self.client.request.side_effect = switched
        with self.assertRaises(MaxWriteOutcomeUnknown):
            self.create()
        self.assertEqual(self.store.list_chats(), [])
        self.store.bind_owner(101)
        self.assertIsNotNone(self.store.load_conversation_creation(101))

    def test_service_recreated_still_blocks_duplicate(self):
        self.client.request.side_effect = MaxTimeout('Synthetic timeout')
        with self.assertRaises(MaxWriteOutcomeUnknown):
            self.create()
        restored = self.make_service(self.store)
        restored.client.request = Mock(side_effect=AssertionError('Must not send'))
        with self.assertRaises(MaxWriteOutcomeUnknown):
            restored.create_conversation('group', 'Other title', context=restored.client.authenticated_session)
        restored.client.request.assert_not_called()

    def test_store_journal_persists_reopen_and_separates_owner(self):
        pending = {'version': 1, 'kind': 'group', 'stage': 'create', 'cid': 700,
                   'values': {'kind': 'group', 'title': 'Synthetic title', 'member_ids': [], 'description': ''}}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'isolated.sqlite'
            first = Store(path); first.bind_owner(101)
            first.reserve_conversation_creation(101, pending)
            first.close()
            second = Store(path)
            try:
                self.assertEqual(second.load_conversation_creation(101), pending)
                second.bind_owner(303)
                self.assertIsNone(second.load_conversation_creation(303))
                with self.assertRaises(ValueError):
                    second.load_conversation_creation(101)
                second.clear_cache(); second.bind_owner(101)
                self.assertEqual(second.load_conversation_creation(101), pending)
                with self.assertRaises(ValueError):
                    second.reserve_conversation_creation(101, pending)
            finally:
                second.close()

    def test_busy_lock_blocks_parallel_create(self):
        lock = self.service._conversation_creation_lock()
        lock.acquire()
        try:
            with self.assertRaises(ValueError):
                self.create()
        finally:
            lock.release()
        self.client.request.assert_not_called()

    def test_server_lower_limits_used_before_write(self):
        self.store.merge_config(101, {'server': {'max-cname-length': 3}})
        with self.assertRaises(ValueError):
            self.create()
        self.client.request.assert_not_called()


if __name__ == '__main__':
    unittest.main()
