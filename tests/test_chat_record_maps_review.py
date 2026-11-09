"""Independent decoded-map permissions/target regression tests; synthetic only."""
import copy
import unittest
from unittest.mock import Mock

import test_conversation_lifecycle as fixtures
from maxclient.core.models import Chat
from maxclient.protocol.conversation_actions_contracts import parse_action_chat_readback


class ChatRecordMapIndependentTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ConversationLifecycleTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.s = self.fixture.service
        self.source = self.fixture.context
        self.target_raw = fixtures.chat(id=404, type='CHANNEL', owner=202,
            joinTime=1, participants={101: 1, '202': 1},
            adminParticipants={101: {'id': 101, 'permissions': 256}})
        self.s.store.upsert_chat(Chat(404, 'Synthetic channel', chat_type='CHANNEL'))
        self.s._server_chats[404] = copy.deepcopy(self.target_raw)
        self.target = self.s.conversation_context(404, activate_files=False)
        self.s.client.chat_info.side_effect = lambda ids, **_: {'chats': [copy.deepcopy(
            self.target_raw if ids == [404] else self.fixture.remote)]}
        self.s.client.share_contact_message = Mock(side_effect=lambda target, _contact, cid, **_: {
            'chatId': target, 'message': {'id': 31, 'time': 150, 'type': 'USER', 'cid': cid}})

    def tearDown(self):
        self.s.client.request.assert_not_called()

    def assert_rejected_before_write(self):
        with self.assertRaises(ValueError):
            self.s.share_conversation_contact(self.source, self.target)
        self.s.client.share_contact_message.assert_not_called()
        self.assertIsNone(self.s.store.conversation_action_journal(101, 303))
        self.assertIsNone(self.s.store.message(404, 31))

    def test_mixed_key_channel_admin_grants_only_captured_owner(self):
        before = copy.deepcopy(self.target_raw)
        self.assertTrue(self.s.share_conversation_contact(self.source, self.target)['confirmed'])
        args, kwargs = self.s.client.share_contact_message.call_args
        self.assertEqual(args[:2], (404, 202))
        self.assertIs(kwargs['expected_session'], self.source.session_context)
        self.s.client.share_contact_message.assert_called_once()
        self.assertEqual(self.s._server_chats[404]['adminParticipants'],
                         {'101': {'id': 101, 'permissions': 256}})
        self.assertEqual(self.target_raw, before)

    def test_integer_channel_admin_for_foreign_identity_does_not_grant_owner(self):
        self.target_raw['adminParticipants'] = {202: {'id': 202, 'permissions': 256}}
        self.assert_rejected_before_write()

    def test_integer_admin_key_with_mismatched_nested_id_is_rejected(self):
        self.target_raw['adminParticipants'] = {101: {'id': 202, 'permissions': 256}}
        self.assert_rejected_before_write()

    def test_fresh_collision_after_writable_cached_projection_stops_write(self):
        self.assertIn(404, [c.id for c in self.s.share_conversation_destinations(self.source)])
        self.target_raw['adminParticipants']['101'] = {'id': 101, 'permissions': 0}
        self.assert_rejected_before_write()

    def test_fresh_other_chat_id_is_not_promoted_to_selected_destination(self):
        self.target_raw['id'] = 405
        self.assert_rejected_before_write()
        self.assertIsNone(self.s.store.get_chat(405))

    def test_fresh_wrong_peer_does_not_replace_captured_direct_destination(self):
        cached = fixtures.chat(id=404, participants={101: 1, 505: 1})
        self.s.store.upsert_chat(Chat(404, 'Synthetic recipient', chat_type='DIALOG', peer_user_id=505))
        self.s._server_chats[404] = cached
        self.s._server_contacts[505] = {'id': 505, 'names': [], 'updateTime': 1}
        self.target = self.s.conversation_context(404, activate_files=False)
        self.target_raw = fixtures.chat(id=404, participants={101: 1, 606: 1})
        self.assert_rejected_before_write()

    def test_normalizer_preserves_other_fields_and_does_not_mutate_transport_input(self):
        raw = fixtures.chat(participants={101: 1, 202: 2}, unrelated={7: {'keep': 'integer key'}})
        before = copy.deepcopy(raw)
        result = parse_action_chat_readback({'chats': [raw]}, 303)
        self.assertEqual(result['participants'], {'101': 1, '202': 2})
        self.assertEqual(result['unrelated'], {7: {'keep': 'integer key'}})
        result['unrelated'][7]['keep'] = 'changed local copy'
        self.assertEqual(raw, before)


if __name__ == '__main__':
    unittest.main()
