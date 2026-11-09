"""Synthetic settings/password dialogs: labels, stages and session-safe callbacks."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import re
import string
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtWidgets import QApplication, QCheckBox, QLabel, QLineEdit, QMessageBox, QPushButton, QWidget
from shiboken6 import delete, isValid

from maxclient.core.config import Settings
from maxclient.core.i18n import SUPPORTED_LANGUAGES, translation_available
from maxclient.core._settings_dialog_translations import SETTINGS_DIALOG_TRANSLATIONS
from maxclient.protocol.client import MaxError, MaxWriteOutcomeUnknown
from maxclient.ui.settings_dialog import (
    SettingsDialog, Change2FADialog, Enable2FADialog, RecoveryEmailDialog,
)


class FakeService:
    def __init__(self, language='en'):
        self.settings = Settings(ui_language=language)
        self.context = (101, object(), object())
        self.calls = []
        self.failure = None

    def my_profile(self):
        return SimpleNamespace(id=101, name='', phone='')

    def _history_read_context(self):
        return self.context

    def _require_history_context(self, context):
        if context is not self.context:
            raise ValueError('Synthetic session changed')
        return context[1]

    def _call(self, name, *args, **kwargs):
        self._require_history_context(kwargs['context'])
        self.calls.append((name, args, kwargs))
        if self.failure:
            raise self.failure

    def get_2fa_status(self, **kwargs):
        self._call('status', **kwargs)
        return {'enabled': True, 'email': 'mail@example.invalid', 'hint': 'Synthetic hint'}

    def change_2fa(self, *args, **kwargs):
        self._call('change', *args, **kwargs)

    def enable_2fa(self, *args, **kwargs):
        self._call('enable', *args, **kwargs)

    def start_set_recovery_email(self, *args, **kwargs):
        self._call('email_start', *args, **kwargs)
        return 'synthetic-track'

    def confirm_recovery_email(self, *args, **kwargs):
        self._call('email_confirm', *args, **kwargs)


class SettingsDialogLocalizationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.service = FakeService()
        self.dialogs = []
        self.async_patch = patch('maxclient.ui.settings_dialog.run_async')
        self.jobs = self.async_patch.start()
        self.info_patch = patch('maxclient.ui.settings_dialog.QMessageBox.information')
        self.info = self.info_patch.start()
        self.modal_patch = patch('maxclient.ui.settings_dialog.QMessageBox.exec', return_value=QMessageBox.StandardButton.Ok)
        self.modal = self.modal_patch.start()
        self.save_patch = patch.object(self.service.settings, 'save')
        self.save = self.save_patch.start()

    def tearDown(self):
        for dialog in self.dialogs:
            if isValid(dialog):
                dialog.close()
                dialog.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()
        self.save.assert_not_called()
        self.save_patch.stop()
        self.info_patch.stop()
        self.modal_patch.stop()
        self.async_patch.stop()

    def make(self, kind, service=None, parent=None):
        service = service or self.service
        dialog = kind(service, service.settings, parent) if kind is SettingsDialog else kind(service, parent)
        self.dialogs.append(dialog)
        return dialog

    def submit(self, kind):
        dialog = self.make(kind)
        if kind is RecoveryEmailDialog:
            dialog.current.setText(' synthetic-current ')
            dialog.email.setText('synthetic@example.invalid')
            dialog._send()
        else:
            if kind is Change2FADialog:
                dialog.current.setText(' synthetic-current ')
            dialog.new.setText(' synthetic-new ')
            dialog.confirm.setText(' synthetic-new ')
            dialog.hint.setText(' Synthetic hint ')
            dialog._submit()
        return dialog, self.jobs.call_args

    def execute(self, job):
        kwargs = dict(job.kwargs)
        done = kwargs.pop('on_done')
        error = kwargs.pop('on_error')
        try:
            result = job.args[0](*job.args[1:], **kwargs)
        except Exception as exc:
            error(str(exc))  # Match the production runner's string-error contract.
        else:
            done(result)

    def texts(self, dialog):
        result = [dialog.windowTitle()]
        for widget in dialog.findChildren(QWidget):
            result.extend([widget.toolTip(), widget.accessibleName()])
            if isinstance(widget, (QLabel, QPushButton, QCheckBox)):
                result.append(widget.text())
            elif isinstance(widget, QLineEdit):
                result.append(widget.placeholderText())
        if hasattr(dialog, 'theme_combo'):
            result.extend(dialog.theme_combo.itemText(i) for i in range(dialog.theme_combo.count()))
        return result

    def test_all_dialogs_follow_seven_languages_without_reads_or_writes(self):
        dialogs = [self.make(kind) for kind in (SettingsDialog, Change2FADialog, Enable2FADialog, RecoveryEmailDialog)]
        initial_jobs = self.jobs.call_count
        for language in SUPPORTED_LANGUAGES:
            self.service.settings.ui_language = language
            for dialog in dialogs:
                dialog.retranslate()
                if language in ('en', 'es', 'pt', 'fr', 'uz'):
                    for value in self.texts(dialog):
                        self.assertIsNone(re.search('[\u0400-\u04ff]', value), (language, value))
        self.assertEqual(self.jobs.call_count, initial_jobs)
        self.assertEqual(self.service.calls, [])
        dialogs[1].retranslate('en')
        self.assertEqual(dialogs[1].windowTitle(), 'Change 2FA password')
        self.assertEqual(dialogs[1].current.placeholderText(), 'Current password')
        self.assertEqual(dialogs[2].btn.text(), 'Уключыць 2FA')

    def test_catalog_has_all_columns_and_preserves_placeholders(self):
        formatter = string.Formatter()
        for source, row in SETTINGS_DIALOG_TRANSLATIONS.items():
            expected = {field for _, field, _, _ in formatter.parse(source) if field}
            self.assertEqual(len(row), 6)
            for language, value in zip(SUPPORTED_LANGUAGES[1:], row):
                self.assertTrue(value)
                self.assertTrue(translation_available(source, language), (source, language))
                self.assertEqual({field for _, field, _, _ in formatter.parse(value) if field}, expected)

    def test_parent_language_and_mock_language_are_safe(self):
        parent = QWidget()
        parent.settings = Settings(ui_language='fr')
        self.dialogs.append(parent)
        dialog = self.make(Change2FADialog, parent=parent)
        self.assertEqual(dialog.windowTitle(), 'Modifier le mot de passe 2FA')
        bare = Change2FADialog(Mock())
        self.dialogs.append(bare)
        self.assertEqual(bare.windowTitle(), 'Сменить пароль 2FA')

    def test_theme_data_and_user_settings_do_not_change_on_translation(self):
        dialog = self.make(SettingsDialog)
        before = (dialog.theme_combo.currentData(), dialog.notif.isChecked(), dialog.enter.isChecked())
        for language in SUPPORTED_LANGUAGES:
            dialog.retranslate(language)
            self.assertEqual([dialog.theme_combo.itemData(i) for i in range(3)], ['system', 'dark', 'light'])
            self.assertEqual((dialog.theme_combo.currentData(), dialog.notif.isChecked(), dialog.enter.isChecked()), before)

    def test_password_input_echo_and_pending_button_survive_translation(self):
        dialog, job = self.submit(Change2FADialog)
        toggle = dialog.findChild(QCheckBox)
        toggle.setChecked(True)
        for language in SUPPORTED_LANGUAGES:
            dialog.retranslate(language)
            self.assertEqual(dialog.current.text(), ' synthetic-current ')
            self.assertEqual(dialog.new.text(), ' synthetic-new ')
            self.assertEqual(dialog.confirm.text(), ' synthetic-new ')
            self.assertEqual(dialog.new.echoMode(), QLineEdit.EchoMode.Normal)
            self.assertFalse(dialog.btn.isEnabled())
            self.assertEqual(dialog.btn.text(), '…')
        self.assertEqual(self.jobs.call_count, 1)
        self.assertIs(job.kwargs['context'], self.service.context)
        self.assertEqual(job.kwargs['current_password'], ' synthetic-current ')
        self.assertEqual(job.args[1:3], (' synthetic-new ', 'Synthetic hint'))

    def test_local_validation_retranslates_without_submission(self):
        dialog = self.make(Change2FADialog)
        dialog._submit()
        self.assertEqual(dialog.error.text(), 'Enter your current password.')
        dialog.retranslate('fr')
        self.assertEqual(dialog.error.text(), 'Saisissez votre mot de passe actuel.')
        self.jobs.assert_not_called()

    def test_raw_service_error_is_literal_and_not_a_translation_key(self):
        for kind in (Change2FADialog, Enable2FADialog, RecoveryEmailDialog):
            with self.subTest(kind=kind):
                dialog, job = self.submit(kind)
                raw = '<b>Synthetic failure</b> & {email}'
                job.kwargs['on_error'](raw)
                dialog.retranslate('es')
                self.assertEqual(dialog.error.text(), raw)
                self.assertEqual(dialog.error.textFormat(), Qt.TextFormat.PlainText)
                button = dialog.send_btn if kind is RecoveryEmailDialog else dialog.btn
                self.assertTrue(button.isEnabled())

    def test_dynamic_2fa_values_stay_literal_while_labels_change(self):
        dialog = self.make(SettingsDialog)
        job = self.jobs.call_args
        job.kwargs['on_done']({'enabled': True, 'email': '<b>mail@example.invalid</b>', 'hint': '{hint} synthetic'})
        dialog.retranslate('en')
        self.assertEqual(dialog.twofa_status.text(), '2FA is on · Recovery email: <b>mail@example.invalid</b> · Hint: {hint} synthetic')
        dialog.retranslate('fr')
        self.assertIn('<b>mail@example.invalid</b>', dialog.twofa_status.text())
        self.assertIn('{hint} synthetic', dialog.twofa_status.text())
        self.assertEqual(dialog.twofa_status.textFormat(), Qt.TextFormat.PlainText)
        self.assertTrue(dialog.email_btn.isEnabled())

    def test_only_exact_boolean_status_enables_a_password_route(self):
        dialog = self.make(SettingsDialog)
        for invalid in ({}, None, [], {'enabled': 1}, {'enabled': 0}, {'enabled': 'false'}, {'enabled': None}):
            with self.subTest(invalid=invalid):
                dialog._on_2fa_status(invalid)
                self.assertIsNone(dialog._twofa_enabled)
                self.assertFalse(dialog.email_btn.isEnabled())
                self.assertEqual(dialog.twofa_btn.text(), 'Update 2FA status')
                with patch('maxclient.ui.settings_dialog.Enable2FADialog') as enable, patch('maxclient.ui.settings_dialog.Change2FADialog') as change:
                    dialog._twofa_primary()
                    enable.assert_not_called()
                    change.assert_not_called()
        dialog._on_2fa_status({'enabled': False})
        self.assertIs(dialog._twofa_enabled, False)
        self.assertEqual(dialog.twofa_status.text(), '2FA is off')

    def test_recovery_sent_stage_retranslates_without_resetting_track_or_code(self):
        dialog, job = self.submit(RecoveryEmailDialog)
        self.execute(job)
        dialog.code.setText('123456')
        for language in SUPPORTED_LANGUAGES:
            dialog.retranslate(language)
            self.assertEqual(dialog._track_id, 'synthetic-track')
            self.assertEqual(dialog.current.text(), '')
            self.assertFalse(dialog.current.isEnabled())
            self.assertFalse(dialog.email.isEnabled())
            self.assertFalse(dialog.send_btn.isEnabled())
            self.assertFalse(dialog.confirm_btn.isHidden())
            self.assertEqual(dialog.code.text(), '123456')
        self.assertEqual(len(self.service.calls), 1)
        self.assertEqual(self.jobs.call_count, 1)

    def test_recovery_confirm_uses_original_context_and_reports_literal_error(self):
        dialog, job = self.submit(RecoveryEmailDialog)
        self.execute(job)
        dialog.code.setText('123456')
        dialog._confirm()
        confirm = self.jobs.call_args
        self.assertIs(confirm.kwargs['context'], self.service.context)
        self.assertEqual(confirm.args[1:3], ('synthetic-track', '123456'))
        confirm.kwargs['on_error']('<b>Synthetic refusal</b>')
        self.assertTrue(dialog.confirm_btn.isEnabled())
        self.assertEqual(dialog.error.textFormat(), Qt.TextFormat.PlainText)

    def test_recovery_information_renders_raw_email_as_plain_text(self):
        dialog = self.make(RecoveryEmailDialog)
        dialog.current.setText('synthetic-current')
        dialog.email.setText('<b>synthetic@example.invalid</b>')
        dialog._send()
        job = self.jobs.call_args
        self.execute(job)
        boxes = dialog.findChildren(QMessageBox)
        self.assertEqual(len(boxes), 1)
        self.assertEqual(boxes[0].textFormat(), Qt.TextFormat.PlainText)
        self.assertEqual(boxes[0].windowTitle(), 'Code sent')
        self.assertIn('<b>synthetic@example.invalid</b>', boxes[0].text())

    def test_closed_dialog_ignores_late_success_and_error(self):
        for kind in (Change2FADialog, Enable2FADialog, RecoveryEmailDialog):
            with self.subTest(kind=kind):
                dialog, job = self.submit(kind)
                dialog.close()
                job.kwargs['on_done']('synthetic-track')
                job.kwargs['on_error']('Synthetic late error')
        self.info.assert_not_called()

    def test_deleted_dialog_ignores_late_success_and_error(self):
        dialog, job = self.submit(Change2FADialog)
        delete(dialog)
        job.kwargs['on_done'](None)
        job.kwargs['on_error']('Synthetic late error')
        self.info.assert_not_called()

    def test_session_change_before_worker_prevents_service_call(self):
        for kind in (Change2FADialog, Enable2FADialog, RecoveryEmailDialog):
            dialog, job = self.submit(kind)
            self.service.context = (101, object(), object())
            self.execute(job)
            self.assertEqual(self.service.calls, [])
        self.info.assert_not_called()

    def test_session_change_before_callback_does_not_show_success_or_enable_retry(self):
        dialog, job = self.submit(Change2FADialog)
        self.service.context = (202, object(), object())
        job.kwargs['on_done'](None)
        job.kwargs['on_error']('Synthetic stale refusal')
        self.assertFalse(dialog.btn.isEnabled())
        self.info.assert_not_called()

    def test_closed_settings_ignore_late_status_and_no_invalid_bool_coercion(self):
        dialog = self.make(SettingsDialog)
        job = self.jobs.call_args
        self.assertIs(job.kwargs['context'], self.service.context)
        dialog.close()
        job.kwargs['on_done']({'enabled': True})
        self.assertIsNone(dialog._twofa_enabled)
        self.assertFalse(dialog.email_btn.isEnabled())

    def test_duplicate_submit_is_not_queued_while_pending(self):
        for kind in (Change2FADialog, Enable2FADialog, RecoveryEmailDialog):
            dialog, job = self.submit(kind)
            before = self.jobs.call_count
            (dialog._send if kind is RecoveryEmailDialog else dialog._submit)()
            self.assertEqual(self.jobs.call_count, before)

    def test_unknown_write_blocks_retry_through_real_runner_contract(self):
        for kind in (Change2FADialog, Enable2FADialog, RecoveryEmailDialog):
            with self.subTest(kind=kind):
                self.service.failure = MaxWriteOutcomeUnknown('Synthetic detail must not be displayed')
                dialog, job = self.submit(kind)
                self.execute(job)
                self.assertTrue(dialog._uncertain)
                self.assertNotIn('Synthetic detail', dialog.error.text())
                before = self.jobs.call_count
                for language in SUPPORTED_LANGUAGES:
                    dialog.retranslate(language)
                    (dialog._send if kind is RecoveryEmailDialog else dialog._submit)()
                self.assertEqual(self.jobs.call_count, before)
                button = dialog.send_btn if kind is RecoveryEmailDialog else dialog.btn
                self.assertFalse(button.isEnabled())
        self.info.assert_not_called()

    def test_unknown_email_confirmation_cannot_resubmit_track(self):
        dialog, job = self.submit(RecoveryEmailDialog)
        self.execute(job)
        self.service.failure = MaxWriteOutcomeUnknown('Synthetic unknown')
        dialog.code.setText('123456')
        dialog._confirm()
        self.execute(self.jobs.call_args)
        before = self.jobs.call_count
        dialog.retranslate('fr')
        dialog._confirm()
        dialog._send()
        self.assertEqual(self.jobs.call_count, before)
        self.assertFalse(dialog.confirm_btn.isEnabled())
        self.assertFalse(dialog.send_btn.isEnabled())
        self.assertTrue(dialog._uncertain)

    def test_explicit_refusal_allows_edit_and_retry(self):
        self.service.failure = MaxError('Synthetic explicit refusal')
        dialog, job = self.submit(Change2FADialog)
        self.execute(job)
        self.assertFalse(dialog._uncertain)
        self.assertTrue(dialog.btn.isEnabled())
        dialog._submit()
        self.assertEqual(self.jobs.call_count, 2)


if __name__ == '__main__':
    unittest.main()
