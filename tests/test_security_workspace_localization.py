"""Security page language changes with deferred synthetic replies, never account writes."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import re
import unittest
from unittest.mock import patch

from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtWidgets import QApplication
from maxclient.core.config import Settings
from maxclient.core.i18n import SUPPORTED_LANGUAGES
from maxclient.ui.settings_workspace import SettingsWorkspace
from test_settings_workspace import SyntheticService


LATIN = ('en', 'es', 'pt', 'fr', 'uz')
CYRILLIC = re.compile('[\u0400-\u04ff]')


class SecurityWorkspaceLocalizationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.service = SyntheticService()
        self.settings = Settings()
        self.patches = [patch('maxclient.ui.settings_workspace.run_async'),
                        patch('maxclient.ui.privacy_settings_panel.run_async'),
                        patch.object(self.settings, 'save'),
                        patch.object(self.settings, 'save_ui_language')]
        self.jobs, self.privacy_jobs, self.save, self.save_language = [p.start() for p in self.patches]
        self.workspace = SettingsWorkspace(self.service, self.settings)
        self.workspace.resize(1200, 900)
        self.workspace.show()
        self.app.processEvents()

    def tearDown(self):
        self.workspace.close()
        self.workspace.deleteLater()
        QCoreApplication.sendPostedEvents(self.workspace, QEvent.Type.DeferredDelete)
        self.app.processEvents()
        self.assertEqual(self.service.status_reads, 0)
        self.assertEqual(self.service.privacy_reads, 0)
        self.assertEqual(self.service.profile_writes, [])
        self.assertEqual(self.service.termination_writes, 0)
        self.save.assert_not_called()
        self.save_language.assert_not_called()
        for p in reversed(self.patches):
            p.stop()

    def language(self, language):
        self.settings.ui_language = language
        self.workspace._language_selected(language)

    def texts(self):
        w = self.workspace
        rows = (w.twofa_button, w.blacklist_row, w.password_change_button, w.email_button)
        return [text for row in rows for text in (row.title_label.text(), row.subtitle_label.text(),
                    row.accessibleName(), row.accessibleDescription())] + [
                    w.password_details.text(), w.security_retry.text(), w.password_back.text()]

    def assert_latin(self):
        for text in self.texts():
            self.assertIsNone(CYRILLIC.search(text), text)

    def reply(self, value):
        self.jobs.call_args.kwargs['on_done'](value)

    def test_initial_security_and_password_rows_follow_language_without_reads(self):
        for language in SUPPORTED_LANGUAGES:
            self.language(language)
            if language in LATIN:
                self.assert_latin()
            self.assertFalse(self.workspace.twofa_button.isEnabled())
            self.assertFalse(self.workspace.email_button.isEnabled())
        self.language('en')
        self.assertEqual(self.workspace.password_back.text(), '‹ Security')
        self.assertEqual(self.workspace.twofa_button.accessibleName(), 'Sign-in password')
        self.jobs.assert_not_called()
        self.privacy_jobs.assert_not_called()

    def test_pending_read_and_late_reply_use_latest_language_without_restarting(self):
        self.language('en')
        self.workspace._load_security()
        request = self.workspace._security_request
        context = self.workspace._security_context
        for language in ('fr', 'es', 'pt', 'uz'):
            self.language(language)
            self.assert_latin()
            self.assertEqual(self.workspace._security_request, request)
            self.assertIs(self.workspace._security_context, context)
            self.assertTrue(self.workspace._security_loading)
            self.assertFalse(self.workspace.password_change_button.isEnabled())
        self.reply({'enabled': True})
        self.assert_latin()
        self.assertTrue(self.workspace.twofa_button.isEnabled())
        self.assertTrue(self.workspace.email_button.isEnabled())
        self.assertEqual(self.jobs.call_count, 1)

    def test_disabled_password_remains_disabled_when_translated(self):
        self.workspace._load_security()
        self.reply({'enabled': False})
        for language in LATIN:
            self.language(language)
            self.assert_latin()
            self.assertIs(self.workspace._twofa_enabled, False)
            self.assertTrue(self.workspace.password_change_button.isEnabled())
            self.assertFalse(self.workspace.email_button.isEnabled())
            self.assertEqual(self.workspace.password_change_button.subtitle_label.text(), '')
        self.language('en')
        self.assertIn('Enable', self.workspace.password_change_button.title_label.text())
        self.assertEqual(self.jobs.call_count, 1)

    def test_email_is_raw_plain_text_and_is_not_retranslated_as_ui(self):
        self.workspace._load_security()
        email = '<em>Настройки{email}</em>@example.invalid'
        self.reply({'enabled': True, 'email': email})
        for language in SUPPORTED_LANGUAGES:
            self.language(language)
            text = self.workspace.password_details.text()
            self.assertEqual(text.count(email), 1)
            self.assertEqual(self.workspace.password_details.textFormat(), Qt.TextFormat.PlainText)
            self.assertEqual(self.workspace._security_email, email)
        self.assertEqual(self.jobs.call_count, 1)

    def test_error_uses_current_language_and_never_displays_raw_exception(self):
        self.workspace._load_security()
        self.jobs.call_args.kwargs['on_error']('<b>private exception</b>')
        for language in LATIN:
            self.language(language)
            self.assert_latin()
            self.assertNotIn('private exception', self.workspace.password_details.text())
            self.assertFalse(self.workspace.twofa_button.isEnabled())
            self.assertFalse(self.workspace.security_retry.isHidden())
        self.assertEqual(self.jobs.call_count, 1)

    def test_stale_reply_cannot_enable_actions_after_language_switch(self):
        self.workspace._load_security()
        self.service.security_session = object()
        self.language('en')
        self.reply({'enabled': True, 'email': 'synthetic@example.invalid'})
        self.assert_latin()
        self.assertIsNone(self.workspace._twofa_enabled)
        self.assertFalse(self.workspace.twofa_button.isEnabled())
        self.assertFalse(self.workspace.password_change_button.isEnabled())
        self.assertFalse(self.workspace.email_button.isEnabled())
        self.assertNotIn('synthetic@example.invalid', self.workspace.password_details.text())
        self.assertEqual(self.jobs.call_count, 1)

    def test_refresh_hides_previous_email_and_language_switch_does_not_restore_it(self):
        self.workspace._load_security()
        self.reply({'enabled': True, 'email': 'synthetic@example.invalid'})
        self.workspace._load_security()
        self.language('fr')
        self.assert_latin()
        self.assertNotIn('synthetic@example.invalid', self.workspace.password_details.text())
        self.assertEqual(self.workspace._security_email, '')
        self.assertFalse(self.workspace.twofa_button.isEnabled())
        self.assertEqual(self.jobs.call_count, 2)


if __name__ == '__main__':
    unittest.main()
