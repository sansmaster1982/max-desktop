"""Leave wire contracts and lifecycle edge cases; synthetic account only."""
import copy
import unittest
from unittest.mock import Mock

import test_conversation_lifecycle as base
from maxclient.core.models import Chat
from maxclient.protocol.client import (
    MaxClient, MaxFrame, AuthenticatedSession, ConnectionState, MaxError,
    MaxTimeout, MaxWriteNotStarted, MaxWriteOutcomeUnknown,
)
from maxclient.protocol.conversation_lifecycle_contracts import build_chat_leave, parse_leave_ack


class ConversationLeaveWireTests(unittest.TestCase):
    def client(self):
        client = MaxClient(auto_reconnect=False)
        client._sock = object()
        client._state = ConnectionState.CONNECTED
        client._device_type = 'WEB'
        client._auth_session = AuthenticatedSession(101, client.device_id, 'WEB', client._sock)
        client.request = Mock(return_value=MaxFrame(1, 1, 58, b'', {}))
        return client

    def test_exact_payload_does_not_reuse_delete_scope(self):
        self.assertEqual(build_chat_leave(-303), {'chatId': -303})
        for invalid in (True, '303', 1.1, None, 2**63):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                build_chat_leave(invalid)

    def test_optional_service_message_is_validated_and_detached(self):
        self.assertEqual(parse_leave_ack({}), {})
        data = {'message': {'id': 9, 'time': 100, 'type': 'USER', 'text': 'synthetic'}}
        parsed = parse_leave_ack(data)
        data['message']['text'] = 'changed'
        self.assertEqual(parsed['message']['text'], 'synthetic')
        for invalid in (None, [], True, {'success': True}, {'chatId': 303},
                        {'message': None}, {'message': {}},
                        {'message': {'id': True, 'time': 1, 'type': 'USER'}}):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                parse_leave_ack(invalid)

    def test_single58_exact_expected_session(self):
        client = self.client()
        expected = client.authenticated_session
        self.assertEqual(client.leave_conversation(-303, expected_session=expected), {})
        client.request.assert_called_once_with(58, {'chatId': -303}, expected_session=expected)

    def test_stale_or_absent_session_never_sends(self):
        client = self.client()
        other = AuthenticatedSession(101, client.device_id, 'WEB', client._sock)
        for expected in (None, other):
            with self.assertRaises(MaxWriteNotStarted):
                client.leave_conversation(-303, expected_session=expected)
        client.request.assert_not_called()

    def test_invalid_ack_and_timeout_are_unknown_without_retry(self):
        for invalid in (None, {'message': None}, {'chat': {'id': -303}}):
            with self.subTest(invalid=invalid):
                client = self.client()
                client.request.return_value = MaxFrame(1, 1, 58, b'', invalid)
                with self.assertRaises(MaxWriteOutcomeUnknown):
                    client.leave_conversation(-303, expected_session=client.authenticated_session)
                client.request.assert_called_once()
        client = self.client()
        client.request.side_effect = MaxTimeout('synthetic')
        with self.assertRaises(MaxWriteOutcomeUnknown):
            client.leave_conversation(-303, expected_session=client.authenticated_session)
        client.request.assert_called_once()


