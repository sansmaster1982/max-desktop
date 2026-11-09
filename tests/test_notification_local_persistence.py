"""Strict local delivery persistence; all files live in synthetic temp dirs."""
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from maxclient.core.config import Settings


class NotificationLocalPersistenceTests(unittest.TestCase):
    def setUp(self):
        temp_root = Path('.test-tmp').resolve()
        temp_root.mkdir(exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(prefix='max-notification-save-', dir=temp_root)
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'settings.json'
        self.settings = Settings(notifications=True, theme='dark')
        self.settings._path = self.path
        self.document = {'notifications': True, 'theme': 'light', 'locale': 'zz',
                         'future': {'nested': [1, 'synthetic', False]}}
        self.path.write_text(json.dumps(self.document), encoding='utf-8')
        self.original = self.path.read_bytes()

    def assert_unchanged(self):
        self.assertTrue(self.settings.notifications)
        self.assertEqual(self.path.read_bytes(), self.original)
        self.assertEqual(list(self.path.parent.glob('*.tmp')), [])

    def test_commit_only_delivery_preserves_unknown_fields_and_reload_false(self):
        self.settings.save_notifications(False)
        self.assertFalse(self.settings.notifications)
        expected = {**self.document, 'notifications': False}
        self.assertEqual(json.loads(self.path.read_text(encoding='utf-8')), expected)
        loaded = Settings.load(SimpleNamespace(settings_file=self.path, media_dir=self.path.parent))
        self.assertIs(loaded.notifications, False)
        self.assertEqual(list(self.path.parent.glob('*.tmp')), [])

    def test_memory_changes_only_after_atomic_replace(self):
        replace = Path.replace
        def commit(temporary, target):
            self.assertTrue(self.settings.notifications)
            self.assertEqual(self.path.read_bytes(), self.original)
            self.assertIs(json.loads(temporary.read_text(encoding='utf-8'))['notifications'], False)
            return replace(temporary, target)
        with patch.object(Path, 'replace', autospec=True, side_effect=commit) as replace_call:
            self.settings.save_notifications(False)
        replace_call.assert_called_once()
        self.assertFalse(self.settings.notifications)

    def test_replace_failure_preserves_file_memory_and_removes_temporary(self):
        with patch.object(Path, 'replace', side_effect=OSError('synthetic replace denied')):
            with self.assertRaises(OSError):
                self.settings.save_notifications(False)
        self.assert_unchanged()

    def test_fsync_failure_never_replaces_or_changes_memory(self):
        with patch('maxclient.core.config.os.fsync', side_effect=OSError('synthetic fsync failure')), \
                patch.object(Path, 'replace') as replace:
            with self.assertRaises(OSError):
                self.settings.save_notifications(False)
        replace.assert_not_called()
        self.assert_unchanged()

    def test_serialization_failure_preserves_existing_document(self):
        with patch('maxclient.core.config.json.dump', side_effect=OSError('synthetic write failure')):
            with self.assertRaises(OSError):
                self.settings.save_notifications(False)
        self.assert_unchanged()

    def test_existing_read_failure_does_not_create_new_settings(self):
        with patch.object(Path, 'read_text', side_effect=OSError('synthetic read denied')):
            with self.assertRaises(OSError):
                self.settings.save_notifications(False)
        self.assert_unchanged()

    def test_missing_file_can_persist_false_without_falsey_default(self):
        self.path.unlink()
        self.settings.save_notifications(False)
        data = json.loads(self.path.read_text(encoding='utf-8'))
        self.assertIs(data['notifications'], False)
        self.assertEqual(data['theme'], 'dark')
        self.assertNotIn('_path', data)
        loaded = Settings.load(SimpleNamespace(settings_file=self.path, media_dir=self.path.parent))
        self.assertIs(loaded.notifications, False)

    def test_new_file_failure_does_not_claim_saved_off(self):
        self.path.unlink()
        with patch.object(Path, 'replace', side_effect=OSError('synthetic no-file failure')):
            with self.assertRaises(OSError):
                self.settings.save_notifications(False)
        self.assertFalse(self.path.exists())
        self.assertTrue(self.settings.notifications)
        self.assertEqual(list(self.path.parent.glob('*.tmp')), [])

    def test_malformed_or_nonobject_document_is_not_overwritten(self):
        for source in ('{bad', '[]', 'null', 'false'):
            with self.subTest(source=source):
                self.path.write_text(source, encoding='utf-8')
                with self.assertRaises(ValueError):
                    self.settings.save_notifications(False)
                self.assertEqual(self.path.read_text(encoding='utf-8'), source)
                self.assertTrue(self.settings.notifications)
        self.assertEqual(list(self.path.parent.glob('*.tmp')), [])

    def test_exact_boolean_and_storage_binding_required(self):
        for invalid in (0, 1, None, 'false', [], {}):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                self.settings.save_notifications(invalid)
        self.assert_unchanged()
        self.settings._path = None
        with self.assertRaises(ValueError):
            self.settings.save_notifications(False)
        self.assert_unchanged()

    def test_enable_commits_true_and_legacy_save_is_not_used(self):
        self.settings.notifications = False
        with patch.object(self.settings, 'save', side_effect=AssertionError('Legacy save forbidden')):
            self.settings.save_notifications(True)
        self.assertIs(self.settings.notifications, True)
        self.assertIs(json.loads(self.path.read_text(encoding='utf-8'))['notifications'], True)

    def test_missing_parent_write_error_is_visible_without_memory_change(self):
        missing = self.path.parent / 'not-created' / 'settings.json'
        self.settings._path = missing
        with self.assertRaises(OSError):
            self.settings.save_notifications(False)
        self.assertFalse(missing.exists())
        self.assertTrue(self.settings.notifications)
