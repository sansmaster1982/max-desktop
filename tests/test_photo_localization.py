"""Synthetic photo localization: no worker/network, account data or actual saves."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import re
import string
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from PySide6.QtCore import QBuffer, QCoreApplication, QEvent, QIODevice, QPoint, Qt
from PySide6.QtGui import QColor, QFont, QPixmap
from PySide6.QtWidgets import QApplication, QLabel
from shiboken6 import isValid

from maxclient import __app_name__
from maxclient.core._photo_viewer_translations import PHOTO_VIEWER_TRANSLATIONS
from maxclient.core.config import Settings
from maxclient.core.i18n import tr
from maxclient.core.models import Message
from maxclient.ui.chat_preview import ChatPreviewDialog
from maxclient.ui.conversation_photo_viewer import ConversationPhotoViewer
from maxclient.ui.timeline_photos import PhotoPreview, TimelinePhotos
from maxclient.ui.widgets.message_bubble import MessageBubble
from maxclient.ui.theme import build_stylesheet, palette_for
from qt_test_lifecycle import capture_qt_state, restore_qt_state
import test_timeline_photos as timeline_fixture


LANGUAGES = ('en', 'es', 'pt', 'fr', 'uz', 'be', 'ru')


def row(key):
    return {'key': key, 'kind': 'PHOTO', 'available': True,
            'open_url': 'https://images.example.test/generated.png',
            'chat_id': 303, 'message_id': 7, 'time_ms': 100}


def image_bytes():
    image = QPixmap(240, 160)
    image.fill(QColor('#69AABB'))
    output = QBuffer()
    output.open(QIODevice.OpenModeFlag.WriteOnly)
    assert image.save(output, 'PNG')
    return bytes(output.data())


class PhotoLocalizationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.widgets = []
        self.context = object()
        self.current = self.context
        self.worker = patch('maxclient.ui.conversation_photo_viewer.run_async')
        self.jobs = self.worker.start()
        self.addCleanup(self.worker.stop)

    def tearDown(self):
        for widget in reversed(self.widgets):
            if isValid(widget):
                widget.close()
                widget.deleteLater()
                QCoreApplication.sendPostedEvents(widget, QEvent.Type.DeferredDelete)
        self.app.processEvents()

    def viewer(self, language='en', loaded=False):
        viewer = ConversationPhotoViewer([row('first'), row('second')], 'first', self.context,
                                         lambda context: context is self.current, language=language)
        self.widgets.append(viewer)
        viewer._clock.stop()
        if loaded:
            self.jobs.call_args.kwargs['on_done'](image_bytes())
        return viewer

    def preview(self, language='en', **values):
        preview = PhotoPreview(timeline_fixture.photo(**values), '#556677', language=language)
        self.widgets.append(preview)
        return preview

    def test_catalogue_all_six_columns_and_template_fields_are_integrated(self):
        self.assertEqual(len(PHOTO_VIEWER_TRANSLATIONS), 14)
        self.assertNotIn('К сообщению', PHOTO_VIEWER_TRANSLATIONS)
        for source, values in PHOTO_VIEWER_TRANSLATIONS.items():
            self.assertEqual(len(values), 6)
            fields = [name for _, name, _, _ in string.Formatter().parse(source) if name]
            for language, value in zip(LANGUAGES, values):
                self.assertEqual(tr(source, language), value)
                self.assertEqual([name for _, name, _, _ in string.Formatter().parse(value) if name], fields)
                if language != 'be':
                    self.assertIsNone(re.search('[А-Яа-яЁё]', value))
            self.assertEqual(tr(source, 'pt-BR'), values[2])
        self.assertEqual(tr('К сообщению', 'en'), 'Go to message')

    def test_all_seven_languages_relabel_loading_controls_without_new_request_or_cancel(self):
        viewer = self.viewer()
        cancel, rows = viewer._cancel, viewer._rows
        count = self.jobs.call_count
        with patch.object(viewer, 'select') as select, patch.object(viewer, '_render') as render:
            for language in LANGUAGES:
                viewer.retranslate(language)
                self.assertEqual(viewer.windowTitle(), tr('Фото — {app}', language, app=__app_name__))
                self.assertEqual(viewer.previous.accessibleName(), tr('Предыдущее фото', language))
                self.assertEqual(viewer.next.accessibleName(), tr('Следующее фото', language))
                for button, source in ((viewer.jump, 'К сообщению'), (viewer.retry, 'Повторить загрузку'),
                                       (viewer.save_button, 'Сохранить фото'), (viewer.close_button, 'Закрыть'),
                                       (viewer.fit_button, 'По размеру окна')):
                    self.assertEqual(button.text(), tr(source, language))
                self.assertEqual(viewer.picture._text, tr('Загрузка…', language))
                self.assertEqual(viewer.status.text(), '')
                self.assertEqual(viewer.counter.text(), '1 / 2')
                self.assertIs(viewer._cancel, cancel)
                self.assertFalse(cancel.is_set())
                self.assertIs(viewer._rows, rows)
                self.assertIs(viewer._context, self.context)
                self.assertEqual(viewer._index, 0)
                self.assertFalse(viewer.save_button.isEnabled())
            select.assert_not_called()
            render.assert_not_called()
        self.assertEqual(self.jobs.call_count, count)

    def test_failed_request_stays_failed_with_explicit_retry_and_sanitized_text(self):
        viewer = self.viewer()
        self.jobs.call_args.kwargs['on_error']('private URL secret Сохранить фото')
        cancel, count = viewer._cancel, self.jobs.call_count
        for language in LANGUAGES:
            viewer.retranslate(language)
            self.assertEqual(viewer.picture._text, tr('Не удалось открыть фото', language))
            self.assertEqual(viewer.status.text(), tr('Повторите загрузку или перейдите к исходному сообщению.', language))
            self.assertNotIn('secret', viewer.status.text())
            self.assertFalse(viewer.retry.isHidden())
            self.assertFalse(viewer.save_button.isEnabled())
            self.assertIs(viewer._cancel, cancel)
        self.assertEqual(self.jobs.call_count, count)
        viewer.retry.click()
        self.assertEqual(self.jobs.call_count, count + 1)
        self.assertTrue(cancel.is_set())

    def test_loaded_zoomed_pixels_and_scroll_survive_language_change(self):
        viewer = self.viewer(loaded=True)
        viewer.resize(1100, 740)
        viewer.show()
        self.app.processEvents()
        viewer.zoom_by(1.0)
        self.app.processEvents()
        viewer.scroller.horizontalScrollBar().setValue(45)
        viewer.scroller.verticalScrollBar().setValue(35)
        expected = (viewer._pixmap.cacheKey(), viewer.picture._image.cacheKey(), viewer._zoom, viewer._fit,
                    viewer.picture.size(), viewer.scroller.horizontalScrollBar().value(),
                    viewer.scroller.verticalScrollBar().value(), viewer.counter.text(), viewer.status.text())
        cancel, count = viewer._cancel, self.jobs.call_count
        with patch.object(viewer, 'select') as select, patch.object(viewer, 'fit_image') as fit, \
                patch.object(viewer.picture, 'setText') as clear_pixels:
            for language in LANGUAGES:
                viewer.retranslate(language)
                self.app.processEvents()
                actual = (viewer._pixmap.cacheKey(), viewer.picture._image.cacheKey(), viewer._zoom, viewer._fit,
                          viewer.picture.size(), viewer.scroller.horizontalScrollBar().value(),
                          viewer.scroller.verticalScrollBar().value(), viewer.counter.text(), viewer.status.text())
                self.assertEqual(actual, expected)
                self.assertIs(viewer._cancel, cancel)
                self.assertTrue(viewer.save_button.isEnabled())
            select.assert_not_called()
            fit.assert_not_called()
            clear_pixels.assert_not_called()
        self.assertFalse(cancel.is_set())
        self.assertEqual(self.jobs.call_count, count)

    def test_counter_and_status_pixels_remain_legible_with_real_app_stylesheet(self):
        self.addCleanup(restore_qt_state, capture_qt_state())
        self.app.setStyleSheet('')
        self.app.setStyle('Fusion')
        self.app.setFont(QFont('Segoe UI', 10))
        for theme in ('light', 'dark'):
            self.app.setStyleSheet(build_stylesheet(theme, '#0085FF'))
            for loaded, language in ((True, 'en'), (False, 'fr')):
                with self.subTest(theme=theme, loaded=loaded, language=language):
                    viewer = self.viewer(language=language, loaded=loaded)
                    if not loaded:
                        self.jobs.call_args.kwargs['on_error']('synthetic failure')
                    viewer.resize(1100, 740)
                    viewer.show()
                    self.app.processEvents()
                    capture = viewer.grab()
                    pixels, scale = capture.toImage(), capture.devicePixelRatio()
                    for label in (viewer.counter, viewer.status):
                        self.assertTrue(label.text())
                        origin = label.mapTo(viewer, QPoint(0, 0))
                        # Use the actual composite, not a stylesheet string or palette.
                        x0, y0 = round(origin.x() * scale), round(origin.y() * scale)
                        width, height = round(label.width() * scale), round(label.height() * scale)
                        colors = [pixels.pixelColor(x, y).getRgb()[:3]
                                  for y in range(y0, y0 + height)
                                  for x in range(x0, x0 + width)]
                        # A glyph can reach a corner (notably French error text).
                        # Check the region's real background and actual glyphs.
                        dark = sum(color == (17, 19, 23) for color in colors)
                        bright = sum(min(color) > 200 for color in colors)
                        self.assertGreater(dark, width * height // 2, 'Label must composite on the dark viewer background')
                        self.assertGreater(bright, 3, 'White text must be rendered on the dark viewer background')
                        self.assertLess(bright, width * height // 2, 'A white label rectangle must not cover the text')
                    viewer.close()
                    self.app.processEvents()

    def test_late_previous_result_after_language_and_selection_change_is_rejected(self):
        viewer = self.viewer()
        old = self.jobs.call_args.kwargs
        viewer.retranslate('fr')
        viewer.select(1)
        current = self.jobs.call_args.kwargs
        old['on_done'](image_bytes())
        old['on_error']('old secret')
        self.assertTrue(viewer._pixmap.isNull())
        self.assertEqual(viewer.picture._text, tr('Загрузка…', 'fr'))
        self.assertEqual(viewer.counter.text(), '2 / 2')
        current['on_done'](image_bytes())
        self.assertFalse(viewer._pixmap.isNull())
        self.assertEqual(viewer.status.text(), '240 × 160')
        self.assertTrue(old['cancelled'].is_set())

    def test_changed_session_and_deleted_viewer_keep_late_results_inert(self):
        viewer = self.viewer()
        callbacks = self.jobs.call_args.kwargs
        self.current = object()
        viewer.retranslate('fr')
        callbacks['on_done'](image_bytes())
        self.assertTrue(viewer._pixmap.isNull())
        viewer._check_session()
        self.assertTrue(callbacks['cancelled'].is_set())
        viewer.retranslate('en')
        viewer.deleteLater()
        QCoreApplication.sendPostedEvents(viewer, QEvent.Type.DeferredDelete)
        self.assertFalse(isValid(viewer))
        viewer.retranslate('uz')
        callbacks['on_error']('late')

    def test_save_caption_and_results_use_current_language_without_repeated_save(self):
        viewer = self.viewer(loaded=True)
        for language in LANGUAGES:
            viewer.retranslate(language)
            with patch('maxclient.ui.conversation_photo_viewer.QFileDialog.getSaveFileName',
                       return_value=('synthetic-photo.png', 'PNG')) as chooser, \
                    patch('maxclient.ui.conversation_photo_viewer.QSaveFile') as factory, \
                    patch.object(QPixmap, 'save', return_value=True) as encode:
                writer = factory.return_value
                writer.open.return_value = writer.commit.return_value = True
                viewer.save_image()
                self.assertEqual(chooser.call_args.args[1:], (tr('Сохранить фото', language), 'photo.png', 'PNG (*.png)'))
                factory.assert_called_once_with('synthetic-photo.png')
                encode.assert_called_once_with(writer, 'PNG')
                writer.commit.assert_called_once()
                self.assertEqual(viewer.status.text(), tr('Фото сохранено.', language))
                viewer.retranslate('fr')
                self.assertEqual(viewer.status.text(), tr('Фото сохранено.', 'fr'))
                self.assertEqual(factory.call_count, 1)

    def test_save_failures_remain_failures_after_locale_change(self):
        viewer = self.viewer(loaded=True)
        for opened, encoded, committed in ((False, True, True), (True, False, True), (True, True, False)):
            with patch('maxclient.ui.conversation_photo_viewer.QFileDialog.getSaveFileName',
                       return_value=('synthetic-photo.png', 'PNG')), \
                    patch('maxclient.ui.conversation_photo_viewer.QSaveFile') as factory, \
                    patch.object(QPixmap, 'save', return_value=encoded):
                writer = factory.return_value
                writer.open.return_value, writer.commit.return_value = opened, committed
                viewer.save_image()
                for language in LANGUAGES:
                    viewer.retranslate(language)
                    self.assertEqual(viewer.status.text(), tr('Не удалось сохранить фото.', language))
                self.assertEqual(factory.call_count, 1)
                if opened:
                    writer.cancelWriting.assert_called_once()

    def test_save_cancel_and_modal_context_or_selection_change_prevent_writer(self):
        viewer = self.viewer(loaded=True)
        for scenario in ('cancel', 'session', 'selection'):
            self.current = self.context
            viewer._index = 0
            def chooser(*_):
                viewer.retranslate('fr')
                if scenario == 'session':
                    self.current = object()
                if scenario == 'selection':
                    viewer._index = 1
                return ('', '') if scenario == 'cancel' else ('must-not-write.png', 'PNG')
            with patch('maxclient.ui.conversation_photo_viewer.QFileDialog.getSaveFileName', side_effect=chooser), \
                    patch('maxclient.ui.conversation_photo_viewer.QSaveFile') as writer:
                viewer.save_image()
                writer.assert_not_called()

    def test_preview_states_all_languages_keep_fixed_size_and_pixels(self):
        preview = self.preview()
        size = preview.size()
        for setup, source in ((lambda: None, 'Загрузка фото…'),
                              (preview.set_failed, 'Не удалось загрузить фото\nНажмите, чтобы открыть'),
                              (lambda: preview.set_waiting(False), 'Фото пока недоступно'),
                              (lambda: preview.set_waiting(True), 'Загрузка фото…')):
            setup()
            for language in LANGUAGES:
                preview.retranslate(language)
                self.assertEqual(preview._text, tr(source, language))
                self.assertEqual(preview.accessibleName(), tr('Фото', language))
                self.assertEqual(preview.size(), size)
        pixmap = QPixmap(100, 70)
        pixmap.fill(QColor('red'))
        preview.set_photo(pixmap)
        clicked = Mock()
        preview.clicked.connect(clicked)
        for language in LANGUAGES:
            preview.retranslate(language)
            self.assertEqual(preview._pixmap.cacheKey(), pixmap.cacheKey())
            self.assertEqual(preview._text, '')
            self.assertEqual(preview.size(), size)
        clicked.assert_not_called()
        preview.set_unavailable()
        self.assertTrue(preview._pixmap.isNull())
        self.assertEqual(preview._text, tr('Фото пока недоступно', 'ru'))

    def test_unavailable_preview_and_deleted_preview_retranslate(self):
        preview = self.preview(base_url='', url='')
        self.assertEqual(preview._text, 'Photo unavailable')
        preview.retranslate('fr')
        self.assertEqual(preview._text, tr('Фото недоступно', 'fr'))
        preview.deleteLater()
        QCoreApplication.sendPostedEvents(preview, QEvent.Type.DeferredDelete)
        preview.retranslate('en')

    def test_bubble_constructor_and_retranslate_preserve_message_text_and_binding(self):
        raw = 'Фото сохранено.'
        message = Message(chat_id=303, id=7, text=raw, attaches=[timeline_fixture.photo()])
        bubble = MessageBubble(message, '#0085FF', palette_for('dark'), language='fr')
        self.widgets.append(bubble)
        binding = bubble._photo_bindings[0]
        preview = binding[1]
        self.assertEqual(preview._text, tr('Загрузка фото…', 'fr'))
        text = bubble.findChild(QLabel, 'MessageText')
        self.assertEqual(text.text(), raw)
        for language in LANGUAGES:
            bubble.retranslate_photo_previews(language)
            self.assertIs(bubble.msg, message)
            self.assertIs(bubble._photo_bindings[0], binding)
            self.assertEqual(text.text(), raw)
            self.assertEqual(preview._text, tr('Загрузка фото…', language))

    def test_timeline_retranslate_updates_presenters_without_refresh_or_binding_changes(self):
        view = timeline_fixture._View()
        self.widgets.append(view)
        view.settings = SimpleNamespace(ui_language='en')
        _message, bubble = view.append(timeline_fixture.photo())
        controller = TimelinePhotos(view)
        controller.timer.stop()
        viewer = self.viewer(loaded=True)
        controller._viewers.append(viewer)
        context, bindings, viewers = controller._context, controller._bindings, controller._viewers
        timer = controller.timer.timerId()
        with patch.object(controller, 'refresh') as refresh, patch.object(controller, 'reset') as reset, \
                patch.object(controller.loader, 'bind') as bind, patch.object(controller.loader, 'request') as request:
            for language in LANGUAGES:
                controller.retranslate(language)
                self.assertEqual(bubble._photo_bindings[0][1]._text, tr('Загрузка фото…', language))
                self.assertEqual(viewer.save_button.text(), tr('Сохранить фото', language))
                self.assertIs(controller._context, context)
                self.assertIs(controller._bindings, bindings)
                self.assertIs(controller._viewers, viewers)
                self.assertEqual(controller.timer.timerId(), timer)
            for method in (refresh, reset, bind, request):
                method.assert_not_called()

    def test_timeline_new_viewer_uses_settings_language_and_keeps_bound_jump_hidden(self):
        fixture = timeline_fixture.TimelinePhotosTests()
        fixture.app = self.app
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        fixture.view.settings = SimpleNamespace(ui_language='fr')
        message, _bubble = fixture.view.append(timeline_fixture.photo())
        fixture.show()
        fixture.controller.open_photo(message, message.attaches[0])
        viewer = fixture.controller._viewers[-1]
        viewer._clock.stop()
        self.assertEqual(viewer.save_button.text(), tr('Сохранить фото', 'fr'))
        self.assertTrue(viewer.jump.isHidden())
        self.assertIs(viewer._context, fixture.controller._context)
        self.assertEqual(self.jobs.call_count, 1)

    def test_passive_chat_preview_passes_language_without_starting_photo_work(self):
        context = object()
        service = SimpleNamespace(is_current_conversation=Mock(return_value=True))
        dialog = ChatPreviewDialog(service, Settings(ui_language='fr'), context)
        self.widgets.append(dialog)
        message = Message(chat_id=303, id=7, text='Photo raw message', attaches=[timeline_fixture.photo()])
        with patch('maxclient.ui.chat_preview.run_async') as history:
            dialog._render([message])
        preview = dialog._bubbles[0]._photo_bindings[0][1]
        self.assertEqual(preview._text, tr('Фото пока недоступно', 'fr'))
        self.assertEqual(preview.accessibleName(), tr('Фото', 'fr'))
        self.assertEqual(preview.cursor().shape(), Qt.CursorShape.ArrowCursor)
        self.jobs.assert_not_called()
        history.assert_not_called()


if __name__ == '__main__':
    unittest.main()
