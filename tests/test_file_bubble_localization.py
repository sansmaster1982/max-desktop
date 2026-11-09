"""Localized FILE cards and menu labels; synthetic models and no file/clipboard IO."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import math
import re
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtGui import QColor, QFont, QPixmap
from PySide6.QtWidgets import QApplication, QLabel, QPushButton
from shiboken6 import isValid

from maxclient.core._file_download_translations import FILE_DOWNLOAD_TRANSLATIONS
from maxclient.core.config import Settings
from maxclient.core.i18n import tr
from maxclient.core.models import Attach, Message
from maxclient.ui.chat_preview import ChatPreviewDialog
from maxclient.ui.theme import build_stylesheet, palette_for
from maxclient.ui.widgets.common import human_size
from maxclient.ui.widgets.message_bubble import MessageBubble
from qt_test_lifecycle import capture_qt_state, restore_qt_state


LANGUAGES = ('en', 'es', 'pt', 'fr', 'uz', 'be', 'ru')
NEW_SOURCES = ('Файл удалён', 'Скачивание недоступно', 'Действия с файлом', 'Копировать имя файла')


def attachment(**values):
    return Attach(**{'type': 'FILE', 'file_id': 17, 'file_name': 'raw-file.txt', 'size': 1024,
                    'url': 'https://invalid.example/never-open', 'local_path': 'C:/never-read.exe', **values})


def capability(**values):
    return {'download_available': True, 'file_name': 'raw-file.txt', 'deleted': False, **values}


class FileBubbleLocalizationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.addCleanup(restore_qt_state, capture_qt_state())
        self.app.setStyleSheet('')
        self.app.setStyle('Fusion')
        self.app.setFont(QFont('Segoe UI', 10))
        self.app.setStyleSheet(build_stylesheet('light', '#0085FF'))
        self.widgets = []
        self.dispatched = []
        self.media = Mock()

    def tearDown(self):
        for widget in reversed(self.widgets):
            if isValid(widget):
                widget.close()
                widget.deleteLater()
                QCoreApplication.sendPostedEvents(widget, QEvent.Type.DeferredDelete)
        self.app.processEvents()

    def bubble(self, *, file=None, caps=None, language='en', theme='light', text='', extra=()):
        file = attachment() if file is None else file
        msg = Message(chat_id=303, id=7, time_ms=123, text=text, attaches=[file, *extra])
        bubble = MessageBubble(msg, '#0085FF', palette_for(theme), file_actions=caps,
                               language=language, on_open_media=self.media)
        bubble.file_action_requested.connect(lambda action, message, index:
                                             self.dispatched.append((action, message, index)))
        self.widgets.append(bubble)
        bubble.resize(540, 180)
        bubble.show()
        self.app.processEvents()
        return bubble

    @staticmethod
    def fields(bubble):
        return (bubble.findChild(QPushButton, 'MessageFilePrimary'), bubble.findChild(QPushButton, 'MessageFileMore'),
                bubble.findChild(QLabel, 'MessageFileName'), bubble.findChild(QLabel, 'MessageFileMeta'))

    def test_four_new_catalogue_keys_have_six_translations_and_english_anchors(self):
        self.assertEqual(len(FILE_DOWNLOAD_TRANSLATIONS), 25)
        english = ('File deleted', 'Download unavailable', 'File actions', 'Copy file name')
        for source, anchor in zip(NEW_SOURCES, english):
            values = FILE_DOWNLOAD_TRANSLATIONS[source]
            self.assertEqual(len(values), 6)
            self.assertEqual(values[0], anchor)
            for language, value in zip(LANGUAGES, values):
                self.assertEqual(tr(source, language), value)
                if language != 'be':
                    self.assertIsNone(re.search('[А-Яа-яЁё]', value))
            self.assertEqual(tr(source, 'pt-BR'), values[2])

    def test_human_size_default_numbers_rounding_and_gigabyte_cap_remain_legacy(self):
        expected = ((None, ''), (0, ''), (False, ''), (True, '1 Б'), (-1, '-1 Б'),
                    (1, '1 Б'), (1023, '1023 Б'), (1024, '1.0 КБ'), (1536, '1.5 КБ'),
                    (2**30, '1.0 ГБ'), (2**40, '1024.0 ГБ'),
                    (float('inf'), 'inf ГБ'), (float('nan'), 'nan ГБ'))
        for value, text in expected:
            self.assertEqual(human_size(value), text)
            self.assertEqual(human_size(value, 'ru'), text)
        for language in LANGUAGES:
            self.assertEqual(human_size(1536, language), '1.5 ' + tr('КБ', language))
            self.assertEqual(human_size(2**40, language), '1024.0 ' + tr('ГБ', language))

    def test_seven_languages_for_available_deleted_unknown_cards_and_actual_menu(self):
        for caps, status, save, copy in (({0: capability()}, 'Сохранить файл', True, True),
                                        ({0: capability(deleted=True)}, 'Файл удалён', False, True),
                                        (None, 'Скачивание недоступно', False, False)):
            bubble = self.bubble(caps=caps)
            primary, more, title, details = self.fields(bubble)
            for language in LANGUAGES:
                bubble.retranslate_file_actions(language)
                self.assertEqual(primary.accessibleName(), tr('Сохранить файл', language))
                self.assertEqual(more.accessibleName(), tr('Действия с файлом', language))
                self.assertEqual(more.toolTip(), tr('Действия с файлом', language))
                self.assertEqual(details._full_text, human_size(1024, language) + ' · ' + tr(status, language))
                self.assertEqual(title._full_text, 'raw-file.txt')
                self.assertIs(primary.isEnabled(), save)
                self.assertIs(more.isEnabled(), save or copy)
                menu = bubble.open_file_menu(0)
                self.app.processEvents()
                self.assertTrue(menu.isVisible())
                self.assertEqual([action.text() for action in menu.actions()],
                                 [tr('Сохранить файл', language), tr('Копировать имя файла', language)])
                self.assertEqual([action.isEnabled() for action in menu.actions()], [save, copy])
                menu.cancel()
        self.assertEqual(self.dispatched, [])

    def test_authoritative_raw_name_key_collisions_html_and_legacy_name_are_literal(self):
        for name in ('Файл', 'Сохранить файл', 'Файл удалён', '<b>Файл & test</b>'):
            for authoritative in (True, False):
                bubble = self.bubble(file=attachment(file_name=name),
                                     caps={0: capability(file_name=name if authoritative else None)})
                _primary, _more, title, _details = self.fields(bubble)
                for language in LANGUAGES:
                    bubble.retranslate_file_actions(language)
                    self.assertEqual(title._full_text, name)
                    self.assertEqual(title.accessibleName(), name)
                    self.assertEqual(title.textFormat(), Qt.TextFormat.PlainText)
                    if '<b>' in name:
                        self.assertIn('&lt;b&gt;', title.toolTip())
                        self.assertNotIn('<b>', title.toolTip())
                self.assertIs(bubble._file_allowed('copy_name', 0), authoritative)

    def test_missing_name_has_translated_fallback_without_becoming_copy_permission(self):
        for authoritative in (None, '', 123):
            bubble = self.bubble(file=attachment(file_name=None), caps={0: capability(file_name=authoritative)})
            title = self.fields(bubble)[2]
            for language in LANGUAGES:
                bubble.retranslate_file_actions(language)
                self.assertEqual(title._full_text, tr('Файл', language))
                self.assertEqual(title.accessibleName(), tr('Файл', language))
                self.assertFalse(bubble._file_allowed('copy_name', 0))
            menu = bubble.open_file_menu(0)
            menu.actions()[1].trigger()
        self.assertEqual(self.dispatched, [])

    def test_card_zero_missing_invalid_and_large_size_keep_original_numeric_rules(self):
        for value in (0, None, True, -1, float('nan'), float('inf'), 1023, 1024, 2**40):
            bubble = self.bubble(file=attachment(size=value), caps={0: capability()})
            details = self.fields(bubble)[3]
            for language in LANGUAGES:
                bubble.retranslate_file_actions(language)
                valid = type(value) in (int, float) and math.isfinite(value) and value >= 0
                size = ('0 ' + tr('Б', language) if value == 0 else human_size(value, language)) if valid else ''
                expected = (size + ' · ' if valid else '') + tr('Сохранить файл', language)
                self.assertEqual(details._full_text, expected)

    def test_retranslate_preserves_identity_flags_focus_raw_text_and_photo_pixels_without_io(self):
        photo = Attach(type='PHOTO', base_url='https://images.example.test/synthetic.png', width=100, height=60)
        bubble = self.bubble(caps={0: capability()}, text='Скачивание недоступно', extra=(photo,))
        primary, more, _title, details = self.fields(bubble)
        primary.setFocus()
        self.app.processEvents()
        preview = bubble._photo_bindings[1][1]
        pixels = QPixmap(100, 60)
        pixels.fill(QColor('red'))
        preview.set_photo(pixels)
        bindings, caps, presentations = bubble._file_bindings, bubble._file_actions, bubble._file_presentations
        message, binding, photo_binding = bubble.msg, bindings[0], bubble._photo_bindings[1]
        flags = (primary.isEnabled(), more.isEnabled(), bubble._file_revoked)
        focus = self.app.focusWidget()
        with patch.object(bubble, '_file_allowed') as permission, patch.object(bubble, '_file_card') as rebuild, \
                patch.object(QApplication, 'clipboard', side_effect=AssertionError('No clipboard from relabel')), \
                patch('os.path.isfile', side_effect=AssertionError('No path reads from relabel')):
            for language in LANGUAGES:
                bubble.retranslate_file_actions(language)
                self.assertIs(bubble.msg, message)
                self.assertIs(bubble._file_bindings, bindings)
                self.assertIs(bindings[0], binding)
                self.assertIs(bubble._file_actions, caps)
                self.assertIs(bubble._file_presentations, presentations)
                self.assertIs(bubble._photo_bindings[1], photo_binding)
                self.assertEqual(preview._pixmap.cacheKey(), pixels.cacheKey())
                self.assertEqual(bubble.findChild(QLabel, 'MessageText').text(), 'Скачивание недоступно')
                self.assertEqual((primary.isEnabled(), more.isEnabled(), bubble._file_revoked), flags)
                self.assertIs(self.app.focusWidget(), focus)
            permission.assert_not_called()
            rebuild.assert_not_called()
        self.assertEqual(self.dispatched, [])
        self.media.assert_not_called()

    def test_captured_size_status_and_fallback_do_not_follow_mutated_model_on_retranslate(self):
        file = attachment(size=1024, file_name=None)
        bubble = self.bubble(file=file, caps={0: capability(file_name=None)})
        file.size, file.file_name, file.deleted = 999999, 'Changed model', True
        bubble.retranslate_file_actions('fr')
        primary, _more, title, details = self.fields(bubble)
        self.assertEqual(title._full_text, tr('Файл', 'fr'))
        self.assertEqual(details._full_text, '1.0 Ko · ' + tr('Сохранить файл', 'fr'))
        primary.click()
        self.assertFalse(bubble._file_current(0))
        self.assertEqual(self.dispatched, [])

    def test_actual_locale_change_revokes_active_popup_in_both_photo_file_orders(self):
        for order in ('photo_first', 'file_first'):
            bubble = self.bubble(caps={0: capability()})
            menu = bubble.open_file_menu(0)
            self.app.processEvents()
            action = menu.actions()[0]
            epoch = bubble._file_menu_epoch
            self.assertTrue(menu.isVisible())
            self.assertTrue(action.isEnabled())
            self.assertTrue(menu._allowed(menu._rows[0][1]))
            methods = (bubble.retranslate_photo_previews, bubble.retranslate_file_actions)
            if order == 'file_first':
                methods = tuple(reversed(methods))
            for method in methods:
                method('fr')
            self.assertGreater(bubble._file_menu_epoch, epoch)
            self.assertIsNone(bubble._file_menu)
            self.assertTrue(menu._revoked)
            action.trigger()
            menu.requested.emit('save', None, bubble)  # A previously queued delivery also retains the old epoch.
            self.assertEqual(self.dispatched, [])
            current = bubble.open_file_menu(0)
            self.assertEqual(current.actions()[0].text(), tr('Сохранить файл', 'fr'))
            current.actions()[0].trigger()
            self.assertEqual(self.dispatched, [('save', bubble.msg, 0)])
            self.dispatched.clear()

    def test_same_normalized_language_keeps_active_popup_and_epoch(self):
        bubble = self.bubble(caps={0: capability()}, language='pt-BR')
        menu = bubble.open_file_menu(0)
        epoch = bubble._file_menu_epoch
        bubble.retranslate_file_actions('pt')
        self.assertIs(bubble._file_menu, menu)
        self.assertEqual(bubble._file_menu_epoch, epoch)
        self.assertFalse(menu._revoked)

    def test_parent_deletion_scheduled_during_popup_cancel_is_safe(self):
        bubble = self.bubble(caps={0: capability()})
        menu = bubble.open_file_menu(0)
        self.app.processEvents()
        scheduled = Mock(side_effect=bubble.deleteLater)
        menu.aboutToHide.connect(scheduled)
        bubble.retranslate_file_actions('fr')
        scheduled.assert_called_once_with()
        # Let native QMenu.close unwind before delivering the normal deferred delete.
        QCoreApplication.sendPostedEvents(bubble, QEvent.Type.DeferredDelete)
        self.assertFalse(isValid(bubble))
        self.assertEqual(self.dispatched, [])

    def test_replaced_attachment_and_closed_deleted_bubble_cannot_be_revived(self):
        bubble = self.bubble(caps={0: capability()})
        primary = self.fields(bubble)[0]
        bubble.msg.attaches[0] = attachment()
        bubble.retranslate_file_actions('fr')
        primary.click()
        self.assertEqual(self.dispatched, [])
        bubble.close()
        bubble.retranslate_file_actions('uz')
        self.assertTrue(bubble._file_revoked)
        primary.click()
        self.assertEqual(self.dispatched, [])
        bubble.deleteLater()
        QCoreApplication.sendPostedEvents(bubble, QEvent.Type.DeferredDelete)
        bubble.retranslate_file_actions('be')

    def test_real_light_dark_styles_keep_narrow_card_controls_and_elided_names_inside(self):
        for theme in ('light', 'dark'):
            self.app.setStyleSheet(build_stylesheet(theme, '#0085FF'))
            for language in LANGUAGES:
                with self.subTest(theme=theme, language=language):
                    name = '<b>Literal & long filename — ' + 'abcdef ' * 12 + '.pdf</b>'
                    bubble = self.bubble(file=attachment(file_name=name), caps={0: capability(file_name=name)},
                                         language=language, theme=theme)
                    bubble.resize(360, bubble.sizeHint().height())
                    self.app.processEvents()
                    primary, more, title, details = self.fields(bubble)
                    self.assertGreaterEqual(primary.height(), primary.minimumHeight())
                    self.assertGreaterEqual(more.width(), more.minimumWidth())
                    self.assertGreaterEqual(more.height(), more.minimumHeight())
                    for widget in (primary, more, title, details):
                        self.assertTrue(bubble.rect().contains(widget.mapTo(bubble, widget.rect().topLeft())))
                        self.assertTrue(bubble.rect().contains(widget.mapTo(bubble, widget.rect().bottomRight())))
                    self.assertEqual(title._full_text, name)
                    self.assertLessEqual(title.fontMetrics().horizontalAdvance(title.text()), title.width())

    def test_passive_chat_preview_keeps_localized_file_controls_disabled(self):
        context = object()
        dialog = ChatPreviewDialog(SimpleNamespace(is_current_conversation=Mock(return_value=True)),
                                   Settings(ui_language='fr'), context)
        self.widgets.append(dialog)
        msg = Message(chat_id=303, id=7, attaches=[attachment(file_name=None)])
        with patch('maxclient.ui.chat_preview.run_async') as history:
            dialog._render([msg])
        primary, more, title, details = self.fields(dialog._bubbles[0])
        self.assertEqual(title._full_text, 'Fichier')
        self.assertIn(tr('Скачивание недоступно', 'fr'), details._full_text)
        self.assertFalse(primary.isEnabled())
        self.assertFalse(more.isEnabled())
        history.assert_not_called()


if __name__ == '__main__':
    unittest.main()
