"""Detached preview regressions: synthetic DTOs, in-memory SQL, no network."""
import copy
from dataclasses import FrozenInstanceError
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from maxclient.core.conversation_info_service import ConversationInfoServiceMixin, ConversationUnavailable
from maxclient.core.conversation_preview_service import ConversationPreviewResult, ConversationPreviewServiceMixin
from maxclient.core.models import Chat, Contact, Message
from maxclient.core.service import MaxService
from maxclient.core.store import Store
from maxclient.protocol.client import (
    AuthenticatedSession, ConnectionState, MaxClient, MaxFrame, MaxWriteNotStarted,
)


DEVICE = '00000000-0000-0000-0000-000000000123'
STAMP = 1_790_000_000_000.25


def message(identity=7, timestamp=STAMP, **fields):
    return {'id': identity, 'time': timestamp, 'type': 'USER', 'sender': 202,
            'text': 'Synthetic literal text', **fields}


def chat(**fields):
    return {'id': 303, 'type': 'DIALOG', 'participants': {'101': 0, '202': 0},
            'status': 'ACTIVE', 'lastMessage': message(), **fields}


class _Socket:
    def sendall(self, _frame):
        raise AssertionError('A preview test must never use a real transport')


class _Service(ConversationInfoServiceMixin, ConversationPreviewServiceMixin):
    _confirmed_profile_id = MaxService._confirmed_profile_id
    _history_read_context = MaxService._history_read_context
    _require_history_context = MaxService._require_history_context

    @property
    def my_id(self):
        return self.session.my_user_id


class ConversationPreviewTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(':memory:')
        self.addCleanup(self.store.close)
        self.store.bind_owner(101)
        self.store.upsert_chat(Chat(id=303, title='Cached title', chat_type='DIALOG', peer_user_id=202,
                                   unread=4, last_time_ms=STAMP, last_preview='Cached preview'))
        self.store.upsert_contact(Contact(id=202, name='Cached contact'))
        self.store.insert_message(Message(chat_id=303, id=1, time_ms=STAMP - 20, text='Cached old message'))
        self.service = _Service()
        self.service.store = self.store
        self.service.session = SimpleNamespace(my_user_id=101, device_id=DEVICE)
        self.service._metadata_lock = threading.RLock()
        self.service._server_metadata_owner = 101
        self.service._server_chats = {303: chat()}
        self.service._server_contacts = {202: {'id': 202, 'names': [{'name': '<b>Actual peer</b>'}]}}
        self.client = MaxClient(device_id=DEVICE, auto_reconnect=False)
        self.client._device_type = 'WEB'
        self.client._sock = _Socket()
        self.client._state = ConnectionState.CONNECTED
        self.client._auth_session = AuthenticatedSession(101, DEVICE, 'WEB', self.client._sock)
        self.client.chat_info = Mock(return_value={'chats': [chat()]})
        self.client.chat_history_window = Mock(return_value={'messages': [message()], 'chat': chat()})
        self.client.request = Mock(side_effect=AssertionError('No extra RPC'))
        self.client.mark_read = Mock(side_effect=AssertionError('No receipt'))
        self.client.mark_unread = Mock(side_effect=AssertionError('No unread mutation'))
        self.client.send_message = Mock(side_effect=AssertionError('No send'))
        self.client.typing = Mock(side_effect=AssertionError('No typing'))
        self.client.contact_info = Mock(side_effect=AssertionError('No hydration'))
        self.service.client = self.client
        self.service.open_chat = Mock(side_effect=AssertionError('No ordinary timeline opening'))
        self.service._activate_conversation_files = Mock(side_effect=AssertionError('No file activation'))
        self.context = self.service.conversation_context(303, activate_files=False)
        self.changes = self.store._db.total_changes

    def tearDown(self):
        for method in ('request', 'mark_read', 'mark_unread', 'send_message', 'typing', 'contact_info'):
            getattr(self.client, method).assert_not_called()
        self.service.open_chat.assert_not_called()
        self.service._activate_conversation_files.assert_not_called()
        self.assertEqual(self.store._db.total_changes, self.changes)

    def load(self, **kwargs):
        return self.service.load_conversation_preview(self.context, **kwargs)

    def replace_session(self):
        self.client._auth_session = AuthenticatedSession(101, DEVICE, 'WEB', self.client._sock)

    def test_real_service_includes_preview_mixin(self):
        self.assertTrue(issubclass(MaxService, ConversationPreviewServiceMixin))

    def test_one_bound_get49_returns_literal_title_without_cache_or_counter_change(self):
        metadata = copy.deepcopy(self.service._server_chats)
        before = copy.deepcopy(self.store.get_chat(303))
        result = self.load()
        self.assertIsInstance(result, ConversationPreviewResult)
        self.assertIs(result.context, self.context)
        self.assertEqual(result.title, '<b>Actual peer</b>')
        self.assertEqual(result.anchor, STAMP)
        self.assertEqual([(item.chat_id, item.id, item.text) for item in result.messages],
                         [(303, 7, 'Synthetic literal text')])
        self.client.chat_history_window.assert_called_once_with(303, from_cursor=STAMP,
            forward=0, backward=20, get_chat=True, expected_session=self.context.session_context)
        self.client.chat_info.assert_not_called()
        self.assertEqual(self.service._server_chats, metadata)
        self.assertEqual(self.store.get_chat(303), before)
        self.assertEqual([item.id for item in self.store.messages(303)], [1])
        with self.assertRaises(FrozenInstanceError):
            result.title = 'Changed'

    def test_result_is_detached_from_server_payload_and_other_previews(self):
        first = self.load()
        first.messages[0].text = 'Changed local result'
        self.client.chat_history_window.return_value['messages'][0]['text'] = 'Later server response'
        second = self.load()
        self.assertEqual(first.messages[0].text, 'Changed local result')
        self.assertEqual(second.messages[0].text, 'Later server response')

    def test_confirmed_self_uses_saved_title(self):
        self.store.upsert_chat(Chat(id=0, chat_type='DIALOG', peer_user_id=101, title='Not saved label'))
        self.service._server_chats[0] = chat(id=0, participants={'101': 0})
        self.context = self.service.conversation_context(0, activate_files=False)
        self.client.chat_history_window.return_value = {'messages': [message(sender=101)]}
        self.changes = self.store._db.total_changes
        result = self.load()
        self.assertEqual(result.title, 'Избранное')
        self.assertTrue(result.messages[0].outgoing)

    def test_missing_anchor_uses_one_bound_get48_then_get49_without_saving_metadata(self):
        self.service._server_chats[303].pop('lastMessage')
        original = copy.deepcopy(self.service._server_chats)
        self.assertEqual(self.load().anchor, STAMP)
        self.client.chat_info.assert_called_once_with([303], expected_session=self.context.session_context)
        self.assertEqual(self.service._server_chats, original)

    def test_confirmed_empty_metadata_returns_empty_and_never_old_cached_rows(self):
        self.service._server_chats[303].pop('lastMessage')
        empty = chat()
        empty.pop('lastMessage')
        self.client.chat_info.return_value = {'chat': empty}
        result = self.load()
        self.assertEqual(result.messages, ())
        self.assertIsNone(result.anchor)
        self.client.chat_history_window.assert_not_called()

    def test_bad_anchor_metadata_cannot_fallback_to_time_id_zero_or_cache(self):
        self.service._server_chats[303].pop('lastMessage')
        invalid = [None, {}, {'id': 7}, {'time': True}, {'time': '0'}, {'time': float('inf')}]
        for last in invalid:
            with self.subTest(last=last):
                self.client.chat_info.return_value = {'chats': [chat(lastMessage=last)]}
                with self.assertRaises(ConversationUnavailable): self.load()
        self.client.chat_history_window.assert_not_called()

    def test_get48_rejects_wrong_chat_duplicate_rows_and_wrong_peer(self):
        self.service._server_chats[303].pop('lastMessage')
        for value in ({}, {'chats': [chat(id=404)]}, {'chats': [chat(), chat()]},
                      {'chats': [chat(participants={'101': 0, '404': 0})]}):
            with self.subTest(value=value):
                self.client.chat_info.return_value = value
                with self.assertRaises(ConversationUnavailable): self.load()
        self.client.chat_history_window.assert_not_called()

    def test_same_owner_reauthentication_during_metadata_stops_before_history(self):
        self.service._server_chats[303].pop('lastMessage')
        self.client.chat_info.side_effect = lambda *a, **k: self.replace_session() or {'chats': [chat()]}
        with self.assertRaises(ConversationUnavailable): self.load()
        self.client.chat_history_window.assert_not_called()

    def test_context_change_during_history_discards_entire_old_result(self):
        for changed in ('session', 'client', 'socket', 'device', 'owner', 'peer'):
            with self.subTest(changed=changed):
                sock = self.client._sock
                session = self.client._auth_session
                def reply(*_args, **_kwargs):
                    if changed == 'session': self.replace_session()
                    elif changed == 'client': self.service.client = object()
                    elif changed == 'socket': self.client._sock = _Socket()
                    elif changed == 'device': self.client.device_id = 'different'
                    elif changed == 'owner': self.service.session.my_user_id = 404
                    else: self.service._server_chats[303]['participants'] = {'101': 0, '404': 0}
                    return {'messages': [message()]}
                self.client.chat_history_window.side_effect = reply
                with self.assertRaises(ConversationUnavailable): self.load()
                self.service.client = self.client
                self.client._sock, self.client._auth_session = sock, session
                self.client.device_id = DEVICE
                self.service.session.my_user_id = 101
                self.service._server_chats[303] = chat()

    def test_owner_cache_rebind_during_history_cannot_return_old_account_data(self):
        def reply(*_args, **_kwargs):
            self.store.bind_owner(404)
            self.changes = self.store._db.total_changes
            return {'messages': [message()]}
        self.client.chat_history_window.side_effect = reply
        with self.assertRaises(ConversationUnavailable): self.load()
        self.assertEqual(self.store.messages(303), [])

    def test_stale_context_before_request_sends_nothing(self):
        self.replace_session()
        with self.assertRaises(ConversationUnavailable): self.load()
        self.client.chat_info.assert_not_called()
        self.client.chat_history_window.assert_not_called()

    def test_wrong_chat_in_response_or_each_message_never_surfaces(self):
        for fields in ({'chatId': 404}, {'chatId': True}, {'chat': chat(id=404)},
                       {'chat': None}, {'chat': chat(participants={'101': 0, '404': 0})},
                       {'messages': [message(chatId=404)]}, {'messages': [message(chatId='303')]}):
            with self.subTest(fields=fields):
                self.client.chat_history_window.return_value = {'messages': [message()], **fields}
                with self.assertRaises(ConversationUnavailable): self.load()

    def test_malformed_rows_and_duplicate_ids_fail_without_partial_result(self):
        for invalid in (None, {}, message(id=True), message(time=True), message(time=float('nan')),
                        message(type='OTHER'), message(sender='202'), message(text=[]),
                        message(attaches=[None]), message(attaches={}), message(updateTime=float('inf'))):
            with self.subTest(invalid=invalid):
                self.client.chat_history_window.return_value = {'messages': [message(), invalid]}
                with self.assertRaises(ConversationUnavailable): self.load()
        self.client.chat_history_window.return_value = {'messages': [message(), message()]}
        with self.assertRaises(ConversationUnavailable): self.load()

    def test_missing_messages_or_oversized_result_cannot_claim_empty(self):
        for value in ({}, {'messages': None}, {'messages': {}}, {'messages': [message()] * 1001}):
            self.client.chat_history_window.return_value = value
            with self.assertRaises(ConversationUnavailable): self.load()

    def test_removed_messages_are_not_shown_or_replaced_by_cached_content(self):
        self.client.chat_history_window.return_value = {'messages': [message(status='REMOVED'),
            message(8, deleted=True), message(9, removed=True)]}
        self.assertEqual(self.load().messages, ())

    def test_actual_timestamp_order_optional_sender_and_bounded_latest_messages(self):
        first = message(1, -0.25)
        first.pop('sender')
        self.client.chat_history_window.return_value = {'messages': [message(9, 0.25), first, message(8, 0)]}
        result = self.load(count=2)
        self.assertEqual([item.time_ms for item in result.messages], [0, 0.25])
        self.client.chat_history_window.return_value = {'messages': [first]}
        self.assertIsNone(self.load().messages[0].sender)

    def test_confirmed_clear_during_request_filters_old_content_at_final_return(self):
        def reply(*_args, **_kwargs):
            self.store.clear_confirmed_history(101, 303, STAMP)
            self.changes = self.store._db.total_changes
            return {'messages': [message(), message(8, STAMP + 1, text='After clear')]}
        self.client.chat_history_window.side_effect = reply
        self.assertEqual([item.id for item in self.load().messages], [8])

    def test_failure_has_no_auth_retry_or_local_history_fallback(self):
        self.client.chat_history_window.side_effect = ConnectionError('Synthetic read failure')
        with self.assertRaises(ConnectionError): self.load()
        self.assertEqual(self.client.chat_history_window.call_count, 1)
        self.client.chat_info.assert_not_called()

    def test_invalid_counts_stop_before_any_read(self):
        for count in (0, 51, -1, 1.5, '20', True):
            with self.assertRaises(ValueError): self.load(count=count)
        self.client.chat_info.assert_not_called()
        self.client.chat_history_window.assert_not_called()


