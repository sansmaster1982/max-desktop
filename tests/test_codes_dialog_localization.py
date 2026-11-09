"""Synthetic message viewer: labels-only locale, bound reads and closed lifetime."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import ast
from datetime import datetime, timezone
from pathlib import Path
import re
import unittest
from unittest.mock import Mock, patch

from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtGui import QFont, QTextCursor
from PySide6.QtWidgets import QApplication, QDialog
from shiboken6 import isValid

from maxclient.core._codes_dialog_translations import CODES_DIALOG_TRANSLATIONS
from maxclient.core.i18n import tr, translation_available
from maxclient.ui.verification_codes_dialog import VerificationCodesDialog
from maxclient.ui.theme import build_stylesheet
from qt_test_lifecycle import capture_qt_state, restore_qt_state


LANGUAGES = ('ru', 'en', 'es', 'pt', 'fr', 'uz', 'be')
CHECKING = 'Проверка доступного служебного чата…'
AVAILABLE = 'Доступные сообщения. Сверьте время сообщения со своим запросом входа.'
NO_TIME = 'Сообщения с подтверждённым серверным временем не найдены.'
UNAVAILABLE = ('Чат «Коды подтверждения» не доступен этой сессии. '
               'Если настроено основное устройство MAX, проверьте код на нём или дождитесь SMS на номер аккаунта.')
EMPTY = ('Служебный чат найден, но доступного сообщения с кодом пока нет. '
         'Если код скрыт для этого устройства, проверьте основное устройство MAX или SMS.')
ERROR = 'Не удалось обновить служебный чат. Проверьте соединение и повторите проверку.'
STALE = 'Сессия недоступна. Закройте окно и откройте его снова.'


class FakeService:
    def __init__(self):
        self.context = (101, object(), object())
        self.reads = []
        self.capture_calls = 0

    def _history_read_context(self):
        self.capture_calls += 1
        if self.context is None:
            raise ValueError('synthetic unavailable')
        return (self.context[0], self.context[1], self.context[2])

    def _require_history_context(self, context):
        current = self.context
        if (current is None or context[0] != current[0] or context[1] is not current[1]
                or context[2] is not current[2]):
            raise ValueError('synthetic changed session')
        return context[1]

    def verification_code_messages(self, *, context=None):
        self._require_history_context(context)
        self.reads.append(context)
        return {'status': 'available', 'messages': [{'time_ms': 0, 'text': 'Synthetic 000123'}]}


class CodesDialogLocalizationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.addCleanup(restore_qt_state, capture_qt_state())
        self.app.setStyleSheet('')
        self.app.setStyle('Fusion')
        self.app.setFont(QFont('Segoe UI', 10))
        self.app.setStyleSheet(build_stylesheet('light', '#0085FF'))
        self.scheduled = []
        self.timer_patch = patch('maxclient.ui.verification_codes_dialog.QTimer.singleShot',
                                 side_effect=lambda delay, callback: self.scheduled.append((delay, callback)))
        self.timer_patch.start()
        self.addCleanup(self.timer_patch.stop)
        self.jobs_patch = patch('maxclient.ui.verification_codes_dialog.run_async')
        self.jobs = self.jobs_patch.start()
        self.addCleanup(self.jobs_patch.stop)
        self.clipboard = Mock()
        self.clipboard_patch = patch('maxclient.ui.verification_codes_dialog.QApplication.clipboard',
                                     return_value=self.clipboard)
        self.clipboard_get = self.clipboard_patch.start()
        self.addCleanup(self.clipboard_patch.stop)
        self.service = FakeService()
        self.dialogs = []
        self.dialog = self.make_dialog()

    def make_dialog(self, language='en', service=None):
        dialog = VerificationCodesDialog(self.service if service is None else service, language=language)
        self.dialogs.append(dialog)
        dialog.show()
        self.app.processEvents()
        return dialog

    def tearDown(self):
        for dialog in self.dialogs:
            if isValid(dialog):
                dialog.close()
                dialog.deleteLater()
                QCoreApplication.sendPostedEvents(dialog, QEvent.Type.DeferredDelete)
        self.app.processEvents()

    def display(self, text='Synthetic 000123', stamp=0, dialog=None):
        (dialog or self.dialog)._received({'status': 'available', 'messages': [{'time_ms': stamp, 'text': text}]})

    def test_thirteen_sources_twelve_new_tuples_six_languages_and_semantic_anchors(self):
        self.assertEqual(len(CODES_DIALOG_TRANSLATIONS), 12)
        for source, values in CODES_DIALOG_TRANSLATIONS.items():
            self.assertEqual(len(values), 6)
            for language, value in zip(LANGUAGES[1:], values):
                self.assertEqual(tr(source, language), value)
                if language != 'be':
                    self.assertIsNone(re.search('[А-Яа-яЁё]', value))
            self.assertEqual(tr(source, 'pt-BR'), values[2])
        self.assertEqual(tr('Копировать последнее сообщение', 'en'), 'Copy the latest message')
        self.assertIn('unavailable in this session', tr(UNAVAILABLE, 'en'))
        self.assertIn('no message containing a code is available yet', tr(EMPTY, 'en'))
        self.assertIn('Check the message time', tr(AVAILABLE, 'en'))
        source = Path(__file__).parents[1] / 'maxclient/ui/verification_codes_dialog.py'
        strings = {node.value for node in ast.walk(ast.parse(source.read_text(encoding='utf-8-sig')))
                   if isinstance(node, ast.Constant) and isinstance(node.value, str)
                   and re.search('[А-Яа-яЁё]', node.value)}
        self.assertEqual(len(strings), 13)
        for source in strings:
            for language in LANGUAGES[1:]:
                self.assertTrue(translation_available(source, language), (source, language))

    def test_constructor_language_static_controls_and_one_initial_check_only(self):
        for language in (*LANGUAGES, 'pt-BR', None):
            before = len(self.scheduled)
            dialog = self.make_dialog(language)
            self.assertEqual(len(self.scheduled), before + 1)
            self.assertEqual(self.scheduled[-1][0], 0)
            self.assertEqual(dialog.windowTitle(), tr('Коды подтверждения — MAX', language))
            self.assertEqual(dialog.title.text(), tr('Коды подтверждения', language))
            self.assertEqual(dialog.messages.placeholderText(), tr('Здесь появится доступное сообщение с серверным временем.', language))
            self.assertEqual(dialog.refresh_button.text(), tr('Проверить сообщения', language))
            self.assertEqual(dialog.copy_button.text(), tr('Копировать последнее сообщение', language))
            self.assertEqual(dialog.close_button.text(), tr('Закрыть', language))
            self.assertEqual(dialog.status.text(), tr(CHECKING, language))
        self.jobs.assert_not_called()
        self.assertEqual(self.service.reads, [])

    def test_every_result_status_and_raw_error_use_current_language(self):
        cases = [({'status': 'empty', 'messages': []}, EMPTY),
                 ({'status': 'chat_not_available', 'messages': []}, UNAVAILABLE),
                 ({'status': 'available', 'messages': [{'time_ms': 0, 'text': 'Synthetic'}]}, AVAILABLE),
                 ({'status': 'available', 'messages': [{'time_ms': 2**63-1, 'text': 'Synthetic'}]}, NO_TIME),
                 ({'status': 'unknown', 'messages': []}, ERROR),
                 ({'status': 'available', 'messages': 'bad'}, ERROR)]
        for result, source in cases:
            self.dialog._received(result)
            for language in LANGUAGES:
                self.dialog.retranslate(language)
                self.assertEqual(self.dialog.status.text(), tr(source, language))
                self.assertEqual(self.dialog._status_source, source)
        self.dialog._failed('<img src="secret"> Коды подтверждения')
        self.assertNotIn('secret', self.dialog.status.text())
        self.assertEqual(self.dialog.status.textFormat(), Qt.TextFormat.PlainText)
        self.jobs.assert_not_called()
        self.clipboard_get.assert_not_called()

    def test_retranslate_preserves_document_raw_times_cursor_selection_scroll_focus_and_state(self):
        text = 'Коды подтверждения\n<b>Synthetic 000123</b>\n' + '\n'.join(f'raw line {i}' for i in range(70))
        stamp = 1793459502000.25
        self.display(text, stamp)
        messages = self.dialog.messages
        messages.setFocus()
        cursor = messages.textCursor()
        cursor.setPosition(28)
        cursor.setPosition(48, QTextCursor.MoveMode.KeepAnchor)
        messages.setTextCursor(cursor)
        messages.verticalScrollBar().setValue(20)
        self.app.processEvents()
        document = messages.document()
        expected = messages.toPlainText()
        expected_stamp = datetime.fromtimestamp(stamp/1000, tz=timezone.utc).astimezone().strftime('%d.%m.%Y %H:%M:%S')
        self.assertEqual(expected, expected_stamp + '\n' + text)
        state = (cursor.position(), cursor.anchor(), messages.verticalScrollBar().value(),
                 messages.horizontalScrollBar().value(), QApplication.focusWidget(), self.dialog._context,
                 self.dialog._busy, self.dialog.refresh_button.isEnabled(), self.dialog.copy_button.isEnabled())
        changed = []
        messages.textChanged.connect(lambda: changed.append('text'))
        messages.selectionChanged.connect(lambda: changed.append('selection'))
        messages.cursorPositionChanged.connect(lambda: changed.append('cursor'))
        with patch.object(self.dialog, 'refresh', side_effect=AssertionError('refresh forbidden')), \
                patch.object(self.service, '_require_history_context', side_effect=AssertionError('context reread forbidden')):
            for language in LANGUAGES:
                self.dialog.retranslate(language)
                self.app.processEvents()
                self.assertIs(messages.document(), document)
                self.assertEqual(messages.toPlainText(), expected)
                self.assertEqual(self.dialog._last_text, text)
                current = messages.textCursor()
                self.assertEqual((current.position(), current.anchor(), messages.verticalScrollBar().value(),
                                  messages.horizontalScrollBar().value(), QApplication.focusWidget(), self.dialog._context,
                                  self.dialog._busy, self.dialog.refresh_button.isEnabled(), self.dialog.copy_button.isEnabled()), state)
        self.assertEqual(changed, [])
        self.jobs.assert_not_called()
        self.clipboard_get.assert_not_called()

    def test_pending_locale_does_not_restart_read_callback_uses_new_language(self):
        self.scheduled[0][1]()
        self.assertTrue(self.dialog._busy)
        self.assertEqual(self.jobs.call_count, 1)
        worker = self.jobs.call_args.args[0]
        done = self.jobs.call_args.kwargs['on_done']
        context = self.dialog._context
        self.dialog.retranslate('fr')
        self.assertTrue(self.dialog._busy)
        self.assertFalse(self.dialog.refresh_button.isEnabled())
        self.assertIs(self.dialog._context, context)
        self.assertEqual(self.jobs.call_count, 1)
        result = worker()
        self.assertIs(self.service.reads[0], context)
        done(result)
        self.assertFalse(self.dialog._busy)
        self.assertEqual(self.dialog.status.text(), tr(AVAILABLE, 'fr'))
        self.assertEqual(self.dialog._last_text, 'Synthetic 000123')
        self.assertEqual(self.service.capture_calls, 1)

    def test_context_switch_before_refresh_or_worker_never_reads_new_account(self):
        original = self.service.context
        self.service.context = (202, object(), object())
        self.scheduled[0][1]()
        self.jobs.assert_not_called()
        self.assertTrue(self.dialog._closed)
        self.assertEqual(self.dialog.status.text(), tr(STALE, 'en'))
        self.service.context = original
        dialog = self.make_dialog()
        dialog.refresh()
        worker = self.jobs.call_args.args[0]
        self.service.context = (original[0], original[1], object())
        with self.assertRaises(ValueError):
            worker()
        self.assertEqual(self.service.reads, [])
        self.jobs.call_args.kwargs['on_error']('private raw error')
        self.assertTrue(dialog._closed)
        self.assertEqual(dialog.messages.toPlainText(), '')
        self.assertEqual(dialog.status.text(), tr(STALE, 'en'))

    def test_context_switch_before_callback_and_copy_purges_without_clipboard_access(self):
        self.dialog.refresh()
        done = self.jobs.call_args.kwargs['on_done']
        old = self.service.context
        self.service.context = (old[0], object(), old[2])
        done({'status': 'available', 'messages': [{'time_ms': 0, 'text': 'Do not reveal'}]})
        self.assertEqual(self.dialog._last_text, '')
        self.assertEqual(self.dialog.messages.toPlainText(), '')
        self.assertEqual(self.dialog.status.text(), tr(STALE, 'en'))
        queued = self.jobs.call_count
        context = self.dialog._context
        self.dialog.retranslate('fr')
        self.assertEqual(self.dialog.status.text(), tr(STALE, 'fr'))
        self.assertTrue(self.dialog._closed)
        self.assertIs(self.dialog._context, context)
        self.dialog._copy()
        self.dialog.refresh()
        self.assertEqual(self.jobs.call_count, queued)
        self.assertEqual(self.service.reads, [])
        self.assertEqual(self.dialog.messages.toPlainText(), '')
        self.service.context = old
        other = self.make_dialog()
        self.display('Synthetic copy candidate', dialog=other)
        self.service.context = (old[0], old[1], object())
        other._copy()
        self.assertTrue(other._closed)
        self.assertFalse(other.copy_button.isEnabled())
        self.assertEqual(other.messages.toPlainText(), '')
        self.assertEqual(other.status.text(), tr(STALE, 'en'))
        self.clipboard_get.assert_not_called()

    def test_explicit_copy_uses_exact_last_body_without_translation_or_timestamp(self):
        raw = 'Копировать последнее сообщение\n<b>Synthetic 000123</b>'
        self.display(raw)
        for language in LANGUAGES:
            self.dialog.retranslate(language)
        self.clipboard_get.assert_not_called()
        self.dialog.copy_button.click()
        self.clipboard.setText.assert_called_once_with(raw)

    def test_close_done_and_reject_purge_idempotently_and_cancel_initial_callback(self):
        for operation in ('close', 'done', 'reject'):
            dialog = self.make_dialog()
            delayed = self.scheduled[-1][1]
            self.display('Synthetic sensitive body', dialog=dialog)
            status_before = dialog.status.text()
            if operation == 'done':
                dialog.done(QDialog.DialogCode.Accepted)
            else:
                getattr(dialog, operation)()
            self.assertTrue(dialog._closed)
            self.assertFalse(dialog._busy)
            self.assertEqual(dialog._last_text, '')
            self.assertEqual(dialog.messages.toPlainText(), '')
            self.assertFalse(dialog.copy_button.isEnabled())
            self.assertFalse(dialog.refresh_button.isEnabled())
            self.assertEqual(dialog.status.text(), status_before)
            dialog.reject()
            delayed()
            dialog._received({'status': 'available', 'messages': [{'time_ms': 0, 'text': 'Late'}]})
            dialog._failed('raw')
            dialog._copy()
            dialog.retranslate('fr')
            self.assertEqual(dialog.messages.toPlainText(), '')
        self.jobs.assert_not_called()
        self.clipboard_get.assert_not_called()

    def test_deleted_dialog_late_calls_do_not_access_qt_or_service(self):
        self.dialog.refresh()
        worker = self.jobs.call_args.args[0]
        callbacks = self.jobs.call_args.kwargs
        self.dialog.deleteLater()
        QCoreApplication.sendPostedEvents(self.dialog, QEvent.Type.DeferredDelete)
        self.assertFalse(isValid(self.dialog))
        with self.assertRaises(ValueError):
            worker()
        callbacks['on_done']({'status': 'empty', 'messages': []})
        callbacks['on_error']('raw')
        self.dialog.retranslate('fr')
        self.dialog.refresh()
        self.dialog._copy()
        self.assertEqual(self.service.reads, [])
        self.clipboard_get.assert_not_called()

    def test_missing_context_and_dispatch_error_are_fail_closed_without_raw_exception(self):
        service = FakeService()
        service.context = None
        dialog = self.make_dialog(service=service)
        self.scheduled[-1][1]()
        self.assertTrue(dialog._closed)
        self.assertEqual(dialog.status.text(), tr(STALE, 'en'))
        self.jobs.assert_not_called()
        self.jobs.side_effect = RuntimeError('private raw exception')
        self.dialog.refresh()
        self.assertFalse(self.dialog._busy)
        self.assertEqual(self.dialog.status.text(), tr(ERROR, 'en'))
        self.assertNotIn('private', self.dialog.status.text())
        self.assertFalse(self.dialog.copy_button.isEnabled())

    def test_production_light_dark_all_languages_keep_visible_controls_within_dialog(self):
        self.dialog._received({'status': 'chat_not_available', 'messages': []})
        for theme in ('light', 'dark'):
            self.app.setStyleSheet(build_stylesheet(theme, '#0085FF'))
            for language in LANGUAGES:
                self.dialog.retranslate(language)
                self.app.processEvents()
                for widget in (self.dialog.title, self.dialog.status, self.dialog.messages,
                               self.dialog.refresh_button, self.dialog.copy_button, self.dialog.close_button):
                    self.assertTrue(widget.isVisible())
                    self.assertTrue(self.dialog.rect().contains(widget.geometry()))
                self.assertFalse(self.dialog.copy_button.isEnabled())
                self.assertEqual(self.dialog.messages.toPlainText(), '')
