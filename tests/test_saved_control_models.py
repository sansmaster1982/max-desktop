"""Actual CONTROL provenance and local welcome label; no account fixtures."""
import unittest

from maxclient.core.models import Attach, Message
from maxclient.core.service import _extract_chats
from maxclient.core.store import Store


KEY = 'welcome.saved.dialog.message'
FRIENDLY = 'Здесь можно сохранять сообщения и файлы.'


def message(*, text='', attaches=None):
    return Message.from_server(0, {'id': 7, 'type': 'USER', 'time': 0, 'text': text,
        'attaches': attaches if attaches is not None else [
            {'_type': 'CONTROL', 'event': 'system', 'message': KEY}]}, 101)


class SavedControlModelTests(unittest.TestCase):
    def test_actual_system_provenance_yields_shared_local_label_and_preview(self):
        for text in ('', KEY):
            model = message(text=text)
            self.assertTrue(model.attaches[0].control_metadata_confirmed)
            self.assertEqual(model.control_display_text, FRIENDLY)
            self.assertEqual(model.preview, FRIENDLY)

    def test_user_text_alone_or_unrelated_outer_text_never_translated(self):
        for model in (message(text=KEY, attaches=[]), message(text='Actual ordinary text')):
            self.assertIsNone(model.control_display_text)
            self.assertEqual(model.preview, model.text)

    def test_saved_welcome_uses_actual_system_caption_not_short_message(self):
        model = message(text=KEY, attaches=[{'_type': 'CONTROL', 'event': 'system',
            'message': 'Save something', 'shortMessage': 'Some other short text'}])
        self.assertEqual(model.control_display_text, 'Save something')
        self.assertEqual(model.preview, 'Save something')

    def test_caption_projection_requires_saved_chat_typed_system_and_nonempty_message(self):
        for changes in ({'message': ''}, {'message': '  '}, {'message': None},
                        {'event': 'hello'}, {'shortMessage': 123}, {'_type': None, 'type': 'CONTROL'}):
            raw = {'_type': 'CONTROL', 'event': 'system', 'message': 'Save something'}
            raw.update(changes)
            with self.subTest(changes=changes):
                self.assertIsNone(message(text=KEY, attaches=[raw]).control_display_text)
        outside = message(text=KEY, attaches=[{'_type': 'CONTROL', 'event': 'system',
            'message': 'Save something'}])
        outside.chat_id = 9
        self.assertIsNone(outside.control_display_text)

    def test_actual_type_and_event_fields_required_not_alias_or_message_type(self):
        for attach in ({'type': 'CONTROL', 'event': 'system', 'message': KEY},
                       {'_type': 'CONTROL', 'message': KEY},
                       {'_type': 'CONTROL', 'event': 'other', 'message': KEY},
                       {'_type': 'CONTROL', 'event': 'system', 'message': KEY, 'shortMessage': {}}):
            model = message(attaches=[attach])
            self.assertFalse(model.attaches[0].control_metadata_confirmed)
            self.assertIsNone(model.control_display_text)

    def test_first_control_wins_and_noncontrol_before_it_is_ignored(self):
        actual = {'_type': 'CONTROL', 'event': 'system', 'message': KEY}
        self.assertIsNone(message(attaches=[{'_type': 'CONTROL', 'event': 'system',
            'message': 'Different actual system text'}, actual]).control_display_text)
        self.assertEqual(message(attaches=[{'_type': 'PHOTO'}, actual]).control_display_text, FRIENDLY)

    def test_unknown_control_is_generic_label_without_fake_file_preview(self):
        model = message(attaches=[{'_type': 'CONTROL', 'event': 'system', 'message': 'Other'}])
        self.assertIsNone(model.control_display_text)
        self.assertEqual(model.preview, '[Системное сообщение]')

    def test_initial_chat_metadata_preview_uses_same_proven_gate(self):
        raw = {'id': 7, 'time': 0, 'type': 'USER', 'text': KEY,
               'attaches': [{'_type': 'CONTROL', 'event': 'system', 'message': KEY}]}
        self.assertEqual(_extract_chats({'chats': [{'id': 0, 'type': 'DIALOG',
            'lastMessage': raw}]}, my_id=101)[0].last_preview, FRIENDLY)
        raw['attaches'] = []
        self.assertEqual(_extract_chats({'chats': [{'id': 0, 'type': 'DIALOG',
            'lastMessage': raw}]}, my_id=101)[0].last_preview, KEY)

    def test_provenance_roundtrips_and_legacy_cache_defaults_to_unproven(self):
        store = Store(':memory:')
        self.addCleanup(store.close)
        store.bind_owner(101)
        store.insert_message(message())
        actual = store.message(0, 7)
        self.assertEqual(actual.control_display_text, FRIENDLY)
        self.assertEqual(actual.attaches[0].control_message, KEY)
        legacy = Message(0, id=8, text=KEY, attaches=[Attach(type='CONTROL')])
        store.insert_message(legacy)
        cached = store.message(0, 8)
        self.assertFalse(cached.attaches[0].control_metadata_confirmed)
        self.assertIsNone(cached.control_display_text)
        self.assertEqual(cached.preview, KEY)
