"""File localization with synthetic descriptors/workers; no transport or file IO."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import ast
from pathlib import Path
from string import Formatter
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtGui import QFont
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QLabel, QPushButton
from shiboken6 import isValid

from maxclient.core._file_download_translations import FILE_DOWNLOAD_TRANSLATIONS
from maxclient.core.conversation_file_service import ConversationFileDownload
from maxclient.core.i18n import SUPPORTED_LANGUAGES, tr, translation_available
from maxclient.ui import conversation_file_dialog as module
from maxclient.ui.conversation_file_controller import ConversationFileController
from maxclient.ui.theme import build_stylesheet
from qt_test_lifecycle import capture_qt_state, restore_qt_state
from test_desktop_shell import _load_offscreen_font


class FileDownloadLocalizationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.addCleanup(restore_qt_state, capture_qt_state())
        _load_offscreen_font(self.app)
        self.descriptor = SimpleNamespace(context=object(), key='synthetic-file', file_id=17,
            file_name='<b>Файл сохранён & raw.txt</b>', size=10485760,
            chat_id=303, message_id=7, time_ms=123)
        self.service = SimpleNamespace(is_current_conversation_file=Mock(return_value=True),
            resolve_conversation_file_download=Mock(side_effect=AssertionError('No live requests')))
        self.runner_patch = patch('maxclient.ui.conversation_file_controller.run_async')
        self.jobs = self.runner_patch.start()
        self.addCleanup(self.runner_patch.stop)

    def controller(self, language='en'):
        controller = ConversationFileController(self.service, self.descriptor, 'synthetic.txt',
                                                object(), language=language)
        self.addCleanup(self.dispose, controller)
        return controller

    @staticmethod
    def dispose(controller):
        if isValid(controller):
            controller.cancel()
            if isValid(controller.dialog):
                controller.dialog.close()
            controller.deleteLater()

    @staticmethod
    def snapshot(dialog):
        return (dialog._state, dialog._cancel_pending, dialog._received, dialog._total,
                dialog._has_progress, dialog.progress_bar.minimum(), dialog.progress_bar.maximum(),
                dialog.progress_bar.value(), dialog.cancel_button.isEnabled(),
                dialog.message_button.isEnabled(), dialog.descriptor)

    def assert_latin(self, text, language):
        if language in ('en', 'es', 'pt', 'fr', 'uz'):
            self.assertFalse(any('\u0400' <= char <= '\u04ff' for char in text), (language, text))

    def test_catalog_placeholders_and_all_owned_dialog_sources(self):
        self.assertEqual(len(FILE_DOWNLOAD_TRANSLATIONS), 25)
        self.assertTrue({'Файл удалён', 'Скачивание недоступно', 'Действия с файлом',
                         'Копировать имя файла'} <= FILE_DOWNLOAD_TRANSLATIONS.keys())
        fields = lambda text: {name for _, name, _, _ in Formatter().parse(text) if name}
        for source, values in FILE_DOWNLOAD_TRANSLATIONS.items():
            self.assertEqual(len(values), 6)
            for language, value in zip(SUPPORTED_LANGUAGES[1:], values):
                self.assertEqual(fields(source), fields(value))
                self.assertEqual(tr(source, language), value)
                self.assert_latin(value, language)
        tree = ast.parse(Path(module.__file__).read_text(encoding='utf-8'))
        sources = {node.value for node in ast.walk(tree)
                   if isinstance(node, ast.Constant) and isinstance(node.value, str)
                   and any('\u0400' <= char <= '\u04ff' for char in node.value)}
        for source in sources:
            for language in SUPPORTED_LANGUAGES:
                self.assertTrue(translation_available(source, language), (source, language))
        for language in SUPPORTED_LANGUAGES:
            self.assertTrue(tr('Все файлы (*)', language).endswith('(*)'))

    def test_constructor_labels_in_seven_languages_preserve_literal_descriptor(self):
        for language in SUPPORTED_LANGUAGES:
            dialog = module.FileDownloadDialog(self.descriptor, language=language)
            self.assertIs(dialog.descriptor, self.descriptor)
            self.assertEqual(dialog.filename_label._full_text, self.descriptor.file_name)
            self.assertEqual(dialog.filename_label.accessibleName(), self.descriptor.file_name)
            self.assertIn('&lt;b&gt;', dialog.filename_label.toolTip())
            self.assertEqual(dialog.filename_label.textFormat(), Qt.TextFormat.PlainText)
            for label in (dialog.heading, dialog.status_label, dialog.size_label, dialog.progress_label):
                self.assert_latin(label.text(), language)
            self.assert_latin(dialog.windowTitle() + dialog.cancel_button.text() +
                              dialog.message_button.text() + dialog.progress_bar.accessibleName(), language)
            self.assertEqual(dialog.progress_label.text(), '')
            self.assertFalse(dialog.isModal())
        self.jobs.assert_not_called()

    def test_independent_english_status_and_progress_anchors(self):
        dialog = module.FileDownloadDialog(self.descriptor, language='en')
        self.assertEqual(dialog.message_button.text(), 'Go to message')
        self.assertEqual(dialog.progress_bar.accessibleName(), 'File download progress')
        self.assertEqual(dialog.size_label.text(), '10.0 MB')
        dialog.set_progress(512, 1024)
        self.assertEqual(dialog.status_label.text(), 'Downloading…')
        self.assertEqual(dialog.progress_label.text(), '512 B of 1.0 KB')
        dialog._request_cancel()
        self.assertEqual(dialog.status_label.text(), 'Cancelling…')
        dialog.set_status('cancelled')
        self.assertEqual(dialog.status_label.text(), 'Download cancelled')

    def test_every_phase_retranslates_without_transition_signal_or_state_mutation(self):
        for state in ('connecting', 'downloading', 'saving', 'done', 'error', 'cancelled', 'cancelling'):
            with self.subTest(state=state):
                dialog = module.FileDownloadDialog(self.descriptor)
                cancelled, message = Mock(), Mock()
                dialog.cancel_requested.connect(cancelled)
                dialog.message_requested.connect(message)
                if state != 'connecting':
                    dialog.set_progress(2**41, 2**42)
                if state not in ('connecting', 'downloading'):
                    dialog.set_status(state)
                before = self.snapshot(dialog)
                with patch.object(dialog, 'set_progress') as progress, \
                     patch.object(dialog, 'set_status') as transition, \
                     patch.object(dialog, '_request_cancel') as cancel:
                    for language in SUPPORTED_LANGUAGES:
                        dialog.retranslate(language)
                        self.assertEqual(self.snapshot(dialog), before)
                        self.assertEqual(dialog.status_label.text(), tr(module._STATES[state], language))
                        self.assert_latin(dialog.progress_label.text(), language)
                    for method in (progress, transition, cancel, cancelled, message):
                        method.assert_not_called()

    def test_pending_local_cancel_keeps_state_and_blocks_late_updates_after_translation(self):
        dialog = module.FileDownloadDialog(self.descriptor, language='en')
        cancelled = Mock()
        dialog.cancel_requested.connect(cancelled)
        dialog.set_progress(3, 10)
        dialog.cancel_button.click()
        self.assertEqual(dialog._state, 'downloading')
        before = self.snapshot(dialog)
        for language in SUPPORTED_LANGUAGES:
            dialog.retranslate(language)
            self.assertEqual(dialog.status_label.text(), tr('Отмена…', language))
            self.assertEqual(self.snapshot(dialog), before)
            self.assertFalse(dialog.set_progress(4, 10))
            self.assertFalse(dialog.set_status('downloading'))
            dialog.cancel_button.click()
        cancelled.assert_called_once_with()
        self.assertTrue(dialog.set_status('done'))
        dialog.retranslate('fr')
        self.assertEqual(dialog.status_label.text(), tr('Файл сохранён', 'fr'))
        self.assertFalse(dialog.set_status('cancelled'))
        self.assertEqual(dialog.progress_bar.value(), 1000)

    def test_initial_empty_observed_zero_unknown_and_zero_total_stay_distinct(self):
        dialog = module.FileDownloadDialog(self.descriptor)
        for language in SUPPORTED_LANGUAGES:
            dialog.retranslate(language)
            self.assertEqual(dialog.progress_label.text(), '')
            self.assertFalse(dialog._has_progress)
        dialog.set_status('downloading')
        dialog.retranslate('en')
        self.assertEqual(dialog.progress_label.text(), '')
        dialog.set_progress(0, None)
        self.assertEqual(dialog.progress_label.text(), '0 B')
        self.assertEqual(dialog.progress_bar.maximum(), 0)
        dialog.set_progress(0, 0)
        self.assertEqual(dialog.progress_label.text(), '0 B of 0 B')
        self.assertEqual(dialog.progress_bar.value(), 0)
        for language in SUPPORTED_LANGUAGES:
            before = self.snapshot(dialog)
            dialog.set_language(language)
            self.assertEqual(self.snapshot(dialog), before)

    def test_size_formatting_changes_units_only_and_preserves_validation(self):
        for value, expected in ((0, '0 B'), (1023, '1023 B'), (1024, '1.0 KB'),
                                (1536, '1.5 KB'), (2**40, '1.0 TB')):
            self.assertEqual(module.format_size(value, 'en'), expected)
        self.assertEqual(module.format_size(1536), '1.5 КБ')
        self.assertEqual(module.format_size(1536, 'fr'), '1.5 Ko')
        for value in (None, True, -1, float('inf'), float('nan'), '1024'):
            for language in SUPPORTED_LANGUAGES:
                self.assertEqual(module.format_size(value, language), '')
        self.descriptor.size = None
        dialog = module.FileDownloadDialog(self.descriptor, language='en')
        self.assertEqual(dialog.size_label.text(), 'Size not provided')

    def test_controller_language_change_preserves_pending_worker_and_event(self):
        controller = self.controller()
        controller.start()
        controller.cancel()
        event = controller._cancel
        state = (controller._active, controller._started, controller._worker_pending, controller._dialog_gone)
        before = self.snapshot(controller.dialog)
        calls = self.jobs.call_count
        with patch.object(controller, 'start') as start, patch.object(controller, 'cancel') as cancel, \
             patch.object(controller, '_current') as current:
            for language in SUPPORTED_LANGUAGES:
                controller.retranslate(language)
                self.assertIs(controller._cancel, event)
                self.assertTrue(event.is_set())
                self.assertEqual((controller._active, controller._started, controller._worker_pending,
                                  controller._dialog_gone), state)
                self.assertEqual(self.snapshot(controller.dialog), before)
            for method in (start, cancel, current):
                method.assert_not_called()
        self.assertEqual(self.jobs.call_count, calls)
        self.assertTrue(controller.active)
        self.jobs.call_args.kwargs['on_error']('raw secret')
        self.assertFalse(controller.active)
        self.assertEqual(controller.dialog.status_label.text(), tr('Скачивание отменено', 'be'))

    def test_delayed_unsafe_ack_uses_current_language_without_resolving_again(self):
        controller = self.controller()
        controller.start()
        controller.retranslate('fr')
        with patch('maxclient.ui.conversation_file_controller.confirm_download', return_value=False) as confirm:
            self.jobs.call_args.kwargs['on_done'](ConversationFileDownload(
                self.descriptor, 'https://files.example.test/synthetic', True))
        confirm.assert_called_once_with(self.descriptor, controller.dialog, theme='light', language='fr')
        self.assertEqual(self.jobs.call_count, 1)
        self.service.resolve_conversation_file_download.assert_not_called()
        self.assertEqual(controller.dialog.status_label.text(), tr('Скачивание отменено', 'fr'))

    def test_live_modal_retranslation_preserves_focus_cancel_and_context_guard(self):
        controller = self.controller()
        controller.start()
        def nested(warning):
            warning.show()
            self.app.processEvents()
            warning.proceed_button.setFocus()
            focus = warning.focusWidget()
            self.assertIs(controller.dialog._unsafe_dialog, warning)
            controller.retranslate('fr')
            self.assertEqual(warning.heading.text(), 'Ce fichier peut être dangereux')
            self.assertIs(warning.focusWidget(), focus)
            self.assertTrue(warning.cancel_button.isDefault())
            self.assertFalse(warning.proceed_button.autoDefault())
            self.assertEqual(warning.result(), QDialog.DialogCode.Rejected)
            self.service.is_current_conversation_file.return_value = False
            return QDialog.DialogCode.Accepted
        with patch.object(module.UnsafeFileDownloadDialog, 'exec', nested):
            self.jobs.call_args.kwargs['on_done'](ConversationFileDownload(
                self.descriptor, 'https://files.example.test/synthetic', True))
        self.assertIsNone(controller.dialog._unsafe_dialog)
        self.assertEqual(self.jobs.call_count, 1)
        self.assertFalse(controller.active)

    def test_unsafe_default_return_escape_and_explicit_accept_in_seven_languages(self):
        for language in SUPPORTED_LANGUAGES:
            for key in (Qt.Key.Key_Return, Qt.Key.Key_Escape):
                warning = module.UnsafeFileDownloadDialog(self.descriptor, language=language)
                warning.show()
                self.app.processEvents()
                warning.retranslate(language)
                self.assertTrue(warning.cancel_button.isDefault())
                self.assertTrue(warning.cancel_button.hasFocus())
                self.assertFalse(warning.proceed_button.autoDefault())
                self.assertEqual(warning.filename_label._full_text, self.descriptor.file_name)
                self.assert_latin(warning.heading.text() + warning.body.text(), language)
                QTest.keyClick(warning, key)
                self.assertEqual(warning.result(), QDialog.DialogCode.Rejected)
                self.assertFalse(warning.isVisible())
            warning = module.UnsafeFileDownloadDialog(self.descriptor, language=language)
            warning.proceed_button.click()
            self.assertEqual(warning.result(), QDialog.DialogCode.Accepted)

    def test_confirm_wrappers_pass_language_and_only_return_modal_acceptance(self):
        for function in (module.confirm_download, module.FileDownloadDialog.confirm_download):
            for accepted in (False, True):
                def inspect(warning):
                    self.assertEqual(warning.heading.text(), 'Ce fichier peut être dangereux')
                    return QDialog.DialogCode.Accepted if accepted else QDialog.DialogCode.Rejected
                with patch.object(module.UnsafeFileDownloadDialog, 'exec', inspect):
                    self.assertIs(function(self.descriptor, language='fr'), accepted)

    def test_parent_destruction_inside_modal_cleans_reference_without_second_confirmation(self):
        parent = module.FileDownloadDialog(self.descriptor)
        def nested(warning):
            parent.deleteLater()
            QCoreApplication.sendPostedEvents(parent, QEvent.Type.DeferredDelete)
            self.assertFalse(isValid(parent))
            self.assertFalse(isValid(warning))
            return QDialog.DialogCode.Rejected
        with patch.object(module.UnsafeFileDownloadDialog, 'exec', nested):
            self.assertFalse(module.confirm_download(self.descriptor, parent, language='en'))
        self.assertIsNone(parent._unsafe_dialog)

    def test_terminal_controller_and_destroyed_receivers_retranslate_safely(self):
        controller = self.controller()
        controller.start()
        self.jobs.call_args.kwargs['on_error']('raw path or URL')
        self.assertFalse(controller.active)
        self.assertTrue(controller.retranslate('en'))
        self.assertEqual(controller.dialog.status_label.text(), 'Could not download the file')
        controller.dialog.close()
        self.assertFalse(controller.retranslate('fr'))
        controller.deleteLater()
        QCoreApplication.sendPostedEvents(controller, QEvent.Type.DeferredDelete)
        self.assertFalse(controller.retranslate('uz'))

    def test_locale_change_does_not_commit_discard_or_repeat_download(self):
        controller = self.controller()
        controller.start()
        self.jobs.call_args.kwargs['on_done'](ConversationFileDownload(
            self.descriptor, 'https://files.example.test/synthetic', False))
        self.assertEqual(self.jobs.call_count, 2)
        staged = Mock()
        self.jobs.call_args.kwargs['on_done'](staged)
        staged.commit.assert_called_once()
        staged.discard.assert_not_called()
        self.assertFalse(controller.active)
        for language in SUPPORTED_LANGUAGES:
            controller.retranslate(language)
            self.assertEqual(controller.dialog.status_label.text(), tr('Файл сохранён', language))
            self.assertEqual(controller.dialog.progress_bar.value(), 1000)
        staged.commit.assert_called_once()
        staged.discard.assert_not_called()
        self.assertEqual(self.jobs.call_count, 2)

    def test_narrow_light_dark_layout_keeps_buttons_and_raw_filename_inside(self):
        # Match MaxApp's actual font/style, then also retain standalone coverage.
        # The global 14px theme makes the button hint 39px rather than 38px.
        self.app.setFont(QFont('Segoe UI', 10))
        self.app.setStyle('Fusion')
        for application_theme in (False, True):
            for language in SUPPORTED_LANGUAGES:
                for theme in ('light', 'dark'):
                    self.app.setStyleSheet(build_stylesheet(theme, '#0085FF') if application_theme else '')
                    for cls in (module.FileDownloadDialog, module.UnsafeFileDownloadDialog):
                        with self.subTest(language=language, theme=theme, dialog=cls.__name__,
                                          style='Fusion', font=self.app.font().toString(),
                                          application_theme=application_theme):
                            dialog = cls(self.descriptor, theme=theme, language=language)
                            dialog.resize(360, 264)
                            dialog.show()
                            self.app.processEvents()
                            for button in dialog.findChildren(QPushButton):
                                details = (button.text(), button.font().toString(), button.fontMetrics().height())
                                self.assertGreaterEqual(button.width(), button.sizeHint().width(), details)
                                self.assertGreaterEqual(button.height(), button.sizeHint().height(), details)
                            for label in dialog.findChildren(QLabel):
                                self.assertTrue(dialog.rect().contains(label.mapTo(dialog, label.rect().bottomRight())))
                            self.assertEqual(dialog.filename_label._full_text, self.descriptor.file_name)
                            dialog.hide()


if __name__ == '__main__':
    unittest.main()
