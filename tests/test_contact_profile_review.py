"""Independent contact action regression tests, no accounts/files/network."""
from __future__ import annotations

import copy
import unittest
from unittest.mock import Mock

import test_contact_lookup_core as base
from maxclient.core.contact_profile_service import ContactContext
from maxclient.core.models import Chat, Contact
from maxclient.protocol.client import MaxFrame, MaxWriteOutcomeUnknown


def dto(identity=202, **fields):
    return {'id': identity, 'updateTime': 200, 'flags': 512, 'phone': 70000000001,
            'names': [{'type': 'ONEME', 'firstName': 'Canonical', 'lastName': 'Name'},
                      {'type': 'CUSTOM', 'firstName': 'Saved', 'lastName': 'Alias'}], **fields}


class ContactProfileIndependentTests(unittest.TestCase):
    def setUp(self):
        self.fixture = base.ContactLookupCoreTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.service, self.store, self.client = self.fixture.service, self.fixture.store, self.fixture.client
        self.raw = dto()
        self.service._remember_contact_info({'contacts': [copy.deepcopy(self.raw)]})
        self.reads, self.writes = [], []
        self.after_read = lambda: None
        self.after_write = lambda: None
        self.write_error = None
        self.apply_write = True
        self.read_override = None
        self.client.contact_info = self.read_contact
        self.client._write_frame = self.write_contact
        self.client.block_contact = Mock(side_effect=AssertionError('Unexpected legacy block write'))
        self.client.unblock_contact = Mock(side_effect=AssertionError('Unexpected legacy unblock write'))
        self.ctx = self.service.contact_context(202)

    def tearDown(self):
        self.client.request.assert_not_called()
        self.client.contact_by_phone.assert_not_called()
        self.client.block_contact.assert_not_called()
        self.client.unblock_contact.assert_not_called()

    def read_contact(self, ids, *, expected_session=None):
        self.assertIs(expected_session, self.client.authenticated_session)
        self.reads.append((copy.deepcopy(ids), expected_session))
        result = copy.deepcopy(self.read_override if self.read_override is not None
                               else {'contacts': [self.raw]})
        self.after_read()
        return result

    def write_contact(self, opcode, payload, *, expected_session=None):
        self.assertIs(expected_session, self.client.authenticated_session)
        self.assertEqual(opcode, 34)
        self.writes.append((copy.deepcopy(payload), expected_session))
        if self.apply_write:
            self.raw['updateTime'] += 1
            if payload['action'] == 'ADD':
                self.raw['flags'] |= 512
                self.raw.pop('status', None)
            elif payload['action'] == 'UPDATE':
                self.raw['names'] = [self.raw['names'][0], {'type': 'CUSTOM',
                    'firstName': payload['firstName'], 'lastName': payload.get('lastName', '')}]
            elif payload['action'] == 'REMOVE':
                self.raw.update(status='REMOVED', flags=0)
            elif payload['action'] == 'BLOCK':
                self.raw['status'] = 'BLOCKED'
            elif payload['action'] == 'UNBLOCK':
                self.raw.pop('status', None)
        self.after_write()
        if self.write_error:
            raise self.write_error
        return MaxFrame(1, 1, 34, b'', {})

    def test_contact_without_dialog_remains_contact_and_never_targets_colliding_group(self):
        group = Chat(202, 'Equal-number unrelated group', is_group=True, chat_type='CHAT')
        self.store.upsert_chat(group)
        state = self.service.refresh_contact_profile(self.ctx)
        self.assertIsNone(state['chat_id'])
        self.assertTrue(state['availability']['edit'])
        self.service.rename_contact_profile(self.ctx, 'Independent', 'Test')
        self.assertEqual(self.writes[0][0]['contactId'], 202)
        self.assertEqual(self.store.get_chat(202), group)
        self.assertEqual(len(self.store.list_chats()), 1)

    def test_old_same_owner_context_cannot_read_or_write_after_reconnect(self):
        self.fixture.switch(same_owner=True)
        with self.assertRaises(ValueError):
            self.service.rename_contact_profile(self.ctx, 'Stale')
        self.assertEqual(self.reads, [])
        self.assertEqual(self.writes, [])

    def test_cross_owner_same_id_never_reads_or_renames_new_owner_contact(self):
        self.fixture.switch()
        other = Contact(202, 'Other owner record')
        self.store.upsert_contact(other)
        with self.assertRaises(ValueError):
            self.service.delete_contact_profile(self.ctx)
        self.assertEqual(self.reads, [])
        self.assertEqual(self.writes, [])
        self.assertEqual(self.store.list_contacts(), [other])

    def test_context_identity_types_and_foreign_client_are_not_coerced(self):
        for owner, client, identity in ((True, self.client, 202), (101, object(), 202),
                                         (101, self.client, True), (101, self.client, '202')):
            with self.subTest(owner=owner, identity=identity), self.assertRaises(ValueError):
                self.service.refresh_contact_profile(ContactContext(owner, client, self.ctx.session_context, identity))
        self.assertEqual(self.reads, [])

    def test_context_change_in_preflight_read_stops_before_write(self):
        self.after_read = lambda: self.fixture.switch(same_owner=True)
        with self.assertRaises(ValueError):
            self.service.set_contact_profile_blocked(self.ctx, True)
        self.assertEqual(self.writes, [])

    def test_account_switch_after_write_never_applies_ack_to_other_owner(self):
        other = Contact(202, 'Other owner protected')
        def switch():
            self.fixture.switch()
            self.store.upsert_contact(other)
        self.after_write = switch
        with self.assertRaises(MaxWriteOutcomeUnknown):
            self.service.rename_contact_profile(self.ctx, 'Written to original account')
        self.assertEqual(len(self.writes), 1)
        self.assertEqual(self.store.list_contacts(), [other])
        self.assertEqual(self.service._server_contacts, {})

    def test_unknown_written_result_blocks_repeat_and_refresh_recovers_without_write(self):
        self.write_error = MaxWriteOutcomeUnknown('Synthetic reply lost')
        with self.assertRaises(MaxWriteOutcomeUnknown):
            self.service.rename_contact_profile(self.ctx, 'Written', 'Once')
        before_reads = len(self.reads)
        with self.assertRaises(MaxWriteOutcomeUnknown):
            self.service.rename_contact_profile(self.ctx, 'Do not repeat')
        self.assertEqual(len(self.reads), before_reads)
        self.assertEqual(len(self.writes), 1)
        self.write_error = None
        state = self.service.refresh_contact_profile(self.ctx)
        self.assertFalse(state['pending'])
        self.assertEqual(state['title'], 'Written Once')
        self.assertEqual(len(self.writes), 1)

    def test_unknown_result_cannot_be_bypassed_using_legacy_block_manager(self):
        self.write_error = MaxWriteOutcomeUnknown('Synthetic reply lost')
        with self.assertRaises(MaxWriteOutcomeUnknown):
            self.service.rename_contact_profile(self.ctx, 'Written')
        before_reads = len(self.reads)
        with self.assertRaises(MaxWriteOutcomeUnknown):
            self.service.block_contact(202)
        self.assertEqual(len(self.reads), before_reads)
        self.assertEqual(len(self.writes), 1)

    def test_shared_lock_rejects_legacy_write_during_contact_mutation(self):
        def concurrent():
            with self.assertRaises(ValueError):
                self.service.block_contact(202)
        self.after_write = concurrent
        self.service.rename_contact_profile(self.ctx, 'One transaction')
        self.assertEqual(len(self.writes), 1)

    def test_full_selected_contact_required_before_write_not_other_contact_or_partial(self):
        for returned in ({'contacts': [dto(303)]}, {'contacts': [{'id': 202}]},
                         {'contacts': [dto(), dto()]}, {'contacts': []}):
            with self.subTest(returned=returned), self.assertRaises(ValueError):
                self.read_override = returned
                self.service.delete_contact_profile(self.ctx)
        self.assertEqual(self.writes, [])

    def test_remove_without_explicit_cleared_flag_does_not_delete_local_row(self):
        def incomplete():
            self.raw.pop('flags')
        self.after_write = incomplete
        with self.assertRaises(MaxWriteOutcomeUnknown):
            self.service.delete_contact_profile(self.ctx)
        self.assertEqual(len(self.store.list_contacts()), 1)
        self.assertTrue(self.service.contact_snapshot(self.ctx)['pending'])

    def test_confirmed_remove_preserves_existing_chat_and_unrelated_contact(self):
        self.fixture.dialog(303, 202)
        other = Contact(404, 'Unrelated contact')
        self.store.upsert_contact(other)
        original_chat = self.store.get_chat(303)
        result = self.service.delete_contact_profile(self.ctx)
        self.assertTrue(result['confirmed'])
        self.assertFalse(result['is_added'])
        self.assertEqual(self.store.list_contacts(), [other])
        self.assertEqual(self.store.get_chat(303), original_chat)

    def test_confirmed_server_rename_preserves_oneme_slot_and_follows_later_custom_sync(self):
        self.fixture.dialog(303, 202)
        canonical = copy.deepcopy(self.raw['names'][0])
        self.service.rename_contact_profile(self.ctx, 'First server alias', 'One')
        self.assertEqual(self.service._server_contacts[202]['names'][0], canonical)
        self.assertEqual(self.service.list_chats()[0].title, 'First server alias One')
        later = dto(updateTime=999, names=[canonical,
                    {'type': 'CUSTOM', 'firstName': 'Updated on another device', 'lastName': 'Two'}])
        self.service._remember_contact_info({'contacts': [later]})
        self.assertEqual(self.service.list_contacts()[0].name, 'Updated on another device Two')
        self.assertEqual(self.service.list_chats()[0].title, 'Updated on another device Two')

    def test_remove_after_server_rename_returns_dialog_to_canonical_profile_name(self):
        self.fixture.dialog(303, 202)
        self.service.rename_contact_profile(self.ctx, 'Saved contact alias')
        self.service.delete_contact_profile(self.ctx)
        self.assertIsNotNone(self.store.get_chat(303))
        self.assertEqual(self.service.list_chats()[0].title, 'Canonical Name')
        self.assertEqual(self.service._server_contacts[202]['names'][0]['firstName'], 'Canonical')

    def test_existing_dialog_info_and_contact_card_share_saved_alias(self):
        self.fixture.dialog(303, 202)
        self.service.rename_contact_profile(self.ctx, 'Server display', 'Name')
        dialog_context = self.service.conversation_context(303)
        info = self.service.conversation_snapshot(dialog_context)
        contact = self.service.contact_snapshot(self.ctx)
        self.assertEqual(info['title'], 'Server display Name')
        self.assertEqual(info['title'], contact['title'])

    def test_late_older_contact_dto_cannot_overwrite_newer_push(self):
        def newer():
            raw = dto(updateTime=999, names=[{'type': 'CUSTOM', 'firstName': 'Latest push'}])
            self.service._remember_contact_info({'contacts': [raw]})
        self.after_read = newer
        try:
            self.service.refresh_contact_profile(self.ctx)
        except ValueError:
            pass
        self.assertEqual(self.service._server_contacts[202]['updateTime'], 999)
        self.assertEqual(self.store.list_contacts()[0].name, 'Latest push')

    def test_stale_readback_does_not_confirm_unknown_after_newer_conflicting_push(self):
        self.write_error = MaxWriteOutcomeUnknown('Synthetic reply lost')
        with self.assertRaises(MaxWriteOutcomeUnknown):
            self.service.set_contact_profile_blocked(self.ctx, True)
        newer = dto(updateTime=999)
        self.service._remember_contact_info({'contacts': [newer]})
        try:
            self.service.refresh_contact_profile(self.ctx)
        except ValueError:
            pass
        self.assertTrue(self.service.contact_snapshot(self.ctx)['pending'])
        self.assertNotEqual(self.service._server_contacts[202].get('status'), 'BLOCKED')
        self.assertEqual(len(self.writes), 1)

    def test_self_contact_never_exposes_or_dispatches_delete_and_block(self):
        self.raw = dto(101)
        self.service._remember_contact_info({'contacts': [self.raw]})
        own = self.service.contact_context(101)
        state = self.service.refresh_contact_profile(own)
        self.assertFalse(state['availability']['delete'])
        self.assertFalse(state['availability']['block'])
        with self.assertRaises(ValueError):
            self.service.delete_contact_profile(own)
        self.assertEqual(self.writes, [])

    def test_directory_refresh_keeps_same_contact_phone_without_auto_membership_write(self):
        self.raw['flags'] = 0
        self.raw.pop('phone')
        for _ in range(2):
            state = self.service.refresh_contact_profile(self.ctx)
            self.assertEqual((state['phone'], state['phone_source']), ('+70000000001', 'local'))
            self.assertTrue(state['availability']['add'])
            self.assertFalse(state['availability']['edit'])
            self.assertFalse(state['availability']['delete'])
            self.assertIsNone(state['chat_id'])
        self.assertEqual(self.writes, [])

    def test_directory_does_not_borrow_phone_from_another_known_contact(self):
        self.store.delete_contact(202)
        self.store.upsert_contact(Contact(404, 'Unrelated', phone='+70000000004'))
        self.raw.pop('phone')
        state = self.service.refresh_contact_profile(self.ctx)
        self.assertEqual((state['phone'], state['phone_source']), ('', 'missing'))
        self.assertEqual(self.writes, [])

    def test_add_lost_ack_keeps_pending_until_get_and_never_replays_write(self):
        self.raw['flags'] = 0
        self.write_error = MaxWriteOutcomeUnknown('Synthetic ADD acknowledgement lost')
        with self.assertRaises(MaxWriteOutcomeUnknown):
            self.service.add_contact_profile(self.ctx)
        self.assertEqual(self.writes[0][0], {'contactId': 202, 'action': 'ADD'})
        count_reads = len(self.reads)
        with self.assertRaises(MaxWriteOutcomeUnknown):
            self.service.add_contact_profile(self.ctx)
        self.assertEqual(len(self.reads), count_reads)
        self.assertTrue(self.service.contact_snapshot(self.ctx)['pending'])
        state = self.service.refresh_contact_profile(self.ctx)
        self.assertTrue(state['is_added'])
        self.assertFalse(state['pending'])
        self.assertTrue(state['availability']['edit'])
        self.assertEqual(len(self.writes), 1)
        self.assertEqual(self.store.list_chats(), [])

    def test_add_ack_without_membership_keeps_existing_contact_read_only_and_blocks_other_writes(self):
        self.raw['flags'] = 0
        self.apply_write = False
        with self.assertRaises(MaxWriteOutcomeUnknown):
            self.service.add_contact_profile(self.ctx)
        state = self.service.refresh_contact_profile(self.ctx)
        self.assertTrue(state['pending'])
        self.assertFalse(state['is_added'])
        for capability in ('add', 'edit', 'delete', 'block', 'unblock'):
            self.assertFalse(state['availability'][capability])
        with self.assertRaises(MaxWriteOutcomeUnknown):
            self.service.set_contact_profile_blocked(self.ctx, True)
        self.assertEqual(len(self.writes), 1)

    def test_add_membership_bit_with_removed_state_is_not_confirmation(self):
        self.raw['flags'] = 0
        self.after_write = lambda: self.raw.update(status='REMOVED')
        with self.assertRaises(MaxWriteOutcomeUnknown):
            self.service.add_contact_profile(self.ctx)
        state = self.service.contact_snapshot(self.ctx)
        self.assertTrue(state['pending'])
        self.assertFalse(state['is_added'])
        self.assertEqual(len(self.writes), 1)

    def test_add_foreign_id_readback_keeps_selected_contact_pending(self):
        self.raw['flags'] = 0
        self.after_write = lambda: setattr(self, 'read_override', {'contacts': [dto(404)]})
        with self.assertRaises(MaxWriteOutcomeUnknown):
            self.service.add_contact_profile(self.ctx)
        self.assertTrue(self.service.contact_snapshot(self.ctx)['pending'])
        self.assertNotIn(404, self.service._server_contacts)
        self.assertFalse(self.service.contact_snapshot(self.ctx)['is_added'])
        self.assertEqual(len(self.writes), 1)

    def test_add_account_switch_after_dispatch_never_imports_phone_or_membership_to_new_owner(self):
        self.raw['flags'] = 0
        other = Contact(202, 'New owner contact', phone='+70000000004')
        def switch():
            self.fixture.switch()
            self.store.upsert_contact(other)
        self.after_write = switch
        with self.assertRaises(MaxWriteOutcomeUnknown):
            self.service.add_contact_profile(self.ctx)
        self.assertEqual(self.store.list_contacts(), [other])
        self.assertEqual(self.service._server_contacts, {})
        self.assertEqual(len(self.writes), 1)

    def test_add_old_same_owner_session_is_rejected_before_contact_read(self):
        self.raw['flags'] = 0
        self.fixture.switch(same_owner=True)
        with self.assertRaises(ValueError):
            self.service.add_contact_profile(self.ctx)
        self.assertEqual(self.reads, [])
        self.assertEqual(self.writes, [])

    def test_add_fresh_existing_membership_prevents_duplicate_write(self):
        stale = dto(flags=0)
        self.service._remember_contact_info({'contacts': [stale]})
        with self.assertRaises(ValueError):
            self.service.add_contact_profile(self.ctx)
        self.assertTrue(self.service.contact_snapshot(self.ctx)['is_added'])
        self.assertEqual(self.writes, [])


if __name__ == '__main__':
    unittest.main()
