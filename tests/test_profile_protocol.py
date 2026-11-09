"""Offline current PROFILE16 schema and actual-producer contracts."""
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from maxclient.protocol.client import MaxClient, MaxError, MaxFrame, MaxTimeout, MaxWriteOutcomeUnknown, _redact


class ProfileProtocolContracts(unittest.TestCase):
    def client(self, response=None):
        client = MaxClient(auto_reconnect=False)
        if response is None:
            response = {'profile': {'contact': {'id': 42, 'firstName': 'Synthetic', 'description': ''}}}
        client.request = Mock(return_value=MaxFrame(1, 7, 16, b'', response))
        return client

    def test_explicit_fields_and_clears_are_kept_without_unrelated_profile_changes(self):
        client = self.client()
        result = client.update_profile(first_name='Synthetic', last_name='', description='', link='synthetic_link')
        client.request.assert_called_once_with(16, {'firstName': 'Synthetic', 'lastName': '',
                                                    'description': '', 'link': 'synthetic_link'})
        self.assertEqual(result['profile']['contact']['id'], 42)
        self.assertNotIn('photoToken', client.request.call_args.args[1])

    def test_legacy_name_edit_uses_current_schema_without_request_id(self):
        client = self.client()
        client.update_profile_name(' Synthetic ')
        client.request.assert_called_once_with(16, {'firstName': 'Synthetic'})
        client.request.reset_mock()
        client.update_profile_name(' Synthetic ', '')
        client.request.assert_called_once_with(16, {'firstName': 'Synthetic', 'lastName': ''})

    def test_uploaded_avatar_token_and_explicit_preset_type_remain_distinct(self):
        client = self.client()
        client.update_profile(photo_token='synthetic-upload-token')
        client.request.assert_called_once_with(16, {'photoToken': 'synthetic-upload-token', 'avatarType': 'USER_AVATAR'})
        client.request.reset_mock()
        client.update_profile(photo_id=81, avatar_type='PRESET_AVATAR')
        client.request.assert_called_once_with(16, {'photoId': 81, 'avatarType': 'PRESET_AVATAR'})
        self.assertEqual(_redact({'photoToken': 'synthetic-upload-token'}), {'photoToken': '***'})

    def test_invalid_or_uncertain_profile_ack_is_never_reported_as_committed(self):
        for reply in [{}, {'profile': {}}, {'profile': {'contact': {'id': True}}}]:
            client = self.client(reply)
            with self.assertRaises(MaxWriteOutcomeUnknown):
                client.update_profile(description='synthetic description')
            self.assertEqual(client.request.call_count, 1)
        client = self.client()
        client.request.side_effect = MaxTimeout('private synthetic detail')
        with self.assertRaises(MaxWriteOutcomeUnknown) as caught:
            client.update_profile(description='synthetic description')
        self.assertNotIn('private synthetic detail', str(caught.exception))
        self.assertEqual(client.request.call_count, 1)

    def test_nack_and_invalid_input_never_fabricate_profile_or_retry(self):
        client = self.client()
        client.request.return_value = MaxFrame(3, 7, 16, b'private synthetic bytes',
                                              {'error': 'synthetic.denied', 'message': 'private synthetic detail'})
        with self.assertRaisesRegex(MaxError, 'synthetic.denied') as caught:
            client.update_profile(first_name='Synthetic')
        self.assertNotIn('private synthetic detail', str(caught.exception))
        self.assertEqual(client.request.call_count, 1)
        client.request.reset_mock()
        for args in [{}, {'description': True}, {'photo_token': ''}, {'photo_id': float('nan')},
                     {'photo_id': True}, {'avatar_type': 'unverified'}]:
            with self.assertRaises(ValueError):
                client.update_profile(**args)
        client.request.assert_not_called()


if __name__ == '__main__':
    unittest.main()
