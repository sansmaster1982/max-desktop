"""Independent destructive-action readback tests; no real accounts or transport."""
import copy
import unittest
from unittest.mock import Mock

import test_conversation_lifecycle as base
from maxclient.core.models import Chat, Message
from maxclient.protocol.client import AuthenticatedSession, MaxWriteOutcomeUnknown
from maxclient.protocol.conversation_lifecycle_contracts import lifecycle_readback


class ConversationLifecycleIndependentTests(unittest.TestCase):
    def setUp(self):
        self.fixture = base.ConversationLifecycleTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.service, self.context = self.fixture.service, self.fixture.context

    def tearDown(self):
        self.service.client.request.assert_not_called()

    def unknown(self, action='clear'):
        self.service.client.change_conversation_lifecycle.side_effect = MaxWriteOutcomeUnknown('Synthetic unknown')
        method = self.service.clear_conversation_history if action == 'clear' else self.service.delete_conversation
        with self.assertRaises(MaxWriteOutcomeUnknown):
            method(self.context)

    def test_newer_incoming_does_not_prove_unknown_clear_removed_older_history(self):
        self.unknown()
        self.fixture.remote['lastMessage'] = {'id': 10, 'time': 150, 'type': 'USER', 'text': 'New incoming'}
        self.fixture.remote['lastEventTime'] = 150
        result = self.service.refresh_conversation_lifecycle(self.context)
        self.assertTrue(result['pending'])
        self.assertFalse(result['confirmed'])
        self.assertIsNotNone(self.service.store.message(303, 9))
        self.assertIsNone(self.service.store.history_boundary(303))
        self.service.client.change_conversation_lifecycle.assert_called_once()

    def test_older_empty_snapshot_cannot_confirm_unknown_clear(self):
        self.unknown()
        self.fixture.remote = base.chat(created=1, modified=1, lastEventTime=1)
        self.fixture.remote.pop('lastMessage')
        result = self.service.refresh_conversation_lifecycle(self.context)
        self.assertTrue(result['pending'])
        self.assertIsNotNone(self.service.store.message(303, 9))
        self.assertIsNone(self.service.store.history_boundary(303))

    def test_older_removed_snapshot_cannot_confirm_unknown_delete(self):
        self.unknown('delete')
        self.fixture.remote = base.chat(created=1, modified=1, lastEventTime=1, status='REMOVED')
        self.fixture.remote.pop('lastMessage')
        result = self.service.refresh_conversation_lifecycle(self.context)
        self.assertTrue(result['pending'])
        self.assertIsNotNone(self.service.store.message(303, 9))
        self.assertIsNone(self.service.store.history_boundary(303))

    def test_contradictory_singular_and_empty_list_is_not_absence(self):
        for record in (base.chat(), base.chat(id=999)):
            with self.subTest(identity=record['id']), self.assertRaises(ValueError):
                lifecycle_readback({'chats': [], 'chat': record}, 303)

    def test_same_session_foreign_peer_readback_does_not_clear_history(self):
        self.unknown()
        self.fixture.remote = base.chat(participants={'101': 1, '404': 1})
        self.fixture.remote.pop('lastMessage')
        with self.assertRaises(ValueError):
            self.service.refresh_conversation_lifecycle(self.context)
        self.assertIsNotNone(self.service.store.message(303, 9))
        self.assertTrue(self.service.conversation_lifecycle_status(self.context)['pending'])

    def test_journal_survives_owner_switch_and_is_not_visible_to_other_owner(self):
        self.unknown()
        store = self.service.store
        before = copy.deepcopy(store.conversation_action_journal(101, 303))
        store.bind_owner(404)
        self.assertIsNone(store.conversation_action_journal(404, 303))
        store.bind_owner(101)
        self.assertEqual(store.conversation_action_journal(101, 303), before)

    def test_late_write_ack_does_not_erase_new_owner_messages(self):
        s = self.service
        def switch(*_args, **_kwargs):
            s.store.bind_owner(404)
            s.session.my_user_id = 404
            s._server_metadata_owner = 404
            s._server_chats = {}
            s._server_contacts = {}
            s.client._auth_session = AuthenticatedSession(404, s.client.device_id, 'WEB', s.client._sock)
            s.store.upsert_chat(Chat(303, 'Other owner chat', chat_type='DIALOG', peer_user_id=505))
            s.store.insert_message(Message(chat_id=303, id=9, time_ms=90, text='Other owner protected'))
        s.client.change_conversation_lifecycle.side_effect = switch
        with self.assertRaises((ValueError, MaxWriteOutcomeUnknown)):
            s.clear_conversation_history(self.context)
        self.assertEqual(s.store.message(303, 9).text, 'Other owner protected')
        self.assertIsNone(s.store.history_boundary(303))
        s.store.bind_owner(101)
        self.assertIsNotNone(s.store.conversation_action_journal(101, 303))

    def test_share_unknown_blocks_other_lifecycle_commands_without_second_write(self):
        s = self.service
        s.client.share_contact_message = Mock(side_effect=MaxWriteOutcomeUnknown('Synthetic share unknown'))
        with self.assertRaises(MaxWriteOutcomeUnknown):
            s.share_conversation_contact(self.context, self.context)
        with self.assertRaises(ValueError):
            s.clear_conversation_history(self.context)
        s.client.share_contact_message.assert_called_once()
        s.client.change_conversation_lifecycle.assert_not_called()
        self.assertIsNotNone(s.store.message(303, 9))

    def test_old_readback_cannot_release_a_newer_reservation(self):
        self.unknown()
        s = self.service
        replacement = {'action': 'delete', 'cutoff': 300, 'for_all': False,
                       'peer_id': 202, 'created': 700, 'acknowledged': False}
        def overlapping_refresh(*_args, **_kwargs):
            # Another refresh settles the old operation while this GET is in
            # flight; an explicit new action obtains the same chat slot.
            s.store.finish_conversation_action(101, 303)
            s.store.reserve_conversation_action(101, 303, replacement)
            old = base.chat()
            old.pop('lastMessage')
            return {'chats': [old]}
        s.client.chat_info.side_effect = overlapping_refresh
        try:
            result = s.refresh_conversation_lifecycle(self.context)
        except ValueError:
            result = {'pending': True}
        self.assertEqual(s.store.conversation_action_journal(101, 303), replacement)
        self.assertTrue(result['pending'])
        self.assertIsNotNone(s.store.message(303, 9))

    def share_ack(self, *, chat_id=303, cid=None):
        def reply(_target, _contact, requested_cid, **_kwargs):
            return {'chatId': chat_id, 'message': {'id': 31, 'time': 150,
                'type': 'USER', 'cid': requested_cid if cid is None else cid,
                'attaches': [{'_type': 'CONTACT', 'contactId': 202}]}}
        return reply

    def test_share_wrong_target_ack_keeps_journal_and_caches_no_foreign_message(self):
        s = self.service
        s.client.share_contact_message = Mock(side_effect=self.share_ack(chat_id=404))
        with self.assertRaises(MaxWriteOutcomeUnknown):
            s.share_conversation_contact(self.context, self.context)
        self.assertIsNone(s.store.message(404, 31))
        self.assertIsNone(s.store.message(303, 31))
        self.assertTrue(s.conversation_lifecycle_status(self.context)['pending'])
        s.client.share_contact_message.assert_called_once()

    def test_share_wrong_cid_ack_keeps_journal_without_resend(self):
        s = self.service
        s.client.share_contact_message = Mock(side_effect=self.share_ack(cid=1))
        with self.assertRaises(MaxWriteOutcomeUnknown):
            s.share_conversation_contact(self.context, self.context)
        with self.assertRaises(ValueError):
            s.share_conversation_contact(self.context, self.context)
        self.assertIsNone(s.store.message(303, 31))
        s.client.share_contact_message.assert_called_once()

    def test_share_refresh_reclassifies_source_bot_before_any_write(self):
        s = self.service
        self.fixture.remote['hasBots'] = True
        s.client.share_contact_message = Mock()
        with self.assertRaises(ValueError):
            s.share_conversation_contact(self.context, self.context)
        s.client.share_contact_message.assert_not_called()
        self.assertFalse(s.conversation_lifecycle_status(self.context)['pending'])

    def test_share_reconnect_during_source_read_does_not_write(self):
        s = self.service
        def reconnect(*_args, **_kwargs):
            s.client._auth_session = AuthenticatedSession(101, s.client.device_id, 'WEB', s.client._sock)
            return {'chats': [base.chat()]}
        s.client.chat_info.side_effect = reconnect
        s.client.share_contact_message = Mock()
        with self.assertRaises(ValueError):
            s.share_conversation_contact(self.context, self.context)
        s.client.share_contact_message.assert_not_called()
        self.assertIsNone(s.store.conversation_action_journal(101, 303))

    def test_late_share_ack_cannot_enter_other_owner_cache_or_release_old_journal(self):
        s = self.service
        def switch(target, contact, cid, **_kwargs):
            s.store.bind_owner(404)
            s.session.my_user_id = 404
            s._server_metadata_owner = 404
            s._server_chats = {}
            s._server_contacts = {}
            s.client._auth_session = AuthenticatedSession(404, s.client.device_id, 'WEB', s.client._sock)
            s.store.upsert_chat(Chat(303, 'Other owner chat', chat_type='DIALOG', peer_user_id=505))
            return self.share_ack()(target, contact, cid)
        s.client.share_contact_message = Mock(side_effect=switch)
        with self.assertRaises(MaxWriteOutcomeUnknown):
            s.share_conversation_contact(self.context, self.context)
        self.assertIsNone(s.store.message(303, 31))
        s.store.bind_owner(101)
        self.assertIsNotNone(s.store.conversation_action_journal(101, 303))
        s.client.share_contact_message.assert_called_once()

    def test_share_cannot_send_to_input_restricted_destination(self):
        s = self.service
        self.fixture.remote['restrictions'] = 64
        s.client.share_contact_message = Mock(side_effect=self.share_ack())
        with self.assertRaises(ValueError):
            s.share_conversation_contact(self.context, self.context)
        s.client.share_contact_message.assert_not_called()
        self.assertIsNone(s.store.message(303, 31))

    def test_share_cannot_send_to_blocked_contact_destination(self):
        s = self.service
        self.fixture.contact['status'] = 'BLOCKED'
        s.client.share_contact_message = Mock(side_effect=self.share_ack())
        with self.assertRaises(ValueError):
            s.share_conversation_contact(self.context, self.context)
        s.client.share_contact_message.assert_not_called()

    def test_share_cannot_post_to_channel_without_post_permission(self):
        s = self.service
        s.store.upsert_chat(Chat(404, 'Readonly synthetic channel', chat_type='CHANNEL'))
        target_raw = base.chat(id=404, type='CHANNEL', owner=202, joinTime=1,
                               participants={'101': 1, '202': 1}, adminParticipants={})
        s._server_chats[404] = target_raw
        target = s.conversation_context(404, activate_files=False)
        s.client.chat_info.side_effect = lambda ids, **_: {'chats': [copy.deepcopy(
            target_raw if ids == [404] else self.fixture.remote)]}
        s.client.share_contact_message = Mock(side_effect=self.share_ack(chat_id=404))
        with self.assertRaises(ValueError):
            s.share_conversation_contact(self.context, target)
        s.client.share_contact_message.assert_not_called()


if __name__ == '__main__':
    unittest.main()
