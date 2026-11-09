"""History/push races with real in-memory SQLite and a synthetic transport only."""
from collections import deque
from types import SimpleNamespace
import threading
import unittest
from unittest.mock import Mock

from maxclient.core.models import Chat, Message
from maxclient.core.service import MaxService
from maxclient.core.store import Store
from maxclient.protocol.client import AuthenticatedSession, ConnectionState, MaxClient, MaxFrame


STAMP = 1_790_000_000_000
DEVICE = '00112233-4455-6677-8899-aabbccddeeff'


class _Socket:
    def sendall(self, _data):
        raise AssertionError('No network writes in this review')
    def close(self):
        pass
    def shutdown(self, _how=None):
        pass


class HistoryReconnectReviewTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(':memory:')
        self.store.bind_owner(101)
        self.service = MaxService.__new__(MaxService)
        self.service.store = self.store
        self.service.session = SimpleNamespace(my_user_id=101, device_id=DEVICE)
        self.service._metadata_lock = threading.RLock()
        self.service._server_metadata_owner = 101
        self.service._server_chats = {303: self.chat()}
        self.service._server_contacts = {202: {'id': 202, 'names': [{'name': 'Synthetic peer'}]}}
        self.service._contacts_cache = {}
        self.service._chat_fetch_ts = {}
        self.service._chat_fetch_anchor = {}
        self.service._processed_ids = set()
        self.service._processed_order = deque()
        self.service.on_message = Mock()
        self.service.on_chat_changed = Mock()
        self.service.on_state_changed = Mock()
        self.service.on_config_changed = None
        self.service._incoming_call_lock = threading.Lock()
        self.service._incoming_call_context = None
        self.service._incoming_call_seen = set()
        self.service._incoming_call_order = deque()
        self.client = MaxClient(device_id=DEVICE, auto_reconnect=False)
        self.client._device_type = 'WEB'
        self.client._sock = _Socket()
        self.client._state = ConnectionState.CONNECTED
        self.context = self.context_for(self.client)
        self.client._auth_session = self.context
        self.client.chat_history_window = Mock(return_value={'messages': [self.raw()]})
        self.client.chat_info = Mock(return_value={'chats': [self.chat()]})
        self.client.contact_info = Mock(return_value={'contacts': [self.service._server_contacts[202]]})
        self.client.connect = Mock(side_effect=AssertionError('History must not authenticate'))
        self.client.login = Mock(side_effect=AssertionError('History must not authenticate'))
        self.service.client = self.client
        self.store.upsert_chat(Chat(id=303, title='Synthetic chat', peer_user_id=202,
                                    chat_type='DIALOG', last_time_ms=STAMP, last_preview='latest', unread=5))

    def tearDown(self):
        self.client.connect.assert_not_called()
        self.client.login.assert_not_called()
        self.store.close()

    def raw(self, identity=7, timestamp=STAMP, sender=202, text='Synthetic visible text'):
        return {'id': identity, 'time': timestamp, 'sender': sender, 'text': text, 'type': 'USER'}

    def chat(self):
        return {'id': 303, 'type': 'DIALOG', 'participants': {101: STAMP, 202: STAMP},
                'lastMessage': self.raw(), 'newMessages': 5}

    def context_for(self, client):
        return AuthenticatedSession(101, client.device_id, 'WEB', client._sock)

    def push(self, raw=None, chat_id=303, *, context=None):
        return MaxFrame(0, 8, 128, b'', {'chatId': chat_id, 'message': raw or self.raw()},
                        auth_context=self.context if context is None else context)

    def change_account(self):
        self.store.bind_owner(404)
        self.service.session.my_user_id = 404
        self.service._server_metadata_owner = 404
        self.service._server_chats.clear()
        self.service._server_contacts.clear()

    def test_late_history_after_owner_switch_never_enters_replacement_cache(self):
        def reply(*_args, **_kwargs):
            self.change_account()
            return {'messages': [self.raw()]}
        self.client.chat_history_window.side_effect = reply
        with self.assertRaises(ValueError):
            self.service.open_chat_window(303, from_cursor=STAMP)
        self.assertEqual(self.store.cache_owner, 404)
        self.assertEqual(self.store.messages(303), [])
        self.assertEqual(self.service._server_chats, {})
        self.assertNotIn(303, self.service._chat_fetch_ts)

    def test_same_owner_new_socket_or_login_context_cannot_accept_old_history(self):
        for change in ('socket', 'context', 'client', 'device'):
            with self.subTest(change=change):
                self.client._sock = self.context.transport
                self.client._auth_session = self.context
                self.client.device_id = DEVICE
                self.service.client = self.client
                self.service.session.device_id = DEVICE
                def reply(*_args, **_kwargs):
                    if change == 'socket':
                        self.client._sock = _Socket()
                    elif change == 'context':
                        self.client._auth_session = self.context_for(self.client)
                    elif change == 'client':
                        self.service.client = SimpleNamespace(authenticated_session=self.context)
                    else:
                        self.client.device_id = 'different-device'
                    return {'messages': [self.raw()]}
                self.client.chat_history_window.side_effect = reply
                with self.assertRaises(ValueError):
                    self.service.open_chat_window(303, from_cursor=STAMP)
                self.assertEqual(self.store.messages(303), [])
                self.assertNotIn(303, self.service._chat_fetch_ts)

    def test_missing_authenticated_context_does_not_request_history_or_relogin(self):
        self.client._auth_session = None
        with self.assertRaises(ValueError):
            self.service.open_chat(303)
        self.client.chat_history_window.assert_not_called()
        self.client.chat_info.assert_not_called()

    def test_late_anchor_metadata_cannot_repopulate_another_owner(self):
        self.service._server_chats.clear()
        def reply(_ids, **kwargs):
            self.assertIs(kwargs.get('expected_session'), self.context)
            self.change_account()
            return {'chats': [self.chat()]}
        self.client.chat_info.side_effect = reply
        with self.assertRaises(ValueError):
            self.service.open_chat(303)
        self.assertEqual(self.store.list_chats(), [])
        self.assertEqual(self.service._server_chats, {})
        self.client.chat_history_window.assert_not_called()

    def test_late_contact_resolution_cannot_repopulate_another_owner(self):
        self.service._server_contacts.clear()
        def reply(_ids, **kwargs):
            self.assertIs(kwargs.get('expected_session'), self.context)
            self.change_account()
            return {'contacts': [{'id': 202, 'names': [{'name': 'Synthetic old owner contact'}]}]}
        self.client.contact_info.side_effect = reply
        with self.assertRaises(ValueError):
            self.service.open_chat(303)
        self.assertEqual(self.store.list_contacts(), [])
        self.assertEqual(self.service._server_contacts, {})
        self.client.chat_history_window.assert_not_called()

    def test_history_then_identical_push_is_not_a_new_unread_or_notification(self):
        self.service.open_chat_window(303, from_cursor=STAMP)
        self.service._on_push(self.push())
        self.service._on_push(self.push())
        self.assertEqual(len(self.store.messages(303)), 1)
        self.assertEqual(self.store.get_chat(303).unread, 5)
        self.service.on_message.assert_not_called()

    def test_newer_then_older_push_keeps_preview_but_counts_both_genuinely_new_messages(self):
        self.service._on_push(self.push(self.raw(7, STAMP + 2000, text='newer')))
        self.service._on_push(self.push(self.raw(8, STAMP + 1000, text='older')))
        chat = self.store.get_chat(303)
        self.assertEqual((chat.last_time_ms, chat.last_preview, chat.unread), (STAMP + 2000, 'newer', 7))
        self.assertEqual(self.service._server_chats[303]['lastMessage']['time'], STAMP + 2000)
        self.assertEqual(len(self.store.messages(303)), 2)
        self.assertEqual(self.service.on_message.call_count, 2)

    def test_same_message_id_in_two_chats_is_two_distinct_server_messages(self):
        self.service._on_push(self.push(chat_id=303))
        self.service._on_push(self.push(chat_id=304))
        self.assertEqual(len(self.store.messages(303)), 1)
        self.assertEqual(len(self.store.messages(304)), 1)
        self.assertEqual(self.service.on_message.call_count, 2)

    def test_duplicate_outgoing_echo_reconciles_status_and_keeps_local_identity(self):
        self.store.insert_message(Message(chat_id=303, id=7, sender=101, text='pending-known-id',
                                          time_ms=STAMP, outgoing=True, status='unknown', local_id='synthetic-local-id'))
        self.service._on_push(self.push(self.raw(sender=101, text='server-confirmed')))
        rows = self.store.messages(303)
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0].status, rows[0].local_id, rows[0].text),
                         ('sent', 'synthetic-local-id', 'server-confirmed'))
        self.assertEqual(self.store.get_chat(303).unread, 5)
        self.service.on_message.assert_not_called()

    def test_reconnect_replay_uses_persistent_message_identity_without_increment(self):
        self.service._on_push(self.push())
        self.client._auth_session = self.context_for(self.client)
        self.service._processed_ids.clear()
        self.service._processed_order.clear()
        self.service._on_push(self.push(context=self.client.authenticated_session))
        self.assertEqual(len(self.store.messages(303)), 1)
        self.assertEqual(self.store.get_chat(303).unread, 6)
        self.assertEqual(self.service.on_message.call_count, 1)

    def test_old_or_missing_push_context_cannot_mutate_cache_or_notify(self):
        for context in (self.context_for(self.client), None):
            frame = self.push()
            frame.auth_context = context
            self.service._on_push(frame)
        self.change_account()
        self.service._on_push(self.push())
        self.assertEqual(self.store.messages(303), [])
        self.service.on_message.assert_not_called()

    def test_history_response_for_different_chat_is_rejected_before_cache_write(self):
        self.client.chat_history_window.return_value = {'chat': {'id': 999}, 'messages': [self.raw()]}
        with self.assertRaises(ValueError):
            self.service.open_chat_window(303, from_cursor=STAMP)
        self.assertEqual(self.store.messages(303), [])

    def test_explicit_finite_timestamp_cursor_is_preserved_without_rounding(self):
        cursor = STAMP + 0.25
        self.service.open_chat_window(303, from_cursor=cursor, backward=15, forward=30)
        self.client.chat_history_window.assert_called_once_with(
            303, from_cursor=cursor, backward=15, forward=30, get_chat=True)
        self.assertIs(type(self.client.chat_history_window.call_args.kwargs['from_cursor']), float)
        self.client.chat_history_window.reset_mock()
        for bad in (True, False, float('nan'), float('inf'), float('-inf'), '123'):
            with self.subTest(kind=type(bad).__name__):
                with self.assertRaises(ValueError):
                    self.service.open_chat_window(303, from_cursor=bad)
        self.client.chat_history_window.assert_not_called()

    def test_parallel_replay_emits_once_and_increments_once(self):
        errors = []
        barrier = threading.Barrier(8)
        def receive():
            try:
                barrier.wait(timeout=2)
                self.service._on_push(self.push())
            except BaseException as error:
                errors.append(type(error).__name__)
        threads = [threading.Thread(target=receive) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=3)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        self.assertEqual(len(self.store.messages(303)), 1)
        self.assertEqual(self.store.get_chat(303).unread, 6)
        self.assertEqual(self.service.on_message.call_count, 1)

    def prepare_reconnect(self, frame):
        self.client.on_state = self.service._on_state
        self.client.on_reauthenticated = lambda result, context: self.service._on_reauthenticated(
            self.client, result, context)
        self.client._token = 'synthetic-login-credential'
        self.client._reconnect_running = True
        self.client._reconnect_stop = threading.Event()
        self.client._reconnect_delay = lambda _attempt: 0
        self.client._state = ConnectionState.RECONNECTING
        def connect(**_kwargs):
            self.client._sock = _Socket()
            self.client._set_state(ConnectionState.CONNECTED)
        self.client.connect = Mock(side_effect=connect)
        self.client._auth_frame = Mock(return_value=frame)
        self.client.login = MaxClient.login.__get__(self.client, MaxClient)

    def assert_reconnect_attempted_once(self):
        self.client.connect.assert_called_once_with(device_type='WEB', reset_closed=False)
        self.client._auth_frame.assert_called_once()
        self.client.connect = Mock()
        self.client.login = Mock()

    def test_reconnect_ingests_existing_login_before_publishing_connected_without_more_rpc(self):
        chat = self.chat()
        chat['lastMessage'] = self.raw(8, STAMP + 2000)
        frame = MaxFrame(1, 9, 19, b'', {'profile': {'contact': {'id': 101}}, 'chats': [chat],
                                        'config': {'user': {'PUSH_DETAILS': False}}})
        self.prepare_reconnect(frame)
        self.service._chat_fetch_ts[303] = 123
        observed = []
        self.service.on_state_changed = lambda state: observed.append(
            (state, self.client.authenticated_session,
             self.service._server_chats[303]['lastMessage']['time']))
        self.client._reconnect_loop()
        self.assert_reconnect_attempted_once()
        self.assertEqual(len(observed), 1)
        self.assertEqual(observed[0][0], ConnectionState.CONNECTED)
        self.assertIs(observed[0][1], self.client.authenticated_session)
        self.assertEqual(observed[0][2], STAMP + 2000)
        self.assertFalse(self.store.load_config(101)['user']['PUSH_DETAILS'])
        self.assertEqual(self.service._chat_fetch_ts, {})
        self.client.chat_history_window.assert_not_called()
        self.client.chat_info.assert_not_called()
        self.client.contact_info.assert_not_called()

    def test_reconnect_never_adopts_a_different_profile_or_retries_login(self):
        frame = MaxFrame(1, 9, 19, b'', {'profile': {'contact': {'id': 404}},
                                        'chats': [{'id': 909, 'lastMessage': self.raw()}]})
        self.prepare_reconnect(frame)
        self.client._reconnect_loop()
        self.assert_reconnect_attempted_once()
        self.assertEqual(self.store.cache_owner, 101)
        self.assertEqual(self.service.my_id, 101)
        self.assertNotIn(909, self.service._server_chats)
        self.assertIsNone(self.client.authenticated_session)
        self.assertFalse(any(call.args == (ConnectionState.CONNECTED,)
                             for call in self.service.on_state_changed.call_args_list))

    def test_stale_reconnect_callback_does_not_close_a_newer_same_socket_session(self):
        frame = MaxFrame(1, 9, 19, b'', {'profile': {'contact': {'id': 101}}, 'chats': []})
        self.prepare_reconnect(frame)
        newer = []
        def callback(result, context):
            new_context = self.context_for(self.client)
            self.client._auth_session = new_context
            newer.append(new_context)
            return self.service._on_reauthenticated(self.client, result, context)
        self.client.on_reauthenticated = callback
        self.client._reconnect_loop()
        self.assert_reconnect_attempted_once()
        self.assertIs(self.client.authenticated_session, newer[0])
        self.assertTrue(self.client.is_connected)
        self.assertFalse(self.client._reconnect_running)

    def test_older_history_metadata_cannot_regress_a_newer_push_anchor(self):
        self.service._on_push(self.push(self.raw(8, STAMP + 2000)))
        self.client.chat_history_window.return_value = {'chat': self.chat(), 'messages': [self.raw()]}
        self.service.open_chat_window(303, from_cursor=STAMP)
        self.assertEqual(self.service._server_chats[303]['lastMessage']['time'], STAMP + 2000)
        self.assertEqual(self.store.get_chat(303).last_time_ms, STAMP + 2000)


if __name__ == '__main__':
    unittest.main()
