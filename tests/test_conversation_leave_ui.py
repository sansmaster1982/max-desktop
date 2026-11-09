"""Leave menu orchestration; synthetic services only, no real conversation writes."""
import unittest
from unittest.mock import Mock, patch

import test_chat_row_menu_context as row_base
import test_conversation_actions_shell as info_base
from maxclient.ui.chat_row_menu import chat_row_entries
from maxclient.ui.conversation_management_dialogs import confirmed_lifecycle_action


def leave_snapshot(snapshot, *, owner=False, kind='group', pending=False):
    snapshot.update(action_pending=pending, leave_private=True)
    snapshot['action_capabilities'] = dict(supported=True, kind=kind, participant=True,
                                          owner=owner, mute=True, more=['leave'])
    snapshot['more_actions'] = [dict(action='leave', label='Выйти из чата',
                                      available=not owner, write=True, value=None)]
    if pending:
        snapshot['more_actions'].append(dict(action='refresh_actions', label='Проверить состояние',
                                             available=True, write=False, value=None))
    return snapshot


class ConversationLeaveRowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        row_base.ChatRowMenuContextTests.setUpClass()

    def setUp(self):
        self.fixture = row_base.ChatRowMenuContextTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.window, self.service = self.fixture.window, self.fixture.service
        original = self.service.conversation_snapshot
        self.service.conversation_snapshot = lambda context: leave_snapshot(
            original(context), pending=self.service.pending)
        self.service.leave_conversation = Mock(side_effect=lambda context: {
            'context': context, 'chat_id': context.chat_id, 'action': 'leave',
            'confirmed': True, 'pending': False})
        self.menu = self.fixture._menu()
        self.context = self.window._row_menu_context
        self.fixture.queue.clear()

    def action(self):
        self.window._chat_row_menu_action('leave', None, self.context)

    def test_owner_and_pending_are_not_enabled(self):
        for owner, pending in ((True, False), (False, True)):
            snapshot = leave_snapshot(self.service.conversation_snapshot(self.context),
                                      owner=owner, pending=pending)
            row = next(row for row in chat_row_entries(snapshot) if row['action'] == 'leave')
            self.assertFalse(row['available'])

    def test_cancel_does_not_queue_or_call_leave(self):
        with patch('maxclient.ui.main_window.confirm_conversation_leave', return_value=False):
            self.action()
        self.assertFalse(self.fixture.queue)
        self.service.leave_conversation.assert_not_called()

    def test_confirm_queues_once_then_completion_preserves_other_chat(self):
        other = self.service.chats[1]
        self.window.chat_view.chat = other
        with patch('maxclient.ui.main_window.confirm_conversation_leave', return_value=True):
            self.action()
            self.action()
        self.assertEqual(len(self.fixture.queue), 1)
        self.service.leave_conversation.assert_not_called()
        self.fixture._run(self.fixture.queue.pop())
        self.service.leave_conversation.assert_called_once_with(self.context)
        self.assertIs(self.window.chat_view.chat, other)
        self.assertEqual(self.window.statusBar().currentMessage(), 'Вы вышли из чата.')

    def test_cancelled_scope_before_worker_prevents_leave(self):
        with patch('maxclient.ui.main_window.confirm_conversation_leave', return_value=True):
            self.action()
        job = self.fixture.queue.pop()
        self.window._cancel_chat_row_menu()
        self.fixture._run(job)
        self.service.leave_conversation.assert_not_called()

    def test_scope_changed_during_confirmation_prevents_queue(self):
        def change(*_args, **_kwargs):
            self.window._cancel_chat_row_menu()
            return True
        with patch('maxclient.ui.main_window.confirm_conversation_leave', side_effect=change):
            self.action()
        self.assertFalse(self.fixture.queue)

    def test_pending_readback_does_not_close_chat_or_claim_success(self):
        chat = self.service.chats[0]
        self.window.chat_view.chat = chat
        self.service.leave_conversation.side_effect = lambda context: {
            'context': context, 'chat_id': context.chat_id, 'action': 'leave',
            'confirmed': False, 'pending': True}
        with patch('maxclient.ui.main_window.confirm_conversation_leave', return_value=True):
            self.action()
        self.service.pending = True
        job = self.fixture.queue.pop()
        # Simulate a completed worker returning unknown; no second dispatch.
        with patch('maxclient.ui.invite_link.show_menu_notice'):
            job[3](self.service.leave_conversation(self.context))
        self.assertIs(self.window.chat_view.chat, chat)
        self.assertNotEqual(self.window.statusBar().currentMessage(), 'Вы вышли из чата.')
        self.assertFalse(next(row for row in self.window._chat_row_entries(self.context)
                              if row['action'] == 'leave')['available'])


class ConversationLeaveInfoTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        info_base.ConversationActionsShellTests.setUpClass()

    def setUp(self):
        self.fixture = info_base.ConversationActionsShellTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.window, self.service = self.fixture.window, self.fixture.service
        self.context = self.fixture.context
        original = self.service.conversation_snapshot
        self.service.conversation_snapshot = lambda context: leave_snapshot(
            original(context), pending=self.service.pending)
        self.window._info_snapshot = self.service.conversation_snapshot(self.context)
        self.service.leave_conversation = Mock(return_value={
            'context': self.context, 'chat_id': self.context.chat_id, 'action': 'leave',
            'confirmed': True, 'pending': False})
        self.fixture.jobs.reset_mock()

    def submit(self):
        with patch('maxclient.ui.main_window.confirm_conversation_leave', return_value=True):
            self.fixture.action('leave')
        return self.fixture.jobs.call_args

    def test_confirmed_matching_leave_closes_target_without_history_deletion(self):
        job = self.submit()
        self.service.leave_conversation.assert_not_called()
        result = job.args[0]()
        self.service.leave_conversation.assert_called_once_with(self.context)
        job.kwargs['on_done'](result)
        self.assertIsNone(self.window._info_context)
        self.assertIsNone(self.window.chat_view.chat)
        self.service.delete_conversation.assert_not_called()
        self.service.clear_conversation_history.assert_not_called()
        self.assertEqual(self.window.statusBar().currentMessage(), 'Вы вышли из чата.')

    def test_queued_worker_rechecks_closed_info(self):
        job = self.submit()
        self.window._close_conversation_info()
        with self.assertRaises(ValueError):
            job.args[0]()
        self.service.leave_conversation.assert_not_called()

    def test_late_success_does_not_close_newly_opened_context(self):
        job = self.submit()
        self.window._close_conversation_info()
        self.window._open_conversation_info(self.service.chats[303])
        new_context = self.window._info_context
        job.kwargs['on_done'](self.service.leave_conversation.return_value)
        self.assertIs(self.window._info_context, new_context)
        self.assertNotEqual(self.window.statusBar().currentMessage(), 'Вы вышли из чата.')

    def test_unconfirmed_and_mismatched_results_are_not_success(self):
        good = self.service.leave_conversation.return_value
        self.assertEqual(confirmed_lifecycle_action(good, self.context, 'leave'), 'leave')
        for changes in ({'pending': True}, {'chat_id': 999}, {'confirmed': False}, {'context': object()}):
            self.assertIsNone(confirmed_lifecycle_action(dict(good, **changes), self.context, 'leave'))
        self.assertEqual(confirmed_lifecycle_action({'lifecycle_result': good}, self.context,
                                                    'refresh_actions'), 'leave')

