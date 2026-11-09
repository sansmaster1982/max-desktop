"""Synthetic incoming137 boundary checks. No socket, SDK, media or account I/O."""
from dataclasses import FrozenInstanceError
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, Mock, patch

from maxclient.core.service import MaxService
from maxclient.protocol.client import AuthenticatedSession, ConnectionState, MaxFrame


class _Socket:
    def sendall(self, _data):
        raise AssertionError('A passive incoming notice must not write to transport')

    def close(self):
        pass

    def shutdown(self, _how=None):
        pass


class IncomingCallDispatchTests(unittest.TestCase):
    def setUp(self):
        self.owner = 101
        self.caller = 202
        self.store = MagicMock()
        self.store.cache_owner = self.owner
        self.config = {'server': {'p2p-calls-enabled': True}}
        self.store.load_config.side_effect = lambda _owner: self.config
        paths = SimpleNamespace(dir=MagicMock(), db_file='synthetic-unused',
                                device_profile_file='synthetic-unused')
        settings = SimpleNamespace(app_version='26.10.11', locale='ru', web_app_version='26.10.11')
        session = SimpleNamespace(device_id='00112233-4455-6677-8899-aabbccddeeff',
                                  my_user_id=self.owner)
        with patch('maxclient.core.service.Store', return_value=self.store), patch(
                'maxclient.core.service._load_or_create_device_profile', return_value={}):
            self.service = MaxService(paths, settings, session)
        self.client = self.service.client
        self.client.auto_reconnect = False
        self.client._sock = _Socket()
        self.client._closed = False
        self.client._state = ConnectionState.CONNECTED
        self.client._device_type = 'WEB'
        self.context = self._new_context()
        self.client._auth_session = self.context
        self.client.request = Mock(side_effect=AssertionError('No RPC for incoming notice'))
        self.client._write_frame = Mock(side_effect=AssertionError('No write for incoming notice'))
        self.service._server_metadata_owner = self.owner
        self.service._server_contacts = {self.caller: {'id': self.caller, 'flags': 0}}
        self.notices = []
        self.service.on_incoming_call = self.notices.append
        self.service.on_message = Mock()
        self.service.on_chat_changed = Mock()

    def _new_context(self):
        return AuthenticatedSession(self.owner, self.client.device_id, 'WEB', self.client._sock)

    def frame(self, *, cmd=0, opcode=137, context=None, **changes):
        data = {'callerId': self.caller, 'chatId': 303, 'conversationId': 'synthetic-conversation',
                'type': 'AUDIO', 'vcp': 'synthetic-opaque-parameters'}
        data.update(changes)
        return MaxFrame(cmd, 7, opcode, b'', data,
                        auth_context=self.context if context is None else context)

    def assert_no_remote_effects(self):
        self.client.request.assert_not_called()
        self.client._write_frame.assert_not_called()
        self.service.on_message.assert_not_called()
        self.service.on_chat_changed.assert_not_called()
        self.store.insert_message.assert_not_called()
        self.store.save_config.assert_not_called()

    def test_passive_notice_is_bound_frozen_and_has_no_remote_effect(self):
        self.service._on_push(self.frame(type='VIDEO'))
        self.assertEqual(len(self.notices), 1)
        notice = self.notices[0]
        self.assertEqual((notice.owner_id, notice.caller_id, notice.chat_id, notice.call_type),
                         (self.owner, self.caller, 303, 'VIDEO'))
        self.assertIs(notice.source_client, self.client)
        self.assertIs(notice.session_context, self.context)
        self.assertEqual(notice.sdk_payload(), {'conversationId': 'synthetic-conversation',
                                               'vcp': 'synthetic-opaque-parameters'})
        self.assertTrue(self.service.is_current_incoming_call(notice))
        with self.assertRaises(FrozenInstanceError):
            notice.call_type = 'AUDIO'
        for sensitive in ('synthetic-conversation', 'synthetic-opaque-parameters'):
            self.assertNotIn(sensitive, repr(notice))
        self.assert_no_remote_effects()

    def test_only_command_zero_137_can_create_notice(self):
        for command in (1, 2, 3):
            self.service._on_push(self.frame(cmd=command))
        # Current public139 is contact indexing, not a call-ended event.
        for opcode in (138, 139, 150, 158):
            self.service._on_push(self.frame(opcode=opcode))
        self.assertEqual(self.notices, [])
        self.service._on_push(self.frame())
        self.assertEqual(len(self.notices), 1)
        self.service._on_push(self.frame(opcode=139))
        self.assertTrue(self.service.is_current_incoming_call(self.notices[0]))
        self.assert_no_remote_effects()

    def test_mandatory_fields_are_typed_and_bounded_without_string_id_coercion(self):
        invalid = [
            {'callerId': True}, {'callerId': '202'}, {'callerId': 1 << 63},
            {'chatId': False}, {'chatId': '303'}, {'chatId': -(1 << 63) - 1},
            {'conversationId': ''}, {'conversationId': []}, {'conversationId': 'x' * 257},
            {'type': 'audio'}, {'type': 'GROUP'}, {'type': True},
            {'vcp': ''}, {'vcp': {'token': 'synthetic'}}, {'vcp': 'x' * 262145},
        ]
        for change in invalid:
            with self.subTest(change=tuple(change)):
                self.service._on_push(self.frame(**change))
        for key in ('callerId', 'chatId', 'conversationId', 'type', 'vcp'):
            frame = self.frame()
            frame.decoded.pop(key)
            self.service._on_push(frame)
        self.assertEqual(self.notices, [])
        self.assert_no_remote_effects()

    def test_missing_or_stale_receive_context_is_rejected(self):
        no_context = self.frame()
        no_context.auth_context = None
        self.service._on_push(no_context)
        self.service._on_push(self.frame(context=self._new_context()))
        self.assertEqual(self.notices, [])
        self.assert_no_remote_effects()

    def test_owner_and_cache_owner_must_both_match_confirmed_session(self):
        for attribute, bad in (('my_user_id', 999),):
            original = getattr(self.service.session, attribute)
            setattr(self.service.session, attribute, bad)
            self.service._on_push(self.frame())
            setattr(self.service.session, attribute, original)
        self.store.cache_owner = 999
        self.service._on_push(self.frame())
        self.store.cache_owner = self.owner
        self.service._server_metadata_owner = 999
        self.service._on_push(self.frame())
        self.assertEqual(self.notices, [])
        self.assert_no_remote_effects()

    def test_server_flag_must_be_explicitly_enabled_and_self_caller_is_not_rung(self):
        for value in (False, None, 1, 'true'):
            self.config = {'server': {'p2p-calls-enabled': value}}
            self.service._on_push(self.frame())
        self.config = {'server': {}}
        self.service._on_push(self.frame())
        self.config = {'server': {'p2p-calls-enabled': True}}
        self.service._on_push(self.frame(callerId=self.owner))
        self.assertEqual(self.notices, [])
        self.assert_no_remote_effects()

    def test_known_bot_is_rejected_but_service_account_bit_alone_is_not_bot(self):
        self.service._server_contacts[self.caller]['flags'] = 2
        self.service._on_push(self.frame())
        self.assertEqual(self.notices, [])
        self.service._server_contacts[self.caller]['flags'] = 8
        self.service._on_push(self.frame())
        self.assertEqual(len(self.notices), 1)
        self.assert_no_remote_effects()

    def test_replay_is_atomic_per_context_and_new_login_has_distinct_context(self):
        barrier = threading.Barrier(8)
        failures = []
        def receive():
            try:
                barrier.wait(timeout=2)
                self.service._on_push(self.frame())
            except BaseException as error:
                failures.append(type(error).__name__)
        workers = [threading.Thread(target=receive) for _ in range(8)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=3)
        self.assertFalse(any(worker.is_alive() for worker in workers))
        self.assertEqual(failures, [])
        self.assertEqual(len(self.notices), 1)
        old = self.notices[0]
        self.context = self._new_context()
        self.client._auth_session = self.context
        self.assertFalse(self.service.is_current_incoming_call(old))
        self.service._on_push(self.frame())
        self.assertEqual(len(self.notices), 2)
        self.assert_no_remote_effects()

    def test_queued_notice_becomes_invalid_after_owner_device_client_or_connection_change(self):
        self.service._on_push(self.frame())
        notice = self.notices[0]
        changes = [(self.service.session, 'my_user_id', 999),
                   (self.service.session, 'device_id', 'changed-session-device'),
                   (self.store, 'cache_owner', 999),
                   (self.client, 'device_id', 'changed-device'),
                   (self.client, '_state', ConnectionState.DISCONNECTED),
                   (self.client, '_device_type', 'ANDROID'),
                   (self.service, 'client', SimpleNamespace(authenticated_session=self.context))]
        for target, attribute, value in changes:
            with self.subTest(attribute=attribute):
                previous = getattr(target, attribute)
                setattr(target, attribute, value)
                self.assertFalse(self.service.is_current_incoming_call(notice))
                setattr(target, attribute, previous)
        self.assertTrue(self.service.is_current_incoming_call(notice))
        self.assert_no_remote_effects()

    def test_context_change_while_reading_config_cannot_rebind_old_frame(self):
        def change_context(_owner):
            self.client._auth_session = self._new_context()
            return self.config
        self.store.load_config.side_effect = change_context
        self.service._on_push(self.frame())
        self.assertEqual(self.notices, [])
        self.assert_no_remote_effects()

    def test_session_device_change_during_config_read_invalidates_queued_notice(self):
        self.service._on_push(self.frame())
        notice = self.notices[0]
        def change_device(_owner):
            self.service.session.device_id = 'changed-session-device'
            return self.config
        self.store.load_config.side_effect = change_device
        self.assertFalse(self.service.is_current_incoming_call(notice))
        self.assert_no_remote_effects()


if __name__ == '__main__':
    unittest.main()
