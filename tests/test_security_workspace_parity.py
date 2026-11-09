"""Offline security presentation and owner/request/lifetime regressions."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from PySide6.QtCore import QCoreApplication, QEvent, QRect, Qt
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtWidgets import QApplication, QComboBox
from shiboken6 import isValid

from maxclient.core.config import Settings
from maxclient.ui.privacy_settings_panel import PrivacySettingsPanel
from maxclient.ui.settings_workspace import SettingsWorkspace
from maxclient.ui.theme import build_stylesheet
from test_desktop_shell import _load_offscreen_font
from test_settings_workspace import SyntheticService


VALUES = {'INCOMING_CALL': 'ALL', 'SEARCH_BY_PHONE': 'ALL', 'CHATS_INVITE': 'ALL',
          'CONTENT_LEVEL_ACCESS': False, 'HIDDEN': False, 'PHONE_NUMBER_PRIVACY': 'CONTACTS',
          'FAMILY_PROTECTION': 'OFF', 'SAFE_MODE': False}


class SecurityWorkspaceParityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setStyle('Fusion')
        _load_offscreen_font(cls.app)
        semibold = Path('C:/Windows/Fonts/seguisb.ttf')
        if semibold.is_file():
            QFontDatabase.addApplicationFont(str(semibold))
        emoji = Path('C:/Windows/Fonts/seguiemj.ttf')
        if emoji.is_file():
            QFontDatabase.addApplicationFont(str(emoji))

    def setUp(self):
        self.service = SyntheticService()
        self.service.update_privacy_settings = Mock(side_effect=AssertionError('No real mutation'))
        self.settings = Settings(theme='light', accent='#0085FF')
        self.settings.save = Mock()
        self.worker = patch('maxclient.ui.privacy_settings_panel.run_async')
        self.run = self.worker.start()
        self.security_worker = patch('maxclient.ui.settings_workspace.run_async')
        self.security_run = self.security_worker.start()
        self.widgets = []
        self.app.setStyleSheet(build_stylesheet('light', '#0085FF'))

    def tearDown(self):
        for widget in reversed(self.widgets):
            if isValid(widget):
                widget.close()
                widget.deleteLater()
                QCoreApplication.sendPostedEvents(widget, QEvent.Type.DeferredDelete)
        self.app.processEvents()
        self.security_worker.stop()
        self.worker.stop()
        self.service.update_privacy_settings.assert_not_called()

    def panel(self):
        panel = PrivacySettingsPanel(self.service)
        self.widgets.append(panel)
        panel.resize(640, 900)
        panel.show()
        panel._read_done(VALUES)
        self.app.processEvents()
        return panel

    def workspace(self, theme='light', width=1218):
        self.settings.theme = theme
        self.app.setStyleSheet(build_stylesheet(theme, '#0085FF'))
        panel = SettingsWorkspace(self.service, self.settings)
        self.widgets.append(panel)
        panel.resize(width, 752)
        panel.show()
        panel.select_page('security')
        self.security_run.call_args.kwargs['on_done']({'enabled': True, 'email': 'example@test.invalid'})
        self.run.call_args.kwargs['on_done'](VALUES)
        self.app.processEvents()
        return panel

    def choose(self, panel, key, value):
        menu = panel.open_menu(key)
        self.assertIsNotNone(menu)
        action = next(a for a in menu.actions() if type(a.data()) is type(value) and a.data() == value)
        action.trigger()

    def test_main_cards_and_values_are_readonly_on_open(self):
        panel = self.workspace()
        self.assertEqual(self.security_run.call_args.args, (self.service.get_2fa_status,))
        self.assertEqual(self.run.call_args.args, (self.service.get_privacy_settings,))
        self.assertIs(self.security_run.call_args.kwargs['context'][2], self.service.security_session)
        self.assertEqual(panel.twofa_button.title_label.text(), 'Пароль для входа')
        self.assertEqual(panel.twofa_status.text(), 'Включён')
        self.assertEqual(panel.privacy_panel.family_status.text(), 'Отключена')
        self.assertFalse(panel.privacy_panel.family_row.isEnabled())
        self.assertFalse(panel.privacy_panel.safe_mode_row.toggle.isEnabled())
        self.assertFalse(panel.privacy_panel.safe_mode_row.toggle.isChecked())
        self.assertEqual([r.title_label.text() for r in panel.privacy_panel.privacy_card.rows],
                         ['Безопасный режим', 'Позвонить', 'Найти меня по номеру', 'Пригласить в чат', 'Показывать контент'])
        self.assertEqual([r.title_label.text() for r in panel.privacy_panel.info_card.rows],
                         ['Видеть статус «в сети»', 'Видеть мой номер'])
        self.assertEqual(panel.blacklist_row.title_label.text(), 'Чёрный список')
        self.assertEqual(panel.privacy_panel.findChildren(QComboBox), [])
        for row in panel.privacy_panel.rows.values():
            self.assertFalse(row.value_label.wordWrap())
            self.assertGreaterEqual(row.value_label.width(), row.value_label.fontMetrics().horizontalAdvance(row.value_label.text()))
            self.assertEqual(row.title_label.font().weight(), QFont.Weight.DemiBold)
        self.assertTrue(panel.privacy_panel.status.isHidden())
        self.assertTrue(panel.security_retry.isHidden())
        self.settings.save.assert_not_called()

    def test_password_summary_routes_only_explicit_existing_guarded_dialogs(self):
        panel = self.workspace()
        before = self.security_run.call_count
        with patch('maxclient.ui.settings_workspace.Change2FADialog') as change:
            panel.twofa_button.click()
            self.assertEqual(panel.current_page, 'password')
            change.assert_not_called()
            self.assertEqual(self.security_run.call_count, before)
            panel.password_change_button.click()
            change.assert_called_once_with(self.service, panel)
            change.return_value.exec.assert_called_once()
        self.assertEqual(self.security_run.call_count, before + 1)

    def test_unknown_values_never_appear_as_disabled_protection(self):
        panel = self.panel()
        panel._read_done({})
        self.assertEqual(panel.family_status.text(), 'Неизвестно')
        self.assertEqual(panel.safe_mode_status.text(), 'Неизвестно')
        self.assertTrue(panel.safe_mode_row.toggle.isHidden())
        self.assertFalse(any(row.isEnabled() for row in panel.rows.values()))
        self.run.assert_not_called()

    def test_old_session_read_is_discarded_and_next_read_is_pinned(self):
        panel = self.panel()
        panel.refresh()
        old = self.run.call_args.kwargs
        self.service.security_session = object()
        old['on_done']({'HIDDEN': True})
        self.assertEqual(panel._values, {})
        self.assertFalse(panel.rows['HIDDEN'].isEnabled())
        panel.refresh()
        fresh = self.run.call_args.kwargs
        self.assertIs(fresh['context'][2], self.service.security_session)
        fresh['on_done'](VALUES)
        old['on_done']({'HIDDEN': True})
        self.assertIs(panel.rows['HIDDEN'].current_value, False)

    def test_uncertain_write_survives_same_owner_reauthentication(self):
        panel = self.panel()
        self.choose(panel, 'HIDDEN', True)
        write = self.run.call_args.kwargs
        self.service.security_session = object()
        write['on_error']('Synthetic private transport detail')
        self.assertEqual(panel._unconfirmed, {'HIDDEN': True})
        self.assertEqual(panel._values, {})
        panel.refresh()
        self.run.call_args.kwargs['on_done'](VALUES)
        self.assertFalse(panel.rows['HIDDEN'].isEnabled())
        before = self.run.call_count
        self.assertIsNone(panel.open_menu('HIDDEN'))
        self.assertEqual(self.run.call_count, before)
        panel.refresh()
        self.run.call_args.kwargs['on_done']({**VALUES, 'HIDDEN': True})
        self.assertTrue(panel.rows['HIDDEN'].isEnabled())
        self.assertEqual(panel._unconfirmed, {})
        self.assertNotIn('private', panel.status.text())

    def test_uncertain_write_does_not_cross_to_a_different_owner(self):
        panel = self.panel()
        self.choose(panel, 'HIDDEN', True)
        write = self.run.call_args.kwargs
        self.service.profile.id = 456
        self.service.security_session = object()
        write['on_error']('unknown')
        panel.refresh()
        self.run.call_args.kwargs['on_done'](VALUES)
        self.assertTrue(panel.rows['HIDDEN'].isEnabled())
        self.assertEqual(panel._unconfirmed, {})

    def test_callbacks_after_deleted_widget_or_partial_row_are_noops(self):
        for target in ('panel', 'subtitle_label', 'chevron', 'safe_value'):
            with self.subTest(target=target):
                panel = self.panel()
                panel.refresh()
                callbacks = self.run.call_args.kwargs
                deleted = (panel if target == 'panel' else panel.safe_mode_row.value_label if target == 'safe_value'
                           else getattr(panel.rows['HIDDEN'], target))
                deleted.deleteLater()
                QCoreApplication.sendPostedEvents(deleted, QEvent.Type.DeferredDelete)
                callbacks['on_done'](VALUES)
                callbacks['on_error']('private')
                panel.apply_theme('dark')
                panel.refresh()

    def test_password_late_result_and_error_cannot_expose_old_session_status(self):
        panel = self.workspace()
        panel._load_security()
        old = self.security_run.call_args.kwargs
        self.service.security_session = object()
        old['on_done']({'enabled': True, 'email': 'private-secret@invalid'})
        self.assertFalse(panel.twofa_button.isEnabled())
        self.assertNotIn('private-secret', panel.password_details.text())
        with patch('maxclient.ui.settings_workspace.Change2FADialog') as change:
            panel._twofa_primary()
            change.assert_not_called()
        panel._load_security()
        new = self.security_run.call_args.kwargs
        new['on_done']({'enabled': False})
        old['on_error']('private raw error')
        self.assertEqual(panel.twofa_status.text(), 'Отключён')
        self.assertNotIn('private', panel.twofa_status.text())
        panel.twofa_button.value_label.deleteLater()
        QCoreApplication.sendPostedEvents(panel.twofa_button.value_label, QEvent.Type.DeferredDelete)
        new['on_done']({'enabled': True})
        new['on_error']('private')

    def test_static_security_fixtures(self):
        output = Path('.test-tmp')
        output.mkdir(exist_ok=True)
        for theme, width, suffix in (('light', 1218, 'light'), ('dark', 1218, 'dark'), ('light', 816, 'narrow')):
            panel = self.workspace(theme=theme, width=width)
            self.app.processEvents()
            self.assertTrue(panel.grab().save(str(output / f'security_native_{suffix}_20261103.png')))
            scroll = panel.page_widgets['security']
            scroll.verticalScrollBar().setValue(scroll.verticalScrollBar().maximum())
            self.app.processEvents()
            self.assertTrue(panel.grab().save(str(output / f'security_native_{suffix}_bottom_20261103.png')))
            subtitle = panel.blacklist_row.subtitle_label
            required_height = subtitle.fontMetrics().boundingRect(
                QRect(0, 0, subtitle.width(), 10000), Qt.TextFlag.TextWordWrap, subtitle.text()).height()
            self.assertGreaterEqual(subtitle.height(), required_height)
            if theme == 'light' and suffix == 'light':
                scroll.verticalScrollBar().setValue(0)
                menu = panel.privacy_panel.open_menu('INCOMING_CALL')
                self.app.processEvents()
                self.assertTrue(menu.grab().save(str(output / 'security_native_popup_20261103.png')))
                menu.close()
                panel.twofa_button.click()
                self.app.processEvents()
                self.assertTrue(panel.grab().save(str(output / 'security_native_password_summary_20261103.png')))


if __name__ == '__main__':
    unittest.main()
