"""Embedded settings checks use synthetic account data and no network."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from maxclient.core.config import Settings
from maxclient.core.models import Profile
from maxclient.ui.main_window import MainWindow
from maxclient.ui.settings_workspace import SettingsWorkspace
from maxclient.ui.theme import build_stylesheet
from test_desktop_shell import _Service


class SyntheticService(_Service):
    def __init__(self):
        super().__init__()
        self.profile = Profile(id=123, name='Тестовый Профиль', phone='+7 000 000-00-00',
                               first_name='Тестовый', last_name='Профиль', description='')
        self.profile_writes = []
        self.avatar_writes = []
        self.status_reads = 0
        self.session_reads = 0
        self.privacy_reads = 0
        self.termination_writes = 0
        self.termination_allowed = True
        self.security_session = object()

    def _history_read_context(self):
        return (self.profile.id, self.client, self.security_session)

    def _require_history_context(self, context):
        if not (isinstance(context, tuple) and len(context) == 3 and context[0] == self.profile.id
                and context[1] is self.client and context[2] is self.security_session):
            raise ValueError('Synthetic session changed')

    def my_profile(self):
        return self.profile

    def get_folders(self):
        return {'folderSync':0,'orderedFolders':[],'missingFolderIds':[]}

    def list_own_stories(self, marker=0):
        return {'stories':[]}

    def update_my_profile(self, **changes):
        self.profile_writes.append(changes)
        for key, value in changes.items():
            setattr(self.profile, key, value)
        self.profile.name = ' '.join(part for part in (self.profile.first_name, self.profile.last_name) if part)
        return self.profile

    def set_my_avatar(self, path):
        self.avatar_writes.append(path)
        self.profile.photo_id = 456
        return self.profile

    def get_2fa_status(self, *, context=None):
        if context is not None:
            self._require_history_context(context)
        self.status_reads += 1
        return {'enabled': True, 'email': None, 'hint': None}

    def list_sessions(self):
        self.session_reads += 1
        return [{'label': '<b>Тестовое устройство</b>', 'current': True, 'location': 'Локальный тест'}]

    def get_privacy_settings(self, *, context=None):
        if context is not None:
            self._require_history_context(context)
        self.privacy_reads += 1
        return {'SEARCH_BY_PHONE': 'CONTACTS', 'HIDDEN': False}

    def can_terminate_other_sessions(self):
        return self.termination_allowed

    def terminate_other_sessions(self):
        self.termination_writes += 1
        return True


def immediate(function, *args, on_done=None, on_error=None, **kwargs):
    try:
        result = function(*args, **kwargs)
    except Exception as error:
        if on_error:
            on_error(error)
        else:
            raise
    else:
        if on_done:
            on_done(result)


class EmbeddedSettingsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        for name in ('segoeui.ttf', 'segoeuib.ttf', 'seguisb.ttf'):
            font = Path('C:/Windows/Fonts') / name
            if font.is_file():
                index = QFontDatabase.addApplicationFont(str(font))
                families = QFontDatabase.applicationFontFamilies(index)
                if name == 'segoeui.ttf' and families:
                    cls.app.setFont(QFont(families[0], 10))
        cls.app.setStyle('Fusion')
        cls.app.setStyleSheet(build_stylesheet('light', '#0085FF'))

    def setUp(self):
        self.service = SyntheticService()
        self.settings = Settings(theme='light', accent='#0085FF')
        self.main_async = patch('maxclient.ui.main_window.run_async')
        self.main_async.start()
        self.settings_async = patch('maxclient.ui.settings_workspace.run_async', side_effect=immediate)
        self.settings_async.start()
        self.privacy_async = patch('maxclient.ui.privacy_settings_panel.run_async', side_effect=immediate)
        self.privacy_async.start()
        self.window = MainWindow(self.service, self.settings)
        self.window.resize(1240, 760)
        self.window.show()
        self.window.settings_button.click()
        self.app.processEvents()
        self.workspace = self.window._settings_workspace

    def tearDown(self):
        self.window.close()
        self.settings_async.stop()
        self.privacy_async.stop()
        self.main_async.stop()
        self.app.processEvents()

    def test_settings_are_embedded_and_navigation_returns_to_same_chat_shell(self):
        self.assertIsInstance(self.workspace, SettingsWorkspace)
        self.assertIs(self.window._workspace_pages.currentWidget(), self.workspace)
        self.assertEqual(self.workspace.navigation.width(), 360)
        self.assertEqual(self.window.settings_button.width(), 64)
        self.assertTrue(self.window.settings_button.isChecked())
        self.assertFalse(self.window.nav_buttons['all'].isChecked())
        self.window.nav_buttons['channels'].click()
        self.assertIs(self.window._workspace_pages.currentWidget(), self.window.splitter)
        self.assertFalse(self.window.settings_button.isChecked())
        self.assertTrue(self.window.nav_buttons['channels'].isChecked())
        self.assertTrue(self.window.codes_button.isEnabled())
        self.assertTrue(self.window.calls_button.isEnabled())

    def test_native_reference_spacing_and_primary_navigation(self):
        self.assertEqual(self.workspace.first_name.parentWidget().width(), 640)
        baseline_step = self.workspace.profile_phone.y() - self.workspace.profile_name.y()
        self.assertEqual(baseline_step, 20)
        keys = list(self.workspace.nav_buttons)
        self.assertEqual(keys[:2], ['profile', 'invite'])
        self.assertNotIn('sferum', keys)
        self.workspace.select_page('sferum')
        self.assertTrue(self.workspace.nav_buttons['about'].isChecked())
        self.assertIn('#SettingsProfileField:focus', self.workspace.styleSheet())
        self.assertIn('#SettingsAboutField:focus', self.workspace.styleSheet())

    def test_profile_has_real_values_and_saves_only_on_explicit_action(self):
        self.assertEqual(self.workspace.first_name.text(), 'Тестовый')
        self.assertEqual(self.workspace.last_name.text(), 'Профиль')
        self.assertEqual(self.workspace.first_name.maxLength(), 59)
        self.assertFalse(self.workspace.about.isReadOnly())
        self.assertTrue(self.workspace.profile_save.isHidden())
        self.workspace.first_name.setText('Новое')
        self.workspace._profile_edited()
        self.assertEqual(self.service.profile_writes, [])
        self.workspace.profile_save.click()
        self.assertEqual(self.service.profile_writes, [{'first_name': 'Новое'}])
        self.assertEqual(self.workspace.profile_name.text(), 'Новое Профиль')
        self.assertTrue(self.workspace.profile_save.isHidden())

    def test_profile_card_stays_visible_when_lower_menu_scrolls(self):
        card = self.workspace.profile_button
        origin = card.mapTo(self.workspace, QPoint(0, 0))
        scroll = self.workspace._menu_scroll
        bar = scroll.verticalScrollBar()
        self.assertGreater(bar.maximum(), 0)
        bar.setValue(bar.maximum())
        self.app.processEvents()
        self.assertEqual(card.mapTo(self.workspace, QPoint(0, 0)), origin)
        self.assertTrue(card.isVisibleTo(self.workspace))
        about = self.workspace.nav_buttons['about']
        self.assertTrue(scroll.viewport().rect().intersects(
            about.rect().translated(about.mapTo(scroll.viewport(), QPoint(0, 0)))))
        QTest.mouseClick(card, Qt.MouseButton.LeftButton)
        self.assertEqual(self.workspace.page_title.text(), 'Редактировать профиль')
        self.assertEqual(self.service.profile_writes, [])

    def test_mouse_navigation_does_not_take_focus_but_keyboard_still_activates(self):
        self.workspace.profile_button.clearFocus()
        button = self.workspace.nav_buttons['language']
        self.workspace._menu_scroll.ensureWidgetVisible(button)
        self.app.processEvents()
        QTest.mouseClick(button, Qt.MouseButton.LeftButton)
        self.assertEqual(self.workspace.page_title.text(), 'Язык приложения')
        self.assertFalse(button.hasFocus())
        self.workspace.select_page('profile')
        self.window.activateWindow()
        button.setFocus(Qt.FocusReason.TabFocusReason)
        self.app.processEvents()
        self.assertTrue(button.hasFocus())
        QTest.keyClick(button, Qt.Key.Key_Space)
        self.assertEqual(self.workspace.page_title.text(), 'Язык приложения')
        self.assertTrue(button.isChecked())

    def test_bio_uses_profile_api_and_preserves_unknown_name_fields(self):
        self.service.profile.first_name = None
        self.service.profile.last_name = None
        self.service.profile.name = 'Имя без канонических частей'
        self.workspace.refresh_profile()
        self.assertEqual(self.workspace.first_name.text(), '')
        self.assertEqual(self.workspace.last_name.text(), '')
        self.workspace.about.setPlainText('Описание из локального теста')
        self.workspace.profile_save.click()
        self.assertEqual(self.service.profile_writes, [{'description': 'Описание из локального теста'}])

    def test_bio_limit_matches_official_html_length_without_splitting_unicode(self):
        self.workspace.about.setPlainText('\U0001f600' * 201)
        self.assertEqual(self.workspace.about.toPlainText(), '\U0001f600' * 200)
        self.assertEqual(self.workspace.about.property('maximumLength'), 400)

    def test_avatar_upload_requires_file_choice_and_keeps_pending_profile_edits(self):
        self.workspace.first_name.setText('Новое')
        self.workspace._profile_edited()
        with patch('maxclient.ui.settings_workspace.QFileDialog.getOpenFileName', return_value=('', '')):
            self.workspace.avatar_button.click()
        self.assertEqual(self.service.avatar_writes, [])
        with patch('maxclient.ui.settings_workspace.QFileDialog.getOpenFileName',
                   return_value=('synthetic-avatar.png', 'Изображения')):
            self.workspace.avatar_button.click()
        self.assertEqual(self.service.avatar_writes, ['synthetic-avatar.png'])
        self.assertEqual(self.workspace.first_name.text(), 'Новое')
        self.assertEqual(self.service.profile_writes, [])

    def test_security_status_is_lazy_and_unknown_status_cannot_change_password(self):
        self.assertEqual(self.service.status_reads, 0)
        self.workspace.nav_buttons['security'].click()
        self.assertEqual(self.service.status_reads, 1)
        self.assertTrue(self.workspace.twofa_button.isEnabled())
        self.workspace._security_error('<b>synthetic error</b>')
        self.assertEqual(self.workspace.twofa_status.textFormat(), Qt.TextFormat.PlainText)
        self.assertFalse(self.workspace.twofa_button.isEnabled())
        with patch('maxclient.ui.settings_workspace.Change2FADialog') as dialog:
            self.workspace._twofa_primary()
            dialog.assert_not_called()

    def test_devices_are_real_read_results_and_termination_needs_confirmation(self):
        self.workspace.nav_buttons['devices'].click()
        self.assertEqual(self.service.session_reads, 1)
        self.assertEqual(self.workspace.devices_list.count(), 1)
        label = self.workspace.devices_list.itemAt(0).widget()
        self.assertEqual(label.title_label.textFormat(), Qt.TextFormat.PlainText)
        self.assertIn('<b>Тестовое устройство</b>', label.text())
        self.assertTrue(self.workspace.terminate_button.isEnabled())
        with patch('maxclient.ui.settings_workspace.QMessageBox.question',
                   return_value=0):
            self.workspace.terminate_button.click()
        self.assertEqual(self.service.termination_writes, 0)
        from PySide6.QtWidgets import QMessageBox
        with patch('maxclient.ui.settings_workspace.QMessageBox.question',
                   return_value=QMessageBox.StandardButton.Yes):
            self.workspace.terminate_button.click()
        self.assertEqual(self.service.termination_writes, 1)
        self.assertEqual(self.service.session_reads, 2)

    def test_unknown_termination_cannot_be_repeated_after_refresh(self):
        self.workspace.select_page('devices')
        self.workspace._termination_error('unknown')
        self.workspace._load_devices()
        self.assertFalse(self.workspace.terminate_button.isEnabled())
        self.workspace._terminate_other_devices()
        self.assertEqual(self.service.termination_writes, 0)

    def test_server_feature_gate_and_config_push_update_the_panel(self):
        self.assertEqual(self.service.privacy_reads, 0)
        self.workspace.select_page('security')
        self.assertEqual(self.service.privacy_reads, 1)
        self.assertEqual(self.workspace.privacy_panel.rows['SEARCH_BY_PHONE'].current_value, 'CONTACTS')
        self.service.on_config_changed()
        self.app.processEvents()
        self.assertEqual(self.service.privacy_reads, 2)
        self.workspace.select_page('devices')
        self.service.termination_allowed = False
        self.service.on_config_changed()
        self.app.processEvents()
        self.assertFalse(self.workspace.terminate_button.isEnabled())

    def test_config_push_during_first_privacy_read_is_not_lost(self):
        with patch('maxclient.ui.privacy_settings_panel.run_async') as pending:
            self.workspace.select_page('security')
            self.assertEqual(pending.call_count, 1)
            self.assertFalse(self.workspace.privacy_panel._loaded)
            self.workspace.select_page('profile')
            self.workspace.on_config_changed()
            self.assertTrue(self.workspace.privacy_panel._refresh_queued)
            self.workspace.privacy_panel._read_done({'HIDDEN': False})
            self.assertEqual(pending.call_count, 2)

    def test_device_read_failure_stays_disabled_after_config_push(self):
        self.workspace.select_page('devices')
        self.assertTrue(self.workspace.terminate_button.isEnabled())
        self.workspace._devices_error('synthetic refresh failure')
        self.workspace.on_config_changed()
        self.assertFalse(self.workspace.terminate_button.isEnabled())

    def test_existing_local_preferences_are_wired(self):
        import json
        from tempfile import TemporaryDirectory
        temp_root = Path('.test-tmp').resolve()
        temp_root.mkdir(exist_ok=True)
        directory = TemporaryDirectory(prefix='notification-workspace-', dir=temp_root)
        self.addCleanup(directory.cleanup)
        self.settings._path = Path(directory.name) / 'settings.json'
        self.workspace.nav_buttons['notifications'].click()
        self.workspace.notifications.click()
        self.assertFalse(self.settings.notifications)
        self.assertIs(json.loads(self.settings._path.read_text(encoding='utf-8'))['notifications'], False)
        self.workspace.nav_buttons['keyboard'].click()
        with patch.object(self.settings, 'save') as save:
            self.workspace.send_on_ctrl_enter.click()
            self.assertFalse(self.settings.send_on_enter)
            self.assertTrue(self.workspace.send_on_ctrl_enter.isChecked())
            self.assertFalse(self.workspace.send_on_enter.isChecked())
            save.assert_called_once()
            self.workspace.send_on_ctrl_enter.click()
            save.assert_called_once()  # Choosing the selected row is not a toggle.
            self.workspace.send_on_enter.click()
            self.assertTrue(self.settings.send_on_enter)
            self.assertFalse(self.workspace.send_on_ctrl_enter.isChecked())
            self.assertEqual(save.call_count, 2)

    def test_every_settings_row_opens_a_truthful_page(self):
        for key in self.workspace.nav_buttons:
            if key == 'invite':
                continue  # Native MAX copies the invitation without navigating.
            self.workspace.select_page(key)
            self.assertIs(self.workspace.pages.currentWidget(), self.workspace.page_widgets[key])
            self.assertEqual(self.workspace.current_page, key)
        self.assertEqual(self.service.profile_writes, [])

    def test_language_switch_relabels_navigation_without_changing_profile_draft_or_transport(self):
        self.workspace.first_name.setText('English')
        self.workspace.about.setPlainText('Настройки')
        phone = self.workspace.profile_phone.text()
        session = self.service.security_session
        with patch.object(self.settings, 'save_ui_language',
                          side_effect=lambda value: setattr(self.settings, 'ui_language', value)) as save:
            self.workspace.select_page('language')
            self.workspace.language_panel.rows['en'].click()
            save.assert_called_once_with('en')
        self.assertEqual(self.workspace.page_title.text(), 'App language')
        self.assertEqual(self.window.settings_button.label.text(), 'Settings')
        self.assertEqual(self.workspace.first_name.text(), 'English')
        self.assertEqual(self.workspace.about.toPlainText(), 'Настройки')
        self.assertEqual(self.workspace.profile_phone.text(), phone)
        self.assertIs(self.service.security_session, session)
        self.assertEqual(self.settings.locale, 'ru')
        self.assertEqual(self.service.profile_writes, [])

    def test_notification_nested_header_back_and_reentry(self):
        self.workspace.select_page('notifications')
        self.assertEqual(self.workspace.page_title.text(), 'Уведомления и звук')
        self.workspace.notification_panel.open_page('groups')
        self.assertEqual(self.workspace.page_title.text(), 'Группы')
        self.assertFalse(self.workspace.page_back.isHidden())
        self.workspace.page_back.click()
        self.assertEqual(self.workspace.notification_panel.current_page, 'main')
        self.assertTrue(self.workspace.page_back.isHidden())
        self.workspace.notification_panel.open_page('channels')
        self.workspace.select_page('language')
        self.assertTrue(self.workspace.page_back.isHidden())
        self.workspace.select_page('notifications')
        self.assertEqual(self.workspace.notification_panel.current_page, 'main')

    def test_storage_path_cancel_and_confirm_are_local_only(self):
        self.workspace.select_page('storage')
        original = self.settings.download_dir
        with patch('maxclient.ui.settings_workspace.QFileDialog.getExistingDirectory', return_value=''), \
                patch.object(self.settings, 'save') as save:
            self.workspace.download_path_row.click()
            save.assert_not_called()
        self.assertEqual(self.settings.download_dir, original)
        with patch('maxclient.ui.settings_workspace.QFileDialog.getExistingDirectory', return_value='C:/Test/Downloads'), \
                patch.object(self.settings, 'save') as save:
            self.workspace.download_path_row.click()
            save.assert_called_once()
        self.assertEqual(self.workspace.download_path.text(), 'C:/Test/Downloads')
        self.assertEqual(self.service.profile_writes, [])

    def test_profile_render_synthetic_only(self):
        self.workspace.select_page('profile')
        self.app.processEvents()
        output = Path('.test-tmp/settings_workspace_light.png')
        output.parent.mkdir(parents=True, exist_ok=True)
        self.assertTrue(self.window.grab().save(str(output)))
        self.workspace.first_name.setFocus()
        self.window.resize(880, 560)
        self.app.processEvents()
        self.assertGreaterEqual(self.workspace.first_name.width(), 150)
        self.assertTrue(self.window.grab().save(str(output.with_name('settings_workspace_minimum.png'))))
        self.window.resize(1240, 760)
        self.settings.theme = 'dark'
        self.app.setStyleSheet(build_stylesheet('dark', '#0085FF'))
        for button in self.window._rail_widgets:
            button.set_dark(True)
        self.workspace.apply_theme()
        self.app.processEvents()
        self.assertTrue(self.window.grab().save(str(output.with_name('settings_workspace_dark.png'))))
        self.app.setStyleSheet(build_stylesheet('light', '#0085FF'))


if __name__ == '__main__':
    unittest.main()
