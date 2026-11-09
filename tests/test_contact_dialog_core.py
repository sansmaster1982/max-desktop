"""Read-only direct navigation and subsequent explicit text, synthetic only."""
import copy
import unittest
from unittest.mock import Mock

import test_contact_lookup_core as fixtures
from maxclient.core.models import Chat
from maxclient.protocol.client import MaxFrame, MaxWriteOutcomeUnknown
from maxclient.protocol.direct_dialog_contracts import direct_dialog_id, direct_dialog_reply


def record(identity=202):
    return {'id': identity, 'updateTime': 20, 'flags': 0,
            'names': [{'type': 'ONEME', 'firstName': 'Synthetic', 'lastName': 'Peer'}]}


def dialog(route=175, owner=101, peer=202, **extra):
    return {'id': route, 'owner': owner, 'type': 'DIALOG', 'status': 'ACTIVE',
            'participants': {str(owner): 0, str(peer): 0}, 'participantsCount': 2,
            'lastEventTime': 0, 'created': 1, **extra}


class ContactDialogCoreTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ContactLookupCoreTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.service, self.client, self.store = self.fixture.service, self.fixture.client, self.fixture.store
        self.service.on_chat_changed = None
        self.service._chat_fetch_ts = {}
        self.service._chat_fetch_anchor = {}
        self.service._remember_contact_info({'contacts': [record()]})
        self.client.contact_info = Mock(side_effect=lambda ids, **kw: {'contacts': [record(ids[0])]})
        self.client.chat_info = Mock(return_value={'chats': []})
        self.client._write_frame = Mock(side_effect=AssertionError('No automatic write'))
        self.context = self.service.contact_context(202)
        self.route = 101 ^ 202

    def test_empty_read_opens_local_draft_without_server_metadata_or_history_rpc(self):
        state = self.service.open_contact_dialog(self.context)
        self.assertEqual((state['chat'].id, state['chat'].peer_user_id, state['chat'].title),
                         (self.route, 202, 'Synthetic Peer'))
        self.assertTrue(state['local_draft'])
        self.assertEqual(self.service._server_chats, {})
        self.assertIsNone(self.service.dialog_for_contact(202))
        self.assertEqual(self.service.open_chat(self.route), [])
        self.client.chat_info.assert_called_once_with([self.route], expected_session=self.context.session_context)
        self.client._write_frame.assert_not_called()
        self.client.request.assert_not_called()

    def test_existing_server_dto_caches_real_mapping(self):
        self.client.chat_info.return_value = {'chats': [dialog()]}
        result = self.service.open_contact_dialog(self.context)
        self.assertFalse(result['local_draft'])
        self.assertEqual(self.service.dialog_for_contact(202).id, self.route)
        self.assertEqual(self.service._server_chats[self.route]['type'], 'DIALOG')
        self.client._write_frame.assert_not_called()

    def test_dialog_owner_zero_does_not_override_exact_participant_identity(self):
        self.client.chat_info.return_value = {'chats': [dialog(owner=0, participants={'101': 0, '202': 0})]}
        result = self.service.open_contact_dialog(self.context)
        self.assertFalse(result['local_draft'])
        self.assertEqual(result['chat'].peer_user_id, 202)

    def test_integer_keyed_fresh48_opens_exact_real_dialog_without_send(self):
        self.client.chat_info.return_value = {'chats': [dialog(owner=0, participants={101: 0, 202: 0})]}
        result = self.service.open_contact_dialog(self.context)
        self.assertFalse(result['local_draft'])
        self.assertEqual((result['chat'].id, result['chat'].peer_user_id), (self.route, 202))
        self.assertEqual(self.service._server_chats[self.route]['participants'], {'101': 0, '202': 0})
        self.client.chat_info.assert_called_once_with([self.route], expected_session=self.context.session_context)
        self.client._write_frame.assert_not_called()
        self.client.request.assert_not_called()

    def test_removed_contact_readback_retains_context_for_read_only_open(self):
        remote = dict(record(), status='REMOVED', flags=0)
        self.client.contact_info.side_effect = None
        self.client.contact_info.return_value = {'contacts': [remote]}
        self.client.chat_info.return_value = {'chats': [dialog(owner=0, participants={101: 0, 202: 0})]}
        result = self.service.open_contact_dialog(self.context)
        self.assertTrue(self.service.is_current_contact(self.context))
        self.assertFalse(result['local_draft'])
        self.assertEqual(result['chat'].peer_user_id, 202)
        self.client._write_frame.assert_not_called()
        self.client.request.assert_not_called()

    def test_open_failure_exposes_only_local_parse_stage_for_ui_diagnostic(self):
        self.client.chat_info.return_value = {'chats': [dialog(peer=404)]}
        with self.assertRaises(ValueError) as failure:
            self.service.open_contact_dialog(self.context)
        self.assertEqual(failure.exception.contact_dialog_stage, 'parse48')
        self.assertNotIn('contact_dialog_payload', vars(failure.exception))
        self.client._write_frame.assert_not_called()

    def test_open_failure_stage_distinguishes_read32_and_read48(self):
        self.client.contact_info.side_effect = ValueError('Synthetic')
        with self.assertRaises(ValueError) as failure:
            self.service.open_contact_dialog(self.context)
        self.assertEqual(failure.exception.contact_dialog_stage, 'read32')
        self.client.contact_info.side_effect = lambda ids, **kw: {'contacts': [record(ids[0])]}
        self.client.chat_info.side_effect = ValueError('Synthetic')
        with self.assertRaises(ValueError) as failure:
            self.service.open_contact_dialog(self.context)
        self.assertEqual(failure.exception.contact_dialog_stage, 'read48')
        self.client._write_frame.assert_not_called()

    def test_open_failure_stage_distinguishes_remember_and_stale_capture(self):
        self.store.upsert_chat = Mock(side_effect=ValueError('Synthetic'))
        with self.assertRaises(ValueError) as failure:
            self.service.open_contact_dialog(self.context)
        self.assertEqual(failure.exception.contact_dialog_stage, 'remember')
        self.fixture.switch(same_owner=True)
        with self.assertRaises(ValueError) as failure:
            self.service.open_contact_dialog(self.context)
        self.assertEqual(failure.exception.contact_dialog_stage, 'capture')
        self.client._write_frame.assert_not_called()

    def test_new_push_wins_over_empty_reply(self):
        def read(*args, **kwargs):
            self.service._remember_chat_info({'chats': [dialog()]}, my_id=101)
            return {'chats': []}
        self.client.chat_info.side_effect = read
        self.assertFalse(self.service.open_contact_dialog(self.context)['local_draft'])
        self.assertEqual(self.service._server_chats[self.route], dialog())

    def test_wrong_or_malformed_readback_is_not_an_empty_draft(self):
        for data in ({}, {'chats': None}, {'chats': [dialog(peer=404)]},
                     {'chats': [dialog(route=999)]}, {'chats': [dialog(type='CHAT')]},
                     {'chats': [dialog(participants={'101': True, '202': 1})]},
                     {'chats': [dialog(participantsCount=3)]}):
            with self.subTest(data=data), self.assertRaises(ValueError):
                direct_dialog_reply(data, 101, 202, self.route)

    def test_source_id_math_is_exact_int64_and_rejects_unconfirmed_operands(self):
        self.assertEqual(direct_dialog_id(101, 101), 0)
        self.assertEqual(direct_dialog_id(2**62 + 123, 2**61 + 202), (2**62 + 123) ^ (2**61 + 202))
        for owner, peer in ((True, 202), (101, 1.5), (0, 202), (101, -2), (2**63, 2)):
            with self.subTest(owner=owner, peer=peer), self.assertRaises(ValueError):
                direct_dialog_id(owner, peer)

    def test_reconnect_invalidates_empty_history_proof(self):
        self.service.open_contact_dialog(self.context)
        self.fixture.switch(same_owner=True)
        with self.assertRaises(ValueError):
            self.service.open_chat(self.route)
        self.client._write_frame.assert_not_called()

    def test_group_collision_during_read_leaves_group_intact(self):
        def read(*args, **kwargs):
            self.store.upsert_chat(Chat(self.route, 'Other group', is_group=True, chat_type='CHAT'))
            return {'chats': []}
        self.client.chat_info.side_effect = read
        with self.assertRaises(ValueError):
            self.service.open_contact_dialog(self.context)
        self.assertTrue(self.store.get_chat(self.route).is_group)
        self.client._write_frame.assert_not_called()

    def _send_reply(self, route=None):
        return {'chatId': self.route if route is None else route,
                'message': {'id': 999, 'time': 200, 'sender': 101, 'text': 'Confirmed'}}

    def test_explicit_send_after_open_uses_exact_peer_and_session_once(self):
        self.service.open_contact_dialog(self.context)
        self.client._write_frame.side_effect = None
        self.client._write_frame.return_value = MaxFrame(1, 1, 64, b'', self._send_reply())
        result = self.service.send_text(self.route, 'Test', context=self.context.session_context)
        self.assertEqual((result.status, result.id), ('sent', 999))
        self.client._write_frame.assert_called_once()
        args, kw = self.client._write_frame.call_args
        self.assertEqual((args[0], args[1]['userId']), (64, 202))
        self.assertNotIn('chatId', args[1])
        self.assertIs(kw['expected_session'], self.context.session_context)

    def test_explicit_send_stale_before_dispatch_does_not_create_pending_or_write(self):
        self.service.open_contact_dialog(self.context)
        self.fixture.switch(same_owner=True)
        with self.assertRaises(ValueError):
            self.service.send_text(self.route, 'Test', context=self.context.session_context)
        self.assertEqual(self.store.messages(self.route), [])
        self.client._write_frame.assert_not_called()

    def test_send_outcome_unknown_never_retries_target(self):
        self.service.open_contact_dialog(self.context)
        self.client._write_frame.side_effect = MaxWriteOutcomeUnknown('Synthetic timeout')
        result = self.service.send_text(self.route, 'Test', context=self.context.session_context)
        self.assertEqual(result.status, 'unknown')
        self.client._write_frame.assert_called_once()

    def test_foreign_ack_does_not_promote_or_relocate_draft(self):
        self.service.open_contact_dialog(self.context)
        self.client._write_frame.side_effect = None
        self.client._write_frame.return_value = MaxFrame(1, 1, 64, b'', self._send_reply(404))
        result = self.service.send_text(self.route, 'Test', context=self.context.session_context)
        self.assertEqual(result.status, 'unknown')
        self.assertIsNone(self.store.get_chat(404))
        self.assertEqual(self.service._server_chats, {})

    def test_account_switch_during_send_cannot_accept_ack_into_new_owner(self):
        self.service.open_contact_dialog(self.context)
        def send(*args, **kwargs):
            self.fixture.switch()
            return MaxFrame(1, 1, 64, b'', self._send_reply())
        self.client._write_frame.side_effect = send
        result = self.service.send_text(self.route, 'Test', context=self.context.session_context)
        self.assertEqual(result.status, 'unknown')
        self.assertEqual(self.store.list_chats(), [])
        self.assertEqual(self.store.messages(self.route), [])


if __name__ == '__main__':
    unittest.main()
