"""Offline profile-photo flow; HTTP is replaced with synthetic responses."""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import requests

from maxclient.protocol.uploader import AttachKind, MaxUploadError, upload_and_build_attach


class ProfileUploadContracts(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1])
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'synthetic-photo.bin'
        self.path.write_bytes(b'synthetic image bytes - no private photo')
        self.client = Mock()
        self.client.request_photo_upload.return_value = {'url': 'https://upload.invalid/synthetic?private=synthetic'}

    def test_profile_photo_flag_and_actual_photos_token_are_used_once(self):
        response = Mock(status_code=200, text='{"photos":[{"token":"synthetic-upload-token"}]}')
        with patch('maxclient.protocol.uploader.requests.post', return_value=response) as post:
            attach = upload_and_build_attach(self.client, str(self.path), AttachKind.PHOTO, profile=True)
        self.client.request_photo_upload.assert_called_once_with(count=1, profile=True)
        self.assertEqual(attach, {'_type': 'PHOTO', 'photoToken': 'synthetic-upload-token'})
        self.assertEqual(post.call_count, 1)
        self.assertIs(post.call_args.kwargs['allow_redirects'], False)

    def test_normal_chat_photo_still_uses_profile_false_and_existing_token_shape(self):
        response = Mock(status_code=200, text='{"photoToken":"synthetic-chat-photo"}')
        with patch('maxclient.protocol.uploader.requests.post', return_value=response):
            attach = upload_and_build_attach(self.client, str(self.path), AttachKind.PHOTO)
        self.client.request_photo_upload.assert_called_once_with(count=1, profile=False)
        self.assertEqual(attach, {'_type': 'PHOTO', 'photoToken': 'synthetic-chat-photo'})

    def test_profile_flag_cannot_upload_other_media_or_unprotected_urls(self):
        with patch('maxclient.protocol.uploader.requests.post') as post:
            with self.assertRaises(ValueError):
                upload_and_build_attach(self.client, str(self.path), AttachKind.FILE, profile=True)
            self.client.request_photo_upload.assert_not_called()
            for url in ['http://upload.invalid/synthetic?secret=synthetic',
                        'https://synthetic:secret@upload.invalid/path', 'invalid-synthetic-url']:
                self.client.request_photo_upload.return_value = {'url': url}
                with self.assertRaises(MaxUploadError) as caught:
                    upload_and_build_attach(self.client, str(self.path), AttachKind.PHOTO, profile=True)
                self.assertNotIn(url, str(caught.exception))
            post.assert_not_called()

    def test_profile_requires_exact_structured_photos_result_and_does_not_follow_redirects(self):
        for status, body in [(200, '{"token":"synthetic-unverified-shape"}'),
                             (200, '{"photos":[{"token":17}]}'),
                             (200, '{"photos":[]}'), (307, 'synthetic redirect body')]:
            with patch('maxclient.protocol.uploader.requests.post',
                       return_value=Mock(status_code=status, text=body)) as post:
                with self.assertRaises(MaxUploadError) as caught:
                    upload_and_build_attach(self.client, str(self.path), AttachKind.PHOTO, profile=True)
            self.assertNotIn(body, str(caught.exception))
            self.assertEqual(post.call_count, 1)
            self.assertIs(post.call_args.kwargs['allow_redirects'], False)

    def test_upload_errors_do_not_expose_upload_urls_tokens_or_response_bodies(self):
        with patch('maxclient.protocol.uploader.requests.post',
                   side_effect=requests.RequestException('https://upload.invalid/?token=synthetic-secret')) as post:
            with self.assertRaises(MaxUploadError) as caught:
                upload_and_build_attach(self.client, str(self.path), AttachKind.PHOTO, profile=True)
        self.assertNotIn('synthetic-secret', str(caught.exception))
        self.assertEqual(post.call_count, 1)


if __name__ == '__main__':
    unittest.main()
