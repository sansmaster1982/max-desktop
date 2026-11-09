"""Durable owner-scoped local removal; synthetic cache and mocked RPC only."""
import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import test_contact_profile_core as profile_fixture
import test_contact_create_core as create_fixture
from maxclient.core.models import Contact
from maxclient.core.store import Store
from maxclient.protocol.client import MaxFrame, MaxWriteOutcomeUnknown


class LocalContactVisibilityTests(unittest.TestCase):
    def setUp(self):
        self.case = profile_fixture.ContactProfileCoreTests()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.service, self.store, self.client = self.case.service, self.case.store, self.case.client
        self.case.record['flags'] = 0
        self.context = self.case.context

    def hide(self):
        self.service.delete_contact(202, context=self.context.session_context)

    def test_metadata_refresh_keeps_hidden_but_profile_and_chat_name_readable(self):
        self.case.fixture.dialog(303, 202)
        before = self.store.get_chat(303)
        self.hide()
        self.case.record['names'][0]['firstName'] = 'Updated'
        self.service._remember_contact_info({'contacts': [copy.deepcopy(self.case.record)]})
        state = self.service.refresh_contact_profile(self.context)
        self.assertEqual(self.service.list_contacts(), [])
        self.assertEqual(self.service.list_contacts('Updated'), [])
        self.assertEqual(self.service._contacts_map()[202].name, 'Updated Person')
        self.assertEqual(state['title'], 'Updated Person')
        self.assertTrue(state['is_local_hidden'])
        self.assertEqual(self.store.get_chat(303), before)
        self.client._write_frame.assert_not_called()

    def test_blocked_list_and_unsolicited_added_flag_do_not_restore_hidden_row(self):
        self.hide()
        blocked = copy.deepcopy(self.case.record)
        blocked.update(status='BLOCKED', flags=512)
        self.client.list_blocked_contacts = Mock(return_value={'contacts': [blocked]})
        self.service.list_blocked_contacts()
        self.assertEqual(self.service.list_contacts(), [])
        state = self.service.contact_snapshot(self.context)
        self.assertTrue(state['blocked'])
        self.assertTrue(state['is_added'])
        self.assertTrue(state['is_local_hidden'])
        self.client._write_frame.assert_not_called()

    def test_fresh_nonmember_has_distinct_local_action_without_rpc(self):
        self.assertFalse(self.service.contact_snapshot(self.context)['availability']['hide_local'])
        state = self.service.refresh_contact_profile(self.context)
        self.assertTrue(state['availability']['hide_local'])
        self.assertFalse(state['availability']['delete'])
        self.client.contact_info.reset_mock()
        state = self.service.hide_contact_profile(self.context)
        self.assertTrue(state['is_local_hidden'])
        self.assertFalse(state['availability']['hide_local'])
        self.assertFalse(state['availability']['delete'])
        self.assertEqual(self.service.list_contacts(), [])
        self.client.contact_info.assert_not_called()
        self.client._write_frame.assert_not_called()

    def test_stale_profile_action_and_member_profile_action_cannot_hide(self):
        with self.assertRaises(ValueError):
            self.service.hide_contact_profile(self.context)
        self.case.record['flags'] = 512
        self.service.refresh_contact_profile(self.context)
        with self.assertRaises(ValueError):
            self.service.hide_contact_profile(self.context)
        self.assertEqual(self.store.hidden_contact_ids(101), set())
        self.client._write_frame.assert_not_called()

    def test_unconfirmed_owner_cannot_create_visibility_preference(self):
        self.service._server_metadata_owner = None
        with self.assertRaises(ValueError):
            self.hide()
        with self.assertRaises(ValueError):
            self.service.hide_contact_profile(self.context)
        self.assertEqual(self.store.hidden_contact_ids(101), set())
        self.client.contact_info.assert_not_called()
        self.client._write_frame.assert_not_called()

    def test_pending_contact_write_keeps_local_profile_action_unavailable(self):
        self.service.refresh_contact_profile(self.context)
        self.service._pending_contact_write = {'owner': 101, 'id': 202, 'blocked': True}
        with self.assertRaises(ValueError):
            self.service.hide_contact_profile(self.context)
        self.assertEqual(self.store.hidden_contact_ids(101), set())
        self.client._write_frame.assert_not_called()

    def test_same_id_other_owner_remains_visible_and_old_context_cannot_hide_it(self):
        self.hide()
        self.case.fixture.switch(404)
        other = Contact(202, 'Other account person')
        self.store.upsert_contact(other)
        self.assertEqual(self.service.list_contacts(), [other])
        with self.assertRaises(ValueError):
            self.service.hide_contact_profile(self.context)
        with self.assertRaises(ValueError):
            self.service.delete_contact(202, context=self.context.session_context)
        self.assertEqual(self.store.hidden_contact_ids(404), set())
        self.case.fixture.switch(101)
        self.service._remember_contact_info({'contacts': [copy.deepcopy(self.case.record)]})
        self.assertEqual(self.service.list_contacts(), [])

    def test_owner_switch_at_local_write_boundary_cannot_hide_new_owner(self):
        original = self.service._history_cache_write

        def switch(binding, apply):
            self.case.fixture.switch(404)
            self.store.upsert_contact(Contact(202, 'Other account person'))
            return original(binding, apply)

        with patch.object(self.service, '_history_cache_write', side_effect=switch):
            with self.assertRaises(ValueError):
                self.hide()
        self.assertEqual(self.store.hidden_contact_ids(404), set())
        self.assertEqual([row.id for row in self.service.list_contacts()], [202])

    def test_phone_lookup_updates_profile_without_restoring_hidden_row(self):
        self.hide()
        result = self.service.add_contact_by_phone('+79990000000', context=self.context.session_context)
        self.assertEqual(result.id, 202)
        self.assertEqual(self.service.list_contacts(), [])
        self.assertEqual(self.store.hidden_contact_ids(101), {202})
        self.assertEqual(self.service._contacts_map()[202].name, 'Synthetic Person')
        self.client._write_frame.assert_not_called()

    def test_explicit_import_restores_hidden_without_server_add(self):
        self.hide()
        with patch('maxclient.core.service.parse_contacts_file',
                   return_value=[('Imported person', '+79990000000')]):
            count = self.service.import_contacts_from_file(
                'synthetic.csv', context=self.context.session_context)
        self.assertEqual(count, 1)
        self.assertEqual([row.id for row in self.service.list_contacts()], [202])
        self.assertEqual(self.store.hidden_contact_ids(101), set())
        self.client._write_frame.assert_not_called()

    def test_failed_or_stale_lookup_does_not_restore_hidden(self):
        self.hide()
        self.client.contact_by_phone.side_effect = ValueError('Synthetic lookup failure')
        with self.assertRaises(ValueError):
            self.service.add_contact_by_phone('+79990000000', context=self.context.session_context)
        self.assertEqual(self.service.list_contacts(), [])
        self.client.contact_by_phone.side_effect = None

        def switch(*_args, **_kwargs):
            self.case.fixture.switch(same_owner=True)
            return copy.deepcopy(self.case.record)

        self.client.contact_by_phone.side_effect = switch
        with self.assertRaises(ValueError):
            self.service.add_contact_by_phone('+79990000000', context=self.context.session_context)
        self.assertEqual(self.store.hidden_contact_ids(101), {202})

    def test_explicit_confirmed_add_restores_hidden_row(self):
        self.hide()
        state = self.service.add_contact_profile(self.context)
        self.assertTrue(state['is_added'])
        self.assertFalse(state['is_local_hidden'])
        self.assertEqual([row.id for row in self.service.list_contacts()], [202])
        self.client._write_frame.assert_called_once()

    def test_unknown_add_restores_only_after_matching_get_only_confirmation(self):
        self.hide()
        self.client._write_frame.side_effect = None
        self.client._write_frame.return_value = MaxFrame(1, 1, 34, b'', {})
        with self.assertRaises(MaxWriteOutcomeUnknown):
            self.service.add_contact_profile(self.context)
        self.assertEqual(self.service.list_contacts(), [])
        self.case.record['flags'] = 512
        state = self.service.refresh_contact_profile(self.context)
        self.assertFalse(state['pending'])
        self.assertFalse(state['is_local_hidden'])
        self.assertEqual([row.id for row in self.service.list_contacts()], [202])
        self.client._write_frame.assert_called_once()


