"""One UPDATE34 ACK plus equivalent older32; never replay a contact write."""
import copy
import json
import unittest
from unittest.mock import Mock

import test_contact_profile_core as base
from maxclient.protocol.client import MaxFrame, MaxTimeout, MaxWriteOutcomeUnknown


class ContactRenameAckReadbackTests(unittest.TestCase):
    def setUp(self):
        self.fixture = base.ContactProfileCoreTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.s, self.client, self.store = self.fixture.service, self.fixture.client, self.fixture.store
        self.context = self.fixture.context
        self.s.log = Mock()
        self.ack = None
        self.push_ack = True
        self.include_ack = True
        self.client._write_frame.side_effect = self.write

    def write(self, opcode, payload, **kwargs):
        self.fixture.write(opcode, payload, **kwargs)
        self.ack = dict(copy.deepcopy(self.fixture.record), updateTime=300)
        if self.push_ack:
            self.s._remember_contact_info({'contacts': [self.ack]})
        return MaxFrame(1, 1, 34, b'', {'contact': copy.deepcopy(self.ack)} if self.include_ack else {})

    def rename(self):
        return self.s.rename_contact_profile(self.context, 'Requested', 'Alias')

    def test_typed_ack_and_equivalent_older32_confirm_once_without_cache_downgrade(self):
        state = self.rename()
        self.assertTrue(state['confirmed'])
        self.assertFalse(state['pending'])
        self.assertTrue(state['fresh'])
        self.assertEqual(state['title'], 'Requested Alias')
        self.assertEqual(self.s._server_contacts[202], self.ack)
        self.client._write_frame.assert_called_once()
        self.assertEqual(self.client.contact_info.call_count, 2)

    def test_ack_revision_is_preserved_even_without_matching_push(self):
        self.push_ack = False
        self.assertTrue(self.rename()['confirmed'])
        self.assertEqual(self.s._server_contacts[202]['updateTime'], 300)

    def test_optional_ack_contact_omission_cannot_relax_stale_mutation_readback(self):
        self.include_ack = False
        with self.assertRaises(MaxWriteOutcomeUnknown):
            self.rename()
        with self.assertRaises(ValueError):
            self.s.refresh_contact_profile(self.context)
        self.assertTrue(self.s.contact_snapshot(self.context)['pending'])
        self.client._write_frame.assert_called_once()

    def test_older_read_with_different_name_or_phone_cannot_settle_matching_ack(self):
        original = self.write
        for change in ({'names': base.contact(first='Old')['names']}, {'phone': 79990000001}):
            with self.subTest(fields=tuple(change)):
                self.s._pending_contact_profile_write = None
                self.fixture.record = base.contact()
                self.s._remember_contact_info({'contacts': [self.fixture.record]})
                def write(*args, **kwargs):
                    frame = original(*args, **kwargs)
                    self.fixture.record.update(copy.deepcopy(change))
                    return frame
                self.client._write_frame.side_effect = write
                with self.assertRaises(MaxWriteOutcomeUnknown):
                    self.rename()
                self.assertEqual(self.s._server_contacts[202], self.ack)
                self.assertTrue(self.s.contact_snapshot(self.context)['pending'])

    def test_newer_conflicting_push_after_ack_blocks_settlement(self):
        original = self.fixture.read
        def read(*args, **kwargs):
            result = original(*args, **kwargs)
            if self.ack is not None:
                self.s._remember_contact_info({'contacts': [base.contact(first='Newer', updateTime=400)]})
            return result
        self.client.contact_info.side_effect = read
        with self.assertRaises(MaxWriteOutcomeUnknown):
            self.rename()
        self.assertEqual(self.s._server_contacts[202]['updateTime'], 400)
        self.assertEqual(self.store.list_contacts()[0].name, 'Newer Person')

    def test_newer_equivalent_push_remains_canonical(self):
        original = self.fixture.read
        def read(*args, **kwargs):
            result = original(*args, **kwargs)
            if self.ack is not None:
                self.s._remember_contact_info({'contacts': [dict(self.ack, updateTime=400)]})
            return result
        self.client.contact_info.side_effect = read
        self.assertTrue(self.rename()['confirmed'])
        self.assertEqual(self.s._server_contacts[202]['updateTime'], 400)

    def test_read_failure_then_get_only_recovery_uses_bound_ack_without_resend(self):
        original = self.fixture.read
        def read(*args, **kwargs):
            if self.ack is not None:
                raise MaxTimeout('Synthetic read failure')
            return original(*args, **kwargs)
        self.client.contact_info.side_effect = read
        with self.assertRaises(MaxWriteOutcomeUnknown):
            self.rename()
        self.client.contact_info.side_effect = original
        state = self.s.refresh_contact_profile(self.context)
        self.assertFalse(state['pending'])
        self.assertEqual(state['title'], 'Requested Alias')
        self.client._write_frame.assert_called_once()

    def test_reconnected_session_cannot_use_old_ack_to_relax_readback(self):
        original = self.fixture.read
        def read(*args, **kwargs):
            if self.ack is not None:
                raise MaxTimeout('Synthetic read failure')
            return original(*args, **kwargs)
        self.client.contact_info.side_effect = read
        with self.assertRaises(MaxWriteOutcomeUnknown):
            self.rename()
        self.fixture.fixture.switch(same_owner=True)
        context = self.s.contact_context(202)
        self.client.contact_info.side_effect = original
        with self.assertRaises(ValueError):
            self.s.refresh_contact_profile(context)
        self.assertTrue(self.s.contact_snapshot(context)['pending'])
        self.client._write_frame.assert_called_once()

    def test_other_contact_actions_keep_strict_older_readback(self):
        with self.assertRaises(MaxWriteOutcomeUnknown):
            self.s.set_contact_profile_blocked(self.context, True)
        self.assertTrue(self.s.contact_snapshot(self.context)['pending'])
        self.client._write_frame.assert_called_once()

    def test_reconnected_session_can_confirm_with_ordinary_newer32_without_resend(self):
        original = self.fixture.read
        def read(*args, **kwargs):
            if self.ack is not None:
                raise MaxTimeout('Synthetic read failure')
            return original(*args, **kwargs)
        self.client.contact_info.side_effect = read
        with self.assertRaises(MaxWriteOutcomeUnknown):
            self.rename()
        self.fixture.fixture.switch(same_owner=True)
        context = self.s.contact_context(202)
        self.fixture.record['updateTime'] = 400
        self.client.contact_info.side_effect = original
        state = self.s.refresh_contact_profile(context)
        self.assertFalse(state['pending'])
        self.assertEqual(state['title'], 'Requested Alias')
        self.assertEqual(self.s._server_contacts[202]['updateTime'], 400)
        self.client._write_frame.assert_called_once()

    def test_pending_replacement_cannot_receive_another_operations_ack(self):
        original = self.write
        replacement = {'owner': 101, 'id': 202, 'action': 'UPDATE', 'inflight': False,
                       'payload': {'contactId': 202, 'action': 'UPDATE', 'firstName': 'Replacement'}}
        def write(*args, **kwargs):
            frame = original(*args, **kwargs)
            self.s._pending_contact_profile_write = replacement
            return frame
        self.client._write_frame.side_effect = write
        with self.assertRaises(MaxWriteOutcomeUnknown):
            self.rename()
        self.assertIs(self.s._pending_contact_profile_write, replacement)
        self.assertNotIn('rename_ack', replacement)

    def test_unchanged_prewrite_newer_canonical_merges_only_confirmed_names(self):
        self.push_ack = False
        before = base.contact(first='Canonical', updateTime=500,
                              description='Preserved metadata', avatarUrl='https://invalid.test/retained')
        self.s._remember_contact_info({'contacts': [before]})
        self.assertTrue(self.rename()['confirmed'])
        expected = dict(before, names=self.ack['names'])
        self.assertEqual(self.s._server_contacts[202], expected)
        self.assertEqual(self.store.list_contacts()[0].name, 'Requested Alias')
        self.client._write_frame.assert_called_once()

    def test_later_unrelated_metadata_keeps_new_clock_and_metadata(self):
        self.push_ack = False
        before = base.contact(first='Canonical', updateTime=500)
        self.s._remember_contact_info({'contacts': [before]})
        original = self.fixture.read
        def read(*args, **kwargs):
            result = original(*args, **kwargs)
            if self.ack is not None:
                self.s._remember_contact_info({'contacts': [dict(before, updateTime=600, description='Later')]})
            return result
        self.client.contact_info.side_effect = read
        self.assertTrue(self.rename()['confirmed'])
        self.assertEqual(self.s._server_contacts[202], dict(before, updateTime=600,
                         description='Later', names=self.ack['names']))

    def test_current_membership_or_phone_change_cannot_merge_old_prewrite_names(self):
        original = self.write
        for change in ({'flags': 0}, {'status': 'REMOVED'}, {'status': 'BLOCKED'},
                       {'phone': 79990000001}, {'accountStatus': 1}, {'options': ['SERVICE_ACCOUNT']}):
            with self.subTest(fields=tuple(change)):
                self.s._pending_contact_profile_write = None
                self.fixture.record = base.contact()
                self.s._remember_contact_info({'contacts': [self.fixture.record]})
                def write(*args, **kwargs):
                    frame = original(*args, **kwargs)
                    self.s._remember_contact_info({'contacts': [base.contact(updateTime=500, **change)]})
                    return frame
                self.client._write_frame.side_effect = write
                with self.assertRaises(MaxWriteOutcomeUnknown):
                    self.rename()
                self.assertTrue(self.s.contact_snapshot(self.context)['pending'])
                self.assertEqual(self.s._server_contacts[202]['updateTime'], 500)

    def test_wrong_or_malformed_ack_cannot_prove_rename(self):
        original = self.write
        for change in ({'names': base.contact(first='Unexpected')['names']}, {'flags': 0},
                       {'status': 'REMOVED'}, {'id': 303}, {'updateTime': True}, {'names': None}):
            with self.subTest(fields=tuple(change)):
                self.s._pending_contact_profile_write = None
                self.fixture.record = base.contact()
                self.s._remember_contact_info({'contacts': [self.fixture.record]})
                def write(*args, **kwargs):
                    frame = original(*args, **kwargs)
                    frame.decoded['contact'].update(copy.deepcopy(change))
                    return frame
                self.client._write_frame.side_effect = write
                with self.assertRaises(MaxWriteOutcomeUnknown):
                    self.rename()
                self.assertTrue(self.s.contact_snapshot(self.context)['pending'])

    def test_write_timeout_without_ack_is_not_a_rename_receipt(self):
        self.s._remember_contact_info({'contacts': [base.contact(updateTime=500)]})
        def write(*args, **kwargs):
            self.fixture.write(*args, **kwargs)
            raise MaxWriteOutcomeUnknown('Synthetic unknown')
        self.client._write_frame.side_effect = write
        with self.assertRaises(MaxWriteOutcomeUnknown):
            self.rename()
        with self.assertRaises(ValueError):
            self.s.refresh_contact_profile(self.context)
        self.assertNotIn('rename_ack', self.s._pending_contact_profile_write)
        self.client._write_frame.assert_called_once()

    def test_retained_ack_is_detached_from_frame(self):
        original_write, original_read = self.write, self.fixture.read
        frames = []
        def write(*args, **kwargs):
            frame = original_write(*args, **kwargs)
            frames.append(frame)
            return frame
        def read(*args, **kwargs):
            if frames:
                frames[0].decoded['contact']['names'][0]['firstName'] = 'Mutated frame'
                raise MaxTimeout('Synthetic read failure')
            return original_read(*args, **kwargs)
        self.client._write_frame.side_effect = write
        self.client.contact_info.side_effect = read
        with self.assertRaises(MaxWriteOutcomeUnknown):
            self.rename()
        self.assertEqual(self.s._pending_contact_profile_write['rename_ack'], self.ack)
        self.client.contact_info.side_effect = original_read
        self.assertFalse(self.s.refresh_contact_profile(self.context)['pending'])
        self.client._write_frame.assert_called_once()

    def test_competing_pending_during_read_remains_untouched(self):
        original = self.fixture.read
        other = {'owner': 101, 'id': 202, 'blocked': False}
        def read(*args, **kwargs):
            result = original(*args, **kwargs)
            if self.ack is not None:
                self.s._pending_contact_write = other
            return result
        self.client.contact_info.side_effect = read
        with self.assertRaises(MaxWriteOutcomeUnknown):
            self.rename()
        self.assertIs(self.s._pending_contact_write, other)
        self.assertIsNotNone(self.s._pending_contact_profile_write)

    def test_diagnostics_never_include_names_phones_clocks_or_metadata(self):
        self.rename()
        line = self.s.log.call_args.args[0]
        detail = json.loads(line.split(' ', 1)[1])
        self.assertEqual(detail['reason'], 'confirmed_rename')
        self.assertEqual(detail['semantic'], 'match')
        for secret in ('Requested', 'Alias', '7999', '202', '300', '101', 'http', 'names":['):
            self.assertNotIn(secret, line)


if __name__ == '__main__':
    unittest.main()
