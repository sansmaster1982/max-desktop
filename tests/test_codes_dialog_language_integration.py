"""Shell wiring for the read-only codes window; fake workers and session only."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import unittest
from unittest.mock import Mock, patch
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QCoreApplication, QEvent
from shiboken6 import isValid
from maxclient.core.i18n import SUPPORTED_LANGUAGES, tr
import test_search_shell as fixture


class CodesDialogLanguageIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.f = fixture.SearchShellTests()
        self.f.app = self.app
        self.f.setUp()
        self.addCleanup(self.f.tearDown)
        self.w, self.service = self.f.window, self.f.service
        self.service._history_read_context = lambda: self.service.context
        def require(context):
            if context is not self.service.context:
                raise ValueError('Synthetic stale session')
            return self.service.client
        self.service._require_history_context = require
        self.service.verification_code_messages = Mock(side_effect=AssertionError('No real read'))
        self.service.logout = Mock()
        self.code_patcher = patch('maxclient.ui.verification_codes_dialog.run_async')
        self.code_jobs = self.code_patcher.start()
        self.addCleanup(self.code_patcher.stop)

    def open_dialog(self, language='fr'):
        self.w.settings.ui_language = language
        self.w._open_verification_codes()
        dialog = self.w._code_dialog
        self.app.processEvents()
        self.assertEqual(self.code_jobs.call_count, 1)
        return dialog, self.code_jobs.call_args.kwargs

    def test_settings_language_applies_on_open_and_live_pending_relabel(self):
        dialog, callbacks = self.open_dialog()
        self.assertEqual(dialog.windowTitle(), tr('Коды подтверждения — MAX', 'fr'))
        for language in SUPPORTED_LANGUAGES:
            self.w.settings.ui_language = language
            self.w._apply_local_language()
            self.assertEqual(dialog.windowTitle(), tr('Коды подтверждения — MAX', language))
            self.assertEqual(self.w.codes_button.accessibleName(), tr('Коды подтверждения', language))
            self.assertFalse(dialog.refresh_button.isEnabled())
        callbacks['on_done']({'status': 'available', 'messages': [{'time_ms':0,'text':'<literal> 000123'}]})
        self.assertIn('<literal> 000123', dialog.messages.toPlainText())
        self.assertEqual(self.code_jobs.call_count, 1)
        self.service.verification_code_messages.assert_not_called()

    def test_open_twice_keeps_same_pending_dialog_and_one_job(self):
        dialog, _ = self.open_dialog()
        self.w._open_verification_codes()
        self.assertIs(self.w._code_dialog, dialog)
        self.assertEqual(self.code_jobs.call_count, 1)

    def test_logout_closes_dialog_before_scheduling_logout_and_late_response_is_ignored(self):
        dialog, callbacks = self.open_dialog()
        callbacks['on_done']({'status':'available','messages':[{'time_ms':0,'text':'synthetic old'}]})
        def schedule(_fn, **_kwargs):
            self.assertIsNone(self.w._code_dialog)
            self.assertFalse(dialog.isVisible())
            self.assertEqual(dialog._last_text, '')
            self.assertEqual(dialog.messages.toPlainText(), '')
        self.f.jobs.side_effect = schedule
        self.w._handle_logout()
        self.w._open_verification_codes()
        self.assertIsNone(self.w._code_dialog)
        self.assertEqual(self.code_jobs.call_count, 1)
        callbacks['on_done']({'status':'available','messages':[{'time_ms':0,'text':'late'}]})
        callbacks['on_error']('late synthetic failure')
        self.assertEqual(dialog._last_text, '')
        self.assertFalse(dialog.copy_button.isEnabled())
        self.service.logout.assert_not_called()

    def test_parent_close_invalidates_pending_dialog_before_late_callback(self):
        dialog, callbacks = self.open_dialog()
        self.w.close()
        self.assertIsNone(self.w._code_dialog)
        self.w._open_verification_codes()
        self.assertIsNone(self.w._code_dialog)
        self.assertEqual(self.code_jobs.call_count, 1)
        callbacks['on_done']({'status':'available','messages':[{'time_ms':0,'text':'late'}]})
        self.assertEqual(dialog.messages.toPlainText(), '')
        self.assertFalse(dialog.copy_button.isEnabled())

    def test_deleted_dialog_reference_is_safe_for_locale_and_next_open(self):
        dialog, callbacks = self.open_dialog()
        dialog.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.assertFalse(isValid(dialog))
        self.w._apply_local_language()
        callbacks['on_done']({'status':'available','messages':[{'time_ms':0,'text':'late'}]})
        self.w._open_verification_codes()
        self.app.processEvents()
        self.assertIsNot(self.w._code_dialog, dialog)
        self.assertEqual(self.code_jobs.call_count, 2)
        self.service.verification_code_messages.assert_not_called()

    def test_rejected_dialog_is_replaced_without_reusing_old_callback(self):
        dialog, callbacks = self.open_dialog()
        dialog.reject()
        self.w._open_verification_codes()
        fresh = self.w._code_dialog
        self.app.processEvents()
        callbacks['on_done']({'status':'available','messages':[{'time_ms':0,'text':'late'}]})
        self.assertIsNot(fresh, dialog)
        self.assertEqual(fresh.messages.toPlainText(), '')
        self.assertEqual(self.code_jobs.call_count, 2)


if __name__ == '__main__':
    unittest.main()
