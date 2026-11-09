"""Bounded LOGIN cache failure locations, without account data or network."""
import unittest
from unittest.mock import Mock, patch

import test_contact_lookup_core as fixture
from maxclient.core.service import _login_cache_failure_stack
from maxclient.protocol.client import MaxFrame


class LoginCacheDiagnosticsTests(unittest.TestCase):
    def test_ingest_failure_logs_only_class_and_function_line_locations(self):
        case = fixture.ContactLookupCoreTests()
        case.setUp()
        self.addCleanup(case.doCleanups)
        service = case.service
        service.log = Mock()
        secret = 'SYNTHETIC_PRIVATE_EXCEPTION_AND_LOCAL_VALUE'

        def cache_leaf():
            private_local = secret
            raise ValueError(private_local)

        def ingest_failure(*_args, **_kwargs):
            cache_leaf()

        frame = MaxFrame(1, 1, 19, b'', {'profile': {'contact': {
            'id': 101, 'names': [{'firstName': 'Synthetic', 'lastName': 'Owner'}]}}})
        with patch.object(service, '_ingest_login', side_effect=ingest_failure):
            service._finish_login(frame, 101)

        logs = [call.args[0] for call in service.log.call_args_list]
        self.assertIn('LOGIN cache update failed: ValueError', logs)
        stacks = [line.removeprefix('LOGIN cache failure stack: ')
                  for line in logs if line.startswith('LOGIN cache failure stack: ')]
        self.assertEqual(len(stacks), 1)
        self.assertIn('cache_leaf:', stacks[0])
        self.assertLessEqual(len(stacks[0].split(' > ')), 8)
        for location in stacks[0].split(' > '):
            self.assertRegex(location, r'^[A-Za-z_][A-Za-z0-9_]{0,63}:[0-9]+$')
        self.assertNotIn(secret, '\n'.join(logs))
        self.assertNotIn(__file__, '\n'.join(logs))
        self.assertEqual(service.store.load_profile().id, 101)
        self.assertEqual(service.session.my_name, 'Synthetic Owner')
        service.client.request.assert_not_called()
        service.client.contact_by_phone.assert_not_called()

    def test_deep_stack_is_bounded_and_never_formats_message_or_filename(self):
        namespace = {}
        synthetic_source = (
            'def recurse(depth):\n'
            '    if depth:\n'
            '        return recurse(depth - 1)\n'
            '    raise ValueError("SYNTHETIC_PRIVATE_MESSAGE")\n'
        )
        exec(compile(synthetic_source, 'SYNTHETIC_PRIVATE_FILENAME', 'exec'), namespace)
        try:
            namespace['recurse'](20)
        except ValueError as error:
            result = _login_cache_failure_stack(error)
        self.assertEqual(len(result.split(' > ')), 8)
        self.assertNotIn('SYNTHETIC_PRIVATE', result)
        self.assertNotIn('ValueError', result)
        self.assertEqual(_login_cache_failure_stack(ValueError('not logged')), 'unavailable')


if __name__ == '__main__':
    unittest.main()
