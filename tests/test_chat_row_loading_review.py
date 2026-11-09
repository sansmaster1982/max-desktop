"""Independent lifetime checks for a row popup awaiting read-only metadata."""
import unittest
from unittest.mock import patch

from PySide6.QtCore import QCoreApplication, QEvent, QPoint
from PySide6.QtWidgets import QApplication
from shiboken6 import isValid

import test_chat_row_menu_context as fixture


class LoadingRowService(fixture.RowService):
    def __init__(self):
        super().__init__()
        self.metadata_supported = False
        self.metadata_reads = []
        self.fail_metadata = False

    def conversation_snapshot(self, context):
        result = super().conversation_snapshot(context)
        result['action_capabilities']['supported'] = self.metadata_supported
        return result

    def refresh_conversation_menu_metadata(self, context):
        if not self.is_current_conversation(context):
            raise ValueError('Synthetic stale metadata read')
        self.metadata_reads.append(context)
        if self.fail_metadata:
            raise ValueError('Synthetic private payload must not reach UI')
        self.metadata_supported = True
        return None  # The UI must obtain the current owner-bound snapshot.


class ChatRowLoadingIndependentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.case = fixture.ChatRowMenuContextTests()
        self.case.app = self.app
        with patch('test_chat_row_menu_context.RowService', LoadingRowService):
            self.case.setUp()
        self.window, self.service, self.queue = self.case.window, self.case.service, self.case.queue

    def tearDown(self):
        self.assertEqual(self.service.writes, [])
        self.case.tearDown()

    def popup(self):
        menu = self.window._build_chat_row_menu(1)
        self.assertIsNotNone(menu, 'Sparse metadata must still show a loading popup')
        menu.popup(self.window.mapToGlobal(QPoint(100, 100)))
        self.app.processEvents()
        return menu

    def pending(self):
        self.assertEqual(len(self.queue), 1)
        return self.queue.pop()

    @staticmethod
    def labels(menu):
        return [action.text() for action in menu.actions() if not action.isSeparator()]

    def test_sparse_cache_shows_disabled_popup_then_fills_same_context_without_write(self):
        menu = self.popup()
        context = self.window._row_menu_context
        self.assertTrue(menu.isVisible())
        self.assertTrue(self.labels(menu))
        self.assertFalse(any(action.isEnabled() for action in menu.actions()))
        self.assertEqual(self.service.metadata_reads, [])
        self.case._run(self.pending())
        self.assertIs(self.window._row_menu, menu)
        self.assertIs(self.window._row_menu_context, context)
        self.assertEqual(self.service.metadata_reads, [context])
        self.assertTrue(any('уведомления' in label for label in self.labels(menu)))
        self.assertIsNone(self.window._current_chat_id)

    def test_fast_worker_during_about_to_show_does_not_mistake_unshown_menu_for_closed(self):
        menu = self.window._build_chat_row_menu(1)
        self.assertIsNotNone(menu)
        completions, visibility = [], []
        def immediate_worker(function, *args, on_done=None, on_error=None, **kwargs):
            if completions:
                return self.case._queue(function, *args, on_done=on_done, on_error=on_error, **kwargs)
            # Real QThreadPool work can run before aboutToShow returns. Its
            # queued GUI callback executes only after popup becomes visible.
            visibility.append(menu.isVisible())
            result = function(*args, **kwargs)
            completions.append((on_done, result))
        with patch('maxclient.ui.main_window.run_async', side_effect=immediate_worker):
            menu.popup(self.window.mapToGlobal(QPoint(100, 100)))
            self.app.processEvents()
        self.assertEqual(visibility, [False])
        self.assertEqual(self.service.metadata_reads, [self.window._row_menu_context])
        done, result = completions[0]
        done(result)
        self.assertTrue(menu.isVisible())
        self.assertTrue(any('уведомления' in label for label in self.labels(menu)))

    def test_close_before_worker_does_not_start_metadata_read(self):
        menu = self.popup()
        job = self.pending()
        menu.close()
        self.case._run(job)
        self.assertEqual(self.service.metadata_reads, [])
        self.assertFalse(menu.isVisible())
        self.assertTrue(menu._revoked)

    def test_close_after_worker_does_not_reopen_or_repopulate_popup(self):
        menu = self.popup()
        before = self.labels(menu)
        job = self.pending()
        result = self.case._run(job, callbacks=False)
        menu.close()
        job[3](result)
        self.assertFalse(menu.isVisible())
        self.assertEqual(self.labels(menu), before)
        self.assertEqual(self.queue, [])

    def test_replacement_before_old_worker_does_not_read_for_old_popup(self):
        old = self.popup()
        old_job = self.pending()
        new = self.popup()
        new_job = self.pending()
        self.case._run(old_job)
        self.assertEqual(self.service.metadata_reads, [])
        self.assertIs(self.window._row_menu, new)
        self.case._run(new_job)
        self.assertEqual(self.service.metadata_reads, [self.window._row_menu_context])
        self.assertTrue(not isValid(old) or old._revoked)

    def test_old_callback_does_not_replace_new_context_or_clear_new_job(self):
        self.popup()
        old_job = self.pending()
        old_result = self.case._run(old_job, callbacks=False)
        self.service.metadata_supported = False
        new = self.popup()
        new_context = self.window._row_menu_context
        new_job = self.pending()
        before = self.labels(new)
        old_job[3](old_result)
        self.assertIs(self.window._row_menu_context, new_context)
        self.assertEqual(self.labels(new), before)
        self.case._run(new_job)
        self.assertTrue(any('уведомления' in label for label in self.labels(new)))

    def test_session_switch_before_worker_never_reads_old_account(self):
        self.popup()
        job = self.pending()
        self.service.security_session = object()
        self.case._run(job)
        self.assertEqual(self.service.metadata_reads, [])

    def test_session_switch_after_worker_does_not_enable_old_popup_actions(self):
        menu = self.popup()
        job = self.pending()
        result = self.case._run(job, callbacks=False)
        self.service.security_session = object()
        job[3](result)
        self.assertFalse(any(action.isEnabled() for action in menu.actions()))
        self.assertEqual(self.queue, [])

    def test_section_change_before_worker_invalidates_loading_context(self):
        self.popup()
        job = self.pending()
        self.window._section = 'different-synthetic-section'
        self.case._run(job)
        self.assertEqual(self.service.metadata_reads, [])

    def test_folder_identity_change_before_worker_invalidates_loading_context(self):
        self.window._folders[self.window._section] = {'id': 'captured-folder'}
        self.popup()
        job = self.pending()
        self.window._folders[self.window._section] = {'id': 'replacement-folder'}
        self.case._run(job)
        self.assertEqual(self.service.metadata_reads, [])

    def test_destroyed_window_ignores_late_success_and_error(self):
        self.popup()
        job = self.pending()
        result = self.case._run(job, callbacks=False)
        self.window.close()
        self.window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.assertFalse(isValid(self.window))
        job[3](result)
        job[4](ValueError('Synthetic error'))
        self.assertEqual(self.queue, [])

    def test_refresh_failure_keeps_explanation_without_exposing_error_or_auto_retry(self):
        menu = self.popup()
        self.service.fail_metadata = True
        self.case._run(self.pending())
        self.assertTrue(menu.isVisible())
        self.assertTrue(self.labels(menu))
        self.assertNotIn('private payload', ' '.join(self.labels(menu)))
        self.assertNotIn('private payload', self.window.statusBar().currentMessage())
        self.assertEqual(len(self.service.metadata_reads), 1)
        self.assertEqual(self.queue, [])


if __name__ == '__main__':
    unittest.main()
