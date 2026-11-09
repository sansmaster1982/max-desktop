"""Detached gallery metadata and stable attachment navigation; no downloads."""
import copy
import json
import math
import unittest

import test_conversation_info_service as fixture
from maxclient.core.conversation_info_service import ConversationUnavailable
from maxclient.protocol.conversation_contracts import (
    ConversationContractError, explicit_media_url, parse_info_media,
)


PHOTO_URL = 'https://cdn.example.test/actual-image?signature=synthetic'
VIDEO_THUMB = 'https://cdn.example.test/actual-video-thumbnail'


class ConversationMediaRowsTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture.ConversationInfoServiceTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.service, self.client, self.context = self.fixture.service, self.fixture.client, self.fixture.context
        self.before_sql = self.service.store._db.total_changes
        self.before_metadata = copy.deepcopy(self.service._server_chats)

    def tearDown(self):
        self.client.request.assert_not_called()
        self.assertEqual(self.service.store._db.total_changes, self.before_sql)
        self.assertEqual(self.service._server_chats, self.before_metadata)

    def page(self, attaches, tab='media', **message_fields):
        raw = {'messages': [fixture.message_dto(attaches=attaches, **message_fields)], 'total': 1}
        self.client.conversation_media.return_value = parse_info_media(raw)
        return self.service.load_conversation_tab(self.context, tab)

    def test_each_relevant_attachment_has_stable_distinct_key_and_source_order(self):
        page = self.page([
            {'_type': 'PHOTO', 'url': PHOTO_URL, 'baseUrl': 'https://unproved.example.test/base',
             'previewUrl': 'https://unproved.example.test/preview', 'width': 640, 'height': 480},
            {'_type': 'FILE', 'name': 'Other tab'},
            {'_type': 'VIDEO', 'thumbnail': VIDEO_THUMB, 'width': 1280, 'height': 720,
             'duration': 1500.25, 'size': 42, 'MP4_1080': 'https://unproved.example.test/stream',
             'embedUrl': 'https://unproved.example.test/embed'}], timestamp=.25)
        rows = page['items']
        self.assertEqual([row['kind'] for row in rows], ['VIDEO', 'PHOTO'])
        self.assertEqual([row['attachment_index'] for row in rows], [2, 0])
        self.assertEqual([row['key'] for row in rows], ['attachment:303:7:2', 'attachment:303:7:0'])
        self.assertEqual(rows[0]['duration_ms'], 1500.25)
        self.assertEqual((rows[0]['width'], rows[0]['height'], rows[0]['size']), (1280, 720, 42))
        self.assertEqual(rows[0]['thumbnail_url'], VIDEO_THUMB)
        self.assertNotIn('open_url', rows[0])
        self.assertEqual(rows[1]['thumbnail_url'], PHOTO_URL)
        self.assertEqual(rows[1]['open_url'], PHOTO_URL)
        for row in rows:
            self.assertEqual((row['chat_id'], row['message_id'], row['time_ms']), (303, 7, .25))
            self.assertEqual((row['action'], row['available']), ('message', True))
        self.assertNotIn('unproved.example', json.dumps(rows))

    def test_files_have_separate_literal_names_and_only_actual_image_previews(self):
        rows = self.page([
            {'_type': 'FILE', 'name': '<b>Literal name</b>', 'size': 12.5, 'fileId': 88,
             'token': 'synthetic-secret', 'url': PHOTO_URL, 'preview': {'_type': 'PHOTO', 'url': PHOTO_URL}},
            {'_type': 'FILE', 'name': 'Second', 'size': 0,
             'preview': {'_type': 'VIDEO', 'thumbnail': VIDEO_THUMB}}], tab='files')['items']
        self.assertEqual([row['title'] for row in rows], ['<b>Literal name</b>', 'Second'])
        self.assertEqual([row['attachment_index'] for row in rows], [0, 1])
        self.assertEqual([row['size'] for row in rows], [12.5, 0])
        self.assertEqual([row['thumbnail_url'] for row in rows], [PHOTO_URL, VIDEO_THUMB])
        self.assertTrue(all('open_url' not in row for row in rows))
        self.assertNotIn('synthetic-secret', json.dumps(rows))
        self.assertNotIn('fileId', json.dumps(rows))

    def test_audio_wire_duration_is_milliseconds_and_no_guessed_stream(self):
        rows = self.page([{'_type': 'AUDIO', 'audioId': 88, 'duration': 0, 'url': PHOTO_URL},
                          {'_type': 'AUDIO', 'audioId': 89, 'duration': 1500}], tab='voice')['items']
        self.assertEqual([row['duration_ms'] for row in rows], [0, 1500])
        self.assertTrue(all(row['kind'] == 'AUDIO' and 'open_url' not in row for row in rows))
        self.assertTrue(all('thumbnail_url' not in row for row in rows))

    def test_share_image_is_thumbnail_not_an_unchecked_external_open(self):
        rows = self.page([{'_type': 'SHARE', 'shareId': 88, 'title': '<i>Literal title</i>',
                           'url': 'https://external.example.test/page', 'image': {'_type': 'PHOTO', 'url': PHOTO_URL}}],
                         tab='links')['items']
        self.assertEqual(rows[0]['title'], '<i>Literal title</i>')
        self.assertEqual(rows[0]['thumbnail_url'], PHOTO_URL)
        self.assertNotIn('open_url', rows[0])

    def test_forward_uses_nested_attachments_with_outer_navigation_identity(self):
        nested = fixture.message_dto(identity=999, timestamp=9.5,
                                      attaches=[{'_type': 'PHOTO', 'url': PHOTO_URL}])
        rows = self.page([{'_type': 'PHOTO', 'url': 'https://outer.example.test/ignored'}],
                         timestamp=.25, link={'type': 'FORWARD', 'chatId': 808, 'message': nested})['items']
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['attachment_source'], 'forward')
        self.assertEqual((rows[0]['chat_id'], rows[0]['message_id'], rows[0]['time_ms']), (303, 7, .25))
        self.assertEqual(rows[0]['open_url'], PHOTO_URL)
        self.assertNotIn('outer.example', json.dumps(rows))

    def test_deeper_forward_without_first_level_attachments_is_navigation_only(self):
        inner = fixture.message_dto(identity=999, link={'type': 'FORWARD', 'chatId': 909,
                  'message': fixture.message_dto(attaches=[{'_type': 'PHOTO', 'url': PHOTO_URL}])})
        rows = self.page([{'_type': 'PHOTO', 'url': PHOTO_URL}],
                         link={'type': 'FORWARD', 'chatId': 808, 'message': inner})['items']
        self.assertEqual(len(rows), 1)
        self.assertNotIn('kind', rows[0])
        self.assertNotIn('open_url', rows[0])
        self.assertEqual(rows[0]['action'], 'message')

    def test_expanded_attachment_count_does_not_change_message_pagination(self):
        raw = {'total': 100, 'messages': [fixture.message_dto(1000-i, fixture.STAMP-30+i,
            attaches=[{'_type': 'PHOTO'}, {'_type': 'PHOTO'}]) for i in range(25)]}
        self.client.conversation_media.return_value = parse_info_media(raw)
        page = self.service.load_conversation_tab(self.context, 'media')
        self.assertEqual(len(page['items']), 50)
        self.assertEqual(page['marker'], 1000)
        self.assertFalse(page['complete'])
        self.assertEqual(len({row['key'] for row in page['items']}), 50)

    def test_missing_or_unknown_attachment_remains_real_message_not_fake_media(self):
        for attaches in ([], [{'_type': 'OTHER', 'token': 'synthetic-secret'}]):
            row = self.page(attaches)['items'][0]
            self.assertEqual(row['key'], 'message:303:7')
            self.assertNotIn('kind', row)
            self.assertNotIn('thumbnail_url', row)
            self.assertNotIn('open_url', row)

    def test_url_validation_rejects_unsafe_or_unproven_origins_without_construction(self):
        urls = ['http://cdn.example.test/x', 'file:///tmp/private', 'data:image/png,secret',
                'javascript:alert(1)', 'https://user:pass@cdn.example.test/x', 'https://127.0.0.1/x',
                'https://[::1]/x', 'https://localhost/x', 'https://a.local/x',
                'https://cdn.example.test:444/x', 'https://cdn.example.test/x#fragment',
                'https://cdn.example.test/\nsecret', 'https://cdn.example.test\\@other.example.test/x']
        for url in urls:
            with self.subTest(url=url):
                self.assertIsNone(explicit_media_url(url))
                row = self.page([{'_type': 'PHOTO', 'url': url, 'baseUrl': PHOTO_URL,
                                  'photoToken': 'synthetic-secret'}])['items'][0]
                self.assertNotIn('open_url', row)
                self.assertNotIn('thumbnail_url', row)
        self.assertEqual(explicit_media_url(PHOTO_URL), PHOTO_URL)

    def test_deleted_media_never_exposes_downloadable_urls(self):
        rows = self.page([{'_type': 'PHOTO', 'url': PHOTO_URL, 'deleted': True},
                          {'_type': 'VIDEO', 'thumbnail': VIDEO_THUMB, 'deleted': True}])['items']
        self.assertTrue(all(row['deleted'] is True for row in rows))
        self.assertTrue(all('open_url' not in row and 'thumbnail_url' not in row for row in rows))

    def test_projected_rows_are_detached_and_malformed_forward_is_not_outer_fallback(self):
        raw = {'total': 1, 'messages': [fixture.message_dto(attaches=[{'_type': 'PHOTO', 'width': 640}])]}
        data = parse_info_media(raw)
        self.client.conversation_media.return_value = data
        self.service.load_conversation_tab(self.context, 'media')['items'][0]['width'] = 1
        self.assertEqual(data['messages'][0]['attaches'][0]['width'], 640)
        for link in ({'type': 'FORWARD', 'chatId': True, 'message': fixture.message_dto()},
                     {'type': 'FORWARD', 'chatId': 808, 'message': {'id': 7}}):
            with self.assertRaises(ConversationContractError):
                parse_info_media({'total': 1, 'messages': [fixture.message_dto(link=link)]})

    def test_usable_dimensions_duration_and_size_do_not_accept_boolean_or_nonfinite(self):
        for attach in ({'_type': 'PHOTO', 'width': True}, {'_type': 'VIDEO', 'height': -1},
                       {'_type': 'VIDEO', 'duration': math.nan}, {'_type': 'FILE', 'size': -1},
                       {'_type': 'AUDIO', 'duration': True}):
            self.client.conversation_media.return_value = {'total': 1,
                'messages': [fixture.message_dto(attaches=[attach])]}
            tab = {'FILE': 'files', 'AUDIO': 'voice'}.get(attach['_type'], 'media')
            with self.subTest(attach=attach), self.assertRaises(ConversationUnavailable):
                self.service.load_conversation_tab(self.context, tab)

    def test_excessive_expansion_is_bounded_before_rendering(self):
        self.client.conversation_media.return_value = {'total': 5, 'messages': [fixture.message_dto(i,
            attaches=[{'_type': 'PHOTO'} for _ in range(1000)]) for i in range(5)]}
        with self.assertRaises(ConversationUnavailable): self.service.load_conversation_tab(self.context, 'media')


if __name__ == '__main__':
    unittest.main()
