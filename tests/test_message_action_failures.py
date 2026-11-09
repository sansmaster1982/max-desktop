"""Sanitized message-action outcomes and Saved self-only scope; synthetic UI only."""
import dataclasses
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from PySide6.QtCore import Qt, QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMessageBox

from maxclient.core.models import Chat, Message
from maxclient.protocol.client import AuthenticatedSession, MaxError, MaxWriteNotStarted, MaxWriteOutcomeUnknown
from maxclient.protocol.errors import MaxLoginFailed, MaxNotConnected, MaxTimeout
from maxclient.ui.chat_view import _bound_message_action, _MessageActionFailure
import test_message_menu_context as menu_fixture


PRIVATE_DETAIL = 'synthetic-private-token-chat-id-message-text'


class MessageActionOutcomeTests(unittest.TestCase):
    def setUp(self):
        self.context = AuthenticatedSession(101, 'synthetic-device', 'WEB', object())
        self.service = SimpleNamespace(search_context=Mock(return_value=self.context), delete_messages=Mock())

    def invoke(self, **kwargs):
        return _bound_message_action(self.service, self.context, self.service.delete_messages, 42, ([100],), kwargs)

    def test_known_errors_become_categories_without_exception_content(self):
        cases = [(MaxWriteNotStarted, 'not_started'), (MaxWriteOutcomeUnknown, 'unknown'),
                 (MaxTimeout, 'unknown'), (MaxNotConnected, 'unknown'), (MaxError, 'rejected'),
                 (ValueError, 'validation'), (RuntimeError, 'unexpected'), (MaxLoginFailed, 'unexpected')]
        for error, category in cases:
            with self.subTest(error=error.__name__):
                self.service.delete_messages.reset_mock()
                self.service.delete_messages.side_effect = error(PRIVATE_DETAIL)
                outcome = self.invoke(for_all=False)
                self.assertEqual(dataclasses.asdict(outcome), {'kind': category})
                self.assertNotIn(PRIVATE_DETAIL, repr(outcome))
                self.service.delete_messages.assert_called_once_with(42, [100], context=self.context, for_all=False)

    def test_missing_changed_and_failed_context_never_invoke_mutation(self):
        self.service.search_context.return_value = object()
        self.assertEqual(self.invoke().kind, 'session')
        self.service.search_context.side_effect = ValueError(PRIVATE_DETAIL)
        self.assertEqual(self.invoke().kind, 'session')
        outcome = _bound_message_action(self.service, None, self.service.delete_messages, 42, ([100],), {})
        self.assertEqual(outcome.kind, 'session')
        self.service.delete_messages.assert_not_called()

    def test_success_value_and_exact_arguments_are_preserved(self):
        result = {'chatId': 42, 'messageIds': [100]}
        self.service.delete_messages.return_value = result
        self.assertIs(self.invoke(for_all=True), result)
        self.service.delete_messages.assert_called_once_with(42, [100], context=self.context, for_all=True)

    def test_saved_all_scope_is_rejected_before_method_and_is_never_downgraded(self):
        result = _bound_message_action(self.service, self.context, self.service.delete_messages,
                                       0, ([100],), {'for_all': True})
        self.assertEqual(result.kind, 'saved_scope')
        self.service.delete_messages.assert_not_called()
        _bound_message_action(self.service, self.context, self.service.delete_messages,
                              0, ([100],), {'for_all': False})
        self.service.delete_messages.assert_called_once_with(0, [100], context=self.context, for_all=False)


class MessageActionFailureUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        menu_fixture.MessageMenuContextTests.setUpClass()

    def setUp(self):
        self.fixture = menu_fixture.MessageMenuContextTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.addCleanup(self.fixture.tearDown)

    def run_error(self, error):
        f = self.fixture
        f.service.delete_messages.side_effect = error
        f.view._run_message_action(f.service.delete_messages, f.message, f.view.chat_metadata_binding(),
                                   [f.message.id], for_all=False)
        with patch('maxclient.ui.chat_view.QMessageBox.warning') as warning, patch.object(f.view, '_after_send') as refresh:
            f.finish(len(f.jobs) - 1)
            refresh.assert_not_called()
            warning.assert_called_once()
            title, text = warning.call_args.args[1:]
        self.assertNotIn(PRIVATE_DETAIL, title + text)
        self.assertIsNone(f.view._message_action_request)
        return title, text

    def test_explicit_rejection_is_distinct_from_unknown_write(self):
        title, text = self.run_error(MaxError(PRIVATE_DETAIL))
        self.assertEqual(title, 'Действие отклонено')
        self.assertIn('MAX отклонил действие', text)
        title, text = self.run_error(MaxWriteOutcomeUnknown(PRIVATE_DETAIL))
        self.assertEqual(title, 'Результат не подтверждён')
        self.assertIn('Результат действия неизвестен', text)
        self.assertEqual(len(self.fixture.jobs), 2)
        self.assertEqual(self.fixture.service.delete_messages.call_count, 2)

    def test_only_typed_not_started_claims_no_send(self):
        title, _text = self.run_error(MaxWriteNotStarted(PRIVATE_DETAIL))
        self.assertEqual(title, 'Действие не отправлено')
        for error in (ValueError(PRIVATE_DETAIL), RuntimeError(PRIVATE_DETAIL), MaxTimeout(PRIVATE_DETAIL)):
            with self.subTest(error=type(error).__name__):
                title, text = self.run_error(error)
                self.assertNotIn('не отправлено', title.lower() + text.lower())
        self.assertEqual(self.fixture.service.delete_messages.call_count, 4)

    def test_raw_async_failure_is_sanitized_and_does_not_retry_or_refresh(self):
        f = self.fixture
        f.view._run_message_action(f.service.delete_messages, f.message, f.view.chat_metadata_binding(), [f.message.id])
        with patch('maxclient.ui.chat_view.QMessageBox.warning') as warning, patch.object(f.view, '_after_send') as refresh:
            f.jobs[0][-1](PRIVATE_DETAIL)
            warning.assert_called_once()
            self.assertEqual(warning.call_args.args[1], 'Действие не подтверждено')
            self.assertNotIn(PRIVATE_DETAIL, warning.call_args.args[2])
            refresh.assert_not_called()
        f.service.delete_messages.assert_not_called()
        self.assertEqual(len(f.jobs), 1)

    def test_worker_start_exception_is_generic_and_never_claims_server_rejection(self):
        f = self.fixture
        f.runner.side_effect = RuntimeError(PRIVATE_DETAIL)
        with patch('maxclient.ui.chat_view.QMessageBox.warning') as warning, patch.object(f.view, '_after_send') as refresh:
            f.view._run_message_action(f.service.delete_messages, f.message, f.view.chat_metadata_binding(), [f.message.id])
            self.assertEqual(warning.call_args.args[1], 'Действие не подтверждено')
            self.assertNotIn(PRIVATE_DETAIL, warning.call_args.args[2])
            refresh.assert_not_called()
        f.service.delete_messages.assert_not_called()

    def prepare_saved(self):
        f = self.fixture
        f.view.open_chat(Chat(0, title='Synthetic saved', chat_type='DIALOG'))
        f.message = Message(0, id=1234567890123, text='Synthetic original', outgoing=True)
        f.app.processEvents()
        f.jobs.clear()
        f.runner.reset_mock()
        return f

    def test_saved_confirmation_has_only_self_and_cancel_and_sends_exact_scope(self):
        f = self.prepare_saved()
        labels = []
        def confirm():
            dialog = QApplication.activeModalWidget()
            self.assertIsInstance(dialog, QMessageBox)
            labels.extend(button.text() for button in dialog.buttons())
            self.assertEqual(dialog.defaultButton(), dialog.button(QMessageBox.StandardButton.Cancel))
            local = next(button for button in dialog.buttons() if button.text() == 'Удалить у меня')
            QTest.mouseClick(local, Qt.MouseButton.LeftButton)
        QTimer.singleShot(0, confirm)
        f.menu_action('Удалить…').trigger()
        self.assertNotIn('Удалить у всех', labels)
        self.assertEqual(len(labels), 2)
        with patch.object(f.view, '_after_send') as refresh:
            f.finish()
            refresh.assert_called_once_with(0)
        f.service.delete_messages.assert_called_once_with(0, [f.message.id], for_all=False, context=f.context)

    def test_saved_confirmation_enter_cancels_without_submission(self):
        f = self.prepare_saved()
        def cancel_by_default():
            dialog = QApplication.activeModalWidget()
            self.assertEqual(dialog.defaultButton(), dialog.button(QMessageBox.StandardButton.Cancel))
            QTest.keyClick(dialog, Qt.Key.Key_Return)
        QTimer.singleShot(0, cancel_by_default)
        f.menu_action('Удалить…').trigger()
        self.assertEqual(f.jobs, [])
        f.service.delete_messages.assert_not_called()

    def test_typed_failure_arriving_after_navigation_is_ignored(self):
        f = self.fixture
        f.view._run_message_action(f.service.delete_messages, f.message, f.view.chat_metadata_binding(), [f.message.id])
        done = f.jobs[0][-2]
        f.view.open_chat(Chat(2, title='Other synthetic chat'))
        with patch('maxclient.ui.chat_view.QMessageBox.warning') as warning, patch.object(f.view, '_after_send') as refresh:
            done(_MessageActionFailure('rejected'))
            warning.assert_not_called()
            refresh.assert_not_called()

