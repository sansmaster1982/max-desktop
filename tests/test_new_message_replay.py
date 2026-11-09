"""Same-ID message replay freshness and legacy migration; synthetic data only."""
import sqlite3
import unittest
from unittest.mock import patch

import test_history_reconnect_review as fixture
from maxclient.core.models import Message
from maxclient.core.store import Store, _SCHEMA


class NewMessageReplayTests(unittest.TestCase):
    def setUp(self):
        self.case = fixture.HistoryReconnectReviewTests()
        self.case.setUp()
        self.addCleanup(self.case.tearDown)
        self.service, self.store = self.case.service, self.case.store

    def test_older_or_unversioned_replay_cannot_overwrite_known_newer_content(self):
        newest = self.case.raw(timestamp=fixture.STAMP + .25, text='Exact newer synthetic text')
        newest.update(updateTime=200.5, status='EDITED')
        self.service._on_push(self.case.push(newest))
        for version in (100.25, None):
            replay = self.case.raw(timestamp=fixture.STAMP, sender=101, text='Older synthetic replay')
            if version is not None:
                replay['updateTime'] = version
            self.service._on_push(self.case.push(replay))
            current = self.store.message(303, 7)
            self.assertEqual((current.text, current.time_ms, current.sender, current.outgoing,
                              current.update_time_ms),
                             (newest['text'], newest['time'], 202, False, 200.5))
            last = self.service._server_chats[303]['lastMessage']
            self.assertEqual((last['text'], last['updateTime']), (newest['text'], 200.5))
        self.assertEqual(len(self.store.messages(303)), 1)
        self.assertEqual(self.store.get_chat(303).unread, 6)
        self.assertEqual(self.service.on_message.call_count, 1)
        # ACK status/local identity is independent of content/version freshness.
        self.store.update_message_by_local_id('not-a-row', status='unknown')
        confirmed = self.store.apply_message_ack(303, {'id': 7}, my_id=101, local_id='confirmed-local')
        self.assertEqual((confirmed.status, confirmed.local_id, confirmed.text, confirmed.update_time_ms),
                         ('sent', 'confirmed-local', newest['text'], 200.5))
        # An explicit request ACK can supply fields without updateTime; it is
        # not an unversioned replay and retains the known version watermark.
        edited = self.store.apply_message_ack(303, {'id': 7, 'text': 'Explicit ACK text'}, my_id=101)
        self.assertEqual((edited.text, edited.update_time_ms), ('Explicit ACK text', 200.5))

    def test_omitted_sender_replay_keeps_outgoing_and_explicit_sender_is_applied(self):
        own = self.case.raw(sender=101)
        own['updateTime'] = 200.25
        self.service._on_push(self.case.push(own))
        for with_version in (True, False):
            replay = dict(own)
            replay.pop('sender')
            if not with_version:
                replay.pop('updateTime')
            self.service._on_push(self.case.push(replay))
            current = self.store.message(303, 7)
            self.assertEqual((current.sender, current.outgoing), (101, True))
        explicit = dict(own, sender=202, updateTime=300.25)
        self.service._on_push(self.case.push(explicit))
        current = self.store.message(303, 7)
        self.assertEqual((current.sender, current.outgoing, current.update_time_ms), (202, False, 300.25))
        self.assertEqual(len(self.store.messages(303)), 1)
        self.assertEqual(self.service.on_message.call_count, 1)
        self.assertEqual(self.store.get_chat(303).unread, 5)

    def test_stale_explicit_ack_can_disable_same_file_without_content_rollback(self):
        newest = self.case.raw(text='Known newer literal text')
        newest.update(updateTime=200, attaches=[{'_type': 'FILE', 'fileId': 25,
            'name': 'Current.bin', 'size': 1, 'deleted': False}])
        self.service._on_push(self.case.push(newest))
        changed = self.store.apply_message_ack(303, {'id': 7, 'updateTime': 100,
            'sender': 101, 'text': 'Stale text', 'time': 0,
            'attaches': [{'_type': 'FILE', 'fileId': 25, 'deleted': True}]}, my_id=101)
        self.assertEqual((changed.text, changed.sender, changed.outgoing, changed.time_ms,
                          changed.update_time_ms), (newest['text'], 202, False, newest['time'], 200))
        file = changed.attaches[0]
        self.assertTrue(file.deleted)
        self.assertEqual((file.file_id, file.file_name, file.file_update_time), (25, 'Current.bin', 200))
        older = dict(newest, updateTime=150)
        self.service._cache_history_messages(303, [older])
        self.assertTrue(self.store.message(303, 7).attaches[0].deleted)
        # A legacy FILE can lack its own version even after an ID-only ACK
        # establishes the message's version; its invalidation must keep that.
        legacy = self.case.raw(identity=8, text='Legacy FILE')
        legacy['attaches'] = [{'_type': 'FILE', 'fileId': 26, 'name': 'Legacy.bin'}]
        self.service._on_push(self.case.push(legacy))
        self.store.apply_message_ack(303, {'id': 8, 'updateTime': 200}, my_id=101)
        restricted = self.store.apply_message_ack(303, {'id': 8, 'updateTime': 100,
            'attaches': [{'_type': 'FILE', 'fileId': 26, 'deleted': True}]}, my_id=101)
        self.assertEqual(restricted.attaches[0].file_update_time, 200)
        self.service._cache_history_messages(303, [dict(legacy, updateTime=150)])
        self.assertTrue(self.store.message(303, 8).attaches[0].deleted)


class LegacyMessageVersionMigrationTests(unittest.TestCase):
    def test_legacy_schema_adds_nullable_version_without_losing_zero_ids_or_ack_state(self):
        connection = sqlite3.connect(':memory:', check_same_thread=False)
        legacy = _SCHEMA.replace('link_json TEXT,\n    update_time_ms INTEGER', 'link_json TEXT')
        self.assertNotIn('update_time_ms', legacy)
        connection.executescript(legacy)
        connection.execute(f'PRAGMA user_version={Store._CACHE_VERSION}')
        connection.execute('INSERT INTO messages(chat_id,server_id,sender,text,time_ms,outgoing,status,local_id) '
                           'VALUES(0,0,101,?,0,1,?,?)',
                           ('Legacy exact text', 'unknown', 'legacy-local'))
        connection.commit()
        with patch('maxclient.core.store.sqlite3.connect', return_value=connection):
            store = Store(':memory:')
        self.addCleanup(store.close)
        current = store.message(0, 0)
        self.assertEqual((current.id, current.chat_id, current.time_ms, current.text,
                          current.status, current.local_id, current.outgoing, current.update_time_ms),
                         (0, 0, 0, 'Legacy exact text', 'unknown', 'legacy-local', True, None))
        confirmed = store.apply_message_ack(0, {'id': 0}, my_id=101, local_id='legacy-local')
        self.assertEqual((confirmed.status, confirmed.text, confirmed.outgoing, confirmed.update_time_ms),
                         ('sent', 'Legacy exact text', True, None))
        for identity, version in ((1, 0), (2, 200.25)):
            message = Message.from_server(0, {'id': identity, 'type': 'USER', 'time': 0,
                'sender': 101, 'text': 'Synthetic', 'updateTime': version}, 101)
            store.insert_message(message)
            self.assertEqual(store.message(0, identity).update_time_ms, version)