class ConversationLeaveCoreTests(unittest.TestCase):
    def setUp(self):
        self.fixture = base.ConversationLifecycleTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.s = self.fixture.service
        self.fixture.remote = base.chat(type='CHANNEL', owner=202, joinTime=1,
            adminParticipants={}, title='Synthetic channel', participantsCount=2)
        self.s._server_chats[303] = copy.deepcopy(self.fixture.remote)
        self.s.store.upsert_chat(Chat(id=303, title='Synthetic channel', chat_type='CHANNEL', is_channel=True))
        self.context = self.s.conversation_context(303, activate_files=False)
        self.s.client.leave_conversation = Mock(side_effect=self.accept)

    def tearDown(self):
        self.s.client.request.assert_not_called()
        self.s.client.change_conversation_lifecycle.assert_not_called()

    def accept(self, *_args, **_kwargs):
        self.fixture.remote.update(status='LEFT', joinTime=0, modified=110)
        return {'message': {'id': 99, 'time': 110, 'type': 'CHANNEL'}}

    def test_channel_ack_with_service_message_updates_only_membership(self):
        result = self.s.leave_conversation(self.context)
        self.assertTrue(result['accepted'])
        self.assertTrue(result['confirmed'])
        self.assertEqual(self.s.list_chats('channels'), [])
        self.assertIsNotNone(self.s.store.get_chat(303))
        self.assertIsNotNone(self.s.store.message(303, 9))
        self.assertIsNone(self.s.store.message(303, 99))
        self.assertEqual(self.s.store.conversation_departure(303), {'cutoff': 110, 'chat_type': 'CHANNEL'})

    def test_ack_alone_leaves_durable_pending_and_no_local_membership_change(self):
        self.s.client.leave_conversation.side_effect = None
        self.s.client.leave_conversation.return_value = {}
        result = self.s.leave_conversation(self.context)
        self.assertTrue(result['accepted'])
        self.assertFalse(result['confirmed'])
        self.assertTrue(result['pending'])
        self.assertIsNone(self.s.store.conversation_departure(303))
        with self.assertRaises(ValueError):
            self.s.leave_conversation(self.context)
        self.s.client.leave_conversation.assert_called_once()

    def test_departed_or_hidden_status_cannot_send_again_with_stale_participant_fields(self):
        for status in ('LEFT', 'REMOVED', 'CLOSED', 'HIDDEN'):
            with self.subTest(status=status):
                self.fixture.remote['status'] = status
                with self.assertRaises(ValueError):
                    self.s.leave_conversation(self.context)
        self.s.client.leave_conversation.assert_not_called()

    def test_blocked_group_with_confirmed_membership_can_leave(self):
        self.fixture.remote['status'] = 'BLOCKED'
        self.assertTrue(self.s.leave_conversation(self.context)['confirmed'])

    def test_membership_change_after_reservation_revokes_write(self):
        reserve = self.s._reserve_lifecycle
        def changed(context, record):
            reserve(context, record)
            self.s._server_chats[303]['owner'] = 101
        self.s._reserve_lifecycle = changed
        with self.assertRaises(ValueError):
            self.s.leave_conversation(self.context)
        self.s.client.leave_conversation.assert_not_called()
        self.assertIsNone(self.s.store.conversation_action_journal(101, 303))

    def test_inflight_refresh_does_not_settle_or_allow_duplicate(self):
        def write(*args, **kwargs):
            self.assertTrue(self.s.refresh_conversation_lifecycle(self.context)['pending'])
            with self.assertRaises(ValueError):
                self.s.leave_conversation(self.context)
            return self.accept(*args, **kwargs)
        self.s.client.leave_conversation.side_effect = write
        self.assertTrue(self.s.leave_conversation(self.context)['confirmed'])
        self.s.client.leave_conversation.assert_called_once()

    def test_foreign_admin_membership_cannot_be_misread_as_leave(self):
        def write(*args, **kwargs):
            ack = self.accept(*args, **kwargs)
            self.fixture.remote['adminParticipants'] = {'101': {'id': 101, 'permissions': 0}}
            return ack
        self.s.client.leave_conversation.side_effect = write
        self.assertTrue(self.s.leave_conversation(self.context)['pending'])
        self.assertIsNone(self.s.store.conversation_departure(303))

    def test_malformed_ack_is_unknown_even_if_remote_happens_to_change(self):
        def write(*args, **kwargs):
            self.accept(*args, **kwargs)
            return None
        self.s.client.leave_conversation.side_effect = write
        with self.assertRaises(MaxWriteOutcomeUnknown):
            self.s.leave_conversation(self.context)
        self.assertTrue(self.s.conversation_lifecycle_status(self.context)['pending'])
        self.assertTrue(self.s.refresh_conversation_lifecycle(self.context)['confirmed'])
        self.s.client.leave_conversation.assert_called_once()

    def test_late_refusal_after_session_change_releases_inflight_only(self):
        def write(*_args, **_kwargs):
            self.fixture.remote.update(status='LEFT', joinTime=0, modified=110)
            self.s.client._auth_session = AuthenticatedSession(
                101, self.s.client.device_id, 'WEB', self.s.client._sock)
            raise MaxError('Synthetic late refusal')
        self.s.client.leave_conversation.side_effect = write
        with self.assertRaises(ValueError):
            self.s.leave_conversation(self.context)
        self.assertNotIn((101, 303), self.s._lifecycle_inflight)
        self.assertIsNotNone(self.s.store.conversation_action_journal(101, 303))
        fresh = self.s.conversation_context(303, activate_files=False)
        self.assertTrue(self.s.refresh_conversation_lifecycle(fresh)['confirmed'])
        self.s.client.leave_conversation.assert_called_once()

    def test_late_refusal_does_not_release_replacement_operation(self):
        replacement = {'action': 'leave', 'chat_type': 'CHANNEL', 'cutoff': 200,
                       'peer_id': None, 'operation_id': 'replacement'}
        def write(*_args, **_kwargs):
            self.s.store.finish_conversation_action(101, 303)
            self.s.store.reserve_conversation_action(101, 303, replacement)
            raise MaxError('Synthetic rejected')
        self.s.client.leave_conversation.side_effect = write
        with self.assertRaises(MaxError):
            self.s.leave_conversation(self.context)
        self.assertEqual(self.s.store.conversation_action_journal(101, 303), replacement)
        self.assertNotIn((101, 303), self.s._lifecycle_inflight)


if __name__ == '__main__':
    unittest.main()
