"""Synthetic creation localization with unchanged drafts and write boundaries."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import ast
from pathlib import Path
from string import Formatter
import unittest
from unittest.mock import Mock, patch

from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtWidgets import QApplication, QLabel, QLineEdit, QPushButton
from shiboken6 import isValid

from maxclient.core._conversation_create_translations import CONVERSATION_CREATE_TRANSLATIONS
from maxclient.core.conversation_create import ConversationCreateResult
from maxclient.core.i18n import SUPPORTED_LANGUAGES, tr, translation_available
from maxclient.core.models import Chat
from maxclient.protocol.client import MaxError, MaxWriteOutcomeUnknown
from maxclient.protocol.conversation_create_contracts import conversation_create_values
from maxclient.ui import conversation_create_dialog as module
from qt_test_lifecycle import capture_qt_state, restore_qt_state
from test_desktop_shell import _load_offscreen_font
import test_conversation_create_dialog as fixture


class ConversationCreateLocalizationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.addCleanup(restore_qt_state, capture_qt_state())
        _load_offscreen_font(self.app)
        self.service = fixture.FakeService()
        self.addCleanup(module._UI_WRITES.pop, self.service, None)
        self.jobs = []
        self.saved = Mock()
        runner = patch.object(module, 'run_async', side_effect=self.queue)
        self.runner = runner.start()
        self.addCleanup(runner.stop)

    def queue(self, function, *args, on_done, on_error, **kwargs):
        self.jobs.append((function, args, kwargs, on_done, on_error))

    def finish(self, index=0):
        function, args, kwargs, done, error = self.jobs[index]
        try:
            result = function(*args, **kwargs)
        except Exception as exc:
            error(str(exc))
        else:
            done(result)

    def dialog(self, kind='channel', language='en'):
        dialog = module.ConversationCreateDialog(self.service, kind, language=language)
        dialog.conversation_created.connect(self.saved)
        return dialog

    def assert_latin(self, text, language):
        if language in ('en', 'es', 'pt', 'fr', 'uz'):
            self.assertFalse(any('\u0400' <= char <= '\u04ff' for char in text), (language, text))

    @staticmethod
    def controls(dialog):
        return tuple((control.isEnabled(), control.isHidden()) for control in (
            dialog.title_input, dialog.description_input, dialog.member_search, dialog.member_list,
            dialog.next_button, dialog.back_button, dialog.create_button, dialog.verify_button,
            dialog.open_button, dialog.cancel_button))

    def test_catalog_and_every_owned_source_are_available_in_six_translations(self):
        self.assertEqual(len(CONVERSATION_CREATE_TRANSLATIONS), 31)
        fields = lambda text: {name for _, name, _, _ in Formatter().parse(text) if name}
        for source, values in CONVERSATION_CREATE_TRANSLATIONS.items():
            self.assertEqual(len(values), 6)
            for language, value in zip(SUPPORTED_LANGUAGES[1:], values):
                self.assertEqual(fields(source), fields(value))
                self.assertEqual(tr(source, language), value)
        tree = ast.parse(Path(module.__file__).read_text(encoding='utf-8'))
        sources = {node.value for node in ast.walk(tree)
                   if isinstance(node, ast.Constant) and isinstance(node.value, str)
                   and any('\u0400' <= char <= '\u04ff' for char in node.value)}
        for language in SUPPORTED_LANGUAGES:
            for source in sources:
                self.assertTrue(translation_available(source, language), (source, language))

    def test_both_constructors_localize_labels_and_keep_raw_contacts_in_seven_languages(self):
        self.service.contacts[1].name = 'Название канала <raw>'
        for language in SUPPORTED_LANGUAGES:
            for kind in ('group', 'channel'):
                with self.subTest(language=language, kind=kind):
                    dialog = self.dialog(kind, language)
                    for label in dialog.findChildren(QLabel):
                        self.assert_latin(label.text(), language)
                    for button in dialog.findChildren(QPushButton):
                        self.assert_latin(button.text(), language)
                    for field in dialog.findChildren(QLineEdit):
                        self.assert_latin(field.placeholderText() + field.accessibleName(), language)
                    self.assert_latin(dialog.result_label.accessibleName(), language)
                    if kind == 'group':
                        self.assertIn('Название канала <raw>', dialog.member_list.item(0).text())
                    self.assertEqual(dialog.title_input.text(), '')
                    self.assertFalse(dialog.create_button.isEnabled())
                    dialog.close()
        self.assertEqual(self.service.calls, [])
        self.assertEqual(self.jobs, [])

    def test_english_labels_have_independent_meaning_anchors(self):
        dialog = self.dialog()
        self.assertEqual(dialog._title_label.text(), 'Channel name')
        self.assertEqual(dialog.verify_button.text(), 'Check result')
        self.assertEqual(dialog.open_button.text(), 'Open created chat')
        self.assertEqual(dialog.result_label.accessibleName(), 'Creation result')
        self.assertEqual(dialog.member_search.accessibleName(), 'Find participants by name or phone number')

    def test_retranslation_keeps_raw_draft_selection_page_and_performs_no_reads(self):
        dialog = self.dialog('group')
        dialog.member_list.item(0).setCheckState(Qt.CheckState.Checked)
        dialog.member_list.setCurrentRow(0)
        dialog.member_search.setText('Alice')
        dialog._next()
        dialog.title_input.setText('Название канала <raw> & {count}')
        dialog.title_input.setSelection(2, 8)
        dialog.description_input.setPlainText('Описание <raw>')
        context, members = dialog._context, set(dialog._member_ids)
        state = self.controls(dialog)
        with patch.object(self.service, 'conversation_creation_limits') as limits, \
             patch.object(self.service, 'list_contacts') as contacts, \
             patch.object(self.service, 'pending_conversation_creation') as pending, \
             patch.object(dialog, '_input_changed') as validate:
            for language in SUPPORTED_LANGUAGES:
                dialog.retranslate(language)
                self.assertIs(dialog._context, context)
                self.assertEqual(dialog._member_ids, members)
                self.assertEqual(dialog.member_search.text(), 'Alice')
                self.assertEqual(dialog.member_list.currentRow(), 0)
                self.assertEqual(dialog.title_input.text(), 'Название канала <raw> & {count}')
                self.assertEqual((dialog.title_input.selectionStart(), len(dialog.title_input.selectedText())), (2, 8))
                self.assertEqual(dialog.description_input.toPlainText(), 'Описание <raw>')
                self.assertEqual(dialog.pages.currentIndex(), 1)
                self.assertEqual(self.controls(dialog), state)
            for method in (limits, contacts, pending, validate):
                method.assert_not_called()
        self.saved.assert_not_called()
        self.assertEqual(self.jobs, [])

    def test_dynamic_title_and_description_limits_translate_without_truncation(self):
        self.service.limits.update({'max-cname-length': 4, 'max-description-length': 7})
        dialog = self.dialog()
        dialog.title_input.setText('🙂🙂🙂')
        for language in SUPPORTED_LANGUAGES:
            dialog.set_language(language)
            self.assertEqual(dialog.details_hint.text(), tr(
                'Название должно содержать от 1 до {limit} символов', language, limit=4))
            self.assertFalse(dialog.create_button.isEnabled())
            self.assertEqual(dialog.title_input.text(), '🙂🙂🙂')
        dialog.title_input.setText('🙂🙂')
        self.assertTrue(dialog.create_button.isEnabled())
        dialog.description_input.setPlainText('🙂🙂🙂🙂')
        for language in SUPPORTED_LANGUAGES:
            dialog.set_language(language)
            self.assertEqual(dialog.details_hint.text(), tr('Описание: не более {limit} символов', language, limit=7))
            self.assertFalse(dialog.create_button.isEnabled())
        self.assertEqual(dialog.description_input.toPlainText(), '🙂🙂🙂🙂')
        dialog._submit()
        self.assertEqual(self.jobs, [])

    def test_member_limit_uses_same_minimum_including_zero(self):
        self.service.limits.update({'max-added-participants': 3, 'max-participants': 1})
        dialog = self.dialog('group')
        dialog.member_list.item(0).setCheckState(Qt.CheckState.Checked)
        dialog._next()
        dialog.title_input.setText('Raw title')
        for language in SUPPORTED_LANGUAGES:
            dialog.set_language(language)
            self.assertEqual(dialog.details_hint.text(), tr('Можно выбрать не более {limit} участников', language, limit=0))
            self.assertFalse(dialog.create_button.isEnabled())
            self.assertEqual(dialog._member_ids, {41})
        dialog._submit()
        self.assertEqual(self.jobs, [])

    def test_service_error_equal_to_owned_template_remains_literal_plain_text(self):
        dialog = self.dialog()
        for raw in ('Название должно содержать от 1 до 200 символов', 'MAX вернул неверные ограничения создания',
                    '<b>Raw server value</b> {limit}'):
            with patch.object(self.service, 'conversation_creation_limits', side_effect=ValueError(raw)):
                dialog.title_input.setText('Raw')
                dialog._input_changed()
                for language in SUPPORTED_LANGUAGES:
                    dialog.retranslate(language)
                    self.assertEqual(dialog.details_hint.text(), raw)
                    self.assertEqual(dialog.details_hint.textFormat(), Qt.TextFormat.PlainText)
                    self.assertFalse(dialog.create_button.isEnabled())
        self.assertEqual(self.jobs, [])

    def test_adapter_rejects_unknown_messages_subclasses_and_incorrect_limit(self):
        limits = self.service.limits
        for error in (ValueError('Raw validation failure'), ValueError('Описание: не более 401 символов'),
                      ValueError('Описание: не более 400 символов extra'),
                      type('OtherError', (ValueError,), {})('Неверные ограничения MAX')):
            self.assertIs(module._local_validation_error(error, limits), error)
        dialog = self.dialog()
        with patch.object(module, 'conversation_create_values', side_effect=ValueError('Описание: не более 401 символов')):
            dialog.title_input.setText('Raw')
        dialog.retranslate('fr')
        self.assertEqual(dialog.details_hint.text(), 'Описание: не более 401 символов')

    def test_known_local_fixed_validation_is_typed_and_payload_rules_are_unchanged(self):
        dialog = self.dialog()
        self.service.limits['max-cname-length'] = True
        dialog.title_input.setText('Raw')
        with self.assertRaises(module._LocalValidationError):
            dialog._values()
        self.assertEqual(dialog.details_hint.text(), 'MAX returned invalid creation limits')
        self.service.limits['max-cname-length'] = 200
        dialog.title_input.setText('  Raw  ')
        dialog.description_input.setPlainText('  Body  ')
        self.assertEqual(dialog._values(), conversation_create_values(
            'channel', '  Raw  ', [], '  Body  ', limits=self.service.limits))

    def test_unknown_result_uses_current_locale_and_never_recreates_after_reopen(self):
        self.service.create_result = MaxWriteOutcomeUnknown('Raw unconfirmed data')
        dialog = self.dialog()
        dialog.title_input.setText('Raw title')
        dialog._submit()
        request, state = dialog._request, self.controls(dialog)
        for language in SUPPORTED_LANGUAGES:
            dialog.retranslate(language)
            self.assertIs(dialog._request, request)
            self.assertEqual(self.controls(dialog), state)
            self.assertEqual(dialog.result_label.text(), tr('Создание…', language))
            dialog._submit()
        self.assertEqual(len(self.jobs), 1)
        self.finish()
        self.assertEqual(dialog.result_label.text(), tr(
            'Результат создания пока не подтверждён. Повторное создание заблокировано.', 'be'))
        self.assertTrue(dialog._uncertain)
        self.assertTrue(dialog.create_button.isHidden())
        self.assertEqual(len(self.service.calls), 1)
        dialog.close()
        replacement = self.dialog(language='fr')
        for language in SUPPORTED_LANGUAGES:
            replacement.retranslate(language)
            replacement._submit()
            replacement._verify()
            self.assertTrue(replacement.create_button.isHidden())
            self.assertFalse(replacement.title_input.isEnabled())
        self.assertEqual(len(self.jobs), 1)
        self.saved.assert_not_called()

    def test_pending_verification_is_read_only_and_partial_description_is_not_success(self):
        self.service.pending = {'has_chat_id': True, 'chat_id': -812}
        raw = Chat(-812, 'Название канала <raw>', is_channel=True)
        self.service.verify_result = ConversationCreateResult(raw, False, 'Raw service detail')
        dialog = self.dialog()
        dialog._verify()
        for language in SUPPORTED_LANGUAGES:
            dialog.set_language(language)
            self.assertEqual(dialog.result_label.text(), tr('Проверка результата…', language))
            dialog._submit()
            dialog._verify()
        self.assertEqual(len(self.jobs), 1)
        self.finish()
        for language in SUPPORTED_LANGUAGES:
            dialog.retranslate(language)
            self.assertIn(tr('Канал создан, но сохранение описания не подтверждено. Можно открыть канал или проверить результат.', language),
                          dialog.result_label.text())
            self.assertNotIn('Raw service detail', dialog.result_label.text())
            self.assertTrue(dialog.create_button.isHidden())
            self.assertTrue(dialog.open_button.isEnabled())
        self.assertEqual(self.service.calls, [('verify', self.service.context)])
        self.saved.assert_not_called()
        dialog._open_confirmed()
        self.saved.assert_called_once_with(raw)
        self.assertEqual(raw.title, 'Название канала <raw>')

    def test_contact_load_error_and_session_expiry_keep_their_status_after_retranslation(self):
        with patch.object(self.service, 'list_contacts', side_effect=ValueError('Raw cache error')):
            dialog = self.dialog('group')
        for language in SUPPORTED_LANGUAGES:
            dialog.set_language(language)
            self.assertEqual(dialog.member_hint.text(), tr(
                'Не удалось загрузить контакты. Откройте создание группы заново.', language))
            self.assertFalse(dialog.next_button.isEnabled())
        self.service.context = object()
        dialog._submit()
        for language in SUPPORTED_LANGUAGES:
            dialog.set_language(language)
            self.assertEqual(dialog.result_label.text(), tr('Сессия изменилась. Откройте создание заново.', language))
            self.assertFalse(dialog.create_button.isEnabled())
            self.assertFalse(dialog.title_input.isEnabled())
        self.assertEqual(self.jobs, [])

    def test_worker_does_not_translate_or_expose_raw_server_error(self):
        dialog = self.dialog()
        dialog.title_input.setText('Raw')
        with patch.object(self.service, 'create_conversation', side_effect=MaxError('Название канала <raw>')):
            dialog._submit()
            self.finish()
        self.assertEqual(dialog.result_label.text(), 'MAX did not confirm the operation. Check the details and connection.')
        self.assertNotIn('<raw>', dialog.result_label.text())
        self.saved.assert_not_called()

    def test_retranslation_after_widget_destruction_is_a_noop(self):
        dialog = self.dialog()
        dialog.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.assertFalse(isValid(dialog))
        dialog.retranslate('fr')
        self.assertEqual(self.jobs, [])


if __name__ == '__main__':
    unittest.main()
