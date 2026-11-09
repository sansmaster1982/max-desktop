"""Saved preview projection with exact cached provenance; all data synthetic."""
import copy
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from maxclient.core.models import Chat, Message
from maxclient.core.service import MaxService, _extract_chats
from maxclient.core.store import Store
from maxclient.protocol.client import MaxFrame

KEY = 'welcome.saved.dialog.message'
FRIENDLY = 'Здесь можно сохранять сообщения и файлы.'


def welcome(**changes):
    raw = {'id': 7, 'type': 'USER', 'time': 100, 'sender': 101, 'text': KEY,
           'attaches': [{'_type': 'CONTROL', 'event': 'system', 'message': KEY}]}
    raw.update(changes)
    return raw


def thin(**changes):
    raw = welcome()
    del raw['attaches']
    raw.update(changes)
    return raw


def record(raw, **changes):
    data = {'id': 0, 'type': 'DIALOG', 'lastMessage': raw}
    data.update(changes)
    return data


class SavedPreviewCacheTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(':memory:')
        self.addCleanup(self.store.close)
        self.store.bind_owner(101)
        self.service = MaxService.__new__(MaxService)
        self.service.store = self.store
        self.service.session = SimpleNamespace(my_user_id=101)
        self.service._metadata_lock = threading.RLock()
        self.service._server_metadata_owner = 101
        self.service._server_chats = {}
        self.service._server_contacts = {}
        self.service._contacts_cache = None
        self.service.client = SimpleNamespace(set_peer=Mock(), contact_by_phone=Mock(
            side_effect=AssertionError('Preview must not request a contact or history')))
        self.store.upsert_chat(Chat(id=0, chat_type='DIALOG', title='Избранное'))

    def seed(self, **changes):
        self.service._cache_history_messages(0, [welcome(**changes)])

    def ingest(self, raw, **changes):
        self.service._remember_chat_info({'chats': [record(raw, **changes)]}, my_id=101)

    def preview(self):
        return next(chat.last_preview for chat in self.service.list_chats() if chat.id == 0)

    def test_partial_login_preserves_history_control_label_without_changing_message(self):
        self.seed()
        before = self.store.message(0, 7)
        self.service._ingest_login(MaxFrame(1, 1, 19, b'', {'chats': [record(thin())]}), my_id=101)
        self.assertEqual(self.store.get_chat(0).last_preview, FRIENDLY)
        self.assertEqual(self.preview(), FRIENDLY)
        self.assertEqual(self.store.message(0, 7), before)

    def test_list_repairs_flat_preview_read_only_with_exact_current_metadata(self):
        self.seed()
        self.service._server_chats[0] = record(thin())
        self.store.update_chat_preview(0, 100, KEY)
        changes = self.store._db.total_changes
        self.assertEqual(self.preview(), FRIENDLY)
        self.assertEqual(self.store.get_chat(0).last_preview, KEY)
        self.assertEqual(self.store._db.total_changes, changes)

    def test_chat_push_135_preserves_proof_from_history(self):
        self.seed()
        self.service._apply_account_push(MaxFrame(0, 1, 135, b'', {'chat': record(thin())}),
                                         (101, self.service.client, object()))
        self.assertEqual(self.store.get_chat(0).last_preview, FRIENDLY)

    def test_message_push_and_partial_replay_preserve_cached_label(self):
        binding = (101, self.service.client, object())
        for raw in (welcome(), thin()):
            self.service._apply_account_push(MaxFrame(0, 1, 128, b'',
                {'chatId': 0, 'message': raw}), binding)
        self.assertEqual(self.store.get_chat(0).last_preview, FRIENDLY)
        self.assertEqual(len(self.store.messages(0)), 1)

    def test_same_timestamp_different_message_never_inherits_label(self):
        self.seed()
        self.ingest(thin(id=8))
        self.assertEqual(self.preview(), KEY)

    def test_history_user_text_at_same_time_is_not_replaced_by_old_metadata(self):
        self.seed()
        self.service._server_chats[0] = record(thin())
        self.service._cache_history_messages(0, [welcome(id=8, attaches=[])])
        self.assertEqual(self.store.message(0, 8).preview, KEY)
        self.assertEqual(self.preview(), KEY)
        self.ingest(thin())
        self.assertEqual(self.preview(), KEY)

    def test_any_second_row_at_timestamp_makes_cached_anchor_ambiguous(self):
        self.seed()
        for status in ('sent', 'removed', 'pending', 'unknown'):
            with self.subTest(status=status):
                self.store._db.execute('DELETE FROM messages WHERE server_id=8 OR local_id=?', ('p',))
                self.store.insert_message(Message(0, id=8 if status != 'pending' else None,
                    local_id='p', time_ms=100, text='Different message', status=status))
                self.ingest(thin())
                self.assertEqual(self.preview(), KEY)

    def test_explicit_empty_alias_or_conflicting_attachments_do_not_inherit(self):
        self.seed()
        for changes in ({'attaches': []}, {'attachments': []}, {'attaches': None},
                        {'attaches': [{'_type': 'CONTROL', 'event': 'other', 'message': KEY}]}):
            with self.subTest(changes=changes):
                self.ingest(thin(**changes))
                self.assertEqual(self.preview(), KEY)

    def test_changed_text_sender_time_or_version_does_not_inherit(self):
        self.seed()
        for changes in ({'text': 'Ordinary text'}, {'sender': 202}, {'time': 101},
                        {'updateTime': 300}, {'type': 'CHANNEL'}, {'id': '7'},
                        {'sender': '101'}):
            with self.subTest(changes=changes):
                self.store.update_chat_preview(0, 100, KEY)
                self.ingest(thin(**changes))
                self.assertNotEqual(self.preview(), FRIENDLY)

    def test_versioned_cache_requires_same_explicit_version(self):
        self.seed(updateTime=200)
        for changes in ({}, {'updateTime': 100}, {'updateTime': 300}, {'updateTime': True}):
            with self.subTest(changes=changes):
                self.ingest(thin(**changes))
                self.assertEqual(self.preview(), KEY)
        self.ingest(thin(updateTime=200))
        self.assertEqual(self.preview(), FRIENDLY)

    def test_forward_marker_even_empty_cannot_inherit_outer_control(self):
        self.seed()
        for changes in ({'link': {'type': 'FORWARD'}}, {'link': None}, {'forwarded': {}}):
            with self.subTest(changes=changes):
                self.ingest(thin(**changes))
                self.assertEqual(self.preview(), KEY)

    def test_no_cache_or_unproven_user_message_remains_literal(self):
        self.ingest(thin())
        self.assertEqual(self.preview(), KEY)
        self.store.insert_message(Message.from_server(0, welcome(attaches=[]), 101))
        self.ingest(thin())
        self.assertEqual(self.preview(), KEY)

    def test_no_session_or_owner_mismatch_does_not_project(self):
        self.seed()
        for field, value in (('my_user_id', None), ('my_user_id', 202)):
            with self.subTest(value=value):
                setattr(self.service.session, field, value)
                self.ingest(thin())
                self.assertEqual(self.store.get_chat(0).last_preview, KEY)
                self.assertEqual(self.preview(), KEY)
        self.service.session.my_user_id = 101
        self.service._server_metadata_owner = 202
        self.ingest(thin())
        self.assertEqual(self.preview(), KEY)

    def test_store_owner_mismatch_does_not_project(self):
        self.seed()
        # Simulate the ownership guard without switching/purging the database.
        self.store._db.execute("UPDATE cache_metadata SET value='202' WHERE key='owner_id'")
        self.ingest(thin())
        self.assertEqual(self.preview(), KEY)

    def test_other_chat_or_unconfirmed_saved_type_does_not_project(self):
        self.seed()
        self.store.insert_message(Message.from_server(9, welcome(), 101))
        self.ingest(thin(), id=9)
        self.assertEqual(self.store.get_chat(9).last_preview, KEY)
        for chat_type in ('CHAT', 'CHANNEL', None):
            with self.subTest(chat_type=chat_type):
                self.ingest(thin(), type=chat_type)
                self.assertEqual(self.store.get_chat(0).last_preview, KEY)

    def test_removed_pending_or_unknown_cache_cannot_supply_proof(self):
        self.seed()
        for status in ('removed', 'pending', 'unknown'):
            with self.subTest(status=status):
                self.store._db.execute('UPDATE messages SET status=? WHERE chat_id=0', (status,))
                self.ingest(thin())
                self.assertEqual(self.preview(), KEY)

    def test_raw_tombstones_never_localize_full_or_thin_previews(self):
        self.seed()
        for change in ({'deleted': True}, {'removed': True}, {'status': 'REMOVED'}):
            for raw in (welcome(**change), thin(**change)):
                with self.subTest(change=change, full='attaches' in raw):
                    parsed = _extract_chats({'chats': [record(raw)]}, my_id=101)[0]
                    self.assertIsNone(parsed.last_preview)
                    self.ingest(raw)
                    self.assertIsNone(self.preview())

    def test_unavailable_metadata_or_newer_preview_cannot_project_old_row(self):
        self.seed()
        self.store.update_chat_preview(0, 101, KEY)
        for data in ({}, {'id': 0, 'type': 'DIALOG'}, record(thin())):
            with self.subTest(data=data):
                self.service._server_chats[0] = copy.deepcopy(data)
                self.assertEqual(self.preview(), KEY)

    def test_partial_older_message_does_not_roll_back_newer_preview(self):
        self.seed()
        self.store.update_chat_preview(0, 200, 'Actual newer message')
        self.ingest(thin())
        self.assertEqual(self.preview(), 'Actual newer message')

    def test_empty_outer_text_proven_control_still_gets_friendly_initial_preview(self):
        parsed = _extract_chats({'chats': [record(welcome(text=''))]}, my_id=101)[0]
        self.assertEqual(parsed.last_preview, FRIENDLY)

    def test_literal_control_caption_beats_outer_welcome_key_on_initial_and_partial_metadata(self):
        raw = welcome(attaches=[{'_type': 'CONTROL', 'event': 'system',
                                'message': 'Сохраните что-нибудь', 'shortMessage': 'Different short text'}])
        self.ingest(raw)
        self.assertEqual(self.preview(), 'Сохраните что-нибудь')
        self.service._cache_history_messages(0, [raw])
        self.ingest(thin())
        self.assertEqual(self.preview(), 'Сохраните что-нибудь')
