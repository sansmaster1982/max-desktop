"""Version-skew observation uses canonical state, never a stale write readback."""
import copy
import json
import unittest
from unittest.mock import Mock, patch

import test_contact_profile_core as base
from maxclient.protocol.client import MaxFrame, MaxWriteOutcomeUnknown


class ContactProfileFreshnessTests(unittest.TestCase):
    def setUp(self):
        self.fixture = base.ContactProfileCoreTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.s, self.client, self.store = self.fixture.service, self.fixture.client, self.fixture.store
        self.context = self.fixture.context
        self.canonical = base.contact(first='Canonical', updateTime=200)
        self.s._remember_contact_info({'contacts': [self.canonical]})
        self.s.log = Mock()

    def refresh(self):
        return self.s.refresh_contact_profile(self.context)

    def test_clock_only_older_read_enables_actions_without_downgrading_cache(self):
        before, local = copy.deepcopy(self.s._server_contacts), copy.deepcopy(self.store.list_contacts())
        state = self.refresh()
        self.assertTrue(state['fresh'])
        self.assertTrue(state['availability']['delete'])
        self.assertTrue(state['availability']['block'])
        self.assertEqual(state['title'], 'Canonical Person')
        self.assertEqual(self.s._server_contacts, before)
        self.assertEqual(self.store.list_contacts(), local)
        self.client._write_frame.assert_not_called()

    def test_optional_defaults_match_but_newer_name_and_phone_are_retained(self):
        self.canonical.update(accountStatus=0, options=[], phone=79990000001)
        self.s._remember_contact_info({'contacts': [self.canonical]})
        state = self.refresh()
        self.assertTrue(state['fresh'])
        self.assertEqual(state['phone'], '+79990000001')
        self.assertEqual(state['title'], 'Canonical Person')

    def test_action_critical_difference_rejects_refresh_and_preflight(self):
        for change in ({'flags': 0}, {'flags': 520}, {'status': 'BLOCKED'},
                       {'status': 'REMOVED'}, {'status': 'HIDDEN'}, {'accountStatus': 1},
                       {'accountStatus': 2}, {'options': ['SERVICE_ACCOUNT']}):
            with self.subTest(change=change):
                self.s._remember_contact_info({'contacts': [self.canonical]})
                self.fixture.record = base.contact(**change)
                for action in (self.refresh, lambda: self.s.set_contact_profile_blocked(self.context, True)):
                    with self.assertRaises(ValueError):
                        action()
                self.assertFalse(self.s.contact_snapshot(self.context)['fresh'])
                self.assertEqual(self.s._server_contacts[202], self.canonical)
        self.client._write_frame.assert_not_called()

    def test_options_compatibility_service_semantics_are_compared(self):
        self.canonical.pop('flags')
        self.canonical['options'] = ['SERVICE_ACCOUNT']
        self.s._remember_contact_info({'contacts': [self.canonical]})
        self.fixture.record = base.contact(flags=0, options=['SERVICE_ACCOUNT'])
        with self.assertRaises(ValueError):
            self.refresh()
        detail = json.loads(self.s.log.call_args.args[0].split(' ', 1)[1])
        self.assertEqual(detail['differences'], ['service'])

    def test_malformed_action_metadata_cannot_establish_equivalence(self):
        for change in ({'accountStatus': True}, {'accountStatus': 3}, {'accountStatus': None},
                       {'options': None}, {'options': 'SERVICE_ACCOUNT'}, {'options': [True]}):
            with self.subTest(change=change):
                self.fixture.record = base.contact(**change)
                with self.assertRaises(ValueError):
                    self.refresh()
                self.assertFalse(self.s.contact_snapshot(self.context)['fresh'])
        self.client._write_frame.assert_not_called()

    def test_malformed_canonical_record_cannot_grant_actions(self):
        for key, value in [('names', None), ('flags', True), ('accountStatus', 'PRIVATE_VALUE'),
                           ('options', [False])]:
            with self.subTest(key=key):
                self.s._server_contacts[202] = dict(self.canonical, **{key: value})
                with self.assertRaises(ValueError):
                    self.refresh()
        self.client._write_frame.assert_not_called()

    def test_newer_push_invalidates_full_canonical_observation(self):
        self.assertTrue(self.refresh()['fresh'])
        for change in ({'status': 'BLOCKED'}, {'flags': 0}, {'updateTime': 201},
                       {'names': [{'type': 'ONEME', 'firstName': 'New push'}]}):
            with self.subTest(change=change):
                self.s._server_contacts[202] = dict(self.canonical, **change)
                state = self.s.contact_snapshot(self.context)
                self.assertFalse(state['fresh'])
                self.assertFalse(state['availability']['block'])
                self.assertFalse(state['availability']['delete'])

    def test_conflicting_read_invalidates_previous_observation_without_cache_write(self):
        self.assertTrue(self.refresh()['fresh'])
        self.fixture.record['status'] = 'BLOCKED'
        with self.assertRaises(ValueError):
            self.refresh()
        self.assertFalse(self.s.contact_snapshot(self.context)['fresh'])
        self.assertEqual(self.s._server_contacts[202], self.canonical)

    def test_reconnect_never_reuses_previous_observation(self):
        self.refresh()
        self.fixture.fixture.switch(same_owner=True)
        with self.assertRaises(ValueError):
            self.refresh()
        context = self.s.contact_context(202)
        self.assertFalse(self.s.contact_snapshot(context)['fresh'])
        self.client._write_frame.assert_not_called()

    def test_owner_switch_rejects_observation_and_dispatch(self):
        self.refresh()
        self.fixture.fixture.switch(404)
        with self.assertRaises(ValueError):
            self.s.set_contact_profile_blocked(self.context, True)
        self.client._write_frame.assert_not_called()

    def test_older_matching_read_cannot_settle_pending_profile_write(self):
        pending = {'owner': 101, 'id': 202, 'action': 'ADD', 'inflight': False,
                   'payload': {'contactId': 202, 'action': 'ADD'}}
        self.s._pending_contact_profile_write = pending
        self.store.set_contact_hidden(101, 202, True)
        with self.assertRaises(ValueError):
            self.refresh()
        self.assertIs(self.s._pending_contact_profile_write, pending)
        self.assertIn(202, self.store.hidden_contact_ids(101))
        self.assertFalse(self.s.contact_snapshot(self.context)['fresh'])

    def test_older_matching_read_cannot_settle_other_pending_write(self):
        for owner in (101, 404):
            with self.subTest(owner=owner):
                pending = {'owner': owner, 'id': 202, 'blocked': False}
                self.s._pending_contact_write = pending
                with self.assertRaises(ValueError):
                    self.refresh()
                self.assertIs(self.s._pending_contact_write, pending)
        self.client._write_frame.assert_not_called()

    def test_accepted_write_with_older_desired_readback_stays_unknown(self):
        with self.assertRaises(MaxWriteOutcomeUnknown):
            self.s.set_contact_profile_blocked(self.context, True)
        self.client._write_frame.assert_called_once()
        self.assertEqual(self.fixture.record['status'], 'BLOCKED')
        self.assertNotIn('status', self.s._server_contacts[202])
        self.assertTrue(self.s.contact_snapshot(self.context)['pending'])
        with self.assertRaises(MaxWriteOutcomeUnknown):
            self.s.set_contact_profile_blocked(self.context, True)
        self.client._write_frame.assert_called_once()

    def test_equal_or_newer_actual_mutation_readback_still_confirms(self):
        original = self.fixture.write
        def write(*args, **kwargs):
            self.fixture.record['updateTime'] = 200
            return original(*args, **kwargs)
        self.client._write_frame.side_effect = write
        state = self.s.set_contact_profile_blocked(self.context, True)
        self.assertTrue(state['confirmed'])
        self.assertTrue(state['blocked'])
        self.assertFalse(state['pending'])
        self.client._write_frame.assert_called_once()

    def test_conflicting_push_after_preflight_rejects_reservation(self):
        original = self.s._read_contact_profile
        def read(*args, **kwargs):
            state = original(*args, **kwargs)
            self.s._server_contacts[202]['status'] = 'BLOCKED'
            return state
        with patch.object(self.s, '_read_contact_profile', side_effect=read):
            with self.assertRaises(ValueError):
                self.s.set_contact_profile_blocked(self.context, True)
        self.client._write_frame.assert_not_called()
        self.assertIsNone(getattr(self.s, '_pending_contact_profile_write', None))

    def test_push_after_reservation_rejects_last_prewrite_guard_and_clears_only_own_intent(self):
        original = self.s._contact_profile_cache
        def cache(*args, **kwargs):
            result = original(*args, **kwargs)
            if getattr(self.s, '_pending_contact_profile_write', None):
                self.s._server_contacts[202]['flags'] = 520
            return result
        with patch.object(self.s, '_contact_profile_cache', side_effect=cache):
            with self.assertRaises(ValueError):
                self.s.set_contact_profile_blocked(self.context, True)
        self.client._write_frame.assert_not_called()
        self.assertIsNone(getattr(self.s, '_pending_contact_profile_write', None))

    def test_strict_internal_read_never_uses_equivalence(self):
        with self.assertRaises(ValueError):
            self.s._read_contact_profile(self.context)
        self.assertFalse(self.s.contact_snapshot(self.context)['fresh'])

    def test_diagnostics_only_use_fixed_semantic_fields_not_input_values(self):
        self.refresh()
        detail = json.loads(self.s.log.call_args.args[0].split(' ', 1)[1])
        self.assertEqual((detail['semantic'], detail['differences']), ('match', []))
        self.fixture.record.update(status='BLOCKED', names=[{'firstName': 'PRIVATE_NAME'}],
                                   token='PRIVATE_TOKEN', phone=79991111111)
        with self.assertRaises(ValueError):
            self.refresh()
        line = self.s.log.call_args.args[0]
        detail = json.loads(line.split(' ', 1)[1])
        self.assertEqual((detail['semantic'], detail['differences']), ('conflict', ['status']))
        for forbidden in ('PRIVATE', '79991111111', 'Canonical', '202', '101'):
            self.assertNotIn(forbidden, line)


if __name__ == '__main__':
    unittest.main()
