"""Independent sound-menu integration regressions, with in-memory config only."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtTest import QSignalSpy
from PySide6.QtWidgets import QApplication
from shiboken6 import isValid

import test_notification_context as fixtures
from maxclient.core.config import Settings
from maxclient.protocol.client import MaxError, MaxWriteOutcomeUnknown
from maxclient.ui.notification_settings_panel import NotificationSettingsPanel


def immediate(function, *args, context, on_done, on_error):
    try:
        value = function(*args, context=context)
    except Exception as exc:
        on_error(exc)
    else:
        on_done(value)


class NotificationSoundReviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.fixture = fixtures.NotificationContextTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.s, self.client, self.store = (
            self.fixture.service, self.fixture.client, self.fixture.store)
        self.adapter = self.fixture.adapter
        self.s._desktop_notification_settings = self.adapter
        self.store.merge_config(101, {'user': {
            'PUSH_SOUND': 'synthetic-custom', 'CHATS_PUSH_SOUND': '_DEFAULT_'}})
        self.client.update_user_settings.return_value = None
        self.settings = SimpleNamespace(theme='light', ui_language='ru',
                                        notifications=True, save=Mock())
        self.worker = patch('maxclient.ui.notification_settings_panel.run_async',
                            side_effect=immediate)
        self.run = self.worker.start()
        self.addCleanup(self.worker.stop)
        self.panel = NotificationSettingsPanel(self.s, self.settings)
        self.panel._done(self.adapter.read(context=self.panel._context))
        self.spy = QSignalSpy(self.panel.local_delivery_changed)

    def tearDown(self):
        if isValid(self.panel):
            self.panel.close()
            self.panel.deleteLater()
            QCoreApplication.sendPostedEvents(self.panel, QEvent.Type.DeferredDelete)
        self.app.processEvents()

    def test_unrelated_sound_ack_cannot_confirm_global_intent(self):
        self.client.update_user_settings.return_value = {
            'hash': 'synthetic-other-sound', 'user': {'CHATS_PUSH_SOUND': '_NONE_'}}
        self.panel.controls['sound'].click()
        self.assertTrue(self.panel._result['pending'])
        self.assertTrue(self.panel.controls['sound'].isChecked())
        self.assertFalse(self.panel.controls['group_sound'].isChecked())
        self.assertFalse(self.panel.rows['sound'].isEnabled())
        self.assertFalse(self.panel.rows['group_sound'].isEnabled())
        self.panel.controls['group_sound'].click()
        self.assertEqual(self.client.update_user_settings.call_count, 1)
        self.store.merge_config(101, {'user': {'PUSH_SOUND': '_NONE_'}})
        self.panel.refresh()
        self.assertFalse(self.panel._result['pending'])
        self.assertFalse(self.panel.controls['sound'].isChecked())
        self.client.update_user_settings.assert_called_once_with(
            {'PUSH_SOUND': '_NONE_'}, expected_session=self.fixture.context[2])

    def test_default_fallback_does_not_confirm_explicit_enable_after_optional_ack(self):
        self.store.merge_config(101, {'user': {'CHATS_PUSH_SOUND': '_NONE_'}})
        self.panel.refresh()
        self.panel.controls['group_sound'].click()
        self.assertTrue(self.panel._result['pending'])
        self.assertFalse(self.panel.controls['group_sound'].isChecked())
        self.store.merge_config(101, {'user': {'CHATS_PUSH_SOUND': '_DEFAULT_'}})
        self.panel.refresh()
        self.assertTrue(self.panel._result['pending'])
        self.assertTrue(self.panel.controls['group_sound'].isChecked())
        self.assertFalse(self.panel.rows['group_sound'].isEnabled())
        self.store.merge_config(101, {'user': {'CHATS_PUSH_SOUND': 'DEFAULT'}})
        self.panel.refresh()
        self.assertFalse(self.panel._result['pending'])
        self.client.update_user_settings.assert_called_once_with(
            {'CHATS_PUSH_SOUND': 'DEFAULT'}, expected_session=self.fixture.context[2])

    def test_unknown_global_sound_keeps_both_toggles_blocked_after_reauthentication(self):
        self.client.update_user_settings.side_effect = MaxWriteOutcomeUnknown('synthetic-private')
        self.panel.controls['sound'].click()
        self.assertTrue(self.panel._result['pending'])
        self.assertNotIn('synthetic-private', self.panel.status.text())
        self.fixture.reauthenticate()
        self.panel.refresh()
        self.assertTrue(self.panel._result['pending'])
        for key in ('sound', 'group_sound'):
            self.assertFalse(self.panel.rows[key].isEnabled())
            self.panel.controls[key].click()
        self.assertEqual(self.client.update_user_settings.call_count, 1)
        self.store.merge_config(101, {'user': {'PUSH_SOUND': '_NONE_'}})
        self.panel.refresh()
        self.assertFalse(self.panel._result['pending'])
        self.assertTrue(self.panel.rows['group_sound'].isEnabled())
        self.assertEqual(self.client.update_user_settings.call_count, 1)

    def test_negative_ack_readonly_refresh_preserves_custom_and_allows_new_explicit_choice(self):
        self.client.update_user_settings.side_effect = MaxError('synthetic-denied')
        self.panel.controls['sound'].click()
        self.assertTrue(self.panel._result['pending'])
        self.panel.refresh()
        self.assertFalse(self.panel._result['pending'])
        self.assertEqual(self.panel._result['settings']['PUSH_SOUND'], 'synthetic-custom')
        self.assertTrue(self.panel.rows['sound'].isEnabled())
        self.panel.retranslate('en')
        self.panel.apply_theme('dark')
        self.panel.retranslate('ru')
        self.assertEqual(self.client.update_user_settings.call_count, 1)
        self.client.update_user_settings.side_effect = None
        self.panel.controls['sound'].click()
        self.assertEqual(self.client.update_user_settings.call_count, 2)
        self.assertEqual(self.client.update_user_settings.call_args.args, ({'PUSH_SOUND': '_NONE_'},))

    def test_local_save_failure_and_server_pending_keep_separate_states(self):
        self.panel.controls['sound'].click()
        self.settings.save.side_effect = OSError('synthetic-private-path')
        self.panel.local_delivery.click()
        self.assertTrue(self.panel._result['pending'])
        self.assertTrue(self.settings.notifications)
        self.assertTrue(self.panel.local_delivery.isChecked())
        self.assertEqual(self.spy.count(), 0)
        self.assertFalse(self.panel.local_status.isHidden())
        self.assertNotIn('synthetic-private-path', self.panel.local_status.text())
        self.assertTrue(self.panel.status.text())
        self.store.merge_config(101, {'user': {'PUSH_SOUND': '_NONE_'}})
        self.panel.refresh()
        self.assertFalse(self.panel._result['pending'])
        self.assertTrue(self.panel.status.isHidden())
        self.assertFalse(self.panel.local_status.isHidden())
        self.settings.save.side_effect = None
        self.panel.local_delivery.click()
        self.assertFalse(self.settings.notifications)
        self.assertTrue(self.panel.local_status.isHidden())
        self.assertEqual(self.spy.at(0), [False])
        self.assertEqual(self.client.update_user_settings.call_count, 1)

    def test_empty_server_sound_survives_navigation_then_explicit_off_on_uses_only_two_values(self):
        self.store.merge_config(101, {'user': {'PUSH_SOUND': ''}})
        self.panel.refresh()
        self.panel.open_page('groups')
        self.panel.back()
        self.panel.retranslate('en')
        self.panel.apply_theme('dark')
        self.assertTrue(self.panel.controls['sound'].isChecked())
        self.assertEqual(self.panel._result['settings']['PUSH_SOUND'], '')
        self.client.update_user_settings.assert_not_called()
        def accept(patch, **_kwargs):
            return {'hash': 'synthetic-accepted', 'user': patch}
        self.client.update_user_settings.side_effect = accept
        self.panel.controls['sound'].click()
        self.assertFalse(self.panel.controls['sound'].isChecked())
        self.panel.controls['sound'].click()
        self.assertTrue(self.panel.controls['sound'].isChecked())
        self.assertEqual([call.args[0] for call in self.client.update_user_settings.call_args_list],
                         [{'PUSH_SOUND': '_NONE_'}, {'PUSH_SOUND': 'DEFAULT'}])

    def test_real_local_persistence_failures_keep_unknown_fields_and_emit_only_after_commit(self):
        self.panel.controls['sound'].click()  # Keep an unrelated server intent pending.
        temp_root = Path(__file__).parent.parent / '.test-tmp'
        temp_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='notification-review-persist-', dir=temp_root) as scratch:
            path = Path(scratch) / 'settings.json'
            document = {'notifications': True, 'theme': 'dark',
                        'future': {'nested': ['synthetic', False, 7]}}
            original = json.dumps(document).encode('utf-8')
            path.write_bytes(original)
            real = Settings(notifications=True, theme='light')
            real._path = path
            real.save = Mock(side_effect=AssertionError('Legacy save must not run'))
            self.panel.settings = real
            for target in ('maxclient.core.config.os.fsync', 'pathlib.Path.replace'):
                with self.subTest(target=target), patch(target, side_effect=OSError('synthetic-private-failure')):
                    self.panel.local_delivery.click()
                self.assertTrue(real.notifications)
                self.assertTrue(self.panel.local_delivery.isChecked())
                self.assertEqual(path.read_bytes(), original)
                self.assertEqual(list(Path(scratch).iterdir()), [path])
                self.assertEqual(self.spy.count(), 0)
                self.assertTrue(self.panel._result['pending'])
                self.assertNotIn('synthetic-private', self.panel.local_status.text())
            observed = []
            self.panel.local_delivery_changed.connect(lambda enabled: observed.append(
                (enabled, real.notifications, json.loads(path.read_text(encoding='utf-8')))))
            self.panel.local_delivery.click()
            self.assertEqual(observed, [(False, False, {**document, 'notifications': False})])
            self.assertTrue(self.panel.local_status.isHidden())
            self.assertTrue(self.panel._result['pending'])
            self.assertEqual(self.client.update_user_settings.call_count, 1)
            real.save.assert_not_called()


if __name__ == '__main__':
    unittest.main()
