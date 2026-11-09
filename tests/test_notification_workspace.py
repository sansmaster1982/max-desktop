"""Native notification pages: synthetic Qt and intercepted account work only."""
import copy
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from PySide6.QtCore import QCoreApplication, QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QFontDatabase, QWheelEvent
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import QApplication
from shiboken6 import isValid

from maxclient.ui.notification_settings_panel import (NotificationSettingsPanel, PAGE_TITLES,
    SOUND_FIELDS, SOUND_CUSTOM_HINT, SOUND_DEFAULT_HINT, SOUND_UNKNOWN_HINT)
from maxclient.core.i18n import SUPPORTED_LANGUAGES, translation_available
from maxclient.ui.theme import build_stylesheet
from test_desktop_shell import _load_offscreen_font


RESULT = {'available': True, 'pending': False,
          'settings': {'PUSH_DETAILS': True, 'CHATS_PUSH_NOTIFICATION': 'ON',
                       'PUSH_SOUND': '_DEFAULT_', 'CHATS_PUSH_SOUND': '_DEFAULT_', 'DONT_DISTURB_UNTIL': 0}}


class FakeNotificationService:
    def __init__(self):
        self.owner, self.client, self.session = 101, object(), object()
        self._desktop_notification_settings = SimpleNamespace(read=Mock(), update=Mock())

    def _history_read_context(self):
        return self.owner, self.client, self.session

    def _require_history_context(self, context):
        if (context[0] != self.owner or context[1] is not self.client or context[2] is not self.session):
            raise ValueError('Synthetic stale context')
        return self.client


class NotificationWorkspaceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setStyle('Fusion')
        _load_offscreen_font(cls.app)
        semibold = Path('C:/Windows/Fonts/seguisb.ttf')
        if semibold.is_file():
            QFontDatabase.addApplicationFont(str(semibold))

    def setUp(self):
        self.service = FakeNotificationService()
        self.settings = SimpleNamespace(theme='light', ui_language='ru', notifications=True, save=Mock())
        self.worker = patch('maxclient.ui.notification_settings_panel.run_async')
        self.run = self.worker.start()
        self.app.setStyleSheet(build_stylesheet('light', '#0085FF'))
        self.panel = NotificationSettingsPanel(self.service, self.settings)
        self.panel.resize(640, 720)
        self.panel.show()
        self.panel._done(copy.deepcopy(RESULT))
        self.app.processEvents()

    def tearDown(self):
        if isValid(self.panel):
            self.panel.close()
            self.panel.deleteLater()
            QCoreApplication.sendPostedEvents(self.panel, QEvent.Type.DeferredDelete)
        self.app.processEvents()
        self.worker.stop()
        self.service._desktop_notification_settings.update.assert_not_called()
        self.service._desktop_notification_settings.read.assert_not_called()
        self.settings.save.assert_not_called()

    def deliver(self, result):
        self.run.call_args.kwargs['on_done'](result)
        self.app.processEvents()

    def assert_intent(self, patch):
        self.assertEqual(self.run.call_args.args, (self.panel.adapter.update, patch))
        self.assertIs(self.run.call_args.kwargs['context'][1], self.service.client)
        self.assertIs(self.run.call_args.kwargs['context'][2], self.service.session)

    def test_main_structure_and_known_values_do_not_imply_unknown_settings_are_off(self):
        self.assertEqual(list(self.panel.navigation_rows), ['personal', 'groups', 'channels'])
        self.assertEqual([r.title_label.text() for r in self.panel.cards[1].rows],
                         ['Показывать текст сообщений', 'Звук уведомлений',
                          'Не беспокоить во время звонка', 'Уведомлять о новых контактах'])
        self.assertEqual(self.panel.navigation_rows['groups'].value_label.text(), 'Включены')
        for key in ('personal', 'channels', 'dnd_call', 'new_contacts'):
            row = self.panel.rows[key]
            self.assertEqual(row.value_label.text(), 'Неизвестно')
            self.assertTrue(row.toggle.isHidden())
            self.assertFalse(row.isEnabled())
            self.assertTrue(row.toolTip())
        self.assertTrue(self.panel.controls['preview'].isChecked())
        self.assertFalse(self.panel.reset_row.isEnabled())
        self.assertTrue(self.panel.status.isHidden())
        self.assertTrue(self.panel.refresh_button.isHidden())
        self.assertTrue(self.panel.local_delivery_row.isVisible())
        self.assertTrue(self.panel.local_delivery.isVisible())
        self.run.assert_not_called()

    def test_local_delivery_initially_off_is_visible_and_operable_without_server_settings(self):
        self.panel.close()
        self.panel.deleteLater()
        QCoreApplication.sendPostedEvents(self.panel, QEvent.Type.DeferredDelete)
        self.settings.notifications = False
        self.panel = NotificationSettingsPanel(self.service, self.settings)
        self.panel.resize(640, 720)
        self.panel.show()
        self.app.processEvents()
        row = self.panel.local_delivery_row
        self.assertTrue(row.isVisible())
        self.assertTrue(self.panel.local_delivery.isVisible())
        self.assertTrue(self.panel.rect().contains(row.mapTo(self.panel, row.rect().center())))
        self.assertEqual(row.title_label.text(), 'Уведомления на этом компьютере')
        self.assertTrue(row.isEnabled())
        self.assertFalse(self.panel.local_delivery.isChecked())
        self.assertFalse(self.panel._result['available'])
        self.settings.save.assert_not_called()
        with patch.object(self.settings, 'save') as save:
            QTest.mouseClick(row, Qt.MouseButton.LeftButton, pos=QPoint(30, row.height() // 2))
            self.assertTrue(self.settings.notifications)
            self.assertTrue(self.panel.local_delivery.isChecked())
            save.assert_called_once_with()
            row.setFocus()
            QTest.keyClick(row, Qt.Key.Key_Space)
            self.assertFalse(self.settings.notifications)
            self.assertFalse(self.panel.local_delivery.isChecked())
            self.assertEqual(save.call_count, 2)
        self.assertEqual(self.panel._result['settings'], {})
        self.run.assert_not_called()

    def test_local_delivery_stays_independent_of_pending_reads_navigation_and_presentation(self):
        result = {**copy.deepcopy(RESULT), 'pending': True}
        self.panel._done(result)
        self.assertFalse(self.panel.rows['preview'].isEnabled())
        self.assertTrue(self.panel.local_delivery_row.isEnabled())
        with patch.object(self.settings, 'save') as save:
            QTest.mouseClick(self.panel.local_delivery_row, Qt.MouseButton.LeftButton, pos=QPoint(30, 30))
            self.assertFalse(self.settings.notifications)
            save.assert_called_once_with()
            self.assertEqual(self.panel._result, result)
            self.panel.open_page('groups')
            self.assertFalse(self.panel.local_delivery_row.isVisible())
            self.panel.back()
            self.panel.apply_theme('dark')
            self.panel.retranslate('en')
            self.panel.retranslate('ru')
            self.panel._expired()
            self.app.processEvents()
            self.assertTrue(self.panel.local_delivery_row.isVisible())
            self.assertTrue(self.panel.local_delivery.isVisible())
            self.assertTrue(self.panel.local_delivery_row.isEnabled())
            self.assertTrue(self.panel.local_delivery.isEnabled())
            self.assertFalse(self.panel.local_delivery.isChecked())
            self.assertEqual(self.panel.local_delivery_row.accessibleName(), 'Уведомления на этом компьютере')
            save.assert_called_once_with()
        self.run.assert_not_called()

    def test_navigation_back_and_titles_are_local_only(self):
        title = QSignalSpy(self.panel.title_changed)
        back = QSignalSpy(self.panel.back_available_changed)
        for index, key in enumerate(('personal', 'groups', 'channels')):
            self.panel.navigation_rows[key].click()
            self.assertEqual(self.panel.current_page, key)
            self.assertIs(self.panel.pages.currentWidget(), self.panel.page_widgets[key])
            self.assertEqual(title.at(index * 2), [PAGE_TITLES[key]])
            self.assertEqual(back.at(index * 2), [True])
            self.assertTrue(self.panel.back())
            self.assertEqual(self.panel.current_page, 'main')
        self.assertFalse(self.panel.back())
        self.assertFalse(self.panel.open_page('unknown'))
        self.run.assert_not_called()

    def test_preview_mouse_choice_sends_once_and_waits_for_confirmed_readback(self):
        row = self.panel.rows['preview']
        QTest.mouseClick(row, Qt.MouseButton.LeftButton, pos=QPoint(30, row.height() // 2))
        self.assert_intent({'PUSH_DETAILS': False})
        self.assertEqual(self.run.call_count, 1)
        self.assertTrue(row.toggle.isChecked())
        self.assertFalse(row.isEnabled())
        row.click()
        self.assertEqual(self.run.call_count, 1)
        result = copy.deepcopy(RESULT)
        result['settings']['PUSH_DETAILS'] = False
        self.deliver(result)
        self.assertFalse(row.toggle.isChecked())
        self.assertTrue(row.isEnabled())
        self.assertTrue(self.panel.status.isHidden())

    def test_groups_master_uses_only_confirmed_on_off_producer(self):
        self.panel.open_page('groups')
        self.panel.rows['groups'].click()
        self.assert_intent({'CHATS_PUSH_NOTIFICATION': 'OFF'})
        self.assertTrue(self.panel.controls['groups'].isChecked())
        result = copy.deepcopy(RESULT)
        result['settings']['CHATS_PUSH_NOTIFICATION'] = 'OFF'
        self.deliver(result)
        self.assertFalse(self.panel.controls['groups'].isChecked())
        self.assertEqual(self.panel.navigation_rows['groups'].value_label.text(), 'Выключены')
        self.assertFalse(any(row.isChecked() for (page, _), row in self.panel.radio_rows.items() if page == 'groups'))
        self.assertFalse(self.panel.radio_rows['groups', 'ON'].isEnabled())

    def test_reply_mode_is_displayed_but_unknown_reply_write_and_channels_stay_disabled(self):
        result = copy.deepcopy(RESULT)
        result['settings']['CHATS_PUSH_NOTIFICATION'] = 'REPLY'
        self.panel._done(result)
        self.panel.open_page('groups')
        reply = self.panel.radio_rows['groups', 'REPLY']
        self.assertTrue(reply.isChecked())
        self.assertFalse(reply.isEnabled())
        reply.click()
        self.panel.open_page('channels')
        for (page, _), row in self.panel.radio_rows.items():
            if page == 'channels':
                self.assertFalse(row.isChecked())
                self.assertFalse(row.isEnabled())
                row.click()
        self.panel.reset_row.click()
        self.run.assert_not_called()

    def test_current_group_radio_is_noop_not_unchecked_or_written(self):
        self.panel.open_page('groups')
        row = self.panel.radio_rows['groups', 'ON']
        self.assertTrue(row.isChecked())
        row.click()
        self.assertTrue(row.isChecked())
        self.run.assert_not_called()

    def test_pending_ack_preserves_confirmed_controls_until_explicit_readback(self):
        self.panel.controls['preview'].click()
        result = copy.deepcopy(RESULT)
        result['pending'] = True
        self.deliver(result)
        self.assertTrue(self.panel.controls['preview'].isChecked())
        self.assertFalse(self.panel.rows['preview'].isEnabled())
        self.assertIn('не подтверждено', self.panel.status.text())
        self.panel.controls['preview'].click()
        self.assertEqual(self.run.call_count, 1)
        self.panel.refresh()
        self.assertEqual(self.run.call_args.args, (self.panel.adapter.read,))
        result['pending'] = False
        result['settings']['PUSH_DETAILS'] = False
        self.deliver(result)
        self.assertFalse(self.panel.controls['preview'].isChecked())
        self.assertTrue(self.panel.rows['preview'].isEnabled())

    def test_unknown_failure_never_retries_write_or_exposes_error_detail(self):
        self.panel.controls['preview'].click()
        self.run.call_args.kwargs['on_error']('synthetic secret should not appear')
        self.assertTrue(self.panel.controls['preview'].isChecked())
        self.assertFalse(self.panel.rows['preview'].isEnabled())
        self.assertNotIn('synthetic secret', self.panel.status.text())
        self.assertEqual(self.run.call_count, 1)
        self.assertFalse(self.panel.refresh_button.isHidden())

    def test_queued_config_refresh_is_one_sequential_read_after_write(self):
        self.panel.controls['preview'].click()
        self.panel.refresh()
        self.panel.refresh()
        self.assertEqual(self.run.call_count, 1)
        self.deliver({**copy.deepcopy(RESULT), 'pending': True})
        self.assertEqual(self.run.call_count, 2)
        self.assertEqual(self.run.call_args.args, (self.panel.adapter.read,))

    def test_old_session_callback_cannot_repopulate_and_fresh_read_has_new_context(self):
        self.panel.refresh()
        old = self.run.call_args.kwargs
        self.service.session = object()
        old['on_done'](copy.deepcopy(RESULT))
        self.assertFalse(self.panel._result['available'])
        self.assertFalse(self.panel.rows['preview'].isEnabled())
        self.panel.refresh()
        fresh = self.run.call_args.kwargs
        self.assertIs(fresh['context'][2], self.service.session)
        fresh['on_done'](copy.deepcopy(RESULT))
        old['on_error']('stale error')
        self.assertTrue(self.panel.rows['preview'].isEnabled())
        self.assertTrue(self.panel.status.isHidden())

    def test_account_switch_before_click_cannot_queue_new_account_write(self):
        self.service.owner = 404
        self.panel.controls['preview'].click()
        self.run.assert_not_called()
        self.assertFalse(self.panel._result['available'])
        self.assertTrue(self.panel.controls['preview'].isHidden())

    def test_session_change_on_navigation_clears_old_values_until_bound_reload(self):
        self.service.owner = 404
        self.panel.open_page('groups')
        self.assertEqual(self.panel.navigation_rows['groups'].value_label.text(), 'Неизвестно')
        self.assertTrue(self.panel.controls['groups'].isHidden())
        self.run.assert_not_called()
        self.panel.ensure_loaded()
        self.assertEqual(self.run.call_args.args, (self.panel.adapter.read,))
        self.assertEqual(self.run.call_args.kwargs['context'][0], 404)

    def test_partial_or_bad_values_never_fake_available_controls(self):
        self.panel._done({'available': True, 'pending': False, 'settings': {'PUSH_DETAILS': False}})
        self.assertFalse(self.panel.controls['preview'].isChecked())
        self.assertTrue(self.panel.rows['preview'].isEnabled())
        self.assertTrue(self.panel.controls['groups'].isHidden())
        self.assertEqual(self.panel.navigation_rows['groups'].value_label.text(), 'Неизвестно')
        self.panel._done({'available': True, 'pending': False, 'settings': {'PUSH_DETAILS': 1}})
        self.assertFalse(self.panel.rows['preview'].isEnabled())
        self.run.assert_not_called()

    def test_theme_translation_and_hover_wheel_have_no_write_side_effect(self):
        for theme in ('dark', 'light'):
            self.panel.apply_theme(theme)
            self.app.setStyleSheet(build_stylesheet(theme, '#0085FF'))
            row = self.panel.rows['preview']
            QTest.mouseMove(row, row.rect().center())
            local = QPointF(row.rect().center())
            event = QWheelEvent(local, QPointF(row.mapToGlobal(local.toPoint())), QPoint(), QPoint(0, -120),
                                Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
                                Qt.ScrollPhase.ScrollUpdate, False)
            QApplication.sendEvent(row, event)
            QTest.keyClick(row, Qt.Key.Key_Escape)
        self.panel.retranslate('en')
        self.panel.retranslate('ru')
        self.assertEqual(self.panel.rows['preview'].title_label.text(), 'Показывать текст сообщений')
        self.assertTrue(self.panel.controls['preview'].isChecked())
        self.run.assert_not_called()

    def test_native_card_heights_and_narrow_text_fit(self):
        for width in (640, 360):
            self.panel.resize(width, 720)
            self.app.processEvents()
            for row in self.panel.navigation_rows.values():
                self.assertGreaterEqual(row.height(), 56)
                self.assertGreaterEqual(row.value_label.width(), row.value_label.fontMetrics().horizontalAdvance(row.value_label.text()))
            for row in self.panel.cards[1].rows:
                self.assertGreaterEqual(row.height(), 48)
                bounds = row.title_label.fontMetrics().boundingRect(
                    0, 0, row.title_label.width(), 10000, Qt.TextFlag.TextWordWrap, row.title_label.text())
                self.assertGreaterEqual(row.title_label.height(), bounds.height())
            if width == 640:
                self.assertEqual([row.height() for row in self.panel.cards[0].rows], [56, 56, 56])
                self.assertEqual([row.height() for row in self.panel.cards[1].rows], [48, 48, 48, 48])
                self.assertEqual(self.panel.reset_row.height(), 64)
        self.run.assert_not_called()

    def test_sound_values_are_read_without_normalizing_custom_or_empty_strings(self):
        local = QSignalSpy(self.panel.local_delivery_changed)
        for key, field in SOUND_FIELDS.items():
            for value in ('DEFAULT', '_DEFAULT_', 'synthetic-custom', '', '_NONE_'):
                result = copy.deepcopy(RESULT)
                result['settings'][field] = value
                self.panel._done(result)
                self.assertEqual(self.panel._result['settings'][field], value)
                self.assertEqual(self.panel.controls[key].isChecked(), value != '_NONE_')
                self.assertTrue(self.panel.rows[key].isEnabled())
                expected = (SOUND_DEFAULT_HINT if value == '_NONE_' else ''
                            if value in ('DEFAULT', '_DEFAULT_') else SOUND_CUSTOM_HINT)
                self.assertEqual(self.panel.rows[key].subtitle_label.text(), expected)
                self.assertNotIn('synthetic-custom', self.panel.rows[key].subtitle_label.text())
        self.assertEqual(local.count(), 0)
        self.settings.save.assert_not_called()
        self.run.assert_not_called()

    def test_sound_off_and_on_send_only_exact_field_and_wait_for_confirmation(self):
        for key, field in SOUND_FIELDS.items():
            self.run.reset_mock()
            result = copy.deepcopy(RESULT)
            result['settings'][field] = 'synthetic-custom'
            self.panel._done(result)
            self.panel.rows[key].click()
            self.assert_intent({field: '_NONE_'})
            self.assertEqual(self.run.call_count, 1)
            self.assertTrue(self.panel.controls[key].isChecked())
            self.assertEqual(self.panel._result['settings'][field], 'synthetic-custom')
            self.assertFalse(self.panel.rows[key].isEnabled())
            result['settings'][field] = '_NONE_'
            self.deliver(result)
            self.assertFalse(self.panel.controls[key].isChecked())
            self.panel.rows[key].click()
            self.assert_intent({field: 'DEFAULT'})
            self.assertEqual(self.run.call_count, 2)
            self.assertFalse(self.panel.controls[key].isChecked())
            result['settings'][field] = 'DEFAULT'
            self.deliver(result)
            self.assertTrue(self.panel.controls[key].isChecked())
            self.assertEqual(self.panel._result['settings']['CHATS_PUSH_NOTIFICATION'], 'ON')
            self.assertEqual(self.panel._result['settings']['DONT_DISTURB_UNTIL'], 0)
        self.settings.save.assert_not_called()

    def test_sound_missing_or_bad_values_are_not_presented_as_disabled(self):
        self.panel._done({'available': True, 'pending': False, 'settings': {'PUSH_DETAILS': False}})
        for key in SOUND_FIELDS:
            row = self.panel.rows[key]
            self.assertTrue(row.toggle.isHidden())
            self.assertFalse(row.isEnabled())
            self.assertEqual(row.value_label.text(), 'Неизвестно')
            self.assertEqual(row.subtitle_label.text(), SOUND_UNKNOWN_HINT)
        for invalid in (None, True, 1, {}):
            self.panel._done({**copy.deepcopy(RESULT), 'settings': {'PUSH_SOUND': invalid}})
            self.assertFalse(self.panel.rows['sound'].isEnabled())
        self.run.assert_not_called()

    def test_sound_unknown_blocks_both_controls_and_refresh_is_only_read(self):
        result = copy.deepcopy(RESULT)
        result['settings']['PUSH_SOUND'] = 'synthetic-custom'
        self.panel._done(result)
        self.panel.rows['sound'].click()
        self.run.call_args.kwargs['on_error']('synthetic-private-error')
        for key in SOUND_FIELDS:
            self.assertFalse(self.panel.rows[key].isEnabled())
            self.panel.rows[key].click()
        self.assertEqual(self.run.call_count, 1)
        self.assertEqual(self.panel._result['settings']['PUSH_SOUND'], 'synthetic-custom')
        self.panel.refresh()
        self.assertEqual(self.run.call_args.args, (self.panel.adapter.read,))
        self.assertNotIn('synthetic-private-error', self.panel.status.text())
        self.assertEqual(self.run.call_count, 2)

    def test_sound_stale_click_and_late_callback_cannot_cross_sessions(self):
        self.panel.rows['sound'].click()
        old = self.run.call_args.kwargs
        self.service.session = object()
        old['on_done'](copy.deepcopy(RESULT))
        self.assertFalse(self.panel._result['available'])
        for key in SOUND_FIELDS:
            self.panel.rows[key].click()
        self.assertEqual(self.run.call_count, 1)
        self.panel.refresh()
        fresh = self.run.call_args.kwargs
        self.assertIs(fresh['context'][2], self.service.session)
        fresh['on_done'](copy.deepcopy(RESULT))
        self.assertTrue(self.panel.rows['sound'].isEnabled())
        old['on_error']('stale')
        self.assertTrue(self.panel.rows['sound'].isEnabled())
        self.service.owner = 404
        self.panel.rows['group_sound'].click()
        self.assertEqual(self.run.call_count, 2)
        self.assertFalse(self.panel._result['available'])

    def test_sound_populate_theme_language_navigation_have_no_write_or_local_signal(self):
        local = QSignalSpy(self.panel.local_delivery_changed)
        result = copy.deepcopy(RESULT)
        result['settings'].update(PUSH_SOUND='synthetic-custom', CHATS_PUSH_SOUND='')
        self.panel._done(result)
        for language in SUPPORTED_LANGUAGES:
            self.panel.retranslate(language)
            self.panel.open_page('groups')
            self.panel.back()
            self.panel.apply_theme('dark')
            self.panel.apply_theme('light')
            self.panel._populate()
        self.assertEqual(self.panel._result['settings']['PUSH_SOUND'], 'synthetic-custom')
        self.assertEqual(self.panel._result['settings']['CHATS_PUSH_SOUND'], '')
        self.assertEqual(local.count(), 0)
        self.settings.save.assert_not_called()
        self.run.assert_not_called()

    def test_local_delivery_signal_only_follows_successful_save_and_failure_rolls_back(self):
        local = QSignalSpy(self.panel.local_delivery_changed)
        self.settings.save.side_effect = OSError('synthetic-private-file')
        before = copy.deepcopy(RESULT)
        before['pending'] = True
        self.panel._done(before)
        server_status = self.panel.status.text()
        self.panel.local_delivery.click()
        self.assertTrue(self.settings.notifications)
        self.assertTrue(self.panel.local_delivery.isChecked())
        self.assertEqual(local.count(), 0)
        self.assertEqual(self.panel._result, before)
        self.assertEqual(self.panel.status.text(), server_status)
        self.assertFalse(self.panel.refresh_button.isHidden())
        self.assertIn('Не удалось сохранить', self.panel.local_status.text())
        self.assertNotIn('synthetic-private-file', self.panel.local_status.text())
        self.settings.save.side_effect = None
        self.panel.local_delivery.click()
        self.assertFalse(self.settings.notifications)
        self.assertEqual(local.count(), 1)
        self.assertEqual(local.at(0), [False])
        self.assertTrue(self.panel.local_status.isHidden())
        self.panel.local_delivery.click()
        self.assertEqual(local.count(), 2)
        self.assertEqual(local.at(1), [True])
        self.panel._local_changed(True)
        self.assertEqual(local.count(), 2)
        self.assertEqual(self.settings.save.call_count, 3)
        self.settings.save.reset_mock()
        self.run.assert_not_called()

    def test_scoped_local_persistence_commits_before_signal_and_never_uses_legacy_save(self):
        observed = []
        self.panel.local_delivery_changed.connect(lambda enabled: observed.append((enabled, self.settings.notifications)))
        def commit(enabled):
            self.assertTrue(self.settings.notifications)
            self.assertFalse(enabled)
            self.assertEqual(observed, [])
            self.settings.notifications = enabled
        self.settings.save_notifications = Mock(side_effect=commit)
        self.panel.local_delivery.click()
        self.settings.save_notifications.assert_called_once_with(False)
        self.assertEqual(observed, [(False, False)])
        self.settings.save.assert_not_called()
        self.settings.save_notifications.side_effect = OSError('synthetic-private-file')
        self.panel.local_delivery.click()
        self.assertEqual(self.settings.save_notifications.call_count, 2)
        self.assertFalse(self.settings.notifications)
        self.assertFalse(self.panel.local_delivery.isChecked())
        self.assertEqual(observed, [(False, False)])
        self.assertIn('Не удалось сохранить', self.panel.local_status.text())
        self.settings.save.assert_not_called()
        self.run.assert_not_called()

    def test_new_sound_strings_have_all_seven_languages(self):
        for language in SUPPORTED_LANGUAGES:
            for source in ('Звук уведомлений', 'Звук в группах', SOUND_DEFAULT_HINT,
                           SOUND_CUSTOM_HINT, SOUND_UNKNOWN_HINT,
                           'Не удалось сохранить настройку на этом компьютере.'):
                self.assertTrue(translation_available(source, language), (source, language))

    def test_child_deletion_and_late_reply_are_ignored(self):
        self.panel.refresh()
        callbacks = self.run.call_args.kwargs
        self.panel.rows['preview'].value_label.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        callbacks['on_done'](copy.deepcopy(RESULT))
        callbacks['on_error']('synthetic late failure')
        self.panel.retranslate('en')
        self.panel.apply_theme('dark')
        self.panel.refresh()
        self.assertEqual(self.run.call_count, 1)


if __name__ == '__main__':
    unittest.main()
