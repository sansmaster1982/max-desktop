"""Saved-dialog information: exact ID zero and owner-scoped profile only."""
import copy
import unittest
from unittest.mock import patch

from maxclient.core.models import Chat, Profile
from maxclient.core.conversation_info_service import _peer, _contact_name, ConversationUnavailable
import test_conversation_info_service as fixture


class SavedConversationInfoTests(unittest.TestCase):
    def setUp(self):
        fixture.ConversationInfoServiceTests.setUp(self)
        self.store.upsert_chat(Chat(id=0, title='Broken cached label', chat_type='DIALOG'))
        self.store.save_profile(Profile(id=101, name='Синтетический Профиль',
            phone='+70000000000', description='Описание собственного профиля'))
        self.service._server_chats[0] = {'id': 0, 'type': 'DIALOG'}
        self.client.chat_info.return_value = {'chats': [{'id': 0, 'type': 'DIALOG'}]}
        self.client.contact_info.return_value = {'contacts': [{'id': 101,
            'names': [{'firstName': 'Синтетический', 'lastName': 'Профиль', 'type': 'ONEME'}]}]}
        self.changes = self.store._db.total_changes
        self.metadata = copy.deepcopy(self.service._server_chats)

    def tearDown(self):
        fixture.ConversationInfoServiceTests.tearDown(self)

    def test_zero_dialog_uses_confirmed_owner_without_participant_map(self):
        context = self.service.conversation_context(0)
        snapshot = self.service.conversation_snapshot(context)
        self.assertEqual(context.peer_id, 101)
        self.assertEqual(snapshot['title'], 'Избранное')
        self.assertEqual(snapshot['profile_name'], 'Синтетический Профиль')
        self.assertEqual(snapshot['phone'], '+70000000000')
        self.assertEqual(snapshot['description'], 'Описание собственного профиля')

    def test_refresh_without_phone_retains_confirmed_own_profile_fields(self):
        snapshot = self.service.refresh_conversation(self.service.conversation_context(0))
        self.assertEqual(snapshot['phone'], '+70000000000')
        self.assertEqual(snapshot['title'], 'Избранное')
        self.client.contact_info.assert_called_once_with([101])

    def test_explicit_phone_null_does_not_get_replaced_by_profile_fallback(self):
        self.client.contact_info.return_value['contacts'][0]['phone'] = None
        snapshot = self.service.refresh_conversation(self.service.conversation_context(0))
        self.assertNotIn('phone', snapshot)

    def test_other_profile_cannot_supply_own_phone(self):
        with patch.object(self.store, 'load_profile', return_value=Profile(id=999, phone='private')):
            snapshot = self.service.conversation_snapshot(self.service.conversation_context(0))
        self.assertNotIn('phone', snapshot)
        self.assertNotIn('profile_name', snapshot)

    def test_nonzero_peer_never_borrows_current_account_profile_phone(self):
        self.client.chat_info.return_value = {'chats': [fixture.chat_dto()]}
        self.client.contact_info.return_value = {'contacts': [{'id': 202, 'names': [{'name': 'Peer'}]}]}
        snapshot = self.service.refresh_conversation(self.context)
        self.assertNotIn('phone', snapshot)
        self.assertNotIn('profile_name', snapshot)

    def test_zero_id_does_not_override_non_dialog_or_conflicting_peer(self):
        self.assertIsNone(_peer(Chat(0, chat_type='CHAT'), {'id': 0, 'type': 'CHAT'}, 101))
        for raw in ({'id': 0, 'type': 'DIALOG'},
                    {'id': 0, 'type': 'DIALOG', 'participants': {'202': 0}}):
            with self.subTest(raw=raw), self.assertRaises(ConversationUnavailable):
                _peer(Chat(0, chat_type='DIALOG', peer_user_id=202), raw, 101)

    def test_first_last_names_win_over_compact_name_without_repr(self):
        self.assertEqual(_contact_name({'names': [{'name': 'Synthetic', 'firstName': 'Synthetic',
            'lastName': 'Person', 'type': 'ONEME'}, {'name': 'Alias', 'type': 'CUSTOM'}]}), 'Synthetic Person')
        self.assertEqual(_contact_name({'names': [{'name': {'unexpected': 1}}]}), '')


if __name__ == '__main__':
    unittest.main()
