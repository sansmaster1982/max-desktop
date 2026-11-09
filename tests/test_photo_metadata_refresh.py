"""In-memory legacy PHOTO deletion refresh; no external resources."""
import copy
import unittest

from maxclient.core.models import Message
from maxclient.core.store import Store


class PhotoMetadataRefreshTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(':memory:')
        self.addCleanup(self.store.close)
        self.store.bind_owner(101)
        self.raw = {'id': 91, 'time': 100, 'sender': 7, 'type': 'USER', 'text': 'Synthetic caption',
                    'updateTime': 50, 'attaches': [{'_type': 'PHOTO', 'baseUrl': 'https://photos.example.test/image',
                                                   'width': 600, 'height': 400, 'deleted': True},
                                                  {'_type': 'FILE', 'fileId': 32, 'name': 'Synthetic.txt'}]}
        old = Message.from_server(42, self.raw, 101)
        old.attaches[0].deleted = False  # Legacy parser discarded PHOTO.deleted.
        self.store.insert_message(old)

    def test_same_version_repairs_only_deletion_and_never_resurrects(self):
        previous_file = copy.deepcopy(self.store.message(42, 91).attaches[1])
        self.store.refresh_message_photo_metadata(42, self.raw)
        current = self.store.message(42, 91)
        self.assertTrue(current.attaches[0].deleted)
        self.assertEqual(current.attaches[1], previous_file)
        self.raw['attaches'][0]['deleted'] = False
        self.store.refresh_message_photo_metadata(42, self.raw)
        self.assertTrue(self.store.message(42, 91).attaches[0].deleted)

    def test_stale_different_url_text_or_index_cannot_patch_current_row(self):
        cases = []
        for key, value in [('updateTime', 49), ('text', 'Changed'), ('sender', 8), ('time', 101), ('status', 'REMOVED')]:
            raw = copy.deepcopy(self.raw)
            raw[key] = value
            cases.append(raw)
        raw = copy.deepcopy(self.raw)
        raw['attaches'][0]['baseUrl'] += '/changed'
        cases.append(raw)
        raw = copy.deepcopy(self.raw)
        raw['attaches'].reverse()
        cases.append(raw)
        for raw in cases:
            self.store.refresh_message_photo_metadata(42, raw)
            self.assertFalse(self.store.message(42, 91).attaches[0].deleted)

    def test_boolean_id_or_nonboolean_flag_is_not_deletion_proof(self):
        for value in (0, None, 'true', 1):
            raw = copy.deepcopy(self.raw)
            raw['attaches'][0]['deleted'] = value
            self.store.refresh_message_photo_metadata(42, raw)
            self.assertFalse(self.store.message(42, 91).attaches[0].deleted)
        raw = copy.deepcopy(self.raw)
        raw['id'] = True
        self.store.refresh_message_photo_metadata(42, raw)
        self.assertFalse(self.store.message(42, 91).attaches[0].deleted)
