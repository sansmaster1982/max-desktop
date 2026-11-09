"""Media locale wiring with synthetic identities, pixels and modal replies only."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtGui import QPixmap, QColor, QTextCursor
from PySide6.QtWidgets import QApplication, QLabel, QPushButton
from shiboken6 import isValid

from maxclient.core.i18n import SUPPORTED_LANGUAGES, tr, translation_available
from maxclient.core.models import Attach, Message
from maxclient.ui.conversation_file_controller import ConversationFileController
from maxclient.ui.conversation_photo_viewer import ConversationPhotoViewer
from test_conversation_file_dialog import Descriptor
import test_conversation_file_shell as file_fixture
from test_conversation_image_io import row, image_bytes


class MediaLanguageIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.f = file_fixture.FileShellTests()
        self.f.app = self.app
        self.f.setUp()
        self.addCleanup(self.f.tearDown)
        self.w = self.f.window

    def test_save_picker_languages_keep_wildcard_and_cancel_without_io(self):
        self.f.choose.return_value = ('', '')
        suggested = []
        for language in SUPPORTED_LANGUAGES:
            self.w.settings.ui_language = language
            self.w._info_file_action('save', self.f.item, self.f.context)
            args = self.f.choose.call_args.args
            self.assertEqual(args[1], tr('Сохранить файл', language))
            self.assertEqual(args[3], tr('Все файлы (*)', language))
            self.assertTrue(args[3].endswith('(*)'))
            suggested.append(args[2])
            self.assertTrue(translation_available('Все файлы (*)', language))
        self.assertEqual(len(set(suggested)), 1)
        self.f.capture.assert_not_called()
        self.f.create.assert_not_called()

    def test_changed_language_in_picker_is_passed_to_new_controller_once(self):
        def choose(*args):
            self.w.settings.ui_language = 'fr'
            return 'synthetic.txt', ''
        self.f.choose.side_effect = choose
        self.w.settings.ui_language = 'en'
        self.w._info_file_action('save', self.f.item, self.f.context)
        self.assertEqual(self.f.choose.call_args.args[1], tr('Сохранить файл', 'en'))
        self.assertEqual(self.f.create.call_args.kwargs['language'], 'fr')
        self.assertIs(self.f.create.call_args.args[1], self.f.descriptor)
        self.assertEqual(self.f.create.call_args.args[2], 'synthetic.txt')
        self.f.create.assert_called_once()
        self.f.create.return_value.start.assert_called_once_with()

    def test_context_changed_in_localized_picker_still_refuses_download(self):
        def choose(*args):
            self.w.settings.ui_language = 'es'
            self.f.service.context = object()
            return 'synthetic.txt', ''
        self.f.choose.side_effect = choose
        self.w._info_file_action('save', self.f.item, self.f.context)
        self.f.capture.assert_not_called()
        self.f.create.assert_not_called()

    def test_capture_failure_shows_owned_translation_not_exception(self):
        self.f.capture.side_effect = ValueError('synthetic private path')
        for language in SUPPORTED_LANGUAGES:
            self.w.settings.ui_language = language
            self.w._info_file_action('save', self.f.item, self.f.context)
            source = 'Не удалось выбрать место для сохранения файла.'
            self.assertEqual(self.w.statusBar().currentMessage(), tr(source, language))
            self.assertTrue(translation_available(source, language))
        self.f.create.assert_not_called()

    def test_other_context_download_notice_is_localized_without_duplicate(self):
        active = Mock(active=True, descriptor=SimpleNamespace(context=object()))
        self.w._info_file_controller = active
        for language in SUPPORTED_LANGUAGES:
            self.w.settings.ui_language = language
            self.w._info_file_action('save', self.f.item, self.f.context)
            source = 'Дождитесь завершения предыдущей загрузки.'
            self.assertEqual(self.w.statusBar().currentMessage(), tr(source, language))
            self.assertTrue(translation_available(source, language))
        self.f.choose.assert_not_called()
        active.show.assert_not_called()
        self.f.create.assert_not_called()

    def test_main_hook_preserves_open_media_pixels_progress_and_worker_identity(self):
        descriptor = Descriptor(self.f.context)
        controller = ConversationFileController(self.f.service, descriptor, 'synthetic.txt', object(), self.w)
        self.w._info_file_controller = controller
        controller.dialog.set_progress(1024, 4096)
        controller.dialog.set_status('downloading')
        with patch('maxclient.ui.conversation_photo_viewer.run_async') as jobs:
            viewer = ConversationPhotoViewer([row()], 'photo:1', self.f.context,
                                             lambda context: context is self.f.context, self.w)
            self.w._info_photo_viewer = viewer
            jobs.call_args.kwargs['on_done'](image_bytes())
            viewer.zoom_by(0.5)
            baseline = (viewer._pixmap.cacheKey(), viewer.picture._image.cacheKey(), viewer._zoom,
                        viewer._fit, viewer._index, viewer._context, viewer._cancel)
            progress = (controller.dialog.progress_bar.minimum(), controller.dialog.progress_bar.maximum(),
                        controller.dialog.progress_bar.value())
            cancel = controller._cancel
            cancelled = Mock()
            controller.dialog.cancel_requested.connect(cancelled)
            calls = jobs.call_count
            for language in SUPPORTED_LANGUAGES:
                self.w.settings.ui_language = language
                self.w._apply_local_language()
                self.assertEqual(controller.dialog.status_label.text(), tr('Скачивание…', language))
                self.assertEqual(viewer.save_button.text(), tr('Сохранить фото', language))
                self.assertEqual((viewer._pixmap.cacheKey(), viewer.picture._image.cacheKey(), viewer._zoom,
                                 viewer._fit, viewer._index, viewer._context, viewer._cancel), baseline)
                self.assertEqual((controller.dialog.progress_bar.minimum(), controller.dialog.progress_bar.maximum(),
                                  controller.dialog.progress_bar.value()), progress)
                self.assertIs(controller._cancel, cancel)
                self.assertFalse(cancel.is_set())
                self.assertFalse(controller._started)
            self.assertEqual(jobs.call_count, calls)
            cancelled.assert_not_called()
        viewer.close()
        controller.cancel()

    def test_chat_bubble_gets_current_locale_and_relabels_without_rebuild(self):
        self.w.settings.ui_language = 'en'
        view = self.w.chat_view
        attach = Attach(type='PHOTO', base_url='https://photos.example.test/synthetic.png', width=80, height=60)
        message = Message(chat_id=303, id=909, text='Загрузка фото…', attaches=[attach])
        bubble = view._make_bubble(message)
        self.addCleanup(bubble.deleteLater)
        preview = bubble._photo_bindings[0][1]
        view._rows.append({'msg': message, 'bubble': bubble})
        self.assertEqual(preview.accessibleName(), tr('Фото', 'en'))
        preview.set_failed()
        with (patch.object(view._photos, 'refresh', side_effect=AssertionError('No refresh')),
              patch.object(view._photos.loader, 'request', side_effect=AssertionError('No load'))):
            for language in SUPPORTED_LANGUAGES:
                view.retranslate_media(language)
                self.assertIs(view._rows[-1]['bubble'], bubble)
                self.assertIs(bubble._photo_bindings[0][1], preview)
                self.assertEqual(preview.accessibleName(), tr('Фото', language))
                self.assertEqual(message.text, 'Загрузка фото…')

    def test_info_viewer_creation_passes_locale_without_changing_rows(self):
        self.w.settings.ui_language = 'be'
        item = row()
        self.w._info_pages['media'] = {'items': [item]}
        with patch.object(self.w, '_known_info_media_item', return_value=item), \
             patch('maxclient.ui.conversation_photo_viewer.run_async') as jobs:
            self.w._open_info_item(item, self.f.context)
            viewer = self.w._info_photo_viewer
            self.assertTrue(isValid(viewer))
            self.assertEqual(viewer.save_button.text(), tr('Сохранить фото', 'be'))
            self.assertEqual(viewer._rows, [item])
            self.assertIs(viewer._context, self.f.context)
            self.assertEqual(jobs.call_count, 1)
            viewer.close()

    def test_main_locale_hook_updates_existing_file_and_photo_without_rebuild_or_io(self):
        self.w.settings.ui_language = 'en'
        view = self.w.chat_view
        raw = '<b>Файл удалён & raw.txt</b>'
        file = Attach(type='FILE', file_name=raw, size=1024)
        photo = Attach(type='PHOTO', base_url='https://photos.example.test/synthetic.png', width=80, height=60)
        message = Message(chat_id=303, id=910, text='Сохранить файл', attaches=[file, photo])
        caps = {0: {'download_available': True, 'file_name': raw, 'deleted': False}}
        with patch.object(view, '_file_actions_for', return_value=caps):
            bubble = view._make_bubble(message)
        self.addCleanup(bubble.deleteLater)
        view._rows.append({'msg': message, 'bubble': bubble})
        primary = bubble.findChild(QPushButton, 'MessageFilePrimary')
        metadata = bubble.findChild(QLabel, 'MessageFileMeta')
        title = bubble.findChild(QLabel, 'MessageFileName')
        preview = bubble._photo_bindings[1][1]
        pixmap = QPixmap(80, 60)
        pixmap.fill(QColor('#698ABC'))
        preview.set_photo(pixmap)
        bindings, capability = bubble._file_bindings, bubble._file_actions
        self.assertEqual(metadata._full_text, '1.0 KB · Save file')
        view.input.setPlainText('untouched draft')
        cursor = view.input.textCursor()
        cursor.setPosition(3)
        cursor.setPosition(9, QTextCursor.MoveMode.KeepAnchor)
        view.input.setTextCursor(cursor)
        draft = (view.input.toPlainText(), cursor.anchor(), cursor.position())
        scroll = view.scroll.verticalScrollBar().value()
        signalled = Mock()
        bubble.file_action_requested.connect(signalled)
        with (patch.object(view, '_make_bubble', side_effect=AssertionError('No rebuild')),
              patch.object(view, '_file_actions_for', side_effect=AssertionError('No new capability')),
              patch.object(view, '_file_action', side_effect=AssertionError('No FILE action')),
              patch.object(view._photos, 'refresh', side_effect=AssertionError('No refresh')),
              patch.object(view._photos.loader, 'request', side_effect=AssertionError('No image load'))):
            for language in SUPPORTED_LANGUAGES:
                self.w.settings.ui_language = language
                self.w._apply_local_language()
                self.assertIs(view._rows[-1]['bubble'], bubble)
                self.assertIs(bubble._file_bindings, bindings)
                self.assertIs(bubble._file_actions, capability)
                self.assertTrue(primary.isEnabled())
                self.assertEqual(primary.accessibleName(), tr('Сохранить файл', language))
                self.assertEqual(metadata._full_text, '1.0 ' + tr('КБ', language) + ' · ' + tr('Сохранить файл', language))
                self.assertEqual(title._full_text, raw)
                self.assertEqual(bubble.findChild(QLabel, 'MessageText').text(), 'Сохранить файл')
                self.assertIs(bubble._photo_bindings[1][1], preview)
                self.assertEqual(preview._pixmap.cacheKey(), pixmap.cacheKey())
                current = view.input.textCursor()
                self.assertEqual((view.input.toPlainText(), current.anchor(), current.position()), draft)
                self.assertEqual(view.scroll.verticalScrollBar().value(), scroll)
        signalled.assert_not_called()
        self.f.choose.assert_not_called()
        self.f.create.assert_not_called()

    def test_file_relabel_defaults_to_settings_and_skips_deleted_bubbles(self):
        view = self.w.chat_view
        self.w.settings.ui_language = 'fr'
        file = Attach(type='FILE', file_name='Файл удалён', size=0)
        message = Message(chat_id=303, id=911, attaches=[file])
        bubble = view._make_bubble(message)
        self.addCleanup(bubble.deleteLater)
        dead = view._make_bubble(message)
        dead.deleteLater()
        QCoreApplication.sendPostedEvents(dead, QEvent.Type.DeferredDelete)
        self.assertFalse(isValid(dead))
        view._rows.extend([{'msg': message, 'bubble': dead}, {'msg': message, 'bubble': bubble}])
        metadata = bubble.findChild(QLabel, 'MessageFileMeta')
        self.assertIn(tr('Скачивание недоступно', 'fr'), metadata._full_text)
        self.w.settings.ui_language = 'uz'
        view.retranslate_media()
        self.assertEqual(metadata._full_text, '0 ' + tr('Б', 'uz') + ' · ' + tr('Скачивание недоступно', 'uz'))
        self.assertEqual(bubble.findChild(QLabel, 'MessageFileName')._full_text, 'Файл удалён')
        self.assertFalse(bubble.findChild(QPushButton, 'MessageFilePrimary').isEnabled())
        view._rows[:] = [row for row in view._rows if isValid(row['bubble'])]


if __name__ == '__main__':
    unittest.main()
