"""Offline VOID52 recovery and category-only diagnostics; no account files."""
import copy
import json
import unittest
from types import MethodType
from unittest.mock import Mock

import test_conversation_lifecycle as base
from maxclient.core.models import Chat, Message
from maxclient.protocol.client import AuthenticatedSession, MaxClient, MaxFrame, MaxWriteOutcomeUnknown
from maxclient.protocol.codec import unpack_payload


class ConversationLifecycleDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.fixture = base.ConversationLifecycleTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.service, self.context = self.fixture.service, self.fixture.context
        self.service.client.change_conversation_lifecycle = MethodType(
            MaxClient.change_conversation_lifecycle, self.service.client)

    def submit(self, *, body=b'', during_write=None):
        def reply(opcode, payload, **kwargs):
            self.assertEqual(opcode, 52)
            self.assertIs(kwargs['expected_session'], self.context.session_context)
            if during_write:
                during_write()
            return MaxFrame(1, 60, opcode, body, unpack_payload(body))
        self.service.client.request = Mock(side_effect=reply)
        return self.service.delete_conversation(self.context)

    def pending(self, *, acknowledged=False):
        record = {'action': 'delete', 'cutoff': 100, 'for_all': False,
                  'peer_id': 202, 'created': 123456789, 'operation_id': 'a' * 32}
        if acknowledged:
            record['acknowledged'] = True
        self.service.store.reserve_conversation_action(101, 303, record)
        return record

    def diagnostic(self):
        value = self.service.log.call_args.args[0]
        prefix = 'conversation_lifecycle_readback '
        self.assertTrue(value.startswith(prefix))
        return json.loads(value[len(prefix):])

    def test_matching_empty_void52_confirms_even_when_entity_remains_active(self):
        result = self.submit()
        self.assertTrue(result['accepted'])
        self.assertTrue(result['confirmed'])
        self.assertFalse(result['pending'])
        self.assertEqual(result['confirmation_reason'], 'ack_confirmed')
        self.assertEqual(self.fixture.remote['status'], 'ACTIVE')
        self.assertIsNone(self.service.store.message(303, 9))
        self.assertTrue(self.service.store.history_boundary(303)['deleted'])
        self.assertIsNone(self.service.store.conversation_action_journal(101, 303))
        self.service.client.request.assert_called_once()
        # Only the existing preflight48: post-ACK48 is not a success gate.
        self.service.client.chat_info.assert_called_once()

    def test_historical_ack_recovery_uses_current_session_without_read_or_resend(self):
        self.pending(acknowledged=True)
        client = self.service.client
        client._auth_session = AuthenticatedSession(101, client.device_id, 'WEB', client._sock)
        current = self.service.conversation_context(303, activate_files=False)
        self.service.client.chat_info = Mock(side_effect=AssertionError('Recovery must not need48'))
        result = self.service.refresh_conversation_lifecycle(current)
        self.assertTrue(result['confirmed'])
        self.service.client.request.assert_not_called()
        self.service.client.chat_info.assert_not_called()
        self.assertIsNone(self.service.store.conversation_action_journal(101, 303))

    def test_newer_push_and_unsent_message_survive_ack_cutoff(self):
        s = self.service
        s.store.insert_message(Message(chat_id=303, time_ms=50, text='draft',
                                       local_id='synthetic-unsent', status='pending'))
        def push():
            s._server_chats[303] = base.chat(lastEventTime=150, modified=150,
                lastMessage={'id': 10, 'time': 150, 'type': 'USER', 'text': 'newer'})
            s.store.insert_message(Message(chat_id=303, id=10, time_ms=150, text='newer'))
            s.store.update_chat_preview(303, 150, 'newer')
        result = self.submit(during_write=push)
        self.assertTrue(result['confirmed'])
        self.assertIsNone(s.store.message(303, 9))
        self.assertEqual([m.text for m in s.store.messages(303)], ['draft', 'newer'])
        self.assertEqual(s.store.get_chat(303).last_preview, 'newer')
        self.assertEqual(s._server_chats[303]['status'], 'ACTIVE')
        self.assertEqual(s._server_chats[303]['lastMessage']['id'], 10)
        self.assertEqual([c.id for c in s.list_chats()], [303])

    def test_session_replaced_before_ack_cannot_confirm_or_mark_journal(self):
        def replace():
            client = self.service.client
            client._auth_session = AuthenticatedSession(101, client.device_id, 'WEB', client._sock)
        with self.assertRaises(MaxWriteOutcomeUnknown):
            self.submit(during_write=replace)
        record = self.service.store.conversation_action_journal(101, 303)
        self.assertIsNot(record.get('acknowledged'), True)
        self.assertIsNone(self.service.store.history_boundary(303))
        self.assertIsNotNone(self.service.store.message(303, 9))

    def test_replacement_operation_cannot_receive_late_ack(self):
        replacement = None
        def replace():
            nonlocal replacement
            store = self.service.store
            replacement = {**store.conversation_action_journal(101, 303),
                           'operation_id': 'b' * 32, 'cutoff': 200}
            store.finish_conversation_action(101, 303)
            store.reserve_conversation_action(101, 303, replacement)
        with self.assertRaises(MaxWriteOutcomeUnknown):
            self.submit(during_write=replace)
        self.assertEqual(self.service.store.conversation_action_journal(101, 303), replacement)
        self.assertIsNone(self.service.store.history_boundary(303))

    def test_nonempty_payload_is_unknown_and_never_tombstones(self):
        for body in (b'\xc0', b'\x80', b'\xc1', b'\xc0\x01', b'\xc3'):
            with self.subTest(body=body):
                with self.assertRaises(MaxWriteOutcomeUnknown):
                    self.submit(body=body)
                record = self.service.store.conversation_action_journal(101, 303)
                self.assertIsNot(record.get('acknowledged'), True)
                self.assertIsNone(self.service.store.history_boundary(303))
                self.assertIsNotNone(self.service.store.message(303, 9))
                self.service.client.request.assert_called_once()
                self.service.store.finish_conversation_action(101, 303)

    def test_unknown_active_or_hidden_remains_pending_and_diagnostics_are_categories(self):
        self.pending()
        for status in ('ACTIVE', 'HIDDEN'):
            self.fixture.remote['status'] = status
            self.fixture.remote.pop('lastMessage', None)
            result = self.service.refresh_conversation_lifecycle(self.context)
            self.assertFalse(result['accepted'])
            self.assertTrue(result['pending'])
            self.assertEqual(result['confirmation_reason'], 'delete_status_unconfirmed')
            details = self.diagnostic()
            self.assertEqual(details['status'], status)
            self.assertEqual(details['freshness'], 'equal')
            self.assertEqual(details['peer_match'], 'match')
        self.service.client.request.assert_not_called()
        self.assertIsNotNone(self.service.store.message(303, 9))

    def test_unknown_fresh_removed_confirms_but_stale_readback_does_not(self):
        self.pending()
        self.fixture.remote = base.chat(status='REMOVED', created=1, modified=1, lastEventTime=1)
        self.fixture.remote.pop('lastMessage')
        result = self.service.refresh_conversation_lifecycle(self.context)
        self.assertEqual(result['confirmation_reason'], 'stale_readback')
        self.assertEqual(self.diagnostic()['freshness'], 'older')
        self.fixture.remote['modified'] = 100
        result = self.service.refresh_conversation_lifecycle(self.context)
        self.assertTrue(result['confirmed'])
        self.assertFalse(result['accepted'])
        self.service.client.request.assert_not_called()

    def test_invalid_readback_logs_presence_not_content(self):
        self.pending()
        secret = 'SYNTHETIC_PRIVATE_TOKEN https://private.invalid/message secret-text'
        self.fixture.remote.update(status=secret, title=secret, description=secret,
                                   id=876543210, lastEventTime=765432109)
        self.fixture.remote['lastMessage']['text'] = secret
        self.fixture.remote.pop('participants')
        with self.assertRaises(ValueError):
            self.service.refresh_conversation_lifecycle(self.context)
        details = self.diagnostic()
        self.assertEqual(details['reason'], 'readback_invalid')
        self.assertEqual(details['status'], 'unavailable')
        self.assertEqual(details['fields']['participants'], 'absent')
        self.assertFalse(details['id_match'])
        rendered = self.service.log.call_args.args[0]
        for prohibited in (secret, '876543210', '765432109', '303', '202', '101', '123456789'):
            self.assertNotIn(prohibited, rendered)
        self.assertIsNone(self.service.store.history_boundary(303))

    def test_transport_exception_text_never_reaches_diagnostic(self):
        self.pending()
        self.service.client.chat_info.side_effect = RuntimeError('SYNTHETIC_SECRET account=101')
        with self.assertRaises(RuntimeError):
            self.service.refresh_conversation_lifecycle(self.context)
        self.assertEqual(self.diagnostic()['reason'], 'readback_failed')
        self.assertNotIn('SYNTHETIC_SECRET', self.service.log.call_args.args[0])
        self.assertNotIn('101', self.service.log.call_args.args[0])

    def test_foreign_peer_cannot_settle_unknown(self):
        self.pending()
        self.fixture.remote['participants'] = {'101': 1, '404': 1}
        self.fixture.remote['status'] = 'REMOVED'
        with self.assertRaises(ValueError):
            self.service.refresh_conversation_lifecycle(self.context)
        self.assertEqual(self.diagnostic()['reason'], 'target_unconfirmed')
        self.assertIsNone(self.service.store.history_boundary(303))

    def test_malformed_ack_journal_is_not_a_new_delete_authority(self):
        original = self.pending(acknowledged=True)
        for field, value in (('operation_id', 'bad'), ('cutoff', -1), ('created', True),
                             ('for_all', 1), ('peer_id', True)):
            with self.subTest(field=field):
                store = self.service.store
                store.finish_conversation_action(101, 303)
                store.reserve_conversation_action(101, 303, {**original, field: value})
                try:
                    result = self.service.refresh_conversation_lifecycle(self.context)
                    self.assertEqual(result['confirmation_reason'], 'ack_record_invalid')
                    self.assertFalse(result['confirmed'])
                except ValueError:
                    self.assertEqual(field, 'peer_id')
                self.assertIsNone(store.history_boundary(303))
                self.assertIsNotNone(store.message(303, 9))
        self.service.client.request.assert_not_called()

    def test_other_owner_cannot_recover_old_ack(self):
        original = self.pending(acknowledged=True)
        s = self.service
        s.store.bind_owner(404)
        s.session.my_user_id = 404
        s._server_metadata_owner = 404
        s.client._auth_session = AuthenticatedSession(404, s.client.device_id, 'WEB', s.client._sock)
        s.store.upsert_chat(Chat(303, 'Other', chat_type='DIALOG', peer_user_id=505))
        s.store.insert_message(Message(chat_id=303, id=9, time_ms=90, text='other'))
        with self.assertRaises(ValueError):
            s.refresh_conversation_lifecycle(self.context)
        self.assertEqual(s.store.message(303, 9).text, 'other')
        self.assertIsNone(s.store.history_boundary(303))
        s.store.bind_owner(101)
        self.assertEqual(s.store.conversation_action_journal(101, 303), original)

    def test_final_cas_mismatch_preserves_journal_and_messages(self):
        original = self.pending(acknowledged=True)
        store = self.service.store
        replacement = {**original, 'operation_id': 'b' * 32}
        real_compare = store.compare_conversation_action
        def replace_then_compare(*args, **kwargs):
            store.finish_conversation_action(101, 303)
            store.reserve_conversation_action(101, 303, replacement)
            return real_compare(*args, **kwargs)
        store.compare_conversation_action = Mock(side_effect=replace_then_compare)
        result = self.service.refresh_conversation_lifecycle(self.context)
        self.assertFalse(result['confirmed'])
        self.assertEqual(result['confirmation_reason'], 'operation_changed')
        self.assertEqual(store.conversation_action_journal(101, 303), replacement)
        self.assertIsNone(store.history_boundary(303))
        self.assertIsNotNone(store.message(303, 9))

    def test_atomic_store_failure_rolls_back_boundary_and_journal(self):
        original = self.pending(acknowledged=True)
        store = self.service.store
        store._db.execute("CREATE TEMP TRIGGER deny_boundary BEFORE DELETE ON conversation_action_journal "
                          "BEGIN SELECT RAISE(ABORT, 'synthetic final failure'); END")
        with self.assertRaises(Exception):
            self.service.refresh_conversation_lifecycle(self.context)
        self.assertEqual(store.conversation_action_journal(101, 303), original)
        self.assertIsNone(store.history_boundary(303))
        self.assertIsNotNone(store.message(303, 9))

    def test_store_requires_exact_acknowledged_delete_cutoff(self):
        original = self.pending(acknowledged=True)
        store = self.service.store
        for cutoff in (99, 101, True, float('nan')):
            with self.subTest(cutoff=cutoff), self.assertRaises(ValueError):
                store.compare_conversation_action(101, 303, original, None, delete_cutoff=cutoff)
            self.assertIsNone(store.history_boundary(303))
            self.assertEqual(store.conversation_action_journal(101, 303), original)

    def test_logging_failure_does_not_change_confirmation(self):
        self.service.log.side_effect = RuntimeError('synthetic logger unavailable')
        self.assertTrue(self.submit()['confirmed'])


if __name__ == '__main__':
    unittest.main()
