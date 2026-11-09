"""Local language persistence and pure, explicitly requested translation."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from maxclient.core.config import Settings
from maxclient.core.i18n import (SUPPORTED_LANGUAGES, LANGUAGE_TITLES, language_rows,
                                normalize_language, tr, translation_available)


class UiLanguageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp_root = Path('.test-tmp')
        cls.temp_root.mkdir(exist_ok=True)
    def test_supported_locales_are_real_and_portuguese_is_brazilian(self):
        self.assertEqual(SUPPORTED_LANGUAGES, ('ru', 'en', 'es', 'pt', 'fr', 'uz', 'be'))
        self.assertEqual(normalize_language('pt-BR'), 'pt')
        self.assertEqual(normalize_language('PT_br'), 'pt')
        for invalid in (None, [], {}, 1, 'nn', 'xx', 'pt-PT'):
            self.assertEqual(normalize_language(invalid), 'ru')
        for locale in SUPPORTED_LANGUAGES:
            self.assertEqual(normalize_language(locale.upper()), locale)

    def test_language_is_saved_separately_from_transport_locale(self):
        with TemporaryDirectory(dir=self.temp_root) as directory:
            file = Path(directory) / 'settings.json'
            settings = Settings(locale='ru', ui_language='pt-BR')
            settings._path = file
            settings.save()
            saved = json.loads(file.read_text(encoding='utf8'))
            self.assertEqual(saved['locale'], 'ru')
            self.assertEqual(saved['ui_language'], 'pt')
            self.assertEqual(Settings(**saved).ui_language, 'pt')
            self.assertEqual(Settings(locale='en').ui_language, 'ru')

    def test_atomic_language_save_preserves_all_other_on_disk_fields(self):
        with TemporaryDirectory(dir=self.temp_root) as directory:
            file = Path(directory) / 'settings.json'
            original = {'locale': 'ru', 'theme': 'dark', 'future_preference': {'keep': True}}
            file.write_text(json.dumps(original), encoding='utf8')
            settings = Settings(theme='light')
            settings._path = file
            settings.save_ui_language('pt-BR')
            self.assertEqual(json.loads(file.read_text(encoding='utf8')), {**original, 'ui_language': 'pt'})
            self.assertEqual(settings.ui_language, 'pt')
            self.assertEqual(list(Path(directory).glob('*.tmp')), [])

    def test_atomic_replace_failure_keeps_old_file_language_and_removes_own_temp(self):
        with TemporaryDirectory(dir=self.temp_root) as directory:
            file = Path(directory) / 'settings.json'
            original = '{"locale":"ru","ui_language":"ru","theme":"dark"}'
            file.write_text(original, encoding='utf8')
            settings = Settings(ui_language='ru')
            settings._path = file
            with patch.object(Path, 'replace', side_effect=OSError('synthetic failure')):
                with self.assertRaises(OSError):
                    settings.save_ui_language('en')
            self.assertEqual(file.read_text(encoding='utf8'), original)
            self.assertEqual(settings.ui_language, 'ru')
            self.assertEqual(list(Path(directory).glob('*.tmp')), [])

    def test_invalid_existing_settings_are_not_overwritten(self):
        with TemporaryDirectory(dir=self.temp_root) as directory:
            file = Path(directory) / 'settings.json'
            file.write_text('[]', encoding='utf8')
            settings = Settings()
            settings._path = file
            with self.assertRaises(ValueError):
                settings.save_ui_language('en')
            self.assertEqual(file.read_text(encoding='utf8'), '[]')
            self.assertEqual(settings.ui_language, 'ru')

    def test_real_catalog_translates_settings_and_rail_in_all_languages(self):
        strings = ('Настройки', 'Уведомления', 'Безопасность', 'Устройства', 'Мои истории', 'Папки',
                   'Оформление', 'Данные и память', 'Язык приложения', 'Сочетания клавиш',
                   'Помощь', 'О приложении', 'Все', 'Новые', 'Каналы', 'Контакты', 'Звонки',
                   'Пароль для входа', 'Семейная защита', 'Папка загрузок', 'Изменить папку')
        for language in SUPPORTED_LANGUAGES[1:]:
            for source in strings:
                with self.subTest(language=language, source=source):
                    self.assertTrue(translation_available(source, language))
                    self.assertTrue(tr(source, language))
        self.assertEqual(tr('Настройки', 'en'), 'Settings')
        self.assertEqual(tr('Настройки', 'be'), 'Налады')
        self.assertEqual(tr('Настройки', 'uz'), 'Sozlamalar')

    def test_native_ru_language_names_and_order(self):
        rows = language_rows('ru')
        self.assertEqual(tuple(row[0] for row in rows), SUPPORTED_LANGUAGES)
        self.assertEqual(tuple(row[1] for row in rows), LANGUAGE_TITLES)
        self.assertEqual(rows[3], ('pt', 'Português (Brasil)', 'Португальский (Бразилия)'))
        self.assertEqual(rows[4], ('fr', 'Французский', 'Французский'))
        self.assertEqual(language_rows('en')[6][2], 'Belarusian')

    def test_source_spacing_case_and_unknown_are_handled_without_external_text_scan(self):
        self.assertEqual(tr('РАЗМЕР ТЕКСТА', 'en'), 'TEXT SIZE')
        self.assertEqual(tr('могут все', 'en'), 'anyone')
        self.assertEqual(tr('Пригласить в чат', 'en').replace('\xa0', ' '), 'Invite to chat')
        source = 'Незнакомая строка из пользовательской переписки'
        self.assertEqual(tr(source, 'en'), source)
        self.assertFalse(translation_available(source, 'en'))
        self.assertEqual(tr('{name}: {message}', 'en', name='Настройки', message='Привет'), 'Настройки: Привет')
        self.assertEqual(tr('\nТекущее устройство', 'en'), '\nCurrent device')
        self.assertEqual(tr('Настройки', 'ru'), 'Настройки')
        with self.assertRaises(TypeError):
            tr(123, 'en')


if __name__ == '__main__':
    unittest.main()
