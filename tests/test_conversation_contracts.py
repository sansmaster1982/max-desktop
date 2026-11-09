"""Pure synthetic198/51 contracts; no network, files, media or account state."""
import unittest

from maxclient.protocol.conversation_contracts import (
    ConversationContractError, build_common_chats, parse_common_chats,
    build_info_media, parse_info_media,
)


def chat():
    return {'id': 303, 'type': 'CHAT', 'status': 'ACTIVE', 'owner': 101,
            'participants': {'101': 0}, 'lastEventTime': 1790000000000.25,
            'created': 0, 'title': '<b>Literal title</b>'}


def message(**values):
    return {'id': 7, 'type': 'USER', 'time': 1790000000000.25, 'text': 'Synthetic text', **values}


class ConversationContractsTests(unittest.TestCase):
    def test_common_request_uses_actual_peer_and_omits_unsupplied_fields(self):
        self.assertEqual(build_common_chats(202), {'userIds': [202]})
        self.assertEqual(build_common_chats(202, marker=0.25, count=3),
                         {'userIds': [202], 'marker': 0.25, 'count': 3})

    def test_common_invalid_ids_markers_and_counts_are_not_coerced(self):
        for bad in (True, '202', 2**63):
            with self.assertRaises(ConversationContractError): build_common_chats(bad)
        for bad in (True, float('nan'), float('inf'), '0'):
            with self.assertRaises(ConversationContractError): build_common_chats(202, marker=bad)
        for bad in (True, 0, -1, 1001, 1.5):
            with self.assertRaises(ConversationContractError): build_common_chats(202, count=bad)

    def test_common_page_preserves_real_order_marker_and_has_more_without_defaults(self):
        first, second = chat(), {**chat(), 'id': 304}
        page = parse_common_chats({'commonChats': [second, first], 'hasMore': True, 'marker': 0})
        self.assertEqual([item['id'] for item in page['commonChats']], [304, 303])
        self.assertEqual(page['marker'], 0)
        self.assertTrue(page['hasMore'])
        self.assertNotIn('marker', parse_common_chats({'commonChats': [], 'hasMore': True}))

    def test_common_nested_dto_requires_actual_identity_and_required_fields(self):
        for field in ('id', 'type', 'status', 'owner', 'participants', 'lastEventTime', 'created'):
            value = chat()
            del value[field]
            with self.subTest(field=field), self.assertRaises(ConversationContractError):
                parse_common_chats({'commonChats': [value], 'hasMore': False})
        for raw in ({'commonChats': []}, {'commonChats': [], 'hasMore': 0},
                    {'commonChats': [], 'hasMore': False, 'marker': float('nan')}):
            with self.assertRaises(ConversationContractError): parse_common_chats(raw)

    def test_common_projection_does_not_share_mutable_maps_or_copy_unknown_data(self):
        raw = chat()
        raw['privateUnknown'] = {'token': 'synthetic-secret'}
        page = parse_common_chats({'commonChats': [raw], 'hasMore': False})
        raw['participants']['101'] = 999
        self.assertEqual(page['commonChats'][0]['participants']['101'], 0)
        self.assertNotIn('privateUnknown', page['commonChats'][0])

    def test_media_exact_tab_filters_and_initial_continuation_counts(self):
        mapping = {'PHOTO_VIDEO': ['PHOTO', 'VIDEO'], 'AUDIO': ['AUDIO'], 'FILE': ['FILE'], 'SHARE': ['SHARE']}
        for tab, types in mapping.items():
            self.assertEqual(build_info_media(303, tab),
                             {'chatId': 303, 'attachTypes': types, 'backward': 25, 'forward': 25})
            self.assertEqual(build_info_media(303, tab, 7),
                             {'chatId': 303, 'messageId': 7, 'attachTypes': types, 'backward': 25, 'forward': 0})
            self.assertEqual(build_info_media(303, tab, 7, initial=True)['forward'], 25)
        self.assertNotIn('messageId', build_info_media(303, 'FILE'))
        self.assertNotIn('fileAttachmentTypes', build_info_media(303, 'FILE'))

    def test_media_builder_rejects_timestamp_cursor_guesses_and_unproven_tabs(self):
        for bad in (True, 7.25, '7', 2**63):
            with self.assertRaises(ConversationContractError): build_info_media(303, 'FILE', bad)
        for tab in ('file', 'MUSIC', 'VIDEO_MSG', None):
            with self.assertRaises(ConversationContractError): build_info_media(303, tab)
        with self.assertRaises(ConversationContractError): build_info_media(303, 'FILE', initial=1)

    def test_media_page_preserves_supplied_order_fraction_zero_and_optional_sender(self):
        page = parse_info_media({'messages': [message(id=8, time=0), message(id=7)], 'total': 2,
                                 'pos': 0, 'forward': 1.5, 'backward': 0})
        self.assertEqual([item['id'] for item in page['messages']], [8, 7])
        self.assertEqual([item['time'] for item in page['messages']], [0, 1790000000000.25])
        self.assertNotIn('sender', page['messages'][0])
        self.assertEqual((page['pos'], page['forward'], page['backward']), (0, 1.5, 0))
        self.assertNotIn('marker', page)

    def test_media_missing_boundaries_and_attachments_remain_omitted(self):
        page = parse_info_media({'messages': [message()], 'total': 1})
        self.assertEqual(set(page), {'messages', 'total'})
        self.assertNotIn('attaches', page['messages'][0])

    def test_media_core_identity_type_and_time_must_be_valid(self):
        for field in ('id', 'time', 'type'):
            raw = message()
            del raw[field]
            with self.subTest(field=field), self.assertRaises(ConversationContractError):
                parse_info_media({'messages': [raw], 'total': 1})
        for bad in (True, float('nan'), float('inf'), '123'):
            with self.assertRaises(ConversationContractError):
                parse_info_media({'messages': [message(time=bad)], 'total': 1})

    def test_media_response_counters_are_strict_and_never_inferred(self):
        for raw in ({'messages': []}, {'messages': [], 'total': True},
                    {'messages': [], 'total': 0, 'pos': '0'},
                    {'messages': [], 'total': 0, 'forward': float('nan')},
                    {'messages': [], 'total': 0, 'backward': False}):
            with self.assertRaises(ConversationContractError): parse_info_media(raw)

    def test_attachment_metadata_keeps_only_explicit_proven_image_url_and_no_tokens(self):
        attaches = [
            {'_type': 'PHOTO', 'photoToken': 'synthetic-secret', 'url': 'https://example.invalid/private'},
            {'_type': 'FILE', 'name': '<b>Literal filename</b>', 'size': 12.5, 'token': 'synthetic-secret'},
            {'_type': 'AUDIO', 'audioId': 88, 'duration': 2, 'url': 'https://example.invalid/private'},
            {'_type': 'VIDEO', 'duration': 2.5, 'size': 10, 'token': 'synthetic-secret'},
            {'_type': 'SHARE', 'shareId': 99, 'title': 'Actual title', 'url': 'https://example.invalid/private'},
            {'_type': 'OTHER', 'nested': {'token': 'synthetic-secret'}},
        ]
        page = parse_info_media({'messages': [message(attaches=attaches)], 'total': 1})
        projected = page['messages'][0]['attaches']
        self.assertEqual(projected[1], {'_type': 'FILE', 'name': '<b>Literal filename</b>', 'size': 12.5})
        self.assertEqual(projected[-1], {'_type': 'OTHER'})
        self.assertNotIn('synthetic-secret', repr(page))
        self.assertEqual(projected[0]['url'], 'https://example.invalid/private')
        self.assertNotIn('url', projected[2])  # AUDIO normal playback needs its ordinary source API.
        self.assertNotIn('url', projected[4])  # SHARE external-link safety flow is outside this stage.

    def test_attachment_supplied_metadata_is_validated_without_raw_error_leaks(self):
        invalid = [None, {'_type': True}, {'_type': 'AUDIO', 'duration': 2},
                   {'_type': 'FILE', 'size': float('nan')}, {'_type': 'FILE', 'name': []},
                   {'_type': 'VIDEO', 'duration': True}, {'_type': 'SHARE', 'title': 'synthetic-secret'}]
        for attach in invalid:
            with self.assertRaises(ConversationContractError) as caught:
                parse_info_media({'messages': [message(attaches=[attach])], 'total': 1})
            self.assertNotIn('synthetic-secret', str(caught.exception))


if __name__ == '__main__':
    unittest.main()
