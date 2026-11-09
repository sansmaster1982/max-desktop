"""Synthetic device-card presentation; no account, transport or OS actions."""
import copy
from datetime import datetime
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from PySide6.QtCore import QCoreApplication, QEvent, QRect, Qt
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtWidgets import QApplication, QPushButton

from maxclient.ui.widgets.settings_device_cards import (
    DeviceSessionRow, DeviceSessionsIntro, DeviceSessionsLogoutRow, INTRO_SUBTITLE,
)


class DeviceCardTests(unittest.TestCase):
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

    def widget(self, cls, *args, **kwargs):
        widget = cls(*args, **kwargs)
        self.addCleanup(widget.close)
        return widget

    def test_known_fields_plaintext_and_current_only_when_confirmed(self):
        session = {'client': '<b>WEB</b>', 'info': '<img src="file:///synthetic">',
                   'location': 'Synthetic place', 'current': True, 'time': 1_790_000_000_000}
        original = copy.deepcopy(session)
        row = self.widget(DeviceSessionRow, session)
        self.assertEqual(row.title_label.text(), '<b>WEB</b> (Текущая)')
        self.assertEqual(row.info_label.text(), session['info'])
        self.assertEqual(row.location_label.text(), session['location'])
        for label in (row.title_label, row.info_label, row.location_label, row.time_label):
            self.assertEqual(label.textFormat(), Qt.TextFormat.PlainText)
        self.assertEqual(session, original)

    def test_false_missing_and_truthy_nonstrict_current_are_not_labeled_current(self):
        for current in (False, None, 1, 'true', {'current': True}):
            with self.subTest(current=current):
                row = self.widget(DeviceSessionRow, {'client': 'WEB', 'current': current})
                self.assertEqual(row.title_label.text(), 'WEB')
                self.assertNotIn('Текущая', row.text())

    def test_null_only_metadata_omitted_but_actual_mixed_text_preserved(self):
        row = self.widget(DeviceSessionRow, {'client': 'WEB', 'info': 'null, null', 'location': ' null '})
        self.assertTrue(row.info_label.isHidden())
        self.assertTrue(row.location_label.isHidden())
        self.assertEqual(row.text(), 'WEB')
        second = self.widget(DeviceSessionRow, {'client': 'WEB', 'info': 'null · Browser, null', 'location': 'City'})
        self.assertEqual(second.info_label.text(), 'Browser, null')
        self.assertEqual(second.location_label.text(), 'City')

    def test_timestamp_is_supplied_value_without_online_or_current_heuristic(self):
        stamp = 1_790_000_000_000
        row = self.widget(DeviceSessionRow, {'client': 'WEB', 'time': stamp, 'current': True, 'online': True})
        expected = datetime.fromtimestamp(stamp / 1000).astimezone().strftime('%d.%m.%Y %H:%M')
        self.assertEqual(row.time_label.text(), expected)
        self.assertNotIn('В сети', row.text())
        self.assertNotIn('онлайн', row.text().lower())

    def test_invalid_timestamps_and_containers_never_become_display_strings(self):
        for value in (None, True, '2026-11-03', float('nan'), 2**63 - 1, 2**63, {'time': 1}):
            with self.subTest(time=value):
                row = self.widget(DeviceSessionRow, {'client': ['WEB'], 'info': {'name': 'secret'}, 'time': value})
                self.assertEqual(row.title_label.text(), 'Устройство')
                self.assertTrue(row.info_label.isHidden())
                self.assertTrue(row.time_label.isHidden())
                self.assertEqual(row.text(), 'Устройство')

    def test_unknown_fields_not_retained_and_source_later_change_not_applied(self):
        source = {'client': 'Synthetic device', 'token': 'synthetic-do-not-display', 'current': False}
        row = self.widget(DeviceSessionRow, source)
        source['client'] = 'Changed after snapshot'
        source['current'] = True
        self.assertEqual(row.text(), 'Synthetic device')
        self.assertFalse(hasattr(row, '_session'))
        self.assertNotIn('synthetic-do-not-display', str(row.__dict__))

    def test_localization_translates_owned_labels_not_device_metadata(self):
        with patch('maxclient.ui.widgets.settings_device_cards.tr', side_effect=lambda text, language: f'{language}:{text}') as translate:
            row = self.widget(DeviceSessionRow, {'client': 'Устройство', 'info': 'Текущая', 'location': 'Москва', 'current': True})
            translate.reset_mock()
            row.set_language('en')
            self.assertEqual(row.title_label.text(), 'Устройство (en:Текущая)')
            self.assertEqual(row.info_label.text(), 'Текущая')
            self.assertEqual(row.location_label.text(), 'Москва')
            self.assertEqual(translate.call_args_list[0].args, ('Текущая', 'en'))
            self.assertEqual(translate.call_count, 1)

    def test_intro_layout_and_language_use_only_static_copy(self):
        intro = self.widget(DeviceSessionsIntro)
        intro.resize(640, 200)
        intro.show()
        self.app.processEvents()
        self.assertEqual(intro.minimumHeight(), 200)
        self.assertEqual(intro.icon_label.size().width(), 72)
        self.assertEqual(intro.icon_label.size().height(), 72)
        self.assertEqual(intro.title_label.text(), 'Устройства с MAX')
        self.assertEqual(intro.subtitle_label.text(), INTRO_SUBTITLE)
        intro.set_language('en')
        self.assertNotEqual(intro.title_label.text(), 'Устройства с MAX')
        self.assertNotEqual(intro.subtitle_label.text(), INTRO_SUBTITLE)

    def test_narrow_row_grows_for_actual_long_metadata_without_clipping(self):
        row = self.widget(DeviceSessionRow, {
            'client': 'Synthetic desktop client with several long words',
            'info': 'Synthetic system description with a full version and build',
            'location': 'Synthetic location with a number of words',
            'time': 1_790_000_000_000, 'current': True})
        row.resize(300, 88)
        row.show()
        self.app.processEvents()
        self.assertGreater(row.minimumHeight(), 88)
        for label in (row.title_label, row.info_label, row.location_label):
            required = label.fontMetrics().boundingRect(
                QRect(0, 0, label.width(), 10000), Qt.TextFlag.TextWordWrap, label.text()).height()
            self.assertGreaterEqual(label.height(), required)
            self.assertLessEqual(label.geometry().bottom(), row.height())

    def test_theme_and_language_refresh_do_not_emit_logout_click(self):
        button = self.widget(DeviceSessionsLogoutRow)
        clicked = Mock()
        button.clicked.connect(clicked)
        for theme in ('dark', 'light', 'system'):
            button.apply_theme(theme)
            button.set_language('en')
        clicked.assert_not_called()
        button.setEnabled(False)
        button.click()
        clicked.assert_not_called()
        button.setEnabled(True)
        button.click()
        clicked.assert_called_once()
        self.assertEqual(button.minimumHeight(), 64)
        button.setText('Synthetic localized action')
        self.assertEqual(button.text(), 'Synthetic localized action')
        self.assertEqual(QPushButton.text(button), '')

    def test_deleted_child_during_teardown_safe_for_theme_and_language(self):
        for cls, args, child in ((DeviceSessionsIntro, (), 'subtitle_label'),
                                 (DeviceSessionRow, ({'client': 'WEB'},), 'info_label'),
                                 (DeviceSessionsLogoutRow, (), 'title_label')):
            with self.subTest(widget=cls.__name__):
                widget = self.widget(cls, *args)
                getattr(widget, child).deleteLater()
                QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
                widget.apply_theme('dark')
                widget.set_language('en')

    def test_logout_wraps_at_narrow_width_in_all_supported_languages(self):
        button = self.widget(DeviceSessionsLogoutRow)
        button.resize(280, 64)
        button.show()
        for language in ('ru', 'en', 'es', 'pt', 'fr', 'uz', 'be'):
            button.set_language(language)
            self.app.processEvents()
            label = button.title_label
            required = label.fontMetrics().boundingRect(
                QRect(0, 0, label.width(), 10000), Qt.TextFlag.TextWordWrap, label.text()).height()
            self.assertGreaterEqual(label.height(), required, language)

    def test_logout_actual_pixels_red_when_enabled_gray_when_disabled(self):
        button = self.widget(DeviceSessionsLogoutRow)
        button.resize(640, 64)
        button.show()
        def colored_pixels():
            self.app.processEvents()
            image = button.title_label.grab().toImage()
            return sum(1 for y in range(image.height()) for x in range(image.width())
                       if (lambda c: c.red() - c.green() > 60 and c.red() - c.blue() > 60)(image.pixelColor(x, y)))
        self.assertGreater(colored_pixels(), 50)
        button.setEnabled(False)
        self.assertEqual(colored_pixels(), 0)


if __name__ == '__main__':
    unittest.main()
