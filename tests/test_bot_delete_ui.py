"""Synthetic bot deletion UI; no account, network, or production worker calls."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QDialogButtonBox, QLabel
from shiboken6 import isValid

import test_chat_row_menu_context as row_base
import test_conversation_actions_shell as info_base
from maxclient.ui.chat_row_menu import chat_row_entries
from maxclient.ui.conversation_bot_delete_dialog import (
    ConversationBotDeleteDialog, bot_delete_available, bot_delete_notice,
    confirm_bot_conversation_delete,
)
from maxclient.ui.conversation_management_dialogs import confirmed_lifecycle_action


def result(context, **changes):
    row = dict(context=context, chat_id=context.chat_id, peer_id=context.peer_id,
               action='delete', lifecycle_kind='bot_delete', stage='complete',
               confirmed=True, pending=False, accepted=True, stop_confirmed=True,
               delete_confirmed=True, partial=False, can_continue_delete=False)
    row.update(changes)
    return row


def stopped(context):
    return result(context, stage='stopped', confirmed=False, pending=True,
                  delete_confirmed=False, partial=True, can_continue_delete=True)


def bot_snapshot(snapshot, *, state=None, suspended=False):
    snapshot['action_capabilities'] = dict(supported=True, kind='bot', service=False,
        suspended=suspended, participant=True, mute=True, last_message=True, more=['delete'])
    snapshot['action_pending'] = state is not None and state['pending']
    snapshot['bot_delete_state'] = state
    snapshot['more_actions'] = [dict(action='delete', value=None, write=True,
        label='Удалить чат' if suspended else 'Удалить и остановить', available=state is None)]
    if state is not None:
        snapshot['more_actions'].extend([
            dict(action='continue_bot_delete', value=None, write=True, label='Удалить чат',
                 available=state['can_continue_delete']),
            dict(action='refresh_actions', value=None, write=False, label='Проверить состояние', available=True)])
    return snapshot


class BotDeleteDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.dialogs = []
        self.context = SimpleNamespace(chat_id=111, peer_id=222)

    def tearDown(self):
        for dialog in self.dialogs:
            if isValid(dialog):
                dialog.close()
                dialog.deleteLater()
                QCoreApplication.sendPostedEvents(dialog, QEvent.Type.DeferredDelete)
        self.app.processEvents()

    def dialog(self, **kwargs):
        kwargs.setdefault('context_is_current', lambda: True)
        dialog = ConversationBotDeleteDialog('<b>Offline bot</b>', **kwargs)
        self.dialogs.append(dialog)
        dialog.open()
        self.app.processEvents()
        return dialog

    def snapshot(self, **kwargs):
        return bot_snapshot(dict(context=self.context, chat_id=111, peer_id=222, title='Offline'), **kwargs)

    def test_enter_defaults_to_cancel_for_both_initial_and_continuation(self):
        for continuation in (False, True):
            dialog = self.dialog(continuation=continuation)
            self.assertTrue(dialog.buttons.button(QDialogButtonBox.StandardButton.Cancel).isDefault())
            self.assertFalse(dialog.delete_button.isDefault())
            self.assertFalse(dialog.delete_button.autoDefault())
            QTest.keyClick(dialog, Qt.Key.Key_Return)
            self.assertEqual(dialog.result(), QDialog.DialogCode.Rejected)
            self.assertFalse(dialog.isVisible())
            self.assertFalse(dialog._scope_timer.isActive())

    def test_escape_cancels_and_scope_loss_rejects_late_accept(self):
        dialog = self.dialog()
        QTest.keyClick(dialog, Qt.Key.Key_Escape)
        self.assertEqual(dialog.result(), QDialog.DialogCode.Rejected)
        current = Mock(return_value=True)
        dialog = self.dialog(context_is_current=current)
        current.return_value = False
        dialog._check_scope()
        dialog.accept()
        self.assertFalse(dialog.isVisible())
        self.assertEqual(dialog.result(), QDialog.DialogCode.Rejected)

    def test_labels_literal_target_and_two_phase_warning(self):
        for suspended in (False, True):
            dialog = self.dialog(suspended=suspended)
            self.assertEqual(dialog.windowTitle(), 'Удалить чат' if suspended else 'Удалить и остановить')
            labels = dialog.findChildren(QLabel)
            self.assertTrue(all(label.textFormat() == Qt.TextFormat.PlainText for label in labels))
            self.assertTrue(any('<b>Offline bot</b>' in label.text() for label in labels))
            self.assertTrue(any('бот останется остановленным' in label.text() for label in labels))
        dialog = self.dialog(continuation=True)
        self.assertTrue(any('только удаление' in label.text() for label in dialog.findChildren(QLabel)))

    def test_confirmation_rechecks_scope_after_modal_accept(self):
        current = {'value': True}
        def accepted(_dialog):
            current['value'] = False
            return QDialog.DialogCode.Accepted
        with patch.object(QDialog, 'exec', new=accepted):
            self.assertFalse(confirm_bot_conversation_delete(None, self.snapshot(), self.context,
                context_is_current=lambda: current['value']))

    def test_snapshot_target_capabilities_and_continuation_are_exact(self):
        snapshot = self.snapshot()
        self.assertTrue(bot_delete_available(snapshot, self.context))
        for changes in ({'context': object()}, {'chat_id': 999}, {'peer_id': 999},
                        {'peer_id': True}, {'action_pending': True}):
            self.assertFalse(bot_delete_available(dict(snapshot, **changes), self.context))
        for cap in ({'supported': False}, {'service': True}, {'kind': 'dialog'}, {'more': []}):
            changed = dict(snapshot, action_capabilities=dict(snapshot['action_capabilities'], **cap))
            self.assertFalse(bot_delete_available(changed, self.context))
        state = stopped(self.context)
        self.assertTrue(bot_delete_available(self.snapshot(state=state), self.context, continuation=True))
        for changes in ({'stage': 'delete_pending'}, {'stop_confirmed': False},
                        {'can_continue_delete': False}, {'peer_id': 999}):
            self.assertFalse(bot_delete_available(self.snapshot(state=dict(state, **changes)),
                                                  self.context, continuation=True))

    def test_success_requires_exact_target_and_both_confirmed_phases(self):
        good = result(self.context)
        self.assertEqual(confirmed_lifecycle_action(good, self.context, 'delete'), 'delete')
        self.assertEqual(confirmed_lifecycle_action(good, self.context, 'continue_bot_delete'), 'delete')
        self.assertEqual(confirmed_lifecycle_action({'lifecycle_result': good}, self.context,
                                                    'refresh_actions'), 'delete')
        for change in ({'pending': True}, {'confirmed': False}, {'chat_id': 999}, {'peer_id': 999},
                       {'context': object()}, {'stop_confirmed': False}, {'delete_confirmed': False}):
            self.assertIsNone(confirmed_lifecycle_action(dict(good, **change), self.context, 'delete'))

    def test_status_distinguishes_stopped_from_unknown_and_is_translated(self):
        partial = bot_delete_notice(stopped(self.context))
        unknown = bot_delete_notice(result(self.context, confirmed=False, pending=True,
            stage='delete_pending', delete_confirmed=False))
        stop_unknown = bot_delete_notice(result(self.context, confirmed=False, pending=True,
            stage='stop_pending', stop_confirmed=False, delete_confirmed=False))
        self.assertIn('Чат не удалён', partial)
        self.assertIn('не повторяет', unknown)
        self.assertIn('не запускалось', stop_unknown)
        self.assertNotIn('Чат удалён.', partial + unknown + stop_unknown)
        self.assertIn('not deleted', bot_delete_notice(stopped(self.context), 'en'))
        gated = bot_delete_notice(dict(stopped(self.context), can_continue_delete=False))
        self.assertIn('Чат не удалён', gated)
        self.assertNotIn('не подтверждено', gated)


class BotDeleteRowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        row_base.ChatRowMenuContextTests.setUpClass()

    def setUp(self):
        self.fixture = row_base.ChatRowMenuContextTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.window, self.service = self.fixture.window, self.fixture.service
        self.state = None
        self.suspended = False
        original = self.service.conversation_snapshot
        self.service.conversation_snapshot = lambda context: bot_snapshot(original(context),
            state=self.state, suspended=self.suspended)
        self.service.delete_conversation = Mock(side_effect=lambda context, **_options: result(context))
        self.service.continue_bot_conversation_delete = Mock(side_effect=lambda context: result(context))
        self.service.refresh_conversation_controls = Mock(side_effect=lambda context: {
            'context': context, 'lifecycle_result': self.state})
        self.menu = self.fixture._menu()
        self.context = self.window._row_menu_context
        self.fixture.queue.clear()

    def submit(self, action='delete', answer=True):
        with patch('maxclient.ui.main_window.confirm_bot_conversation_delete', return_value=answer):
            self.window._chat_row_menu_action(action, None, self.context)

    def test_active_and_suspended_labels_and_cancel_without_worker(self):
        self.assertEqual(next(row for row in self.window._chat_row_entries(self.context)
                              if row['action'] == 'delete')['label'], 'Удалить и остановить')
        self.suspended = True
        self.assertEqual(next(row for row in self.window._chat_row_entries(self.context)
                              if row['action'] == 'delete')['label'], 'Удалить чат')
        self.submit(answer=False)
        self.assertFalse(self.fixture.queue)
        self.service.delete_conversation.assert_not_called()

    def test_one_confirmed_submit_and_matching_completion(self):
        self.window.chat_view.chat = self.service.chats[0]
        self.submit()
        self.submit()
        self.assertEqual(len(self.fixture.queue), 1)
        self.fixture._run(self.fixture.queue.pop())
        self.service.delete_conversation.assert_called_once_with(self.context, for_all=False)
        self.assertIsNone(self.window.chat_view.chat)
        self.assertEqual(self.window.statusBar().currentMessage(), 'Бот остановлен. Чат удалён.')

    def test_account_change_during_confirmation_or_before_worker_never_writes(self):
        def changed(*_args, **_kwargs):
            self.service.security_session = object()
            return True
        with patch('maxclient.ui.main_window.confirm_bot_conversation_delete', side_effect=changed):
            self.window._chat_row_menu_action('delete', None, self.context)
        self.assertFalse(self.fixture.queue)
        self.service.delete_conversation.assert_not_called()

    def test_changed_target_before_worker_is_rejected(self):
        self.submit()
        job = self.fixture.queue.pop()
        original = self.service.conversation_snapshot
        self.service.conversation_snapshot = lambda context: dict(original(context), peer_id=999)
        self.assertIsInstance(self.fixture._run(job, callbacks=False), ValueError)
        self.service.delete_conversation.assert_not_called()

    def test_partial_result_preserves_chat_and_offers_explicit_continuation(self):
        chat = self.service.chats[0]
        self.window.chat_view.chat = chat
        self.submit()
        job = self.fixture.queue.pop()
        self.state = stopped(self.context)
        with patch('maxclient.ui.invite_link.show_menu_notice') as notice:
            job[3](self.state)
        self.assertIs(self.window.chat_view.chat, chat)
        self.assertIn('Чат не удалён', notice.call_args.args[1])
        rows = self.window._chat_row_entries(self.context)
        self.assertFalse(next(row for row in rows if row['action'] == 'delete')['available'])
        self.assertTrue(next(row for row in rows if row['action'] == 'continue_bot_delete')['available'])
        self.assertFalse(self.fixture.queue)

    def test_continuation_only_calls_its_explicit_method_and_cancel_does_nothing(self):
        self.state = stopped(self.context)
        self.submit('continue_bot_delete', answer=False)
        self.assertFalse(self.fixture.queue)
        self.submit('continue_bot_delete')
        self.fixture._run(self.fixture.queue.pop())
        self.service.continue_bot_conversation_delete.assert_called_once_with(self.context)
        self.service.delete_conversation.assert_not_called()

    def test_unknown_delete_blocks_continuation_and_refresh_never_dispatches_write(self):
        self.state = result(self.context, stage='delete_pending', pending=True, confirmed=False,
                            delete_confirmed=False, partial=True)
        self.submit('continue_bot_delete')
        self.assertFalse(self.fixture.queue)
        self.window._chat_row_menu_action('refresh_actions', None, self.context)
        with patch('maxclient.ui.invite_link.show_menu_notice'):
            self.fixture._run(self.fixture.queue.pop())
        self.service.refresh_conversation_controls.assert_called_once_with(self.context)
        self.service.delete_conversation.assert_not_called()
        self.service.continue_bot_conversation_delete.assert_not_called()
        self.assertFalse(self.fixture.queue)


class BotDeleteInfoTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        info_base.ConversationActionsShellTests.setUpClass()

    def setUp(self):
        self.fixture = info_base.ConversationActionsShellTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.window, self.service = self.fixture.window, self.fixture.service
        self.context = self.fixture.context
        self.state = None
        original = self.service.conversation_snapshot
        self.service.conversation_snapshot = lambda context: bot_snapshot(original(context), state=self.state)
        self.service.delete_conversation = Mock(side_effect=lambda context, **_options: result(context))
        self.service.continue_bot_conversation_delete = Mock(side_effect=lambda context: result(context))
        self.service.refresh_conversation_controls = Mock(side_effect=lambda context: {
            'context': context, 'lifecycle_result': self.state})
        self.render()

    def render(self):
        self.window._info_panel.set_conversation(self.window._info_with_avatar(
            self.service.conversation_snapshot(self.context)))
        self.fixture.jobs.reset_mock()

    def submit(self, action='delete', answer=True):
        with patch('maxclient.ui.main_window.confirm_bot_conversation_delete', return_value=answer):
            self.fixture.action(action)
        return self.fixture.jobs.call_args

    def test_cancel_and_single_submit_for_original_target(self):
        self.assertIsNone(self.submit(answer=False))
        job = self.submit()
        self.submit()
        self.assertEqual(self.fixture.jobs.call_count, 1)
        outcome = job.args[0]()
        self.service.delete_conversation.assert_called_once_with(self.context, for_all=False)
        job.kwargs['on_done'](outcome)
        self.assertIsNone(self.window._info_context)
        self.assertIsNone(self.window.chat_view.chat)
        self.assertEqual(self.window.statusBar().currentMessage(), 'Бот остановлен. Чат удалён.')

    def test_closed_info_before_worker_or_late_callback_does_not_touch_new_view(self):
        job = self.submit()
        self.window._close_conversation_info()
        with self.assertRaises(ValueError):
            job.args[0]()
        self.window._open_conversation_info(self.service.chats[303])
        next_context = self.window._info_context
        job.kwargs['on_done'](result(self.context))
        self.assertIs(self.window._info_context, next_context)
        self.service.delete_conversation.assert_not_called()

    def test_partial_and_unknown_keep_info_and_never_claim_completion(self):
        for state in (stopped(self.context), result(self.context, confirmed=False, pending=True,
                      delete_confirmed=False, stage='delete_pending')):
            self.state = None
            self.render()
            job = self.submit()
            self.state = state
            job.kwargs['on_done'](state)
            self.assertIs(self.window._info_context, self.context)
            self.assertNotEqual(self.window.statusBar().currentMessage(), 'Бот остановлен. Чат удалён.')
            self.assertIn('Бот остановлен.', self.window._info_panel.status.text())
            self.assertEqual(self.fixture.jobs.call_count, 1)

    def test_partial_continuation_is_enabled_in_info_more_despite_pending(self):
        self.state = stopped(self.context)
        self.render()
        row = next(row for row in self.window._info_snapshot['more_actions']
                   if row['action'] == 'continue_bot_delete')
        self.assertTrue(self.window._info_panel._action_pending)
        self.assertTrue(self.window._info_panel._menu_allowed(row))
        self.assertIsNone(self.submit('continue_bot_delete', answer=False))
        job = self.submit('continue_bot_delete')
        self.assertFalse(self.window._info_panel._menu_allowed(row))
        job.kwargs['on_done'](job.args[0]())
        self.service.continue_bot_conversation_delete.assert_called_once_with(self.context)
        self.service.delete_conversation.assert_not_called()

    def test_read_only_refresh_never_advances_stopped_phase(self):
        self.state = stopped(self.context)
        self.render()
        self.fixture.action('refresh_actions')
        job = self.fixture.jobs.call_args
        job.kwargs['on_done'](job.args[0](*job.args[1:]))
        self.service.refresh_conversation_controls.assert_called_once_with(self.context)
        self.service.delete_conversation.assert_not_called()
        self.service.continue_bot_conversation_delete.assert_not_called()
        self.assertIs(self.window._info_context, self.context)
        self.assertEqual(self.fixture.jobs.call_count, 1)

    def test_wrong_peer_or_missing_bot_phase_result_cannot_close_info(self):
        for outcome in (result(self.context, peer_id=999),
                        dict(context=self.context, action='delete', confirmed=True, pending=False)):
            self.render()
            job = self.submit()
            job.kwargs['on_done'](outcome)
            self.assertIs(self.window._info_context, self.context)
            self.assertIsNotNone(self.window.chat_view.chat)


if __name__ == '__main__':
    unittest.main()
