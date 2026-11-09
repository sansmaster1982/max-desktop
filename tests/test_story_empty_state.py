"""Confirmed story archive empty state, rendered with fake-only offscreen Qt."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from types import SimpleNamespace
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from PySide6.QtCore import QByteArray, QCoreApplication, QEvent, Qt
from PySide6.QtGui import QColor, QFont, QFontDatabase, QFontMetrics
from PySide6.QtWidgets import QApplication, QVBoxLayout, QWidget
from shiboken6 import isValid

from maxclient.ui.settings_read_panels import SettingsReadPanel
from maxclient.ui.theme import build_stylesheet


class StoryEmptyStateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls._original_font = QFont(cls.app.font())
        cls._font_id = -1
        # Restricted Windows processes may not enumerate system fonts. Load
        # the installed product font explicitly for meaningful text renders.
        font = QFont('Segoe UI', 10)
        if not QFontMetrics(font).inFontUcs4(0x041C):
            path = Path('C:/Windows/Fonts/segoeui.ttf')
            if path.is_file():
                cls._font_id = QFontDatabase.addApplicationFontFromData(QByteArray(path.read_bytes()))
        cls.app.setFont(font)

    @classmethod
    def tearDownClass(cls):
        cls.app.setFont(cls._original_font)
        if cls._font_id >= 0:
            QFontDatabase.removeApplicationFont(cls._font_id)

    def setUp(self):
        self.service = SimpleNamespace(settings=SimpleNamespace(theme='light'),
                                       list_own_stories=Mock(), get_folders=Mock())
        self.host = QWidget()
        layout = QVBoxLayout(self.host)
        layout.setContentsMargins(16, 16, 16, 16)
        self.panel = SettingsReadPanel(self.service, 'stories', self.host)
        layout.addWidget(self.panel)
        self.host.resize(480, 540)
        self.host.show()
        self.app.processEvents()

    def tearDown(self):
        self.service.list_own_stories.assert_not_called()
        self.service.get_folders.assert_not_called()
        if isValid(self.host):
            self.host.close()
            self.host.deleteLater()
            QCoreApplication.sendPostedEvents(self.host, QEvent.Type.DeferredDelete)
        self.app.processEvents()

    def test_unknown_initial_archive_is_not_presented_as_empty(self):
        self.assertFalse(self.panel._loaded)
        self.assertFalse(self.panel._confirmed_empty)
        self.assertTrue(self.panel.empty_card.isHidden())
        self.assertTrue(self.panel.status.isVisible())
        self.assertIn('Обновить', self.panel.status.text())

    def test_confirmed_empty_end_has_only_card_and_accessible_refresh(self):
        for result in ({'stories': []}, {'stories': [], 'marker': 0}):
            with self.subTest(result=result):
                self.panel._stories(result)
                self.app.processEvents()
                self.assertTrue(self.panel._confirmed_empty)
                self.assertTrue(self.panel.empty_card.isVisible())
                self.assertTrue(self.panel.items.isHidden())
                self.assertTrue(self.panel.status.isHidden())
                self.assertTrue(self.panel.note.isHidden())
                self.assertTrue(self.panel.more_button.isHidden())
                self.assertTrue(self.panel.refresh_button.isEnabled())
                self.assertEqual(self.panel.refresh_button.accessibleName(), 'Обновить архив историй')
                self.assertEqual(self.panel.empty_card.title.text(), 'Место для воспоминаний')
                self.assertEqual(self.panel.empty_card.subtitle.text(),
                                 'Здесь можно будет пересмотреть истории и вспомнить, как всё было')

    def test_empty_page_with_continuation_is_partial_not_empty_archive(self):
        self.panel._stories({'stories': [], 'marker': 123})
        self.assertFalse(self.panel._confirmed_empty)
        self.assertTrue(self.panel.empty_card.isHidden())
        self.assertTrue(self.panel.more_button.isVisible())
        self.assertIn('не полностью', self.panel.status.text())
        with patch('maxclient.ui.settings_read_panels.run_async') as run:
            self.panel._load(more=True)
        self.assertEqual(run.call_args.args, (self.service.list_own_stories,))
        self.assertEqual(run.call_args.kwargs['marker'], 123)
        self.panel._stories({'stories': []}, append=True)
        self.assertTrue(self.panel._confirmed_empty)

    def test_bad_or_unknown_reply_never_keeps_the_empty_success_card(self):
        for result in ({}, {'stories': None}, {'stories': [None]},
                       {'stories': [{'id': True}]}, {'stories': [], 'marker': None},
                       {'stories': [], 'marker': '0'}, {'stories': [], 'marker': True}):
            with self.subTest(result=result):
                self.panel._stories({'stories': []})
                self.panel._stories(result)
                self.assertTrue(self.panel.empty_card.isHidden())
                self.assertFalse(self.panel._confirmed_empty)
                self.assertIn('Не удалось', self.panel.status.text())
        self.panel._stories({'stories': [], 'marker': -1})
        self.assertFalse(self.panel._confirmed_empty)
        self.assertIn('не полностью', self.panel.status.text())

    def test_refresh_loading_and_error_replace_previous_empty_success(self):
        self.panel._stories({'stories': []})
        with patch('maxclient.ui.settings_read_panels.run_async') as run:
            self.panel.refresh()
            self.panel.refresh()
            run.assert_called_once()
            self.assertEqual(run.call_args.kwargs['marker'], 0)
            self.assertTrue(self.panel.empty_card.isHidden())
            self.assertEqual(self.panel.status.text(), 'Загрузка…')
            self.assertFalse(self.panel.refresh_button.isEnabled())
            run.call_args.kwargs['on_error']('synthetic error not displayed')
        self.assertFalse(self.panel._confirmed_empty)
        self.assertTrue(self.panel.empty_card.isHidden())
        self.assertIn('Не удалось', self.panel.status.text())
        self.assertTrue(self.panel.refresh_button.isEnabled())

    def test_nonempty_archive_keeps_list_note_dedup_and_last_page(self):
        self.panel._stories({'stories': []})
        story = {'id': 7, 'time': 123000, 'viewsCount': 2}
        self.panel._stories({'stories': [story], 'marker': 100})
        self.assertTrue(self.panel.empty_card.isHidden())
        self.assertTrue(self.panel.items.isVisible())
        self.assertTrue(self.panel.note.isVisible())
        self.assertTrue(self.panel.more_button.isVisible())
        self.panel._stories({'stories': [story, {'id': 8, 'time': 100000}]}, append=True)
        self.assertEqual(self.panel.items.count(), 2)
        self.assertIsNone(self.panel._marker)
        self.panel._stories({'stories': []}, append=True)
        self.assertEqual(self.panel.items.count(), 2)
        self.assertFalse(self.panel._confirmed_empty)
        self.assertEqual(self.panel.status.text(), 'Историй: 2')

    def test_folder_kind_keeps_its_existing_empty_list_and_note(self):
        folder = SettingsReadPanel(self.service, 'folders', self.host)
        folder.show()
        folder._folders({'orderedFolders': []})
        self.assertIsNone(folder.empty_card)
        self.assertFalse(folder.items.isHidden())
        self.assertFalse(folder.note.isHidden())
        self.assertEqual(folder.status.text(), 'Папки не созданы.')
        self.assertEqual(folder.refresh_button.objectName(), 'SettingsContentAction')

    def test_theme_update_preserves_confirmed_state_without_request(self):
        self.panel._stories({'stories': []})
        for theme in ('dark', 'light', 'system'):
            self.panel.apply_theme(theme)
            self.assertTrue(self.panel._confirmed_empty)
            self.assertTrue(self.panel.empty_card.isVisible())
            self.assertFalse(self.panel._busy)
            self.assertTrue(self.panel._loaded)
            self.assertEqual(self.panel.empty_card.subtitle.textFormat(), Qt.TextFormat.PlainText)

    def test_light_dark_render_and_narrow_text_geometry(self):
        self.assertTrue(QFontMetrics(self.panel.empty_card.title.font()).inFontUcs4(0x041C))
        self.panel._stories({'stories': []})
        card = self.panel.empty_card
        for theme, surface in (('light', '#FFFFFF'), ('dark', '#141821')):
            for width in (320, 480, 680):
                with self.subTest(theme=theme, width=width):
                    self.host.resize(width, 540)
                    self.panel.apply_theme(theme)
                    self.app.processEvents()
                    image = card.grab().toImage()
                    self.assertFalse(image.isNull())
                    self.assertEqual(image.pixelColor(24, 24), QColor(surface))
                    for label in (card.title, card.subtitle):
                        self.assertTrue(card.rect().contains(label.geometry()))
                        self.assertGreater(label.width(), 0)
                        self.assertGreaterEqual(label.height(), label.heightForWidth(label.width()))

    def test_real_application_stylesheet_keeps_title_and_subtitle_hierarchy(self):
        previous = self.app.styleSheet()
        try:
            for theme in ('light', 'dark'):
                self.app.setStyleSheet(build_stylesheet(theme, '#0085FF'))
                self.panel.apply_theme(theme)
                self.panel._stories({'stories': []})
                self.app.processEvents()
                card = self.panel.empty_card
                self.assertEqual(card.title.font().pixelSize(), 19)
                self.assertEqual(card.title.font().weight(), QFont.Weight.DemiBold)
                self.assertEqual(card.subtitle.font().pixelSize(), 14)
                self.assertTrue(card.isVisible())
                for label in (card.title, card.subtitle):
                    self.assertGreaterEqual(label.height(), label.heightForWidth(label.width()))
        finally:
            self.app.setStyleSheet(previous)

    def test_deleted_empty_card_blocks_late_callbacks_and_refresh(self):
        card = self.panel.empty_card
        card.deleteLater()
        QCoreApplication.sendPostedEvents(card, QEvent.Type.DeferredDelete)
        self.assertFalse(isValid(card))
        with patch('maxclient.ui.settings_read_panels.run_async') as run:
            self.panel._stories({'stories': []})
            self.panel._failed('synthetic')
            self.panel.refresh()
            self.panel.apply_theme('dark')
            run.assert_not_called()
        self.assertFalse(self.panel._loaded)


if __name__ == '__main__':
    unittest.main()
