"""Complete notification-page localization with synthetic values and no writes."""
import copy
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import re
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtTest import QSignalSpy
from PySide6.QtWidgets import QApplication, QLabel, QPushButton, QWidget
from shiboken6 import isValid

from maxclient.core.i18n import SUPPORTED_LANGUAGES, translation_available
from maxclient.ui.notification_settings_panel import (NotificationSettingsPanel, PAGE_TITLES,
    UNSUPPORTED, SOUND_CUSTOM_HINT, SOUND_DEFAULT_HINT, SOUND_UNKNOWN_HINT)
from maxclient.ui.theme import build_stylesheet
from test_notification_workspace import FakeNotificationService, RESULT
from test_desktop_shell import _load_offscreen_font

LATIN_LANGUAGES = ('en', 'es', 'pt', 'fr', 'uz')
CYRILLIC = re.compile(r'[\u0400-\u052f]')
RUNTIME_SOURCES = ('Неизвестно', 'Включены', 'Выключены', 'Обновить',
    'Состояние настроек уведомлений', 'Сессия изменилась. Обновите настройки.',
    'Загрузка настроек…', 'Сохранение…', 'Изменение ещё не подтверждено MAX.',
    'MAX пока не передал настройки уведомлений.',
    'Не удалось подтвердить настройки. Нажмите «Обновить», чтобы проверить состояние.',
    'Не удалось сохранить настройку на этом компьютере.')


def owned_texts(panel, *, visible_only=True):
    """Inspect actual rendered widgets and accessibility, not catalogue output."""
    texts = []
    for widget in [panel, *panel.findChildren(QWidget)]:
        if visible_only and widget is not panel and not widget.isVisibleTo(panel):
            continue
        if isinstance(widget, (QLabel, QPushButton)) and widget.text():
            texts.append(widget.text())
        for attribute in ('toolTip', 'accessibleName', 'accessibleDescription'):
            value = getattr(widget, attribute)()
            if value:
                texts.append(value)
    return texts


class NotificationLocalizationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        _load_offscreen_font(cls.app)

    def setUp(self):
        self.service = FakeNotificationService()
        self.settings = SimpleNamespace(theme='light', ui_language='ru', notifications=True,
            save=Mock(), save_notifications=Mock(side_effect=AssertionError('No local persistence')))
        self.worker = patch('maxclient.ui.notification_settings_panel.run_async')
        self.jobs = self.worker.start()
        self.app.setStyleSheet(build_stylesheet('light', '#0085FF'))
        self.panel = NotificationSettingsPanel(self.service, self.settings)
        self.panel.resize(640, 980)
        self.panel.show()
        self.panel._done(copy.deepcopy(RESULT))
        self.local_signal = QSignalSpy(self.panel.local_delivery_changed)
        self.title_signal = QSignalSpy(self.panel.title_changed)
        self.app.processEvents()

    def tearDown(self):
        if isValid(self.panel):
            self.panel.close()
            self.panel.deleteLater()
            QCoreApplication.sendPostedEvents(self.panel, QEvent.Type.DeferredDelete)
        self.app.processEvents()
        self.worker.stop()
        self.service._desktop_notification_settings.read.assert_not_called()
        self.service._desktop_notification_settings.update.assert_not_called()
        self.settings.save.assert_not_called()
        self.settings.save_notifications.assert_not_called()
        self.assertEqual(self.local_signal.count(), 0)

    def assert_localized(self, language, texts=None):
        texts = owned_texts(self.panel) if texts is None else texts
        self.assertTrue(texts)
        for text in texts:
            if language in LATIN_LANGUAGES:
                self.assertIsNone(CYRILLIC.search(text), (language, text))
            self.assertIsNone(re.search(r'\{[A-Za-z_][^}]*\}', text), text)

    def ready(self, **changes):
        # Rebind via the actual read path after a simulated expired context;
        # queued workers remain intercepted and never reach the adapter.
        if self.panel._context is None:
            self.panel.refresh()
        result = copy.deepcopy(RESULT)
        result['settings'].update(changes)
        self.panel._done(result)

    def test_exact_english_main_headings_values_and_accessibility(self):
        self.panel.retranslate('en')
        self.panel.open_page('main')
        self.assertEqual(self.title_signal.at(self.title_signal.count() - 1), ['Notifications and sound'])
        self.assertEqual(self.panel.heading.text(), 'NOTIFICATIONS')
        self.assertEqual([row.title_label.text() for row in self.panel.navigation_rows.values()],
                         ['Personal chats', 'Groups', 'Channels'])
        self.assertEqual(self.panel.navigation_rows['groups'].value_label.text(), 'On')
        self.assertEqual(self.panel.navigation_rows['personal'].value_label.text(), 'Unknown')
        self.assertEqual(self.panel.navigation_rows['channels'].value_label.text(), 'Unknown')
        self.ready(CHATS_PUSH_NOTIFICATION='OFF')
        self.assertEqual(self.panel.navigation_rows['groups'].value_label.text(), 'Off')
        self.assertEqual(self.panel.status.accessibleName(), 'Notification settings status')
        self.assertEqual(self.panel.refresh_button.text(), 'Update')
        self.assert_localized('en')
        self.jobs.assert_not_called()

    def test_every_nested_page_has_exact_english_title_and_no_russian_fallback(self):
        self.panel.retranslate('en')
        for page, expected in (('personal', 'Personal chats'), ('groups', 'Groups'), ('channels', 'Channels')):
            self.panel.navigation_rows[page].click()
            self.assertEqual(self.panel.current_page, page)
            self.assertEqual(self.title_signal.at(self.title_signal.count() - 1), [expected])
            self.assert_localized('en')
            self.assertTrue(self.panel.back())
            self.assertEqual(self.title_signal.at(self.title_signal.count() - 1), ['Notifications and sound'])
        self.jobs.assert_not_called()

    def test_all_pages_and_runtime_states_have_no_latin_locale_cyrillic_fallback(self):
        for language in LATIN_LANGUAGES:
            self.panel.retranslate(language)
            for state in ('ready', 'unknown', 'pending', 'error', 'stale', 'loading', 'saving'):
                self.ready(PUSH_SOUND='синтетическое-имя', CHATS_PUSH_SOUND='')
                if state == 'unknown':
                    self.panel._done(dict(available=False, pending=False, settings={}))
                elif state == 'pending':
                    self.panel._done({**copy.deepcopy(self.panel._result), 'pending': True})
                elif state == 'error':
                    self.panel._error('private-synthetic-error')
                elif state == 'stale':
                    self.panel._expired()
                elif state == 'loading':
                    self.panel.refresh()
                elif state == 'saving':
                    self.panel._show_status('Сохранение…')
                # Stale navigation deliberately re-renders the current expired state.
                for page in PAGE_TITLES:
                    self.panel.open_page(page)
                    self.app.processEvents()
                    self.assert_localized(language)
                    self.assertNotIn('private-synthetic-error', self.panel.status.text())
                    self.assertNotIn('синтетическое-имя', '\n'.join(owned_texts(self.panel)))
                # Every job, if any, is a read; none is executed by this fixture.
                self.assertTrue(all(call.args == (self.panel.adapter.read,) for call in self.jobs.call_args_list))

    def test_language_switch_preserves_raw_custom_values_selection_context_and_local_preference(self):
        self.ready(PUSH_SOUND='синтетическое-имя', CHATS_PUSH_SOUND='', CHATS_PUSH_NOTIFICATION='REPLY')
        expected, context = copy.deepcopy(self.panel._result), self.panel._context
        for language in SUPPORTED_LANGUAGES:
            self.panel.retranslate(language)
            for page in PAGE_TITLES:
                self.panel.open_page(page)
                self.panel.apply_theme('dark')
                self.panel.apply_theme('light')
            self.assertEqual(self.panel._result, expected)
            self.assertIs(self.panel._context, context)
            self.assertTrue(self.panel.controls['sound'].isChecked())
            self.assertTrue(self.panel.controls['group_sound'].isChecked())
            self.assertTrue(self.panel.radio_rows['groups', 'REPLY'].isChecked())
            self.assertFalse(self.panel.radio_rows['groups', 'REPLY'].isEnabled())
            self.assertTrue(self.settings.notifications)
            self.assertEqual(self.settings.ui_language, 'ru')
        self.jobs.assert_not_called()

    def test_local_error_translates_without_hiding_server_pending_or_retrying_save(self):
        self.panel._done({**copy.deepcopy(RESULT), 'pending': True})
        self.settings.save_notifications.side_effect = OSError('private-synthetic-file')
        self.panel.local_delivery.click()
        self.settings.save_notifications.assert_called_once_with(False)
        self.settings.save_notifications.reset_mock()
        for language in SUPPORTED_LANGUAGES:
            self.panel.retranslate(language)
            self.panel.open_page('main')
            self.assertTrue(self.panel.local_status.isVisible())
            self.assertTrue(self.panel.status.isVisible())
            self.assertTrue(self.panel.refresh_button.isVisible())
            self.assertTrue(self.panel._result['pending'])
            self.assertTrue(self.settings.notifications)
            self.assert_localized(language)
            self.assertNotIn('private-synthetic-file', self.panel.local_status.text())
        self.jobs.assert_not_called()

    def test_update_button_wording_matches_english_and_french_error_instruction(self):
        for language, word in (('en', 'Update'), ('fr', 'Actualiser')):
            self.panel.retranslate(language)
            self.panel._error('synthetic')
            self.assertEqual(self.panel.refresh_button.text(), word)
            self.assertIn(word, self.panel.status.text())
            self.assert_localized(language)

    def test_all_page_source_keys_have_seven_language_coverage_including_belarusian(self):
        sources = (set(self.panel._source_titles.values()) | set(PAGE_TITLES.values())
            | set(UNSUPPORTED.values()) | {source for _widget, source in self.panel._captions}
            | set(RUNTIME_SOURCES) | {SOUND_CUSTOM_HINT, SOUND_DEFAULT_HINT, SOUND_UNKNOWN_HINT})
        for source in sources:
            for language in SUPPORTED_LANGUAGES:
                self.assertTrue(translation_available(source, language), (language, source))
        self.panel.retranslate('be')
        for page in PAGE_TITLES:
            self.panel.open_page(page)
            self.assert_localized('be')
        self.jobs.assert_not_called()

    def test_long_translations_and_custom_hint_fit_narrow_rows(self):
        self.ready(PUSH_SOUND='custom', CHATS_PUSH_SOUND='custom')
        for language in ('fr', 'pt', 'es', 'uz', 'be'):
            self.panel.retranslate(language)
            for width in (640, 360):
                self.panel.resize(width, 1100)
                for page in ('main', 'groups'):
                    self.panel.open_page(page)
                    self.app.processEvents()
                    for key in ('sound', 'group_sound'):
                        row = self.panel.rows[key]
                        if not row.isVisibleTo(self.panel):
                            continue
                        for label in (row.title_label, row.subtitle_label):
                            bounds = label.fontMetrics().boundingRect(0, 0, label.width(), 10000,
                                Qt.TextFlag.TextWordWrap, label.text())
                            self.assertGreaterEqual(label.height(), bounds.height(), (language, width, key, label.text()))
        self.jobs.assert_not_called()

    def test_real_workspace_header_back_and_language_callback_follow_nested_page(self):
        import test_settings_workspace as fixture_module
        fixture_module.EmbeddedSettingsTests.setUpClass()
        fixture = fixture_module.EmbeddedSettingsTests()
        with patch('maxclient.ui.main_window.DesktopNotifications'), \
             patch('maxclient.ui.notification_settings_panel.run_async') as jobs:
            fixture.setUp()
            try:
                workspace, settings = fixture.workspace, fixture.settings
                with patch.object(settings, 'save') as save, patch.object(settings, 'save_notifications') as persist:
                    settings.ui_language = 'en'
                    workspace._language_selected('en')
                    workspace.select_page('notifications')
                    self.assertEqual(workspace.page_title.text(), 'Notifications and sound')
                    workspace.notification_panel.navigation_rows['groups'].click()
                    self.assertEqual(workspace.page_title.text(), 'Groups')
                    self.assertTrue(workspace.page_back.isVisible())
                    self.assertEqual(workspace.page_back.accessibleName(), 'Back')
                    workspace.page_back.click()
                    self.assertEqual(workspace.page_title.text(), 'Notifications and sound')
                    self.assertFalse(workspace.page_back.isVisible())
                    workspace.notification_panel.navigation_rows['channels'].click()
                    settings.ui_language = 'fr'
                    workspace._language_selected('fr')
                    self.assertEqual(workspace.notification_panel.current_page, 'channels')
                    self.assertIsNone(CYRILLIC.search(workspace.page_title.text()))
                    self.assertTrue(workspace.page_back.isVisible())
                    save.assert_not_called()
                    persist.assert_not_called()
                    self.assertTrue(all(call.args == (workspace.notification_panel.adapter.read,)
                                        for call in jobs.call_args_list))
            finally:
                fixture.tearDown()
