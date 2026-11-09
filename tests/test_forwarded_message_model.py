"""First-level received snapshot, cache freshness, and exact FILE capability."""
import copy
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

import test_timeline_file_service as fixture
from maxclient.core.models import Message, forwarded_to_data
from maxclient.core.store import Store


def incoming(version=100, **outer):
    raw = fixture.raw_message(updateTime=version)
    raw['attaches'] = []
    raw['text'] = ''
    raw['link'] = {'type': 'FORWARD', 'chatId': 808, 'chatName': 'Synthetic origin',
        'message': {'id': 70, 'time': 123.5, 'sender': 101, 'type': 'USER',
                    'text': 'Nested original', 'attaches': [{'_type': 'FILE', 'fileId': 0,
                    'name': 'nested.txt', 'size': 12, 'url': 'https://invalid.example/private',
                    'token': 'synthetic-not-persisted'}]}}
    raw.update(outer)
    return raw


class ForwardedMessageModelTests(unittest.TestCase):
    def setUp(self):
        self.case = fixture.TimelineFileServiceTests()
        self.case.setUp()
        self.service, self.store = self.case.service, self.case.store

    def tearDown(self):
        self.case.tearDown()
        self.case.doCleanups()

    def load(self, raw=None):
        return self.case.load(raw or incoming())

    def test_snapshot_roundtrip_preserves_projection_without_opaque_fields(self):
        message = self.load()
        restored = self.store.message(303, 7)
        self.assertEqual(message, restored)
        self.assertEqual(restored.display_source, 'forward')
        self.assertEqual((restored.forwarded.source_chat_id, restored.forwarded.source_message_id), (808, 70))
        data = json.dumps(forwarded_to_data(restored.forwarded))
        self.assertNotIn('synthetic-not-persisted', data)
        self.assertNotIn('https://', data)
        self.assertIsNone(restored.link)  # Incoming snapshot is not an outbound reference.
        self.assertEqual(self.case.descriptor(restored).file_id, 0)

    def test_legacy_database_migration_preserves_rows_and_new_projection_across_reopen(self):
        with tempfile.TemporaryDirectory(prefix='forward-cache-') as folder:
            path = Path(folder) / 'cache.sqlite'
            initial = Store(path)
            initial.insert_message(Message(chat_id=303, id=1, text='Legacy row'))
            initial.close()
            db = sqlite3.connect(path)
            db.execute('ALTER TABLE messages DROP COLUMN forwarded_json')
            db.commit(); db.close()
            migrated = Store(path)
            self.assertEqual(migrated.message(303, 1).text, 'Legacy row')
            expected = Message.from_server(303, incoming(), 101)
            migrated.insert_message(expected); migrated.close()
            reopened = Store(path)
            try:
                self.assertEqual(reopened.message(303, 7), expected)
                self.assertEqual(reopened.message(303, 1).text, 'Legacy row')
            finally:
                reopened.close()

    def test_actual_forward_only_history_inserts_visible_message(self):
        self.service._cache_history_messages(303, [incoming()])
        self.assertEqual(self.store.message(303, 7).display_text, 'Nested original')
        self.assertEqual(self.store.get_chat(303).last_preview, 'Nested original')

    def test_older_push_and_history_cannot_replace_newer_nested_content(self):
        self.load()
        newer = incoming(200)
        newer['link']['message']['text'] = 'Newer content'
        newer['link']['message']['attaches'][0]['deleted'] = True
        self.store.apply_message_ack(303, newer, replay=True)
        stale = incoming(100)
        self.store.apply_message_ack(303, stale, replay=True)
        self.service._cache_history_messages(303, [stale])
        current = self.store.message(303, 7)
        self.assertEqual(current.display_text, 'Newer content')
        self.assertTrue(current.display_attaches[0].deleted)
        self.assertEqual(self.store.get_chat(303).last_preview, 'Newer content')
        self.assertFalse(self.service.timeline_file_actions(self.case.context, current)[0]['download_available'])

    def test_partial_ack_preserves_snapshot_and_outer_schedule_changes_revoke_descriptor(self):
        message = self.load()
        descriptor = self.case.descriptor(message)
        current = self.store.apply_message_ack(303, {'id': 7, 'reactionInfo': {'totalCount': 0}})
        self.assertEqual(current.forwarded, message.forwarded)
        current = self.store.apply_message_ack(303, {'id': 7, 'delayedAttributes': {'timeToFire': 800}})
        self.assertEqual(current.display_attaches[0].file_item_type, 'DELAYED')
        self.assertFalse(self.service.is_current_conversation_file(self.case.context, descriptor))
        self.assertEqual(self.case.descriptor(current).item_type, 'DELAYED')

    def test_same_version_cannot_reenable_deleted_nested_file_or_admit_a_replacement(self):
        raw = incoming(200)
        raw['link']['message']['attaches'][0]['deleted'] = True
        self.load(raw)
        restored = self.store.apply_message_ack(303, incoming(200), replay=True)
        self.assertTrue(restored.display_attaches[0].deleted)
        replacement = incoming(200)
        replacement['link']['message']['attaches'][0]['fileId'] = 77
        current = self.store.apply_message_ack(303, replacement, replay=True)
        self.assertFalse(current.display_attaches[0].file_metadata_confirmed)
        replacement['updateTime'] = 300
        current = self.store.apply_message_ack(303, replacement, replay=True)
        self.assertTrue(current.display_attaches[0].file_metadata_confirmed)

    def test_old_explicit_ack_can_only_restrict_matching_file_without_content_rollback(self):
        self.load(incoming(200))
        older = incoming(100)
        older['link']['message']['text'] = 'Stale text'
        older['link']['message']['attaches'][0]['deleted'] = True
        current = self.store.apply_message_ack(303, older)
        self.assertEqual(current.display_text, 'Nested original')
        self.assertTrue(current.display_attaches[0].deleted)
        self.assertEqual(current.update_time_ms, 200)

    def test_undated_repeat_cannot_restore_known_deleted_nested_file(self):
        raw = incoming(); raw.pop('updateTime')
        raw['link']['message']['attaches'][0]['deleted'] = True
        self.load(raw)
        raw['link']['message']['attaches'][0]['deleted'] = False
        current = self.store.apply_message_ack(303, raw, replay=True)
        self.assertTrue(current.display_attaches[0].deleted)

    def test_reference_ack_only_preserves_the_same_proven_origin(self):
        message = self.load()
        ref = {'type': 'FORWARD', 'chatId': 808, 'messageId': 70}
        current = self.store.apply_message_ack(303, {'id': 7, 'link': ref})
        self.assertEqual(current.forwarded, message.forwarded)
        current = self.store.apply_message_ack(303, {'id': 7, 'link': {**ref, 'messageId': 71}})
        self.assertEqual(current.display_source, 'unavailable_forward')
        self.assertEqual(self.service.timeline_file_actions(self.case.context, current), {})

    def test_malformed_explicit_snapshot_does_not_reuse_reference_permission(self):
        self.load()
        ref = {'type': 'FORWARD', 'chatId': 808, 'messageId': 70, 'message': None}
        current = self.store.apply_message_ack(303, {'id': 7, 'link': ref})
        self.assertEqual(current.display_source, 'unavailable_forward')
        with self.assertRaises(ValueError):
            self.case.descriptor(current)

    def test_source_id_collision_is_not_an_outer_attachment_selection(self):
        raw = incoming()
        raw['attaches'] = [{'_type': 'FILE', 'fileId': 0, 'name': 'different-outer.txt'}]
        current = self.load(raw)
        self.assertEqual(current.display_attaches[0].file_name, 'nested.txt')
        self.assertFalse(self.service.timeline_file_actions(self.case.context, current)[0]['download_available'])
        with self.assertRaises(ValueError):
            self.case.descriptor(current)

    def test_corrupt_forward_cache_never_falls_back_to_outer_download(self):
        raw = incoming()
        raw['attaches'] = [{'_type': 'FILE', 'fileId': 99, 'name': 'outer.txt'}]
        self.load(raw)
        for bad in ('{', '{}', '{"version":true}', '{"available":true}'):
            self.store._db.execute('UPDATE messages SET forwarded_json=? WHERE server_id=7', (bad,))
            current = self.store.message(303, 7)
            self.assertEqual(current.display_source, 'unavailable_forward')
            self.assertEqual(self.service.timeline_file_actions(self.case.context, current), {})

    def test_deleted_outer_message_cannot_regain_forward_file_permission(self):
        self.load()
        self.store.apply_message_ack(303, {'id': 7, 'status': 'REMOVED', 'updateTime': 200})
        current = self.store.apply_message_ack(303, incoming(300), replay=True)
        self.assertEqual(current.status, 'removed')
        self.assertFalse(current.display_attaches[0].file_metadata_confirmed)
        with self.assertRaises(ValueError):
            self.case.descriptor(current)

    def test_old_reference_can_only_be_enriched_by_matching_full_history(self):
        raw = incoming()
        legacy = {**raw, 'link': {'type': 'FORWARD', 'chatId': 808, 'messageId': 70}}
        old = Message.from_server(303, legacy, 101)
        old.forwarded = None  # Actual pre-feature cache shape.
        self.store.insert_message(old)
        wrong = copy.deepcopy(raw); wrong['link']['message']['id'] = 71
        self.service._cache_history_messages(303, [wrong])
        self.assertEqual(self.store.message(303, 7).display_source, 'unavailable_forward')
        self.service._cache_history_messages(303, [raw])
        self.assertEqual(self.store.message(303, 7).display_source, 'forward')

    def test_invalid_thread_depth_ids_and_files_never_authorize(self):
        variants = []
        for patch in ({'postId': 0}, {'chatId': True}, {'message': None}):
            raw = incoming(); raw['link'].update(patch); variants.append(raw)
        raw = incoming(); raw['link']['message']['link'] = incoming()['link']; variants.append(raw)
        for file_id in (None, True, '0', float('nan')):
            raw = incoming(); raw['link']['message']['attaches'][0]['fileId'] = file_id; variants.append(raw)
        for i, raw in enumerate(variants):
            raw['id'] = 10 + i
            message = self.load(raw)
            self.assertFalse(any(action['download_available'] for action in
                self.service.timeline_file_actions(self.case.context, message).values()))
        self.case.client.conversation_file_download.assert_not_called()


if __name__ == '__main__':
    unittest.main()
