"""Synthetic language selection; no network, accounts, dialogs or real settings."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from pathlib import Path
import unittest
from unittest.mock import Mock

from PySide6.QtCore import QCoreApplication, QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QFontDatabase, QWheelEvent
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import QApplication, QScrollArea

from maxclient.core.config import Settings
from maxclient.core.i18n import SUPPORTED_LANGUAGES
from maxclient.ui.language_panel import LanguagePanel
from maxclient.ui.theme import build_stylesheet
from test_desktop_shell import _load_offscreen_font


class LanguagePanelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setStyle('Fusion')
        _load_offscreen_font(cls.app)
        semibold = Path('C:/Windows/Fonts/seguisb.ttf')
        if semibold.is_file():
            QFontDatabase.addApplicationFont(str(semibold))

    def setUp(self):
        self.settings = Settings(ui_language='ru', locale='ru', theme='light')
        self.settings.save = Mock()
        self.settings.save_ui_language = Mock(wraps=self.settings.save_ui_language)
        self.panel = LanguagePanel(self.settings)
        self.panel.resize(640, 560)
        self.panel.show()
        self.spy = QSignalSpy(self.panel.language_changed)
        self.app.setStyleSheet(build_stylesheet('light', '#0085FF'))
        self.app.processEvents()

    def tearDown(self):
        self.panel.close()
        self.panel.deleteLater()
        QCoreApplication.sendPostedEvents(self.panel, QEvent.Type.DeferredDelete)
        self.app.processEvents()

    def test_open_retranslate_theme_and_reselect_do_not_save(self):
        self.assertEqual(tuple(self.panel.rows), SUPPORTED_LANGUAGES)
        self.assertEqual(self.panel.card.height(), 416)
        self.assertEqual(self.panel.footer.text(), 'Язык изменится только на этом устройстве')
        self.assertTrue(self.panel.coverage_note.isHidden())
        self.panel.rows['ru'].click()
        self.panel.retranslate('en')
        self.panel.apply_theme('dark')
        self.assertEqual(self.panel.rows['en'].subtitle.text(), 'English')
        self.assertTrue(self.panel.rows['ru'].isChecked())
        self.assertTrue(self.panel.coverage_note.isVisible())
        self.settings.save.assert_not_called()
        self.settings.save_ui_language.assert_not_called()
        self.assertEqual(self.spy.count(), 0)
        self.assertEqual(self.settings.locale, 'ru')

    def test_explicit_mouse_selection_saves_only_ui_language_once(self):
        QTest.mouseClick(self.panel.rows['en'], Qt.MouseButton.LeftButton)
        self.assertEqual(self.settings.ui_language, 'en')
        self.assertEqual(self.settings.locale, 'ru')
        self.settings.save_ui_language.assert_called_once_with('en')
        self.settings.save.assert_not_called()
        self.assertEqual(self.spy.count(), 1)
        self.assertEqual(self.spy.at(0), ['en'])
        self.assertEqual([code for code, row in self.panel.rows.items() if row.isChecked()], ['en'])
        QTest.mouseClick(self.panel.rows['en'], Qt.MouseButton.LeftButton)
        self.assertEqual(self.spy.count(), 1)
        self.assertEqual(self.settings.save_ui_language.call_count, 1)

    def test_keyboard_space_selects_and_arrows_escape_wheel_do_not(self):
        row = self.panel.rows['es']
        row.setFocus()
        for key in (Qt.Key.Key_Down, Qt.Key.Key_Up, Qt.Key.Key_Escape):
            QTest.keyClick(row, key)
        event = QWheelEvent(QPointF(10, 10), QPointF(row.mapToGlobal(QPoint(10, 10))),
                            QPoint(), QPoint(0, -120), Qt.MouseButton.NoButton,
                            Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.NoScrollPhase, False)
        QApplication.sendEvent(row, event)
        self.settings.save.assert_not_called()
        self.settings.save_ui_language.assert_not_called()
        QTest.keyClick(row, Qt.Key.Key_Space)
        self.assertEqual(self.settings.ui_language, 'es')
        self.assertEqual(self.settings.save_ui_language.call_count, 1)

    def test_write_failure_rolls_back_and_hides_raw_error(self):
        self.settings.save_ui_language.side_effect = OSError('synthetic private path')
        self.panel.rows['fr'].click()
        self.assertEqual(self.settings.ui_language, 'ru')
        self.assertTrue(self.panel.rows['ru'].isChecked())
        self.assertFalse(self.panel.rows['fr'].isChecked())
        self.assertEqual(self.spy.count(), 0)
        self.assertTrue(self.panel.error.isVisible())
        self.assertNotIn('private', self.panel.error.text())

    def test_loaded_language_and_synthetic_fixtures(self):
        output = Path('.test-tmp')
        output.mkdir(exist_ok=True)
        for code, theme, width in (('ru', 'light', 640), ('en', 'light', 640),
                                  ('pt', 'dark', 640), ('be', 'light', 344)):
            self.settings.ui_language = code
            self.panel.retranslate(code)
            self.panel.apply_theme(theme)
            self.app.setStyleSheet(build_stylesheet(theme, '#0085FF'))
            self.panel.resize(width, 600)
            self.app.processEvents()
            self.assertEqual([name for name, row in self.panel.rows.items() if row.isChecked()], [code])
            self.assertTrue(self.panel.grab().save(str(output / f'language_panel_{code}_{theme}_20261103.png')))
        self.settings.save.assert_not_called()
        self.settings.save_ui_language.assert_not_called()

    def test_deleted_child_makes_handlers_inert(self):
        self.panel.rows['en'].subtitle.deleteLater()
        QCoreApplication.sendPostedEvents(self.panel.rows['en'].subtitle, QEvent.Type.DeferredDelete)
        self.panel._choose('en')
        self.panel.retranslate('fr')
        self.panel.apply_theme('dark')
        self.settings.save.assert_not_called()
        self.settings.save_ui_language.assert_not_called()
        self.assertEqual(self.spy.count(), 0)


if __name__ == '__main__':
    unittest.main()
