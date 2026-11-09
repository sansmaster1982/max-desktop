"""File-save state machine with fake RPC and staged files; no network or disk."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication
from shiboken6 import isValid

from maxclient.core.conversation_file_service import ConversationFileDownload
from maxclient.ui.conversation_file_controller import ConversationFileController


class FileControllerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.context = object()
        self.descriptor = SimpleNamespace(context=self.context, key='file:1', file_id=17,
            file_name='<b>sample</b>.txt', size=100, chat_id=303, message_id=7, time_ms=123)
        self.service = SimpleNamespace(is_current_conversation_file=Mock(return_value=True),
            resolve_conversation_file_download=Mock(side_effect=AssertionError('No real worker')))
        self.job_patch = patch('maxclient.ui.conversation_file_controller.run_async')
        self.jobs = self.job_patch.start()
        self.confirm_patch = patch('maxclient.ui.conversation_file_controller.confirm_download', return_value=False)
        self.confirm = self.confirm_patch.start()
        self.target = object()
        self.controller = ConversationFileController(self.service, self.descriptor, 'synthetic.txt', self.target)

    def tearDown(self):
        if isValid(self.controller):
            self.controller.cancel()
            if isValid(self.controller.dialog): self.controller.dialog.close()
            self.controller.deleteLater()
        self.app.processEvents()
        self.confirm_patch.stop()
        self.job_patch.stop()

    def resolve(self, unsafe=False, descriptor=None):
        self.controller.start()
        self.jobs.call_args.kwargs['on_done'](ConversationFileDownload(
            descriptor or self.descriptor, 'https://files.example.test/object', unsafe))

    def test_start_queues_exact_bound_descriptor_once(self):
        self.controller.start()
        self.controller.start()
        self.assertEqual(self.jobs.call_count, 1)
        self.assertEqual(self.jobs.call_args.args, (self.service.resolve_conversation_file_download,
                                                  self.context, self.descriptor))
        self.service.resolve_conversation_file_download.assert_not_called()

    def test_safe_reply_queues_stream_only_after_ack(self):
        self.resolve()
        self.assertEqual(self.jobs.call_count, 2)
        self.confirm.assert_not_called()
        call = self.jobs.call_args
        self.assertEqual(call.args[1:], ('https://files.example.test/object', 'synthetic.txt'))
        self.assertIs(call.kwargs['expected_target'], self.target)
        self.assertEqual(self.controller.dialog._state, 'downloading')

    def test_unsafe_cancel_never_starts_stream(self):
        self.resolve(True)
        self.confirm.assert_called_once()
        self.assertEqual(self.jobs.call_count, 1)
        self.assertFalse(self.controller.active)
        self.assertEqual(self.controller.dialog._state, 'cancelled')

    def test_unsafe_accept_rechecks_context_before_stream(self):
        def changed(*_, **__):
            self.service.is_current_conversation_file.return_value = False
            return True
        self.confirm.side_effect = changed
        self.resolve(True)
        self.assertEqual(self.jobs.call_count, 1)
        self.assertFalse(self.controller.active)

    def test_unsafe_accept_uses_same_reply_without_second_rpc(self):
        self.confirm.return_value = True
        self.resolve(True)
        self.assertEqual(self.jobs.call_count, 2)
        self.service.resolve_conversation_file_download.assert_not_called()

    def test_wrong_descriptor_or_missing_unsafe_is_not_downloadable(self):
        self.resolve(descriptor=object())
        self.assertEqual(self.jobs.call_count, 1)
        self.assertEqual(self.controller.dialog._state, 'error')

    def test_cancel_before_reply_discards_late_authorization(self):
        self.controller.start()
        done = self.jobs.call_args.kwargs['on_done']
        self.controller.cancel()
        self.assertTrue(self.controller.active)
        self.assertEqual(self.controller.dialog._state, 'cancelling')
        done(ConversationFileDownload(self.descriptor, 'https://files.example.test/object', False))
        self.assertEqual(self.jobs.call_count, 1)
        self.assertFalse(self.controller.active)
        self.assertEqual(self.controller.dialog._state, 'cancelled')

    def test_cancelled_staged_file_is_discarded_without_commit(self):
        self.resolve()
        staged = Mock()
        done = self.jobs.call_args.kwargs['on_done']
        self.controller.cancel()
        self.assertTrue(self.controller.active)
        staged.discard.side_effect = lambda: self.assertTrue(self.controller.active)
        done(staged)
        staged.discard.assert_called_once()
        staged.commit.assert_not_called()
        self.assertFalse(self.controller.active)

    def test_closed_pending_dialog_waits_for_worker_before_disposal(self):
        self.resolve()
        done = self.jobs.call_args.kwargs['on_done']
        self.controller.dialog.close()
        QCoreApplication.sendPostedEvents(self.controller.dialog, QEvent.Type.DeferredDelete)
        self.assertTrue(isValid(self.controller))
        self.assertTrue(self.controller.active)
        staged = Mock()
        done(staged)
        staged.discard.assert_called_once()
        staged.commit.assert_not_called()
        QCoreApplication.sendPostedEvents(self.controller, QEvent.Type.DeferredDelete)
        self.assertFalse(isValid(self.controller))

    def test_closed_completed_dialog_releases_controller(self):
        self.resolve()
        self.jobs.call_args.kwargs['on_done'](Mock())
        self.controller.dialog.close()
        QCoreApplication.sendPostedEvents(self.controller.dialog, QEvent.Type.DeferredDelete)
        QCoreApplication.sendPostedEvents(self.controller, QEvent.Type.DeferredDelete)
        self.assertFalse(isValid(self.controller))

    def test_worker_error_acknowledges_pending_cancel(self):
        self.resolve()
        failed = self.jobs.call_args.kwargs['on_error']
        self.controller.cancel()
        self.assertTrue(self.controller.active)
        failed('private signed URL')
        self.assertFalse(self.controller.active)
        self.assertEqual(self.controller.dialog._state, 'cancelled')

    def test_commit_failure_does_not_report_success_or_expose_error(self):
        self.resolve()
        staged = Mock()
        staged.commit.side_effect = ValueError('private signed URL secret')
        self.jobs.call_args.kwargs['on_done'](staged)
        staged.discard.assert_called_once()
        self.assertEqual(self.controller.dialog._state, 'error')
        self.assertNotIn('secret', self.controller.dialog.status_label.text())

    def test_done_requires_commit_and_ignores_delayed_progress(self):
        self.resolve()
        staged = Mock()
        self.jobs.call_args.kwargs['on_done'](staged)
        staged.commit.assert_called_once()
        self.controller._report_progress(50, 100)
        self.app.processEvents()
        self.assertEqual(self.controller.dialog._state, 'done')
        self.assertFalse(self.controller.active)

    def test_invalid_context_start_does_not_leave_an_active_job(self):
        self.service.is_current_conversation_file.return_value = False
        self.controller.start()
        self.jobs.assert_not_called()
        self.assertFalse(self.controller.active)

    def test_destroyed_controller_cancels_and_discards_late_staging(self):
        self.resolve()
        call = self.jobs.call_args
        self.controller.deleteLater()
        QCoreApplication.sendPostedEvents(self.controller, QEvent.Type.DeferredDelete)
        self.assertTrue(call.kwargs['cancelled'].is_set())
        staged = Mock()
        call.kwargs['on_done'](staged)
        staged.discard.assert_called_once()
        staged.commit.assert_not_called()
