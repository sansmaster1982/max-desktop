"""Existing-contact ADD differs from rename; synthetic owner-bound41/32 only."""
import copy
import unittest
from unittest.mock import Mock

import test_contact_create_core as fixtures
from test_contact_create_contracts import PHONE, record
from maxclient.core.contact_create import ContactCreateResult
from maxclient.core.models import Contact
from maxclient.protocol.client import MaxFrame, MaxTimeout, MaxWriteOutcomeUnknown


class ExistingContactCreateTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ContactCreateCoreTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.service, self.client, self.store = self.fixture.service, self.fixture.client, self.fixture.store
        self.service._contact_creation_state()
        self.actual = record()
        self.actual['names'][0]['firstName'] = 'Current synthetic name'
        self.new = False
        self.service.log = Mock()
        self.client.request.side_effect = self.reply

    def reply(self, opcode, payload, **kwargs):
        self.assertIs(kwargs['expected_session'], self.fixture.context)
        value = copy.deepcopy(self.actual)
        if opcode == 41:
            return MaxFrame(1, 1, 41, b'', {'contact': value, 'new': self.new})
        if opcode == 32:
            return MaxFrame(1, 2, 32, b'', {'contacts': [value]})
        if opcode == 46:
            return MaxFrame(1, 3, 46, b'', {'contact': value})
        self.fail('Unexpected synthetic opcode')

    def verify(self):
        return self.service.verify_contact_creation(PHONE, context=self.fixture.context)

    def test_existing_ack_and_actual_name_confirm_add_without_claiming_rename(self):
        result = self.fixture.create()
        self.assertIs(result.is_new, False)
        self.assertFalse(result.name_matches)
        self.assertEqual(result.contact.name, 'Current synthetic name Person')
        self.assertEqual([c.args[0] for c in self.client.request.call_args_list], [41, 32])
        self.assertEqual(self.service._contact_create_pending, {})
        self.assertEqual(self.store.list_chats(), [])

    def test_actual32_name_wins_over_ack_name(self):
        original = self.reply
        def reply(opcode, payload, **kwargs):
            frame = original(opcode, payload, **kwargs)
            if opcode == 41:
                frame.decoded['contact']['names'] = record()['names']
            return frame
        self.client.request.side_effect = reply
        result = self.fixture.create()
        self.assertFalse(result.name_matches)
        self.assertEqual(result.contact.name, 'Current synthetic name Person')

    def test_existing_matching_name_reports_match_and_two_argument_result_is_compatible(self):
        self.actual = record()
        self.assertTrue(self.fixture.create().name_matches)
        self.assertTrue(ContactCreateResult(Contact(202), False).name_matches)

    def test_existing_generic_custom_name_is_displayed_without_guessing_first_last(self):
        self.actual['names'] = [{'type': 'CUSTOM', 'name': 'Actual synthetic alias'}]
        result = self.fixture.create()
        self.assertEqual(result.contact.name, 'Actual synthetic alias')
        self.assertFalse(result.name_matches)

    def test_new_true_still_requires_requested_name(self):
        self.new = True
        with self.assertRaises(MaxWriteOutcomeUnknown):
            self.fixture.create()
        self.assertTrue(self.service._contact_create_pending)
        self.assertEqual(self.store.list_contacts(), [])

    def test_unknown_write_recovery_cannot_assume_existing_and_relax_name(self):
        self.client.request.side_effect = MaxTimeout('Synthetic timeout')
        with self.assertRaises(MaxWriteOutcomeUnknown):
            self.fixture.create()
        self.client.request.side_effect = self.reply
        with self.assertRaises(MaxWriteOutcomeUnknown):
            self.verify()
        self.assertEqual([c.args[0] for c in self.client.request.call_args_list], [41, 46, 32])
        self.assertEqual(self.store.list_contacts(), [])

    def test_explicit_existing_ack_recovers_with_only32_after_failed_readback(self):
        original = self.reply
        def reply(opcode, payload, **kwargs):
            if opcode == 32:
                raise MaxTimeout('Synthetic read failure')
            return original(opcode, payload, **kwargs)
        self.client.request.side_effect = reply
        with self.assertRaises(MaxWriteOutcomeUnknown):
            self.fixture.create()
        self.client.request.side_effect = self.reply
        result = self.verify()
        self.assertFalse(result.name_matches)
        self.assertEqual([c.args[0] for c in self.client.request.call_args_list], [41, 32, 32])
        self.assertEqual(self.service._contact_create_pending, {})

    def test_existing_does_not_relax_membership_status_phone_or_identity(self):
        for override in ({'flags': 0}, {'status': 'REMOVED'}, {'status': 'HIDDEN'}, {'phone': 79990000001}):
            with self.subTest(fields=tuple(override)):
                self.service._contact_create_pending.clear()
                self.actual = dict(record(), **override)
                with self.assertRaises(MaxWriteOutcomeUnknown):
                    self.fixture.create()
                self.assertEqual(self.store.list_contacts(), [])

    def test_wrong_ack_phone_cannot_bind_existing_id_even_if32_omits_phone(self):
        original = self.reply
        def reply(opcode, payload, **kwargs):
            frame = original(opcode, payload, **kwargs)
            if opcode == 41:
                frame.decoded['contact']['phone'] = 79990000001
            else:
                frame.decoded['contacts'][0].pop('phone')
            return frame
        self.client.request.side_effect = reply
        with self.assertRaises(MaxWriteOutcomeUnknown):
            self.fixture.create()
        self.assertEqual([c.args[0] for c in self.client.request.call_args_list], [41])

    def test_session_replacement_after32_never_installs_existing_record(self):
        original = self.reply
        def reply(opcode, payload, **kwargs):
            frame = original(opcode, payload, **kwargs)
            if opcode == 32:
                self.fixture.switch()
            return frame
        self.client.request.side_effect = reply
        with self.assertRaises(MaxWriteOutcomeUnknown):
            self.fixture.create()
        self.assertEqual(self.store.list_contacts(), [])

    def test_older_conflicting32_cannot_overwrite_newer_cached_name_or_membership(self):
        for new in (False, True):
            for override in ({'names': [{'type': 'CUSTOM', 'firstName': 'Newer synthetic'}]}, {'flags': 0}):
                with self.subTest(new=new, fields=tuple(override)):
                    self.service._contact_create_pending.clear()
                    self.new, self.actual = new, record()
                    latest = dict(record(), updateTime=300, **override)
                    self.service._remember_contact_info({'contacts': [latest]})
                    stored = self.store.list_contacts()
                    with self.assertRaises(MaxWriteOutcomeUnknown):
                        self.fixture.create()
                    self.assertEqual(self.service._server_contacts[202], latest)
                    self.assertEqual(self.store.list_contacts(), stored)

    def test_semantically_matching_newer_cache_is_preserved_instead_of_downgraded(self):
        latest = dict(self.actual, updateTime=300, baseUrl='https://example.invalid/synthetic')
        self.service._remember_contact_info({'contacts': [latest]})
        result = self.fixture.create()
        self.assertFalse(result.name_matches)
        self.assertEqual(self.service._server_contacts[202], latest)

    def test_newer_ack_conflict_blocks_old32_with_empty_cache_and_preserves_pending(self):
        for new in (False, True):
            with self.subTest(new=new):
                self.service._contact_create_pending.clear()
                self.client.request.reset_mock()
                self.new, self.actual = new, record()
                ack = dict(record(), updateTime=300, names=[{'type': 'CUSTOM', 'firstName': 'Newer ACK'}])
                original = self.reply
                def reply(opcode, payload, **kwargs):
                    frame = original(opcode, payload, **kwargs)
                    if opcode == 41:
                        frame.decoded['contact'] = copy.deepcopy(ack)
                    return frame
                self.client.request.side_effect = reply
                with self.assertRaises(MaxWriteOutcomeUnknown):
                    self.fixture.create()
                with self.assertRaises(MaxWriteOutcomeUnknown):
                    self.verify()
                with self.assertRaises(MaxWriteOutcomeUnknown):
                    self.fixture.create()
                self.assertEqual([c.args[0] for c in self.client.request.call_args_list], [41, 32, 32])
                self.assertEqual(self.store.list_contacts(), [])
                self.assertEqual(self.service._server_contacts, {})
                self.assertEqual(self.service._contact_create_pending[(101, PHONE)]['ack_contact'], ack)

    def test_matching_older32_keeps_newer_ack_revision_and_metadata(self):
        ack = dict(self.actual, updateTime=300, baseUrl='https://example.invalid/ack-avatar')
        original = self.reply
        def reply(opcode, payload, **kwargs):
            frame = original(opcode, payload, **kwargs)
            if opcode == 41:
                frame.decoded['contact'] = copy.deepcopy(ack)
            return frame
        self.client.request.side_effect = reply
        result = self.fixture.create()
        self.assertFalse(result.name_matches)
        self.assertEqual(self.service._server_contacts[202], ack)

    def test_newer32_can_confirm_its_actual_name_after_older_ack(self):
        self.actual['updateTime'] = 400
        original = self.reply
        def reply(opcode, payload, **kwargs):
            frame = original(opcode, payload, **kwargs)
            if opcode == 41:
                frame.decoded['contact'] = dict(record(), updateTime=300)
            return frame
        self.client.request.side_effect = reply
        result = self.fixture.create()
        self.assertFalse(result.name_matches)
        self.assertEqual(self.service._server_contacts[202], self.actual)

    def test_verify_catches_up_to_newer_ack_using32_only(self):
        ack = dict(record(), updateTime=300)
        original = self.reply
        def reply(opcode, payload, **kwargs):
            frame = original(opcode, payload, **kwargs)
            if opcode == 41:
                frame.decoded['contact'] = copy.deepcopy(ack)
            return frame
        self.client.request.side_effect = reply
        with self.assertRaises(MaxWriteOutcomeUnknown):
            self.fixture.create()
        self.actual = copy.deepcopy(ack)
        self.assertTrue(self.verify().name_matches)
        self.assertEqual([c.args[0] for c in self.client.request.call_args_list], [41, 32, 32])
        self.assertEqual(self.service._contact_create_pending, {})

    def test_ack_snapshot_is_detached_from_frame_mutations_during_readback(self):
        frame = MaxFrame(1, 1, 41, b'', {'contact': dict(record(), updateTime=300), 'new': False})
        original = copy.deepcopy(frame.decoded['contact'])
        def reply(opcode, payload, **kwargs):
            if opcode == 41:
                return frame
            frame.decoded['contact']['names'][0]['firstName'] = 'Mutated source frame'
            raise MaxTimeout('Synthetic read failure')
        self.client.request.side_effect = reply
        with self.assertRaises(MaxWriteOutcomeUnknown):
            self.fixture.create()
        self.assertEqual(self.service._contact_create_pending[(101, PHONE)]['ack_contact'], original)

    def test_diagnostics_contain_only_schema_categories_and_booleans(self):
        self.actual['names'][0]['firstName'] = 'SYNTHETIC_PRIVATE_NAME'
        self.actual['extra'] = {'token': 'SYNTHETIC_PRIVATE_TOKEN', 'url': 'https://example.invalid/private'}
        self.fixture.create()
        logged = '\n'.join(call.args[0] for call in self.service.log.call_args_list)
        self.assertIn('contact-create-readback ', logged)
        self.assertIn('"is_new":false', logged)
        self.assertIn('"name_matches":false', logged)
        for private in ('SYNTHETIC_PRIVATE', PHONE, PHONE[1:], 'https://', '202'):
            self.assertNotIn(private, logged)

    def test_diagnostic_sink_failure_does_not_change_confirmed_result(self):
        self.service.log.side_effect = RuntimeError('Synthetic private diagnostic sink')
        self.assertFalse(self.fixture.create().name_matches)


if __name__ == '__main__':
    unittest.main()
