"""Synthetic standalone contact cards: exact identity and one-write recovery."""
import copy
import unittest
from unittest.mock import Mock, patch

from maxclient.core.models import Chat, Contact
from maxclient.protocol.client import MaxFrame, MaxError, MaxWriteNotStarted, MaxWriteOutcomeUnknown
import test_contact_lookup_core as fixtures


def contact(identity=202, first='Synthetic', last='Person', **extra):
    return {'id': identity, 'updateTime': 100, 'flags': 512, 'phone': 79990000000,
            'names': [{'type': 'CUSTOM', 'firstName': first, 'lastName': last}], **extra}


class ContactProfileCoreTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ContactLookupCoreTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.service, self.client, self.store = self.fixture.service, self.fixture.client, self.fixture.store
        self.record = contact()
        self.service._remember_contact_info({'contacts': [self.record]})
        self.client.contact_info = Mock(side_effect=self.read)
        self.client._write_frame = Mock(side_effect=self.write)
        self.context = self.service.contact_context(202)

    def read(self, identities, **kwargs):
        self.assertEqual(identities, [202])
        self.assertIs(kwargs['expected_session'], self.client._auth_session)
        return {'contacts': [copy.deepcopy(self.record)]}

    def write(self, opcode, payload, **kwargs):
        self.assertEqual(opcode, 34)
        self.assertEqual(payload['contactId'], 202)
        self.assertIs(kwargs['expected_session'], self.context.session_context)
        action = payload['action']
        if action == 'ADD':
            self.record['flags'] = self.record.get('flags', 0) | 512
            self.record.pop('status', None)
        elif action == 'UPDATE':
            self.record['names'] = [{'type': 'CUSTOM', 'firstName': payload['firstName'],
                                      **({'lastName': payload['lastName']} if 'lastName' in payload else {})}]
        elif action == 'REMOVE':
            self.record.update(status='REMOVED', flags=0)
        elif action == 'BLOCK':
            self.record['status'] = 'BLOCKED'
        elif action == 'UNBLOCK':
            self.record.pop('status', None)
        else:
            self.fail('Unexpected action')
        self.record['updateTime'] += 1
        return MaxFrame(1, 1, 34, b'', {})

    def test_cached_profile_without_chat_and_fresh_read_capabilities(self):
        state = self.service.contact_snapshot(self.context)
        self.assertEqual((state['title'], state['phone'], state['chat_id']),
                         ('Synthetic Person', '+79990000000', None))
        self.assertFalse(state['availability']['edit'])
        self.client.contact_info.assert_not_called()
        state = self.service.refresh_contact_profile(self.context)
        self.assertTrue(state['availability']['edit'])
        self.assertTrue(state['availability']['delete'])
        self.assertTrue(state['availability']['block'])
        self.assertTrue(state['availability']['chat'])
        self.assertEqual(self.store.list_chats(), [])

    def test_only_confirmed_existing_dialog_is_routed(self):
        self.store.upsert_chat(Chat(202, 'Equal ID group', chat_type='CHAT', is_group=True))
        self.assertIsNone(self.service.contact_snapshot(self.context)['chat_id'])
        self.fixture.dialog(303, 202)
        self.assertEqual(self.service.contact_snapshot(self.context)['chat_id'], 303)
        self.assertEqual(self.store.get_chat(202).title, 'Equal ID group')

    def test_fresh_optional_phone_omission_preserves_known_same_id_local_phone(self):
        self.record.pop('phone')
        state = self.service.refresh_contact_profile(self.context)
        self.assertEqual(state['phone'], '+79990000000')
        self.assertEqual(state['phone_source'], 'local')
        self.client._write_frame.assert_not_called()

    def test_optional_phone_missing_without_known_phone_is_not_invented(self):
        self.store.delete_contact(202)
        self.record.pop('phone')
        state = self.service.refresh_contact_profile(self.context)
        self.assertEqual(state['phone'], '')
        self.assertEqual(state['phone_source'], 'missing')

    def test_fresh_supplied_phone_is_authoritative_and_origin_is_server(self):
        self.record['phone'] = 79990000001
        state = self.service.refresh_contact_profile(self.context)
        self.assertEqual(state['phone'], '+79990000001')
        self.assertEqual(state['phone_source'], 'server')

    def test_explicit_add_one_actual_id_write_then_membership_confirmation(self):
        self.record['flags'] = 0
        state = self.service.refresh_contact_profile(self.context)
        self.assertTrue(state['availability']['add'])
        self.assertFalse(state['availability']['edit'])
        self.assertFalse(state['availability']['delete'])
        self.client._write_frame.assert_not_called()
        state = self.service.add_contact_profile(self.context)
        self.assertTrue(state['is_added'])
        self.assertFalse(state['availability']['add'])
        self.assertTrue(state['availability']['edit'])
        self.assertTrue(state['availability']['delete'])
        self.client._write_frame.assert_called_once_with(34, {'contactId': 202, 'action': 'ADD'},
                                                       expected_session=self.context.session_context)
        self.assertEqual(self.store.list_chats(), [])

    def test_add_unknown_blocks_repeat_and_get_only_resolves_membership(self):
        self.record['flags'] = 0
        self.client._write_frame.side_effect = None
        self.client._write_frame.return_value = MaxFrame(1, 1, 34, b'', {})
        with self.assertRaises(MaxWriteOutcomeUnknown):
            self.service.add_contact_profile(self.context)
        with self.assertRaises(MaxWriteOutcomeUnknown):
            self.service.add_contact_profile(self.context)
        self.client._write_frame.assert_called_once()
        state = self.service.refresh_contact_profile(self.context)
        self.assertTrue(state['pending'])
        self.record['flags'] = 512
        state = self.service.refresh_contact_profile(self.context)
        self.assertFalse(state['pending'])
        self.assertTrue(state['availability']['edit'])
        self.client._write_frame.assert_called_once()

    def test_add_session_change_during_preflight_never_dispatches(self):
        self.record['flags'] = 0
        def read(*args, **kwargs):
            self.fixture.switch(same_owner=True)
            return {'contacts': [copy.deepcopy(self.record)]}
        self.client.contact_info.side_effect = read
        with self.assertRaises(ValueError):
            self.service.add_contact_profile(self.context)
        self.client._write_frame.assert_not_called()

    def test_add_same_id_foreign_owner_phone_is_never_used(self):
        self.fixture.switch(404)
        self.service._remember_contact_info({'contacts': [contact(phone=79990000002, flags=0)]})
        with self.assertRaises(ValueError):
            self.service.add_contact_profile(self.context)
        self.client.contact_info.assert_not_called()
        self.client._write_frame.assert_not_called()

    def test_update_is_exact_34_then_confirmed_32_and_no_fake_dialog(self):
        state = self.service.rename_contact_profile(self.context, 'New', 'Name')
        self.assertIs(state['context'], self.context)
        self.assertEqual(state['title'], 'New Name')
        self.assertTrue(state['confirmed'])
        self.client._write_frame.assert_called_once_with(34, {
            'contactId': 202, 'action': 'UPDATE', 'firstName': 'New', 'lastName': 'Name'},
            expected_session=self.context.session_context)
        self.assertEqual(self.client.contact_info.call_count, 2)
        self.assertEqual(self.store.list_chats(), [])

    def test_empty_last_name_omitted_from_update_and_readback_confirms_removal(self):
        state = self.service.rename_contact_profile(self.context, 'New', '')
        self.assertEqual(state['last_name'], '')
        self.assertNotIn('lastName', self.client._write_frame.call_args.args[1])

    def test_update_only_renames_proven_existing_dialog(self):
        self.fixture.dialog(303, 202)
        self.store.upsert_chat(Chat(202, 'Unrelated equal-ID group', chat_type='CHAT', is_group=True))
        self.service.rename_contact_profile(self.context, 'New', 'Name')
        self.assertEqual(self.store.get_chat(303).title, 'New Name')
        self.assertFalse(self.store.get_chat(303).title_locked)
        self.assertEqual(self.store.get_chat(202).title, 'Unrelated equal-ID group')

    def test_confirmed_rename_replaces_old_local_override_and_allows_later_server_alias(self):
        self.fixture.dialog(303, 202)
        self.store.rename_chat(303, 'Old local alias')
        self.service.rename_contact_profile(self.context, 'New', 'Name')
        self.assertFalse(self.store.get_chat(303).title_locked)
        self.record['names'][0]['firstName'] = 'Later'
        self.record['updateTime'] += 1
        self.service._remember_contact_info({'contacts': [self.record]})
        self.assertEqual(self.service.list_chats()[0].title, 'Later Name')

    def test_remove_requires_actual_full_readback_and_keeps_dialog_history(self):
        self.fixture.dialog(303, 202)
        before = self.store.get_chat(303)
        state = self.service.delete_contact_profile(self.context)
        self.assertFalse(state['is_added'])
        self.assertFalse(state['availability']['delete'])
        self.assertEqual(self.store.list_contacts(), [])
        self.assertEqual(self.store.get_chat(303), before)
        self.assertTrue(self.service.is_current_contact(self.context))
        self.assertEqual(self.client._write_frame.call_args.args[1], {'contactId': 202, 'action': 'REMOVE'})

    def test_block_unblock_without_a_conversation_uses_typed_readback(self):
        state = self.service.set_contact_profile_blocked(self.context, True)
        self.assertTrue(state['blocked'])
        self.assertTrue(state['availability']['unblock'])
        state = self.service.set_contact_profile_blocked(self.context, False)
        self.assertFalse(state['blocked'])
        self.assertTrue(state['availability']['block'])
        self.assertEqual(self.store.list_chats(), [])

    def test_removed_contact_metadata_push_cannot_readd_address_book_row(self):
        self.service.delete_contact_profile(self.context)
        self.service._remember_contact_info({'contacts': [self.record]})
        self.assertEqual(len(self.store.list_contacts()), 1)  # metadata retained for dialog/profile
        self.assertEqual(self.service.list_contacts(), [])
        self.record.update(flags=512)
        self.record.pop('status')
        self.service._remember_contact_info({'contacts': [self.record]})
        self.assertEqual(len(self.service.list_contacts()), 1)

    def test_timeout_remains_pending_and_repeated_write_never_replays(self):
        self.client._write_frame.side_effect = MaxWriteOutcomeUnknown('Unknown')
        with self.assertRaises(MaxWriteOutcomeUnknown):
            self.service.set_contact_profile_blocked(self.context, True)
        self.assertTrue(self.service.contact_snapshot(self.context)['pending'])
        with self.assertRaises(MaxWriteOutcomeUnknown):
            self.service.set_contact_profile_blocked(self.context, True)
        self.client._write_frame.assert_called_once()
        state = self.service.refresh_contact_profile(self.context)
        self.assertTrue(state['pending'])
        self.record['status'] = 'BLOCKED'
        state = self.service.refresh_contact_profile(self.context)
        self.assertFalse(state['pending'])
        self.client._write_frame.assert_called_once()

    def test_update_unknown_readback_only_resolves_exact_custom_name(self):
        self.client._write_frame.return_value = MaxFrame(1, 1, 34, b'', {})
        self.client._write_frame.side_effect = None
        with self.assertRaises(MaxWriteOutcomeUnknown):
            self.service.rename_contact_profile(self.context, 'New', 'Name')
        self.record['names'] = [{'type': 'ONEME', 'firstName': 'New', 'lastName': 'Name'}]
        self.assertTrue(self.service.refresh_contact_profile(self.context)['pending'])
        self.record['names'][0]['type'] = 'CUSTOM'
        self.assertFalse(self.service.refresh_contact_profile(self.context)['pending'])
        self.client._write_frame.assert_called_once()

    def test_remove_empty_readback_never_implicitly_deletes_local_contact(self):
        self.client._write_frame.side_effect = None
        self.client._write_frame.return_value = MaxFrame(1, 1, 34, b'', {})
        self.client.contact_info.side_effect = [self.read([202], expected_session=self.context.session_context), {'contacts': []}]
        with self.assertRaises(MaxWriteOutcomeUnknown):
            self.service.delete_contact_profile(self.context)
        self.assertEqual(len(self.store.list_contacts()), 1)
        self.assertTrue(self.service.contact_snapshot(self.context)['pending'])

    def test_structured_rejection_and_not_started_unlock_explicit_retry(self):
        for failure in (MaxFrame(3, 1, 34, b'', {'error': 'denied'}), MaxWriteNotStarted('Not sent')):
            with self.subTest(failure=type(failure).__name__):
                self.client._write_frame.side_effect = failure if isinstance(failure, Exception) else None
                self.client._write_frame.return_value = failure
                with self.assertRaises((MaxError, MaxWriteNotStarted)):
                    self.service.delete_contact_profile(self.context)
                self.assertFalse(self.service.contact_snapshot(self.context)['pending'])

    def test_wrong_ack_identity_and_opcode_hold_pending(self):
        for frame in (MaxFrame(1, 1, 99, b'', {}), MaxFrame(1, 1, 34, b'', {'contact': contact(999)})):
            self.service._pending_contact_profile_write = None
            self.client._write_frame.side_effect = None
            self.client._write_frame.return_value = frame
            with self.assertRaises(MaxWriteOutcomeUnknown):
                self.service.delete_contact_profile(self.context)
            self.assertTrue(self.service.contact_snapshot(self.context)['pending'])

    def test_stale_session_fails_before_read_and_write(self):
        self.fixture.switch(same_owner=True)
        self.assertFalse(self.service.is_current_contact(self.context))
        with self.assertRaises(ValueError):
            self.service.delete_contact_profile(self.context)
        self.client.contact_info.assert_not_called()
        self.client._write_frame.assert_not_called()

    def test_session_switch_during_preflight_never_dispatches_write(self):
        def read(*args, **kwargs):
            self.fixture.switch(same_owner=True)
            return {'contacts': [contact()]}
        self.client.contact_info.side_effect = read
        with self.assertRaises(ValueError):
            self.service.delete_contact_profile(self.context)
        self.client._write_frame.assert_not_called()

    def test_session_switch_after_write_leaves_unknown_not_false_confirmation(self):
        def write(*args, **kwargs):
            self.fixture.switch(same_owner=True)
            return MaxFrame(1, 1, 34, b'', {})
        self.client._write_frame.side_effect = write
        with self.assertRaises(MaxWriteOutcomeUnknown):
            self.service.delete_contact_profile(self.context)
        self.assertIsNotNone(self.service._pending_contact_profile_write)

    def test_self_profile_cannot_edit_delete_or_block_contact(self):
        self.service._remember_contact_info({'contacts': [contact(101)]})
        own = self.service.contact_context(101)
        self.client.contact_info.side_effect = None
        self.client.contact_info.return_value = {'contacts': [contact(101)]}
        state = self.service.refresh_contact_profile(own)
        for action in ('edit', 'delete', 'block', 'unblock'):
            self.assertFalse(state['availability'][action])
        with self.assertRaises(ValueError):
            self.service.delete_contact_profile(own)
        self.client._write_frame.assert_not_called()

    def test_malformed_full_contact_never_enables_action_or_changes_cache(self):
        for change in ({'flags': True}, {'phone': 'raw'}, {'updateTime': None}, {'status': 'ACTIVE'}):
            self.record = contact(**change)
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.service.refresh_contact_profile(self.context)
        self.client._write_frame.assert_not_called()

    def test_older_readback_does_not_overwrite_newer_push(self):
        self.service._server_contacts[202]['updateTime'] = 200
        self.service._server_contacts[202]['names'][0]['firstName'] = 'Newer'
        state = self.service.refresh_contact_profile(self.context)
        self.assertTrue(state['fresh'])
        self.assertTrue(state['availability']['delete'])
        self.assertEqual(self.service._server_contacts[202]['names'][0]['firstName'], 'Newer')

    def test_foreign_cache_owner_never_reads_or_mutates_contact(self):
        self.service._server_metadata_owner = 404
        with self.assertRaises(ValueError):
            self.service.refresh_contact_profile(self.context)
        self.client.contact_info.assert_not_called()

    def test_pending_old_owner_cannot_be_overwritten_by_new_owner_action(self):
        self.client._write_frame.side_effect = MaxWriteOutcomeUnknown('Unknown')
        with self.assertRaises(MaxWriteOutcomeUnknown):
            self.service.delete_contact_profile(self.context)
        pending = self.service._pending_contact_profile_write
        self.fixture.switch(404)
        self.service._remember_contact_info({'contacts': [contact()]})
        context = self.service.contact_context(202)
        with self.assertRaises(MaxWriteOutcomeUnknown):
            self.service.delete_contact_profile(context)
        self.assertIs(self.service._pending_contact_profile_write, pending)
        self.client._write_frame.assert_called_once()

    def test_invalid_names_ids_and_bool_block_make_no_request(self):
        for identity in (True, '202', 2**63):
            with self.assertRaises(ValueError):
                self.service.contact_context(identity)
        for name in ('', 'x'*61, '\x00bad', None):
            with self.assertRaises(ValueError):
                self.service.rename_contact_profile(self.context, name)
        with self.assertRaises(ValueError):
            self.service.set_contact_profile_blocked(self.context, 1)
        self.client.contact_info.assert_not_called()
        self.client._write_frame.assert_not_called()


if __name__ == '__main__':
    unittest.main()
