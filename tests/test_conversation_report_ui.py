"""Synthetic report dialog + row/Info routes. No transport or account files."""
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog
from shiboken6 import isValid

import test_chat_row_menu_context as row_base
import test_conversation_actions_shell as info_base
from maxclient.protocol.client import MaxError, MaxWriteOutcomeUnknown
from maxclient.ui.conversation_report_dialog import ConversationReportDialog


def report_snapshot(context, *, kind='channel', available=True):
    return {'context': context, 'chat_id': context.chat_id, 'peer_id': 77,
            'title': '<b>Тестовый канал</b>', 'action_pending': False,
            'action_capabilities': {'supported': True, 'kind': kind, 'owner': False,
                                    'participant': True, 'more': ['report']},
            'row_menu': {'chat_status': 'ACTIVE'}, 'availability': {},
            'more_actions': [{'action': 'report', 'label': 'Пожаловаться',
                             'available': available, 'write': True, 'value': None}]}


class ConversationReportDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.context = SimpleNamespace(chat_id=123)
        self.current = True
        self.ticket = object()
        self.data = dict(context=self.context, chat_id=123, type_id=2, target_id=123,
                         pending=False, already_reported=False, available=True, ticket=self.ticket,
                         reasons=[{'reason_id': 2, 'title': 'Спам & реклама'},
                                  {'reason_id': -3, 'title': 'Другая причина'}])
        self.result = dict(context=self.context, chat_id=123, type_id=2, target_id=123,
                           action='report', confirmed=True, pending=False)
        self.service = SimpleNamespace(is_current_conversation=Mock(return_value=True),
             get_conversation_report_reasons=Mock(side_effect=lambda _c: self.data),
             submit_conversation_report=Mock(side_effect=lambda *_a, **_k: self.result))
        self.patch = patch('maxclient.ui.conversation_report_dialog.run_async')
        self.jobs = self.patch.start()
        self.addCleanup(self.patch.stop)
        self.dialog = ConversationReportDialog(self.service, self.context, report_snapshot(self.context),
                                                context_is_current=lambda: self.current)
        self.dialog.show()
        self.addCleanup(self.dispose)

    def dispose(self):
        if isValid(self.dialog):
            self.dialog.close()
            self.dialog.deleteLater()
        self.app.processEvents()

    def run_job(self):
        job = self.jobs.call_args
        self.jobs.reset_mock()
        result = job.args[0]()
        job.kwargs['on_done'](result)
        return result

    def load(self):
        self.dialog.start_loading()
        self.run_job()

    def test_load_does_not_submit_and_reason_is_required(self):
        self.load()
        self.assertEqual(self.dialog.title.textFormat(), Qt.TextFormat.PlainText)
        self.assertEqual(len(self.dialog.group.buttons()), 2)
        self.assertFalse(self.dialog.submit_button.isEnabled())
        self.dialog.submit()
        self.jobs.assert_not_called()
        self.service.submit_conversation_report.assert_not_called()

    def test_reason_selection_dispatches_once_with_original_ticket(self):
        self.load()
        self.dialog.group.button(1).setChecked(True)
        self.assertTrue(self.dialog.submit_button.isEnabled())
        self.dialog.submit()
        self.dialog.submit()
        self.assertEqual(self.jobs.call_count, 1)
        self.run_job()
        self.service.submit_conversation_report.assert_called_once_with(self.context, -3, ticket=self.ticket)
        self.assertEqual(self.dialog.status.text(), 'Жалоба отправлена')
        self.assertFalse(self.dialog.submit_button.isEnabled())

    def test_escape_and_default_enter_do_not_submit(self):
        self.load()
        self.dialog.group.button(0).setChecked(True)
        QTest.keyClick(self.dialog, Qt.Key.Key_Escape)
        self.assertEqual(self.dialog.result(), QDialog.DialogCode.Rejected)
        self.service.submit_conversation_report.assert_not_called()
        self.jobs.assert_not_called()

    def test_cancel_queued_submit_never_calls_service(self):
        self.load()
        self.dialog.group.button(0).setChecked(True)
        self.dialog.submit()
        self.dialog.reject()
        self.run_job()
        self.service.submit_conversation_report.assert_not_called()

    def test_empty_or_foreign_reason_responses_cannot_submit(self):
        for change in ({'reasons': []}, {'context': object()}, {'target_id': 77},
                       {'ticket': None}, {'reasons': [{'reason_id': True, 'title': 'invalid'}]}):
            with self.subTest(change=tuple(change)):
                original = dict(self.data)
                self.data.update(change)
                self.load()
                self.assertFalse(self.dialog.submit_button.isEnabled())
                self.assertEqual(self.dialog.group.buttons(), [])
                self.data = original
        self.service.submit_conversation_report.assert_not_called()

    def test_late_reasons_after_context_change_not_displayed(self):
        self.dialog.start_loading()
        self.current = False
        self.run_job()
        self.assertEqual(self.dialog.group.buttons(), [])
        self.assertFalse(self.dialog.submit_button.isEnabled())

    def test_unknown_or_negative_ack_never_claims_success(self):
        self.load()
        self.dialog.group.button(0).setChecked(True)
        self.service.submit_conversation_report.side_effect = MaxWriteOutcomeUnknown('private payload')
        self.dialog.submit()
        self.run_job()
        self.assertIn('Повторная отправка остановлена', self.dialog.status.text())
        self.assertNotIn('private', self.dialog.status.text())
        self.dialog.submit()
        self.dialog.start_loading()
        self.jobs.assert_not_called()

    def test_rejected_report_has_no_success_or_automatic_retry(self):
        self.load()
        self.dialog.group.button(0).setChecked(True)
        self.service.submit_conversation_report.side_effect = MaxError('server secret')
        self.dialog.submit()
        self.run_job()
        self.assertEqual(self.dialog.status.text(), 'MAX отклонил жалобу.')
        self.assertFalse(self.dialog.retry.isVisible())

    def test_matching_success_required_and_does_not_touch_chat(self):
        self.load()
        self.dialog.group.button(0).setChecked(True)
        self.result['target_id'] = 999
        completed = Mock()
        self.dialog.completed.connect(completed)
        self.dialog.submit()
        self.run_job()
        completed.assert_called_once_with(None)
        self.assertNotEqual(self.dialog.status.text(), 'Жалоба отправлена')


class ConversationReportRoutesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        row_base.ChatRowMenuContextTests.setUpClass()
        info_base.ConversationActionsShellTests.setUpClass()

    def test_row_uses_clicked_chat_and_closes_on_context_revoke(self):
        fixture = row_base.ChatRowMenuContextTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        fixture.service.conversation_snapshot = lambda ctx: report_snapshot(ctx)
        fixture._menu()
        context = fixture.window._row_menu_context
        with patch('maxclient.ui.conversation_report_dialog.run_async') as jobs:
            fixture.window._chat_row_menu_action('report', None, context)
            dialog = fixture.window._report_dialog
            self.assertIsNotNone(dialog)
            self.assertIs(dialog.context, context)
            self.assertEqual(jobs.call_count, 1)
            fixture.window._chat_row_menu_action('report', None, context)
            self.assertEqual(jobs.call_count, 1)
            fixture.window._cancel_chat_row_menu()
            self.assertIsNone(fixture.window._report_dialog)
            self.assertFalse(dialog._lifetime['active'])

    def test_info_route_captures_card_context_and_does_not_send(self):
        fixture = info_base.ConversationActionsShellTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        fixture.service.conversation_snapshot = lambda ctx: report_snapshot(ctx, kind='bot')
        fixture.window._info_snapshot = fixture.service.conversation_snapshot(fixture.context)
        with patch('maxclient.ui.conversation_report_dialog.run_async') as jobs:
            fixture.window._info_menu_action('report', None, fixture.context)
            dialog = fixture.window._report_dialog
            self.assertIs(dialog.context, fixture.context)
            self.assertEqual(dialog._target_id, 77)
            self.assertEqual(jobs.call_count, 1)
            fixture.window._close_conversation_info()
            self.assertIsNone(fixture.window._report_dialog)

