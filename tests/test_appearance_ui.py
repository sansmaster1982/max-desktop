"""Offline appearance integration and verified SVG rendering; synthetic data only."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import hashlib
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtWidgets import QApplication, QGridLayout, QLabel, QVBoxLayout, QWidget

from maxclient.core.appearance import CHAT_FONT_SCALES, CHAT_MESSAGE_FONT_PIXELS, CHAT_THEME_IDS
from maxclient.core.config import Settings
from maxclient.core.models import Chat, Message
from maxclient.ui.appearance_tokens import PATTERN_ASSETS, PATTERN_SHA256, PATTERN_SIZES, PRESET_LABELS
from maxclient.ui.chat_view import ChatView
from maxclient.ui.main_window import MainWindow
from maxclient.ui.theme import build_stylesheet
from maxclient.ui.widgets.chat_wallpaper import ASSET_DIR, ChatWallpaper, centered_mask_offset, css_color
from test_settings_workspace import SyntheticService, immediate


class AppearanceUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        for name in ('segoeui.ttf', 'segoeuib.ttf', 'seguisb.ttf'):
            path = Path('C:/Windows/Fonts') / name
            if path.is_file():
                identifier = QFontDatabase.addApplicationFont(str(path))
                if name == 'segoeui.ttf':
                    families = QFontDatabase.applicationFontFamilies(identifier)
                    if families:
                        cls.app.setFont(QFont(families[0], 10))
        cls.app.setStyle('Fusion')

    def setUp(self):
        self.app.setStyleSheet(build_stylesheet('light', '#0085FF'))
        self.patches = [patch('maxclient.ui.main_window.run_async'),
                        patch('maxclient.ui.settings_workspace.run_async', side_effect=immediate),
                        patch('maxclient.ui.privacy_settings_panel.run_async', side_effect=immediate)]
        for item in self.patches:
            item.start()
        self.widgets = []

    def tearDown(self):
        for widget in self.widgets:
            widget.close()
        for item in reversed(self.patches):
            item.stop()
        self.app.processEvents()

    def window(self):
        service = SyntheticService()
        settings = Settings(theme='light', accent='#0085FF')
        settings.save = Mock()
        window = MainWindow(service, settings)
        self.widgets.append(window)
        window.resize(1240, 760)
        window.show()
        window._open_settings()
        window._settings_workspace.select_page('appearance')
        self.app.processEvents()
        return window, window._settings_workspace, service, settings

    def test_scheme_font_and_all_eight_presets_are_local_functional_controls(self):
        window, panel, service, settings = self.window()
        self.assertEqual(list(panel.theme_buttons), ['system', 'light', 'dark'])
        self.assertEqual((panel.font_slider.minimum(), panel.font_slider.maximum(), panel.font_slider.singleStep()),
                         (0, 7, 1))
        self.assertEqual(panel.font_slider.value(), 2)
        self.assertEqual(tuple(panel.preset_buttons), CHAT_THEME_IDS)
        for index, scale in enumerate(CHAT_FONT_SCALES):
            panel.font_slider.setValue(index)
            self.assertEqual(settings.chat_font_scale, scale)
            self.assertEqual(panel.font_slider.accessibleDescription(), f'{CHAT_MESSAGE_FONT_PIXELS[index]} пикселей')
        for preset, button in panel.preset_buttons.items():
            button.click()
            self.app.processEvents()
            self.assertEqual(settings.chat_theme, preset)
            self.assertEqual(window.chat_view.container.preset, preset)
            self.assertEqual(sum(b.isChecked() for b in panel.preset_buttons.values()), 1)
        panel.theme_buttons['system'].click()
        self.assertEqual(settings.theme, 'system')
        self.assertEqual(service.profile_writes, [])
        self.assertEqual(service.avatar_writes, [])
        self.assertEqual(service.termination_writes, 0)
        self.assertEqual(service.session_reads, 0)

    def test_font32_wraps_without_changing_literal_text_metadata_draft_or_reply(self):
        service = Mock()
        settings = Settings(theme='light')
        view = ChatView(service, settings)
        self.widgets.append(view)
        view.resize(720, 620)
        view.chat = Chat(id=42, title='Synthetic')
        view._set_composer_visible(True)
        literal = '<b>literal</b> tokenAttrs REGISTER: Пример длинного сообщения для переноса строк. ' * 3
        message = Message(chat_id=42, id=81, sender=77, time_ms=1793459502608, text=literal,
                          reaction_info={'counters': [{'reaction': '👍', 'count': 2}], 'yourReaction': '👍'},
                          link={'type': 'REPLY', 'messageId': 70})
        view._full_render([message])
        view.input.setPlainText('synthetic draft <literal>')
        view._set_reply_target(message)
        view.show()
        self.app.processEvents()
        before = view._rows[0]['bubble'].findChild(QLabel, 'MessageText')
        old_height = before.height()
        settings.chat_font_scale = 'ax2'
        view.apply_appearance()
        self.app.processEvents()
        self.app.processEvents()  # Flush the propagated scroll-content layout request.
        after = view._rows[0]['bubble'].findChild(QLabel, 'MessageText')
        self.assertEqual(after.text(), literal)
        self.assertEqual(after.textFormat(), Qt.TextFormat.PlainText)
        self.assertEqual(after.font().pixelSize(), 32)
        self.assertTrue(after.wordWrap())
        self.assertGreater(after.height(), old_height)
        self.assertGreater(after.height(), after.fontMetrics().lineSpacing() * 2)
        self.assertGreaterEqual(after.height(), after.heightForWidth(after.width()),
                                f'body {after.size().toTuple()}, required {after.heightForWidth(after.width())}')
        frame = after.parentWidget()
        self.assertGreaterEqual(frame.height(), frame.minimumSizeHint().height())
        self.assertGreaterEqual(view.container.height(), view.container.minimumSizeHint().height())
        self.assertEqual(view._rows[0]['msg'].reaction_info, message.reaction_info)
        self.assertEqual(view._rows[0]['msg'].link, message.link)
        self.assertEqual(view.input.toPlainText(), 'synthetic draft <literal>')
        self.assertIs(view._reply_target, message)
        service.open_chat.assert_not_called()
        service.local_history.assert_not_called()
        output = Path('.test-tmp/appearance_font32.png')
        output.parent.mkdir(exist_ok=True)
        self.assertTrue(view.grab().save(str(output)))
        view.scroll.verticalScrollBar().setValue(view.scroll.verticalScrollBar().maximum())
        self.app.processEvents()
        self.assertTrue(view.grab().save(str(output.with_name('appearance_font32_bottom.png'))))

    def test_css_alpha_is_not_misread_as_qcolor_argb(self):
        self.assertEqual(css_color('#007aff4d').getRgb(), (0, 122, 255, 77))
        self.assertEqual(css_color('#fff6').getRgb(), (255, 255, 255, 102))
        self.assertEqual(css_color('#0000').getRgb(), (0, 0, 0, 0))
        self.assertEqual(css_color('#fff').getRgb(), (255, 255, 255, 255))

    def test_centered_repeated_masks_handle_negative_positions_and_resize(self):
        for width, height, tw, th in [(360, 420, 280, 280), (1000, 1001, 284, 284),
                                     (141, 69, 280, 280), (281, 283, 280, 280)]:
            offset = centered_mask_offset(width, height, tw, th)
            self.assertTrue(0 <= offset.x() < tw)
            self.assertTrue(0 <= offset.y() < th)
            self.assertEqual(offset, centered_mask_offset(width + 2 * tw, height + 2 * th, tw, th))
        self.assertEqual(centered_mask_offset(360, 420, 280, 280).toTuple(), (240., 210.))

    def test_original_masks_match_public_variant_hashes_and_intrinsic_sizes(self):
        for name, expected in PATTERN_SHA256.items():
            self.assertEqual(hashlib.sha256((ASSET_DIR / name).read_bytes()).hexdigest(), expected)
        for preset in CHAT_THEME_IDS:
            surface = ChatWallpaper('light', preset=preset)
            self.widgets.append(surface)
            surface.resize(360, 240)
            surface.show()
            self.app.processEvents()
            if preset == 'simple':
                self.assertIsNone(surface._tile)
            else:
                self.assertEqual(surface._tile.size().toTuple(), PATTERN_SIZES[preset])

    def test_wallpaper_caches_paints_and_invalidates_size_scheme_and_preset(self):
        surface = ChatWallpaper('light')
        self.widgets.append(surface)
        surface.resize(360, 240)
        with patch.object(surface, '_render_canvas', wraps=surface._render_canvas) as render:
            surface.show()
            self.app.processEvents()
            surface.grab()
            surface.grab()
            self.assertEqual(render.call_count, 1)
            surface.resize(402, 287)
            self.app.processEvents()
            surface.grab()
            self.assertEqual(render.call_count, 2)
            surface.set_appearance('dark', 'nature')
            self.app.processEvents()
            surface.grab()
            self.assertEqual(render.call_count, 3)
            surface.set_appearance('dark', 'nature')
            self.app.processEvents()
            surface.grab()
            self.assertEqual(render.call_count, 3)

    def test_simple_has_exact_flat_colors_and_every_preset_renders_both_schemes(self):
        for scheme in ('light', 'dark'):
            catalog = QWidget()
            catalog.setStyleSheet(build_stylesheet(scheme, '#0085FF'))
            self.widgets.append(catalog)
            grid = QGridLayout(catalog)
            for index, preset in enumerate(CHAT_THEME_IDS):
                box = QWidget()
                layout = QVBoxLayout(box)
                name = QLabel(PRESET_LABELS[preset])
                name.setTextFormat(Qt.TextFormat.PlainText)
                name.setWordWrap(True)
                layout.addWidget(name)
                surface = ChatWallpaper(scheme, preset=preset)
                surface.setFixedSize(220, 140)
                layout.addWidget(surface)
                grid.addWidget(box, index // 4, index % 4)
                if preset == 'simple':
                    simple = surface
            catalog.show()
            self.app.processEvents()
            self.assertEqual(simple.grab().toImage().pixelColor(100, 100).name(),
                             '#ffffff' if scheme == 'light' else '#0e0e11')
            output = Path(f'.test-tmp/appearance_presets_{scheme}.png')
            output.parent.mkdir(exist_ok=True)
            self.assertTrue(catalog.grab().save(str(output)))

    def test_controls_fit_minimum_viewport_and_synthetic_theme_captures(self):
        window, panel, _service, settings = self.window()
        for width, height, theme, name in [(1240, 760, 'light', 'appearance_light'),
                                           (880, 560, 'light', 'appearance_minimum'),
                                           (1240, 760, 'dark', 'appearance_dark')]:
            settings.theme = theme
            self.app.setStyleSheet(build_stylesheet(theme, settings.accent))
            window._apply_local_theme()
            window.resize(width, height)
            self.app.processEvents()
            for button in panel.preset_buttons.values():
                self.assertGreaterEqual(button.width(), 56)
                self.assertEqual(button.label.font().pixelSize(), 12)
                self.assertGreaterEqual(button.label.height(), button.label.fontMetrics().lineSpacing() * 2)
                self.assertLessEqual(button.x() + button.width(), button.parentWidget().width())
            self.assertTrue(window.grab().save(str(Path('.test-tmp') / f'{name}.png')))


if __name__ == '__main__':
    unittest.main()
