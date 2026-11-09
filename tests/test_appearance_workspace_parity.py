"""Native-style appearance layout, synthetic samples and local-only controls."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QFont, QFontDatabase, QWheelEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QComboBox

from maxclient.core.appearance import CHAT_FONT_SCALES, CHAT_MESSAGE_FONT_PIXELS, CHAT_THEME_IDS
from maxclient.core.config import Settings
from maxclient.ui.settings_workspace import SettingsWorkspace
from maxclient.ui.theme import build_stylesheet
from maxclient.ui.widgets.appearance_preview import ConversationAppearancePreview, PREVIEW_MESSAGES
from test_settings_workspace import SyntheticService


class AppearanceWorkspaceParityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setStyle('Fusion')
        for name in ('segoeui.ttf', 'segoeuib.ttf', 'seguisb.ttf', 'seguiemj.ttf', 'seguisym.ttf'):
            font_path = Path('C:/Windows/Fonts') / name
            if font_path.is_file():
                identifier = QFontDatabase.addApplicationFont(str(font_path))
                families = QFontDatabase.applicationFontFamilies(identifier)
                if name == 'segoeui.ttf' and families:
                    cls.app.setFont(QFont(families[0], 10))

    def setUp(self):
        self.widgets = []
        self.app.setStyleSheet(build_stylesheet('light', '#0085FF'))

    def tearDown(self):
        for widget in self.widgets:
            widget.close()
            widget.deleteLater()
        self.app.processEvents()

    def panel(self, **values):
        settings = Settings(**{'theme': 'light', 'accent': '#0085FF', **values})
        settings.save = Mock()
        service = SyntheticService()
        # Opening Appearance must not read history or start account operations.
        for name in ('history', 'local_history', 'open_chat', 'send_text'):
            setattr(service, name, Mock(side_effect=AssertionError(name)))
        with patch('maxclient.ui.settings_workspace.run_async') as runner:
            panel = SettingsWorkspace(service, settings)
            panel.resize(1176, 760)
            panel.show()
            panel.select_page('appearance')
            self.app.processEvents()
            runner.assert_not_called()
        self.widgets.append(panel)
        return panel, settings, service

    def test_open_and_restyle_are_readonly_and_preserve_saved_choices(self):
        panel, settings, service = self.panel(theme='system', chat_theme='simple', chat_font_scale='ax1', accent='#8759FF')
        panel.apply_theme()
        self.app.processEvents()
        self.assertEqual(list(panel.theme_buttons), ['system', 'light', 'dark'])
        self.assertTrue(panel.theme_buttons['system'].isChecked())
        self.assertTrue(panel.preset_buttons['simple'].isChecked())
        self.assertEqual(panel.font_slider.value(), 6)
        self.assertEqual(settings.accent, '#8759FF')
        self.assertGreater(panel.preset_scroll.horizontalScrollBar().value(), 0)
        self.assertTrue(panel.preset_buttons['simple'].label.property('selected'))
        settings.save.assert_not_called()
        self.assertEqual(panel.page_widgets['appearance'].findChildren(QComboBox), [])
        for name in ('history', 'local_history', 'open_chat', 'send_text'):
            getattr(service, name).assert_not_called()
        self.assertEqual(service.profile_writes + service.avatar_writes, [])
        self.assertEqual(service.session_reads + service.termination_writes, 0)

    def test_segments_update_preview_and_save_once_for_new_choice(self):
        panel, settings, _ = self.panel()
        changed = Mock()
        panel.theme_changed.connect(changed)
        for theme in ('dark', 'system', 'light'):
            before = settings.save.call_count
            panel.theme_buttons[theme].click()
            self.app.processEvents()
            self.assertEqual(settings.theme, theme)
            self.assertEqual(panel.appearance_preview.theme, theme)
            self.assertEqual(sum(b.isChecked() for b in panel.theme_buttons.values()), 1)
            self.assertEqual(settings.save.call_count, before + 1)
            panel.theme_buttons[theme].click()
            self.assertEqual(settings.save.call_count, before + 1)
        self.assertEqual(changed.call_count, 3)

    def test_static_preview_uses_all_fonts_presets_and_wraps_without_clipping(self):
        preview = ConversationAppearancePreview('light', 'space', 'large')
        self.widgets.append(preview)
        preview.resize(640, 230)
        preview.show()
        for theme in ('light', 'dark'):
            for preset in CHAT_THEME_IDS:
                for index, scale in enumerate(CHAT_FONT_SCALES):
                    preview.resize(640 if index < 4 else 280, preview.minimumHeight())
                    preview.apply_appearance(theme, preset, scale)
                    self.app.processEvents()
                    self.assertEqual(preview.preset, preset)
                    self.assertEqual([b.message_label.text() for b in preview.bubbles], list(PREVIEW_MESSAGES))
                    previous_bottom = 0
                    for bubble in preview.bubbles:
                        text = bubble.message_label
                        self.assertEqual(text.textFormat(), Qt.TextFormat.PlainText)
                        self.assertEqual(text.font().pixelSize(), CHAT_MESSAGE_FONT_PIXELS[index])
                        self.assertGreaterEqual(text.height(), text.heightForWidth(text.width()))
                        self.assertGreaterEqual(bubble.y(), previous_bottom)
                        self.assertGreaterEqual(bubble.x(), 0)
                        self.assertLessEqual(bubble.geometry().right(), preview.width())
                        previous_bottom = bubble.geometry().bottom()
                    self.assertGreaterEqual(preview.minimumHeight(), previous_bottom)

    def test_horizontal_strip_preserves_single_row_and_keyboard_reaches_last(self):
        panel, settings, _ = self.panel()
        panel.resize(800, 650)
        self.app.processEvents()
        buttons = list(panel.preset_buttons.values())
        self.assertEqual(len({b.y() for b in buttons}), 1)
        self.assertEqual([b.preview.size().toTuple() for b in buttons], [(112, 80)] * 8)
        bar = panel.preset_scroll.horizontalScrollBar()
        self.assertGreater(bar.maximum(), 0)
        last = buttons[-1]
        last.setFocus(Qt.FocusReason.TabFocusReason)
        self.app.processEvents()
        self.assertGreater(bar.value(), 0)
        viewport = panel.preset_scroll.viewport()
        self.assertGreaterEqual(last.mapTo(viewport, QPoint()).x(), 0)
        self.assertLessEqual(last.mapTo(viewport, QPoint(last.width(), 0)).x(), viewport.width())
        QTest.keyClick(last, Qt.Key.Key_Space)
        self.assertEqual(settings.chat_theme, 'simple')
        self.assertEqual(panel.appearance_preview.preset, 'simple')
        self.assertTrue(last.preview.selected)
        self.assertEqual(sum(b.preview.selected for b in buttons), 1)
        # A normal wheel moves the horizontal gallery without losing the page.
        bar.setValue(0)
        wheel = QWheelEvent(QPointF(20, 20), QPointF(20, 20), QPoint(), QPoint(0, -120),
                            Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
                            Qt.ScrollPhase.NoScrollPhase, False)
        QApplication.sendEvent(viewport, wheel)
        self.assertGreater(bar.value(), 0)

    def test_all_local_controls_update_static_preview_without_account_calls(self):
        panel, settings, service = self.panel()
        for index, scale in enumerate(CHAT_FONT_SCALES):
            panel.font_slider.setValue(index)
            self.assertEqual(settings.chat_font_scale, scale)
            self.assertEqual(panel.appearance_preview._font_scale, scale)
        for preset, button in panel.preset_buttons.items():
            button.click()
            self.assertEqual(settings.chat_theme, preset)
            self.assertEqual(panel.appearance_preview.preset, preset)
        for name in ('history', 'local_history', 'open_chat', 'send_text'):
            getattr(service, name).assert_not_called()
        self.assertEqual(service.profile_writes + service.avatar_writes, [])

    def test_preset_changes_sample_pixels_instead_of_only_wallpaper(self):
        preview = ConversationAppearancePreview('light', 'feb23', 'large')
        self.widgets.append(preview)
        preview.resize(640, 230)
        preview.show()
        self.app.processEvents()
        bubble = preview.bubbles[1]
        image = bubble.grab().toImage()
        self.assertEqual(image.pixelColor(3, image.height() // 2).name(), '#e0fff9')
        preview.apply_appearance('light', 'mar8', 'large')
        self.app.processEvents()
        image = bubble.grab().toImage()
        self.assertEqual(image.pixelColor(3, image.height() // 2).name(), '#f6e0ff')
        preview.apply_appearance('light', 'simple', 'large')
        self.app.processEvents()
        image = bubble.grab().toImage()
        self.assertNotEqual(image.pixelColor(3, image.height() // 2),
                            image.pixelColor(image.width() - 4, image.height() // 2))

    def test_capture_light_dark_and_narrow_max_font(self):
        output = Path('.test-tmp')
        output.mkdir(exist_ok=True)
        for theme, width, scale, suffix in (
            ('light', 1218, 'large', 'light'),
            ('dark', 1218, 'large', 'dark'),
            ('light', 816, 'ax2', 'narrow_font32'),
        ):
            panel, _, _ = self.panel(theme=theme, chat_font_scale=scale)
            self.app.setStyleSheet(build_stylesheet(theme, '#0085FF'))
            panel.apply_theme()
            panel.resize(width, 752)
            self.app.processEvents()
            self.app.processEvents()
            self.assertTrue(panel.grab().save(str(output / f'appearance_native_{suffix}_20261103.png')))
            if scale == 'ax2':
                scroll = panel.page_widgets['appearance']
                self.assertGreater(scroll.verticalScrollBar().maximum(), 0)
                scroll.verticalScrollBar().setValue(scroll.verticalScrollBar().maximum())
                self.app.processEvents()
                self.assertTrue(panel.grab().save(str(output / 'appearance_native_narrow_bottom_20261103.png')))


if __name__ == '__main__':
    unittest.main()