class PreviewProtocolTests(unittest.TestCase):
    def test_history_preserves_wire_packet_and_binds_expected_transport(self):
        client = MaxClient(device_id=DEVICE, auto_reconnect=False)
        context = AuthenticatedSession(101, DEVICE, 'WEB', object())
        client.request = Mock(return_value=MaxFrame(1, 7, 49, b'', {'messages': []}))
        self.assertEqual(client.chat_history_window(303, from_cursor=STAMP, backward=20,
                         expected_session=context), {'messages': []})
        client.request.assert_called_once_with(49, {'chatId': 303, 'from': STAMP, 'forward': 0,
            'backward': 20, 'getMessages': True, 'getChat': True}, timeout=30, expected_session=context)

    def test_stale_authenticated_session_is_rejected_before_socket_send(self):
        client = MaxClient(device_id=DEVICE, auto_reconnect=False)
        sock = _Socket()
        client._sock, client._state = sock, ConnectionState.CONNECTED
        original = AuthenticatedSession(101, DEVICE, 'WEB', sock)
        client._device_type = 'WEB'
        client._auth_session = AuthenticatedSession(101, DEVICE, 'WEB', sock)
        with self.assertRaises(MaxWriteNotStarted):
            client.chat_history_window(303, from_cursor=STAMP, expected_session=original)
        self.assertFalse(client._pending)


if __name__ == '__main__':
    unittest.main()
