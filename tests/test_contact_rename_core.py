"""Local alias updates use real owner/contact/dialog identities; no RPC."""
import copy
import unittest
from unittest.mock import patch

import test_contact_lookup_core as fixture
from maxclient.core.models import Chat, Contact


class ContactRenameCoreTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture.ContactLookupCoreTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.service = self.fixture.service
        self.store = self.fixture.store
        self.client = self.fixture.client
        self.original = Contact(202, 'Original synthetic name', '+70000000001',
                                'https://example.test/avatar.png', True)
        self.store.upsert_contact(self.original)

    def tearDown(self):
        self.client.request.assert_not_called()
        self.client.contact_by_phone.assert_not_called()

    def test_alias_locks_confirmed_unequal_dialog_and_preserves_contact_fields(self):
        self.fixture.dialog(303, 202)
        self.store.upsert_chat(Chat(202, 'Unrelated group', is_group=True, chat_type='CHAT'))
        metadata = copy.deepcopy(self.service._server_chats)
        result = self.service.rename_contact(202, '  My local alias  ', context=self.fixture.context)
        self.assertEqual(result, Contact(202, 'My local alias', self.original.phone,
                                         self.original.avatar_url, True))
        self.assertEqual(self.store.list_contacts(), [result])
        chat = self.store.get_chat(303)
        self.assertEqual((chat.title, chat.title_locked), ('My local alias', True))
        unrelated = self.store.get_chat(202)
        self.assertEqual((unrelated.title, unrelated.title_locked), ('Unrelated group', False))
        self.assertEqual(self.service._server_chats, metadata)
        self.assertEqual(self.service.dialog_for_contact(202).title, 'My local alias')

    def test_local_alias_does_not_rewrite_canonical_server_contact(self):
        self.fixture.dialog()
        raw = self.fixture.contact()
        raw.update(flags=512, description='Synthetic server description')
        self.service._server_contacts[202] = copy.deepcopy(raw)
        self.service.rename_contact(202, 'Local only')
        self.assertEqual(self.service._server_contacts[202], raw)
        self.assertEqual(self.service.list_contacts()[0].name, 'Local only')
        self.assertEqual(self.service.list_chats()[0].title, 'Local only')

    def test_missing_dialog_does_not_create_contact_id_placeholder(self):
        result = self.service.rename_contact(202, 'Local only')
        self.assertEqual(result.name, 'Local only')
        self.assertEqual(self.store.list_chats(), [])

    def test_unconfirmed_equal_id_legacy_row_is_not_renamed(self):
        self.store.upsert_chat(Chat(202, 'Legacy unconfirmed', peer_user_id=202))
        self.service.rename_contact(202, 'Local alias')
        row = self.store.get_chat(202)
        self.assertEqual((row.title, row.title_locked), ('Legacy unconfirmed', False))

    def test_malformed_peer_metadata_does_not_lock_a_dialog(self):
        self.fixture.dialog()
        self.service._server_chats[303]['participants'] = {101: 1, 404: 1}
        before = self.store.get_chat(303)
        self.service.rename_contact(202, 'Local alias')
        self.assertEqual(self.store.get_chat(303), before)

    def test_self_alias_preserves_saved_zero_and_never_creates_owner_chat(self):
        self.fixture.dialog(0, 101)
        self.store.upsert_contact(Contact(101, 'My profile', '+70000000000',
                                          'https://example.test/self.png', True))
        before = self.store.get_chat(0)
        result = self.service.rename_contact(101, 'Local self label')
        self.assertEqual(result.name, 'Local self label')
        self.assertEqual(self.store.get_chat(0), before)
        self.assertIsNone(self.store.get_chat(101))
        self.assertEqual(self.service.dialog_for_contact(101).title, 'Избранное')

    def test_rename_reads_current_row_instead_of_stale_contact_cache(self):
        self.service._contacts_cache = {202: Contact(202, 'Stale', '+70000009999', None, False)}
        result = self.service.rename_contact(202, 'New alias')
        self.assertEqual((result.phone, result.avatar_url, result.is_verified),
                         (self.original.phone, self.original.avatar_url, True))
        self.assertIsNone(self.service._contacts_cache)

    def test_supplied_field_presence_is_preserved_on_contact_copy(self):
        fields = frozenset({'avatar_url', 'is_verified'})
        current = Contact(202, 'Old', self.original.phone, None, False, fields)
        with patch.object(self.store, 'list_contacts', return_value=[current]), \
                patch.object(self.store, 'upsert_contact') as save:
            result = self.service.rename_contact(202, 'New')
        self.assertIs(result.server_fields, fields)
        self.assertIsNone(result.avatar_url)
        self.assertFalse(result.is_verified)
        save.assert_called_once_with(result)
        self.assertEqual(current.name, 'Old')

    def test_missing_contact_is_not_created(self):
        with self.assertRaises(ValueError):
            self.service.rename_contact(999, 'Missing')
        self.assertEqual(self.store.list_contacts(), [self.original])
        self.assertEqual(self.store.list_chats(), [])

    def test_invalid_id_or_name_makes_no_writes(self):
        for identity in (True, '202', 202.0, 2 ** 63, -(2 ** 63) - 1):
            with self.subTest(identity=identity), self.assertRaises(ValueError):
                self.service.rename_contact(identity, 'Invalid')
        for name in (None, 1, True, [], {}, '', '   '):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.service.rename_contact(202, name)
        self.assertEqual(self.store.list_contacts(), [self.original])
        self.assertEqual(self.store.list_chats(), [])

    def test_full_int64_contact_and_chat_ids_are_preserved(self):
        peer, chat_id = 2 ** 62 + 7, -(2 ** 62) + 9
        self.store.upsert_contact(Contact(peer, 'Large ID'))
        self.fixture.dialog(chat_id, peer)
        result = self.service.rename_contact(peer, 'Large alias')
        self.assertEqual(result.id, peer)
        self.assertEqual(self.store.get_chat(chat_id).title, 'Large alias')
        self.assertIsNone(self.store.get_chat(peer))

    def test_expired_supplied_session_is_rejected_before_cache_read(self):
        self.fixture.switch(same_owner=True)
        with patch.object(self.store, 'list_contacts') as read, self.assertRaises(ValueError):
            self.service.rename_contact(202, 'Stale', context=self.fixture.context)
        read.assert_not_called()
        self.assertEqual(self.store.list_contacts(), [self.original])

    def test_session_changed_during_contact_read_prevents_writes(self):
        original_read = self.store.list_contacts
        def read():
            rows = original_read()
            self.fixture.switch(same_owner=True)
            return rows
        with patch.object(self.store, 'list_contacts', side_effect=read), \
                patch.object(self.store, 'upsert_contact') as save, \
                patch.object(self.store, 'rename_chat') as rename, self.assertRaises(ValueError):
            self.service.rename_contact(202, 'Stale')
        save.assert_not_called()
        rename.assert_not_called()

    def test_session_changed_during_dialog_resolution_prevents_writes(self):
        self.fixture.dialog()
        original_resolve = self.service.dialog_for_contact
        def resolve(identity):
            result = original_resolve(identity)
            self.fixture.switch(same_owner=True)
            return result
        with patch.object(self.service, 'dialog_for_contact', side_effect=resolve), \
                patch.object(self.store, 'upsert_contact') as save, \
                patch.object(self.store, 'rename_chat') as rename, self.assertRaises(ValueError):
            self.service.rename_contact(202, 'Stale')
        save.assert_not_called()
        rename.assert_not_called()

    def test_cache_owner_mismatch_is_rejected_before_writes(self):
        self.store.bind_owner(404)
        other = Contact(202, 'Other owner')
        self.store.upsert_contact(other)
        with self.assertRaises(ValueError):
            self.service.rename_contact(202, 'Stale')
        self.assertEqual(self.store.list_contacts(), [other])
        self.assertEqual(self.store.list_chats(), [])

    def test_account_change_during_read_never_relabels_new_owner_row(self):
        original_read = self.store.list_contacts
        other = Contact(202, 'New owner same ID')
        def read():
            rows = original_read()
            self.fixture.switch(404)
            self.store.upsert_contact(other)
            return rows
        with patch.object(self.store, 'list_contacts', side_effect=read), self.assertRaises(ValueError):
            self.service.rename_contact(202, 'Old account alias')
        self.assertEqual(self.store.list_contacts(), [other])
        self.assertEqual(self.store.list_chats(), [])

    def test_conversation_rename_uses_actual_peer_and_preserves_colliding_contact(self):
        self.fixture.dialog(303, 202)
        collision = Contact(303, 'Unrelated same-number contact', '+70000000003')
        self.store.upsert_contact(collision)
        self.service.rename_conversation(303, 'Dialog alias', context=self.fixture.context)
        by_id = {item.id: item for item in self.store.list_contacts()}
        self.assertEqual(by_id[303], collision)
        self.assertEqual(by_id[202], Contact(202, 'Dialog alias', self.original.phone,
                                           self.original.avatar_url, True))
        self.assertEqual(self.store.get_chat(303).title, 'Dialog alias')
        self.assertTrue(self.store.get_chat(303).title_locked)
        self.assertIsNone(self.store.get_chat(202))

    def test_conversation_does_not_redirect_selected_chat_to_another_dialog_for_peer(self):
        self.fixture.dialog(303, 202)
        self.fixture.dialog(404, 202)
        before = self.store.get_chat(303)
        self.service.rename_conversation(404, 'Only selected dialog')
        self.assertEqual(self.store.get_chat(303), before)
        self.assertEqual(self.store.get_chat(404).title, 'Only selected dialog')

    def test_group_or_channel_rename_never_renames_equal_id_contact(self):
        for kind in ('CHAT', 'CHANNEL'):
            with self.subTest(kind=kind):
                self.store.upsert_chat(Chat(202, 'Group original', is_group=kind == 'CHAT',
                                            is_channel=kind == 'CHANNEL', chat_type=kind))
                self.service.rename_conversation(202, 'Local group title')
                self.assertEqual(self.store.list_contacts(), [self.original])
                self.assertEqual(self.store.get_chat(202).title, 'Local group title')
                self.assertTrue(self.store.get_chat(202).title_locked)

    def test_dialog_without_contact_can_change_title_without_creating_contact(self):
        self.fixture.dialog(303, 404)
        self.service.rename_conversation(303, 'Actual dialog alias')
        self.assertEqual(self.store.list_contacts(), [self.original])
        self.assertEqual(self.store.get_chat(303).title, 'Actual dialog alias')

    def test_self_conversation_rejects_rename_without_contact_or_saved_writes(self):
        self.fixture.dialog(0, 101)
        self.store.upsert_contact(Contact(101, 'Own profile'))
        before = self.store.list_contacts(), self.store.get_chat(0)
        with self.assertRaisesRegex(ValueError, 'Избранное'):
            self.service.rename_conversation(0, 'No self alias')
        self.assertEqual((self.store.list_contacts(), self.store.get_chat(0)), before)
        self.assertIsNone(self.store.get_chat(101))

    def test_unknown_or_mismatched_dialog_metadata_never_locks_title(self):
        self.fixture.dialog(303, 202)
        original = copy.deepcopy(self.service._server_chats[303])
        malformed = [{}, None, {'id': 404, 'type': 'DIALOG'},
                     {**original, 'participants': {101: 1, 404: 1}},
                     {**original, 'participants': [202]},
                     {**original, 'type': 'CHAT'}]
        before = self.store.list_contacts(), self.store.get_chat(303)
        for raw in malformed:
            with self.subTest(raw=raw):
                self.service._server_chats[303] = raw
                with self.assertRaises(ValueError):
                    self.service.rename_conversation(303, 'Invalid mapping')
                self.assertEqual((self.store.list_contacts(), self.store.get_chat(303)), before)

    def test_unknown_cached_equal_id_chat_does_not_trigger_contact_fallback(self):
        self.store.upsert_chat(Chat(202, 'Unknown chat'))
        before = self.store.get_chat(202)
        with self.assertRaises(ValueError):
            self.service.rename_conversation(202, 'Do not infer')
        self.assertEqual(self.store.get_chat(202), before)
        self.assertEqual(self.store.list_contacts(), [self.original])

    def test_missing_or_invalid_conversation_is_not_inserted(self):
        for identity in (999, True, '202', 202.0, 2**63):
            with self.subTest(identity=identity), self.assertRaises(ValueError):
                self.service.rename_conversation(identity, 'No placeholder')
        self.assertEqual(self.store.list_chats(), [])
        self.assertEqual(self.store.list_contacts(), [self.original])

    def test_conversation_with_stale_expected_session_has_no_writes(self):
        self.fixture.dialog()
        before = self.store.get_chat(303)
        self.fixture.switch(same_owner=True)
        with self.assertRaises(ValueError):
            self.service.rename_conversation(303, 'Stale', context=self.fixture.context)
        self.assertEqual(self.store.get_chat(303), before)
        self.assertEqual(self.store.list_contacts(), [self.original])

    def test_conversation_session_change_during_peer_contact_read_prevents_writes(self):
        self.fixture.dialog()
        original_read = self.store.list_contacts
        def read():
            rows = original_read()
            self.fixture.switch(same_owner=True)
            return rows
        with patch.object(self.store, 'list_contacts', side_effect=read), \
                patch.object(self.store, 'upsert_contact') as save, \
                patch.object(self.store, 'rename_chat') as rename, self.assertRaises(ValueError):
            self.service.rename_conversation(303, 'Stale')
        save.assert_not_called()
        rename.assert_not_called()

    def test_conversation_owner_change_during_chat_read_never_updates_new_owner(self):
        self.fixture.dialog()
        original_read = self.store.get_chat
        other = Chat(303, 'New owner title', is_group=True, chat_type='CHAT')
        def read(identity):
            row = original_read(identity)
            self.fixture.switch(404)
            self.store.upsert_chat(other)
            return row
        with patch.object(self.store, 'get_chat', side_effect=read), self.assertRaises(ValueError):
            self.service.rename_conversation(303, 'Old owner alias')
        self.assertEqual(self.store.get_chat(303), other)
        self.assertEqual(self.store.list_contacts(), [])


if __name__ == '__main__':
    unittest.main()
