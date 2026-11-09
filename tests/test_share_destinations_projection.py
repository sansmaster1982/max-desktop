"""Service picker uses actual cached transport shapes without network writes."""
import copy
import unittest
from unittest.mock import Mock

import test_conversation_lifecycle as fixtures
from maxclient.core.models import Chat


class ShareDestinationProjectionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ConversationLifecycleTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.s = self.fixture.service
        self.ctx = self.fixture.context
        # LOGIN/push preserve nested MessagePack map keys. Message pushes need
        # not repeat the message type or unrelated creation timestamps.
        dialog = fixtures.chat(participants={101: 1, 202: 1},
            lastMessage={'id': 9, 'time': 90, 'text': 'Partial message'})
        dialog.pop('created')
        saved = {'id': 0, 'type': 'DIALOG', 'status': 'ACTIVE', 'owner': 101,
                 'participants': {101: 1}, 'lastMessage': {'id': 7, 'text': 'Saved'}}
        self.s._server_chats.update({303: dialog, 0: saved})
        self.s.store.upsert_chat(Chat(id=0, chat_type='DIALOG', peer_user_id=101))
        self.s._server_contacts[101] = {'id': 101, 'names': [{'name': 'Viewer'}]}

    def test_picker_keeps_real_dialog_and_saved_despite_unrelated_partial_fields(self):
        before = copy.deepcopy(self.s._server_chats)
        destinations = self.s.share_conversation_destinations(self.ctx)
        self.assertEqual({row.id for row in destinations}, {0, 303})
        self.assertEqual(self.s._server_chats, before)
        self.s.client.chat_info.assert_not_called()
        self.s.client.contact_info.assert_not_called()
        self.s.client.request.assert_not_called()

    def test_permission_fields_still_exclude_blocked_peer_and_input_restriction(self):
        self.s._server_contacts[202]['status'] = 'BLOCKED'
        self.assertEqual({row.id for row in self.s.share_conversation_destinations(self.ctx)}, {0})
        self.s._server_contacts[202].pop('status')
        self.s._server_chats[303]['restrictions'] = 64
        self.assertEqual({row.id for row in self.s.share_conversation_destinations(self.ctx)}, {0})

    def test_explicit_share_uses_fresh_integer_keyed48_before_exact_one64(self):
        s = self.s
        fresh = {303: fixtures.chat(participants={101: 1, 202: 1}),
                 0: fixtures.chat(id=0, participants={101: 1})}
        contacts = {202: self.fixture.contact,
                    101: {'id': 101, 'names': [{'name': 'Self', 'type': 'ONEME'}], 'updateTime': 1}}
        s.client.chat_info.side_effect = lambda ids, **_: {'chats': [copy.deepcopy(fresh[ids[0]])]}
        s.client.contact_info.side_effect = lambda ids, **_: {'contacts': [copy.deepcopy(contacts[ids[0]])]}
        target = s.conversation_context(0, activate_files=False)
        def accepted(chat_id, contact_id, cid, **kwargs):
            self.assertEqual((chat_id, contact_id), (0, 202))
            self.assertIs(kwargs['expected_session'], self.ctx.session_context)
            return {'chatId': 0, 'message': {'id': 31, 'time': 150, 'type': 'USER', 'cid': cid}}
        s.client.share_contact_message = Mock(side_effect=accepted)
        self.assertTrue(s.share_conversation_contact(self.ctx, target)['confirmed'])
        s.client.share_contact_message.assert_called_once()
        self.assertEqual([call.args[0] for call in s.client.chat_info.call_args_list], [[303], [0]])
        self.assertIsNotNone(s.store.message(0, 31))
        s.client.request.assert_not_called()

    def test_collision_in_fresh48_prevents_any_explicit_write(self):
        self.fixture.remote['participants'] = {101: 1, '101': 2, 202: 1}
        self.s.client.share_contact_message = Mock()
        with self.assertRaises(ValueError):
            self.s.share_conversation_contact(self.ctx, self.ctx)
        self.s.client.share_contact_message.assert_not_called()
        self.assertIsNone(self.s.store.conversation_action_journal(101, 303))


if __name__ == '__main__':
    unittest.main()
