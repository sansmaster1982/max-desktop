"""Local contact deletion stays bound to the confirmed owner; no RPC or files."""
import copy
import unittest
from unittest.mock import patch

import test_contact_lookup_core as fixture
from maxclient.core.models import Chat, Contact


class ContactDeleteCoreTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture.ContactLookupCoreTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.service = self.fixture.service
        self.store = self.fixture.store
        self.client = self.fixture.client
        self.original = Contact(202, 'Original synthetic contact', '+70000000001')
        self.store.upsert_contact(self.original)

    def tearDown(self):
        self.client.request.assert_not_called()
        self.client.contact_by_phone.assert_not_called()

    def test_deletion_hides_local_row_preserving_profile_and_invalidates_cache(self):
        other = Contact(303, 'Other synthetic contact')
        self.store.upsert_contact(other)
        chat = Chat(202, 'Unrelated equal-ID chat', is_group=True, chat_type='CHAT')
        self.store.upsert_chat(chat)
        self.service._server_contacts[202] = self.fixture.contact()
        metadata = copy.deepcopy(self.service._server_contacts)
        self.service._contacts_map()
        self.service.delete_contact(202, context=self.fixture.context)
        self.assertEqual(self.service.list_contacts(), [other])
        self.assertEqual({row.id for row in self.store.list_contacts()}, {202, 303})
        self.assertEqual(self.store.hidden_contact_ids(101), {202})
        self.assertEqual(self.store.get_chat(202), chat)
        self.assertEqual(self.service._server_contacts, metadata)
        self.assertIsNone(self.service._contacts_cache)

    def test_invalid_identity_cannot_delete_or_invalidate_current_cache(self):
        cache = self.service._contacts_map()
        for identity in (True, '202', 202.0, 2**63, -(2**63) - 1):
            with self.subTest(identity=identity), self.assertRaises(ValueError):
                self.service.delete_contact(identity, context=self.fixture.context)
        self.assertEqual(self.store.list_contacts(), [self.original])
        self.assertIs(self.service._contacts_cache, cache)

    def test_expired_same_owner_session_cannot_delete(self):
        self.fixture.switch(same_owner=True)
        with patch.object(self.store, 'delete_contact') as delete, self.assertRaises(ValueError):
            self.service.delete_contact(202, context=self.fixture.context)
        delete.assert_not_called()
        self.assertEqual(self.store.list_contacts(), [self.original])

    def test_expired_other_owner_session_cannot_delete_equal_id(self):
        self.fixture.switch()
        other = Contact(202, 'Other owner same ID')
        self.store.upsert_contact(other)
        with patch.object(self.store, 'delete_contact') as delete, self.assertRaises(ValueError):
            self.service.delete_contact(202, context=self.fixture.context)
        delete.assert_not_called()
        self.assertEqual(self.store.list_contacts(), [other])

    def test_owner_change_between_binding_and_write_cannot_delete_equal_id(self):
        original_apply = self.service._history_cache_write
        other = Contact(202, 'Owner changed before write')

        def switch_before_write(binding, apply):
            self.fixture.switch()
            self.store.upsert_contact(other)
            return original_apply(binding, apply)

        with patch.object(self.service, '_history_cache_write', side_effect=switch_before_write), \
                patch.object(self.store, 'delete_contact') as delete, self.assertRaises(ValueError):
            self.service.delete_contact(202, context=self.fixture.context)
        delete.assert_not_called()
        self.assertEqual(self.store.list_contacts(), [other])

    def test_transport_change_between_binding_and_write_cannot_delete(self):
        original_apply = self.service._history_cache_write

        def switch_before_write(binding, apply):
            self.fixture.switch(same_owner=True)
            return original_apply(binding, apply)

        with patch.object(self.service, '_history_cache_write', side_effect=switch_before_write), \
                patch.object(self.store, 'delete_contact') as delete, self.assertRaises(ValueError):
            self.service.delete_contact(202, context=self.fixture.context)
        delete.assert_not_called()
        self.assertEqual(self.store.list_contacts(), [self.original])

    def test_missing_contact_is_idempotent_without_other_deletions(self):
        self.service.delete_contact(999, context=self.fixture.context)
        self.assertEqual(self.store.list_contacts(), [self.original])


if __name__ == '__main__':
    unittest.main()
