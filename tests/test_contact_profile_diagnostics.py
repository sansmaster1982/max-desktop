"""Bounded contact readback diagnostics, synthetic transport and in-memory SQL."""
import copy
import json
import unittest
from unittest.mock import Mock

import test_contact_dialog_core as fixtures


class ContactProfileDiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ContactDialogCoreTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.s, self.client = self.fixture.service, self.fixture.client
        self.context = self.fixture.context
        self.s.log = Mock()

    def diagnostic(self):
        self.s.log.assert_called_once()
        line = self.s.log.call_args.args[0]
        self.assertNotIn('PRIVATE', line)
        self.assertNotIn('Synthetic', line)
        self.assertNotIn('202', line)
        self.assertNotIn('101', line)
        return json.loads(line.removeprefix('contact-profile-readback '))

    def test_invalid_fields_are_types_and_validity_without_values(self):
        for key, value, kind in [('phone', 'PRIVATE_PHONE', 'string'), ('phone', None, 'null'),
                                 ('phone', False, 'boolean'), ('flags', 'PRIVATE_FLAGS', 'string'),
                                 ('updateTime', 20.5, 'number'), ('status', 'PRIVATE_STATUS', 'string'),
                                 ('names', 'PRIVATE_NAMES', 'string')]:
            with self.subTest(key=key, kind=kind):
                self.s.log.reset_mock()
                raw = fixtures.record()
                raw[key] = value
                raw['secret'] = 'PRIVATE_TOKEN'
                self.client.contact_info.side_effect = None
                self.client.contact_info.return_value = {'contacts': [raw], 'secret': 'PRIVATE_PASSWORD'}
                with self.assertRaises(ValueError):
                    self.s._read_contact_profile(self.context)
                record = self.diagnostic()
                self.assertEqual(record['reason'], 'parse_failed')
                self.assertEqual(record['fields'][key], {'type': kind, 'valid': False})
                self.assertEqual(record['target_matches'], 'one')
                self.client._write_frame.assert_not_called()

    def test_missing_required_field_and_bad_name_record_are_distinguished(self):
        raw = fixtures.record()
        raw.pop('updateTime')
        self.client.contact_info.side_effect = None
        self.client.contact_info.return_value = {'contacts': [raw]}
        with self.assertRaises(ValueError):
            self.s._read_contact_profile(self.context)
        self.assertEqual(self.diagnostic()['fields']['updateTime'], {'type': 'absent'})
        self.s.log.reset_mock()
        self.client.contact_info.return_value = {'contacts': [dict(fixtures.record(), names=[{'firstName': None}])]}
        with self.assertRaises(ValueError):
            self.s._read_contact_profile(self.context)
        self.assertFalse(self.diagnostic()['name_fields_valid'])

    def test_missing_or_ambiguous_target_never_logs_contact_identity(self):
        for payload, target in [({}, 'none'), ({'contacts': []}, 'none'),
                                ({'contacts': [fixtures.record(999)]}, 'none'),
                                ({'contacts': [fixtures.record(), fixtures.record()]}, 'multiple')]:
            with self.subTest(target=target):
                self.s.log.reset_mock()
                self.client.contact_info.side_effect = None
                self.client.contact_info.return_value = payload
                with self.assertRaises(ValueError):
                    self.s._read_contact_profile(self.context)
                self.assertEqual(self.diagnostic()['target_matches'], target)

    def test_stale_clock_is_classified_without_timestamps_or_cache_changes(self):
        before = copy.deepcopy(self.s._server_contacts)
        self.client.contact_info.side_effect = None
        self.client.contact_info.return_value = {'contacts': [dict(fixtures.record(), updateTime=19)]}
        with self.assertRaises(ValueError):
            self.s._read_contact_profile(self.context)
        record = self.diagnostic()
        self.assertEqual((record['reason'], record['clock']), ('stale_contact', 'older'))
        self.assertEqual(self.s._server_contacts, before)

    def test_transport_error_text_is_not_logged(self):
        self.client.contact_info.side_effect = ValueError('PRIVATE_TRANSPORT_TOKEN')
        with self.assertRaises(ValueError):
            self.s._read_contact_profile(self.context)
        self.assertEqual(self.diagnostic()['reason'], 'read_failed')

    def test_replaced_context_is_distinguished_from_parse_failure(self):
        def read(*args, **kwargs):
            self.fixture.fixture.switch(same_owner=True)
            return {'contacts': [fixtures.record()]}
        self.client.contact_info.side_effect = read
        with self.assertRaises(ValueError):
            self.s._read_contact_profile(self.context)
        self.assertEqual(self.diagnostic()['reason'], 'context_changed')

    def test_cache_and_final_snapshot_failure_have_distinct_reasons(self):
        original = self.s._remember_contact_info
        self.s._remember_contact_info = Mock(side_effect=ValueError('PRIVATE_CACHE'))
        with self.assertRaises(ValueError):
            self.s._read_contact_profile(self.context)
        self.assertEqual(self.diagnostic()['reason'], 'cache_failed')
        self.s.log.reset_mock()
        self.s._remember_contact_info = original
        self.s.contact_snapshot = Mock(side_effect=ValueError('PRIVATE_RENDER'))
        with self.assertRaises(ValueError):
            self.s._read_contact_profile(self.context)
        self.assertEqual(self.diagnostic()['reason'], 'snapshot_failed')

    def test_broken_logger_preserves_original_exception_and_acceptance_rules(self):
        self.s.log.side_effect = RuntimeError('PRIVATE_LOG')
        self.client.contact_info.side_effect = ValueError('Original failure')
        with self.assertRaisesRegex(ValueError, 'Original failure'):
            self.s._read_contact_profile(self.context)
        self.client.contact_info.side_effect = None
        self.client.contact_info.return_value = {'contacts': [fixtures.record()]}
        self.assertTrue(self.s._read_contact_profile(self.context)['fresh'])
        self.assertEqual(self.s.log.call_count, 1)  # Successful reads are not logged.


if __name__ == '__main__':
    unittest.main()
