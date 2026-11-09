"""Privacy localization preserves typed intent, captured session and pending state."""
import copy
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import re
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtWidgets import QApplication, QLabel, QPushButton, QWidget
from shiboken6 import isValid

from maxclient.core.i18n import SUPPORTED_LANGUAGES, translation_available
from maxclient.ui.privacy_settings_panel import PrivacySettingsPanel, FIELDS, GUARDED
from test_desktop_shell import _load_offscreen_font

VALUES = dict(SEARCH_BY_PHONE='ALL', INCOMING_CALL='CONTACTS', CHATS_INVITE='ALL',
    PHONE_NUMBER_PRIVACY='CONTACTS', HIDDEN=False, CONTENT_LEVEL_ACCESS=False,
    FAMILY_PROTECTION='OFF', SAFE_MODE=False)
RAW_OPTIONS = dict(INCOMING_CALL=['CONTACTS', 'ALL'], SEARCH_BY_PHONE=['CONTACTS', 'ALL'],
    CHATS_INVITE=['CONTACTS', 'ALL'], CONTENT_LEVEL_ACCESS=[False, True],
    HIDDEN=[True, False], PHONE_NUMBER_PRIVACY=['ALL', 'CONTACTS', 'NOBODY'])
CYRILLIC = re.compile(r'[\u0400-\u052f]')
SOURCES = ('Семейная защита', 'Безопасный режим', 'Информация', 'Неизвестно',
    'Доступно только в мобильном приложении', 'Обновить настройки', 'Отключена',
    'Вы администратор', 'Профиль под защитой', 'никто (текущее значение)',
    'Состояние настроек приватности', 'Открытие семейной защиты пока недоступно в этом клиенте.',
    'Эта настройка управляется безопасным режимом или семейной защитой.',
    'Сессия изменилась. Обновите настройки.', 'Загрузка настроек…', 'Сохранение…',
    'MAX пока не передал настройки.', 'Изменение ещё не подтверждено MAX. Показаны известные значения.',
    'Не удалось загрузить настройки. Попробуйте обновить позже.',
    'Запрос принят. Изменение ещё не подтверждено MAX.',
    'Не удалось подтвердить изменение. Проверьте состояние перед повтором.')


class PrivacyLocalizationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        _load_offscreen_font(cls.app)

    def setUp(self):
        self.service = Mock()
        self.settings = SimpleNamespace(ui_language='ru', save=Mock())
        self.service.settings = self.settings
        self.context = (101, object(), object())
        self.service._history_read_context.return_value = self.context
        def current(context):
            if context is not self.context:
                raise ValueError('Synthetic stale context')
            return context[1]
        self.service._require_history_context.side_effect = current
        self.patch = patch('maxclient.ui.privacy_settings_panel.run_async')
        self.jobs = self.patch.start()
        self.panel = PrivacySettingsPanel(self.service)
        self.panel.resize(640, 1000)
        self.panel.show()
        self.app.processEvents()

    def tearDown(self):
        if isValid(self.panel):
            self.panel.close()
            self.panel.deleteLater()
            QCoreApplication.sendPostedEvents(self.panel, QEvent.Type.DeferredDelete)
        self.app.processEvents()
        self.patch.stop()
        self.service.get_privacy_settings.assert_not_called()
        self.service.update_privacy_settings.assert_not_called()
        self.settings.save.assert_not_called()

    def loaded(self, **extra):
        self.panel._read_done({**VALUES, **extra})

    def choose(self, key, selected):
        menu = self.panel.open_menu(key)
        self.assertIsNotNone(menu)
        action = next(action for action in menu.actions()
                      if type(action.data()) is type(selected) and action.data() == selected)
        action.trigger()

    def assert_latin(self, language):
        for widget in [self.panel, *self.panel.findChildren(QWidget)]:
            if widget is not self.panel and not widget.isVisibleTo(self.panel):
                continue
            texts = [getattr(widget, name)() for name in ('toolTip', 'accessibleName', 'accessibleDescription')]
            if isinstance(widget, (QLabel, QPushButton)):
                texts.append(widget.text())
            for text in texts:
                if language in ('en', 'es', 'pt', 'fr', 'uz'):
                    self.assertIsNone(CYRILLIC.search(text), (language, text))
        if self.panel._menu is not None:
            for action in self.panel._menu.actions():
                if language in ('en', 'es', 'pt', 'fr', 'uz'):
                    self.assertIsNone(CYRILLIC.search(action.text()), (language, action.text()))

    def state(self):
        return dict(values=copy.deepcopy(self.panel._values), busy=self.panel._busy,
            unconfirmed=copy.deepcopy(self.panel._unconfirmed), intent_owner=self.panel._intent_owner,
            write_key=self.panel._write_key, write_value=self.panel._write_value,
            request=self.panel._request, context=self.panel._context, queued=self.panel._refresh_queued,
            loaded=self.panel._loaded, read_failed=self.panel._read_failed)

    def switch_preserving_state(self):
        before = self.state()
        values, intents = self.panel._values, self.panel._unconfirmed
        jobs = self.jobs.call_count
        for language in SUPPORTED_LANGUAGES:
            self.panel.retranslate(language)
            self.assertEqual(self.state(), before)
            self.assertIs(self.panel._values, values)
            self.assertIs(self.panel._unconfirmed, intents)
            self.assertEqual(self.jobs.call_count, jobs)
            self.assert_latin(language)

    def test_initial_language_reads_typed_service_preference_and_mock_falls_back_to_russian(self):
        self.panel.close()
        self.panel.deleteLater()
        QCoreApplication.sendPostedEvents(self.panel, QEvent.Type.DeferredDelete)
        self.settings.ui_language = 'en'
        self.panel = PrivacySettingsPanel(self.service)
        self.panel.show()
        self.assertEqual(self.panel.info_heading.text(), 'Info')
        self.assertEqual(self.panel.family_row.title_label.text(), 'Family protection')
        self.assertEqual(self.panel.safe_mode_row.title_label.text(), 'Safe mode')
        self.assertEqual(self.panel.rows['HIDDEN'].value_label.text(), 'Unknown')
        self.assertFalse(self.panel.rows['HIDDEN'].isEnabled())
        self.service.settings = Mock()
        self.panel.retranslate()
        self.assertEqual(self.panel.info_heading.text(), 'Информация')
        self.service.settings = self.settings
        self.panel.retranslate()
        self.assertEqual(self.panel.info_heading.text(), 'Info')
        self.jobs.assert_not_called()

    def test_exact_english_menu_labels_with_unchanged_boolean_and_string_data(self):
        self.loaded()
        self.panel.retranslate('en')
        self.assertEqual([(a.text(), a.data()) for a in self.panel.open_menu('HIDDEN').actions()],
                         [('no one', True), ('contacts can', False)])
        self.assertEqual([(a.text(), a.data()) for a in self.panel.open_menu('CONTENT_LEVEL_ACCESS').actions()],
                         [('all', False), ('safe', True)])
        self.assertEqual([(a.text(), a.data()) for a in self.panel.open_menu('SEARCH_BY_PHONE').actions()],
                         [('contacts can', 'CONTACTS'), ('anyone', 'ALL')])
        self.assertEqual(self.panel.rows['HIDDEN'].current_value, False)
        self.assertEqual(self.panel.rows['HIDDEN'].value_label.text(), 'contacts can')
        self.jobs.assert_not_called()

    def test_all_seven_languages_preserve_exact_typed_menu_choices_and_read_only_nobody(self):
        self.loaded()
        for language in SUPPORTED_LANGUAGES:
            self.panel.retranslate(language)
            for key, expected in RAW_OPTIONS.items():
                menu = self.panel.open_menu(key)
                actual = [action.data() for action in menu.actions()]
                self.assertEqual(actual, expected)
                self.assertEqual([type(value) for value in actual], [type(value) for value in expected])
                self.assertEqual(sum(action.isChecked() for action in menu.actions()), 1)
                self.assert_latin(language)
            self.loaded(INCOMING_CALL='NOBODY', CHATS_INVITE='NOBODY')
            for key in ('INCOMING_CALL', 'CHATS_INVITE'):
                menu = self.panel.open_menu(key)
                current = next(action for action in menu.actions() if action.data() == 'NOBODY')
                self.assertFalse(current.isEnabled())
                self.assertTrue(current.isChecked())
                self.assertEqual([a.data() for a in menu.actions() if a.isEnabled()], ['CONTACTS', 'ALL'])
                self.assert_latin(language)
            self.loaded()
        self.jobs.assert_not_called()

    def test_language_change_closes_popup_and_old_action_cannot_write(self):
        self.loaded()
        menu = self.panel.open_menu('HIDDEN')
        old_action = menu.actions()[0]
        self.panel.retranslate('en')
        self.assertIsNone(self.panel._menu)
        self.assertFalse(menu.isVisible())
        old_action.trigger()
        self.jobs.assert_not_called()
        self.choose('HIDDEN', True)
        self.assertEqual(self.jobs.call_count, 1)
        self.assertEqual(self.jobs.call_args.args, (self.service.update_privacy_settings, {'HIDDEN': True}))
        self.assertIs(self.jobs.call_args.kwargs['context'], self.context)

    def test_explicit_translated_choice_queues_original_payload_once(self):
        for language, key, value in (('fr', 'SEARCH_BY_PHONE', 'CONTACTS'),
                                     ('es', 'CONTENT_LEVEL_ACCESS', True), ('uz', 'HIDDEN', True)):
            self.loaded()
            self.panel.retranslate(language)
            before = self.jobs.call_count
            self.choose(key, value)
            self.assertEqual(self.jobs.call_count, before + 1)
            self.assertEqual(self.jobs.call_args.args, (self.service.update_privacy_settings, {key: value}))
            self.assertIs(type(self.jobs.call_args.args[1][key]), type(value))
            self.jobs.call_args.kwargs['on_done'](dict(accepted=True, pending=False, settings={**VALUES, key: value}))
            self.assertEqual(self.panel._values[key], value)

    def test_loading_language_change_keeps_callback_and_never_starts_another_read(self):
        self.panel.refresh()
        callback = self.jobs.call_args.kwargs['on_done']
        self.switch_preserving_state()
        self.assertTrue(self.panel._busy)
        self.assertEqual(self.jobs.call_count, 1)
        callback(VALUES)
        self.assertEqual(self.panel._values, VALUES)
        self.assertFalse(self.panel._busy)

    def test_pending_write_and_pending_ack_survive_retranslation_without_retry(self):
        self.loaded()
        self.choose('HIDDEN', True)
        callback = self.jobs.call_args.kwargs['on_done']
        self.switch_preserving_state()
        self.assertIs(self.panel._write_value, True)
        callback(dict(accepted=True, pending=True, settings={'HIDDEN': True}))
        self.switch_preserving_state()
        self.assertEqual(self.panel._unconfirmed, {'HIDDEN': True})
        self.assertIs(self.panel.rows['HIDDEN'].current_value, False)
        self.assertFalse(self.panel.rows['HIDDEN'].isEnabled())
        self.assertEqual(self.jobs.call_count, 1)

    def test_unknown_write_and_read_error_preserve_source_status_and_raw_values(self):
        self.loaded()
        self.choose('HIDDEN', True)
        self.jobs.call_args.kwargs['on_error']('private-synthetic-server-text')
        source = self.panel._status_source
        self.switch_preserving_state()
        self.assertEqual(self.panel._status_source, source)
        self.assertNotIn('private-synthetic-server-text', self.panel.status.text())
        self.panel._read_error('private-synthetic-read-error')
        self.switch_preserving_state()
        self.assertFalse(any(row.isEnabled() for row in self.panel.rows.values()))
        self.assertEqual(self.jobs.call_count, 1)

    def test_stale_session_keeps_unconfirmed_intent_and_late_callback_is_ignored(self):
        self.loaded()
        self.choose('HIDDEN', True)
        callback = self.jobs.call_args.kwargs['on_done']
        self.context = (101, self.context[1], object())
        self.panel._expired()
        self.assertIsNone(self.panel._context)
        self.assertEqual(self.panel._unconfirmed, {'HIDDEN': True})
        self.switch_preserving_state()
        callback(dict(accepted=True, pending=False, settings={**VALUES, 'HIDDEN': True}))
        self.assertEqual(self.panel._values, {})
        self.assertEqual(self.panel._unconfirmed, {'HIDDEN': True})
        self.assertEqual(self.jobs.call_count, 1)

    def test_family_safe_and_unknown_guards_are_unchanged_in_every_language(self):
        for flags in ({'SAFE_MODE': True, 'FAMILY_PROTECTION': 'OFF'},
                      {'SAFE_MODE': False, 'FAMILY_PROTECTION': 'MANAGEABLE'}):
            self.loaded(**flags)
            for language in SUPPORTED_LANGUAGES:
                self.panel.retranslate(language)
                for key, row in self.panel.rows.items():
                    self.assertEqual(row.isEnabled(), key not in GUARDED)
                    if key in GUARDED:
                        self.assertTrue(row.toolTip())
                        self.assertIn(row.toolTip(), row.accessibleDescription())
                        self.assertIsNone(self.panel.open_menu(key))
                self.assertFalse(self.panel.safe_mode_row.isEnabled())
                self.assertFalse(self.panel.family_row.isEnabled())
                self.assert_latin(language)
        self.panel._read_done({})
        self.switch_preserving_state()
        self.assertFalse(any(row.isEnabled() for row in self.panel.rows.values()))
        self.jobs.assert_not_called()

    def test_all_source_keys_have_seven_languages_and_no_unrendered_placeholders(self):
        sources = set(SOURCES)
        for _key, title, options in FIELDS:
            sources.add(title)
            sources.update(title for title, _value in options)
        for language in SUPPORTED_LANGUAGES:
            for source in sources:
                self.assertTrue(translation_available(source, language), (language, source))
        self.loaded(FAMILY_PROTECTION='ADMIN')
        for language in SUPPORTED_LANGUAGES:
            self.panel.retranslate(language)
            self.assert_latin(language)
            self.assertIsNone(re.search(r'\{[A-Za-z_][^}]*\}', self.panel.status.text()))
        self.jobs.assert_not_called()

    def test_long_locale_rows_fit_and_deleted_widgets_ignore_retranslate(self):
        self.loaded(SAFE_MODE=True, FAMILY_PROTECTION='MANAGEABLE')
        for language in ('fr', 'pt', 'uz', 'be'):
            self.panel.retranslate(language)
            self.panel.resize(460, 1100)
            self.app.processEvents()
            for row in (self.panel.family_row, self.panel.safe_mode_row, *self.panel.rows.values()):
                for label in (row.title_label, row.subtitle_label):
                    if not label.isVisibleTo(self.panel):
                        continue
                    bounds = label.fontMetrics().boundingRect(0, 0, label.width(), 10000,
                        Qt.TextFlag.TextWordWrap, label.text())
                    self.assertGreaterEqual(label.height(), bounds.height(), (language, label.text()))
        self.panel.rows['HIDDEN'].value_label.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.panel.retranslate('en')
        self.jobs.assert_not_called()
