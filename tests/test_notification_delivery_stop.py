"""Local delivery OFF dismisses an existing toast and mocked audio immediately."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import time
import unittest
from unittest.mock import Mock, patch

from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtTest import QSignalSpy
from PySide6.QtWidgets import QApplication
from shiboken6 import isValid

from maxclient.core.config import Settings
from maxclient.core.models import Message
from maxclient.core.notification_settings import DEFAULTS
from maxclient.core.notifications import Notice
from maxclient.ui.main_window import MainWindow
from maxclient.ui.settings_workspace import SettingsWorkspace
from test_settings_workspace import SyntheticService
from test_desktop_shell import _load_offscreen_font


class NotificationDeliveryStopTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        _load_offscreen_font(cls.app)

    def setUp(self):
        self.patches = []
        for target in ('maxclient.ui.main_window.run_async', 'maxclient.ui.chat_view.run_async',
                       'maxclient.ui.settings_workspace.run_async', 'maxclient.ui.notification_settings_panel.run_async'):
            item = patch(target)
            self.patches.append(item)
            item.start()
            self.addCleanup(item.stop)
        # Never instantiate a real multimedia backend, even if a regression
        # accidentally calls the sound-start path.
        self.audio = patch('maxclient.ui.desktop_notifications.QAudioOutput',
                           side_effect=AssertionError('Real audio forbidden'))
        self.audio_factory = self.audio.start()
        self.addCleanup(self.audio.stop)
        self.media = patch('maxclient.ui.desktop_notifications.QMediaPlayer',
                           side_effect=AssertionError('Real player forbidden'))
        self.media_factory = self.media.start()
        self.addCleanup(self.media.stop)
        self.settings = Settings(theme='light', notifications=True)
        self.settings.save = Mock()
        self.settings.save_notifications = Mock(side_effect=lambda enabled: setattr(self.settings, 'notifications', enabled))
        self.service = SyntheticService()
        self.service.my_id = 123
        self.service.logout = Mock(side_effect=AssertionError('Real logout forbidden'))
        self.window = MainWindow(self.service, self.settings)
        self.window._open_settings()
        self.workspace = self.window._settings_workspace
        self.panel = self.workspace.notification_panel
        self.notices = self.window._notifications
        self.player = self.notices.player = Mock()
        self.player.playing = True
        self.player.stop.side_effect = lambda: setattr(self.player, 'playing', False)
        self.notices.toast.present(Notice(2, 'Synthetic sender', 'Synthetic preview', True), show=False)
        self.notices.toast.show()
        self.app.processEvents()
        self.spy = QSignalSpy(self.workspace.local_delivery_changed)

    def tearDown(self):
        self.settings.save.side_effect = None
        if isValid(self.window):
            self.window.close()
            self.window.deleteLater()
            QCoreApplication.sendPostedEvents(self.window, QEvent.Type.DeferredDelete)
        self.app.processEvents()
        self.audio_factory.assert_not_called()
        self.media_factory.assert_not_called()
        self.service.logout.assert_not_called()

    def assert_dismissed(self):
        self.assertTrue(self.notices.toast.isHidden())
        self.assertEqual(self.notices.toast.title.text(), '')
        self.assertEqual(self.notices.toast.body.text(), '')
        self.assertIsNone(self.notices.toast.chat_id)
        self.assertFalse(self.notices.toast.timer.isActive())
        self.assertFalse(self.player.playing)

    def test_successful_off_immediately_erases_toast_and_stops_existing_player(self):
        self.panel.local_delivery.click()
        # Direct GUI signal delivery must finish before the click returns.
        self.settings.save_notifications.assert_called_once_with(False)
        self.settings.save.assert_not_called()
        self.assertEqual(self.spy.count(), 1)
        self.assertEqual(self.spy.at(0), [False])
        self.assertFalse(self.settings.notifications)
        self.assert_dismissed()
        self.player.stop.assert_called_once_with()

    def test_failed_save_restores_state_without_stop_or_success_signal(self):
        self.settings.save_notifications.side_effect = OSError('synthetic private filesystem detail')
        self.panel.local_delivery.click()
        self.assertTrue(self.settings.notifications)
        self.assertTrue(self.panel.local_delivery.isChecked())
        self.assertEqual(self.spy.count(), 0)
        self.assertEqual(self.notices.toast.body.text(), 'Synthetic preview')
        self.assertTrue(self.player.playing)
        self.player.stop.assert_not_called()
        self.assertNotIn('synthetic private', self.panel.local_status.text())
        self.assertTrue(self.panel.local_status.text())

    def test_reenable_does_not_replay_toast_or_start_audio(self):
        self.panel.local_delivery.click()
        self.player.stop.reset_mock()
        self.panel.local_delivery.click()
        self.assertTrue(self.settings.notifications)
        self.assertEqual(self.spy.at(1), [True])
        self.assert_dismissed()
        self.player.stop.assert_not_called()
        self.player.play.assert_not_called()

    def test_off_with_no_player_does_not_create_audio_resources(self):
        self.notices.player = None
        self.panel.local_delivery.click()
        self.assertIsNone(self.notices.player)
        self.assertTrue(self.notices.toast.isHidden())
        self.assertFalse(self.notices.toast.timer.isActive())
        self.player.stop.assert_not_called()

    def test_disabled_delivery_suppresses_later_synthetic_message(self):
        self.panel.local_delivery.click()
        self.notices.preferences.read = Mock(return_value={'available': True, 'settings': dict(DEFAULTS)})
        with patch.object(self.notices.toast, 'present') as present, patch.object(self.notices, '_play_sound') as sound:
            self.notices.receive(Message(2, id=999, sender=456, text='Synthetic later',
                time_ms=int(time.time() * 1000)))
        present.assert_not_called()
        sound.assert_not_called()
        self.notices.preferences.read.assert_called_once_with()

    def test_stale_false_after_reenable_does_not_stop_new_presentation(self):
        self.workspace.local_delivery_changed.emit(False)
        self.assertTrue(self.settings.notifications)
        self.player.stop.assert_not_called()
        self.assertEqual(self.notices.toast.body.text(), 'Synthetic preview')

    def test_replaced_workspace_signal_is_ignored(self):
        stale = self.workspace
        replacement = SettingsWorkspace(self.service, self.settings, self.window)
        self.window._settings_workspace = replacement
        self.settings.notifications = False
        stale.local_delivery_changed.emit(False)
        self.player.stop.assert_not_called()
        self.assertEqual(self.notices.toast.body.text(), 'Synthetic preview')

    def test_closed_window_ignores_late_signal(self):
        self.window.close()
        self.player.stop.reset_mock()
        self.settings.notifications = False
        self.workspace.local_delivery_changed.emit(False)
        self.player.stop.assert_not_called()

    def test_destroyed_window_automatically_disconnects_workspace_delivery(self):
        self.workspace.setParent(None)  # Keep sender alive past its former receiver.
        self.window.close()
        self.window.deleteLater()
        QCoreApplication.sendPostedEvents(self.window, QEvent.Type.DeferredDelete)
        self.assertFalse(isValid(self.window))
        self.player.stop.reset_mock()
        self.settings.notifications = False
        self.workspace.local_delivery_changed.emit(False)
        self.player.stop.assert_not_called()
        self.workspace.close()
        self.workspace.deleteLater()
        QCoreApplication.sendPostedEvents(self.workspace, QEvent.Type.DeferredDelete)

    def test_server_config_clear_still_dismisses_and_stops_audio(self):
        with patch.object(self.workspace, 'on_config_changed'), patch.object(self.window, '_poll_info_action_state'):
            self.window._on_config_changed()
        self.assert_dismissed()
        self.player.stop.assert_called_once_with()
        self.assertTrue(self.settings.notifications)
        self.settings.save.assert_not_called()

    def test_account_teardown_still_clears_and_late_signal_cannot_reopen(self):
        self.window._handle_logout()
        self.assert_dismissed()
        self.player.stop.assert_called_once_with()
        self.workspace.local_delivery_changed.emit(True)
        self.player.play.assert_not_called()
        self.assertTrue(self.settings.notifications)

    def test_workspace_open_retranslate_and_reopen_do_not_change_delivery(self):
        self.settings.save.assert_not_called()
        self.player.stop.assert_not_called()
        self.window._open_settings()
        self.workspace.retranslate()
        self.assertIs(self.window._settings_workspace, self.workspace)
        self.assertEqual(self.spy.count(), 0)
        self.assertTrue(self.settings.notifications)
        self.player.stop.assert_not_called()
        self.panel.local_delivery.click()
        self.player.stop.assert_called_once_with()