class LocalContactCreationVisibilityTests(unittest.TestCase):
    def test_confirmed_explicit_create_restores_hidden_row(self):
        case = create_fixture.ContactCreateCoreTests()
        case.setUp()
        self.addCleanup(case.doCleanups)
        case.store.upsert_contact(Contact(202, 'Hidden person'))
        case.service.delete_contact(202, context=case.context)
        self.assertEqual(case.service.list_contacts(), [])
        case.create()
        self.assertEqual([row.id for row in case.service.list_contacts()], [202])
        self.assertEqual(case.store.hidden_contact_ids(101), set())


class LocalContactVisibilityPersistenceTests(unittest.TestCase):
    def test_hidden_choice_survives_reopen_cache_clear_and_owner_roundtrip(self):
        with tempfile.TemporaryDirectory(prefix='max-local-visibility-') as directory:
            path = Path(directory) / 'synthetic.sqlite'
            store = Store(path)
            try:
                store.bind_owner(101)
                store.upsert_contact(Contact(202, 'Synthetic person'))
                store.set_contact_hidden(101, 202, True)
            finally:
                store.close()
            store = Store(path)
            try:
                self.assertEqual(store.hidden_contact_ids(101), {202})
                store.clear_cache()
                store.bind_owner(404)
                self.assertEqual(store.hidden_contact_ids(404), set())
                store.set_contact_hidden(404, 202, False)
                with self.assertRaises(ValueError):
                    store.set_contact_hidden(101, 202, False)
                store.bind_owner(101)
                self.assertEqual(store.hidden_contact_ids(101), {202})
            finally:
                store.close()

    def test_invalid_identity_cannot_modify_visibility(self):
        store = Store(':memory:')
        self.addCleanup(store.close)
        store.bind_owner(101)
        for identity in (True, '202', 202.0, 2**63, -(2**63) - 1):
            with self.subTest(identity=identity), self.assertRaises(ValueError):
                store.set_contact_hidden(101, identity, True)
        for owner in (True, '101', 101.0, 404):
            with self.subTest(owner=owner), self.assertRaises(ValueError):
                store.set_contact_hidden(owner, 202, True)
        self.assertEqual(store.hidden_contact_ids(101), set())


if __name__ == '__main__':
    unittest.main()
