"""Appearance settings use temporary synthetic preferences and no account data."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from PySide6.QtCore import Qt

from maxclient.app import MaxApp
from maxclient.core.appearance import CHAT_FONT_SCALES, CHAT_THEME_IDS, chat_font_index
from maxclient.core.config import Settings
from maxclient.ui.theme import DARK, LIGHT, palette_for, resolve_theme


class AppearancePreferences(unittest.TestCase):
    def test_official_local_ids_defaults_and_slider_index_are_stable(self):
        settings = Settings()
        self.assertEqual((settings.theme, settings.chat_theme, settings.chat_font_scale),
                         ('system', 'space', 'large'))
        self.assertEqual(len(CHAT_THEME_IDS), 8)
        self.assertEqual(CHAT_THEME_IDS,
                         ('space', 'feb23', 'mar8', 'nature', 'neon', 'moscow', 'lebedev', 'simple'))
        self.assertEqual(chat_font_index(settings.chat_font_scale), 2)
        for index, scale in enumerate(CHAT_FONT_SCALES):
            self.assertEqual(chat_font_index(scale), index)

    def test_unknown_preset_font_or_scheme_migrates_to_the_official_default(self):
        for value in (None, '', True, 7, [], {}, 'unverified-value'):
            settings = Settings(theme=value, chat_theme=value, chat_font_scale=value)
            self.assertEqual((settings.theme, settings.chat_theme, settings.chat_font_scale),
                             ('system', 'space', 'large'))
        self.assertEqual(Settings(theme='light').theme, 'light')
        self.assertEqual(Settings(theme='dark').theme, 'dark')

    def test_load_and_save_preserve_explicit_old_theme_and_new_local_choices(self):
        base = Path('.test-tmp')
        base.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='appearance_', dir=base) as temporary:
            folder = Path(temporary)
            paths = SimpleNamespace(settings_file=folder / 'settings.json', media_dir=folder / 'media')
            paths.settings_file.write_text(json.dumps({'theme': 'dark'}), encoding='utf-8')
            settings = Settings.load(paths)
            self.assertEqual((settings.theme, settings.chat_theme, settings.chat_font_scale),
                             ('dark', 'space', 'large'))
            settings.chat_theme = 'nature'
            settings.chat_font_scale = 'ax2'
            settings.save()
            restored = Settings.load(paths)
            self.assertEqual((restored.theme, restored.chat_theme, restored.chat_font_scale),
                             ('dark', 'nature', 'ax2'))
            self.assertEqual(set(folder.iterdir()), {paths.settings_file})

    def test_invalid_saved_appearance_values_and_non_object_json_are_safe(self):
        base = Path('.test-tmp')
        base.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='appearance_', dir=base) as temporary:
            folder = Path(temporary)
            paths = SimpleNamespace(settings_file=folder / 'settings.json', media_dir=folder / 'media')
            for data in [{'theme': [], 'chat_theme': 'unverified', 'chat_font_scale': False}, []]:
                paths.settings_file.write_text(json.dumps(data), encoding='utf-8')
                restored = Settings.load(paths)
                self.assertEqual((restored.theme, restored.chat_theme, restored.chat_font_scale),
                                 ('system', 'space', 'large'))

    def test_system_resolution_does_not_rewrite_the_preference(self):
        self.assertEqual(resolve_theme('system', color_scheme=Qt.ColorScheme.Dark), 'dark')
        self.assertEqual(resolve_theme('system', color_scheme=Qt.ColorScheme.Light), 'light')
        self.assertEqual(resolve_theme('system', color_scheme=Qt.ColorScheme.Unknown), 'light')
        self.assertEqual(resolve_theme('light', color_scheme=Qt.ColorScheme.Dark), 'light')
        self.assertEqual(resolve_theme('dark', color_scheme=Qt.ColorScheme.Light), 'dark')
        self.assertIs(palette_for('light'), LIGHT)
        self.assertIs(palette_for('dark'), DARK)

    def test_os_change_refreshes_only_system_mode_without_saving_or_authentication(self):
        app = MaxApp.__new__(MaxApp)  # Do not initialize a service, session or AppPaths.
        app.settings = Settings(theme='system')
        app.settings.save = Mock()
        app._main = Mock()
        app._system_theme_changed(Qt.ColorScheme.Dark)
        app._main._apply_local_theme.assert_called_once_with()
        self.assertEqual(app.settings.theme, 'system')
        app.settings.save.assert_not_called()
        app._main.reset_mock()
        app.settings.theme = 'light'
        app._system_theme_changed(Qt.ColorScheme.Dark)
        app._main._apply_local_theme.assert_not_called()


if __name__ == '__main__':
    unittest.main()
