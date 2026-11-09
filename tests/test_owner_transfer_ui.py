"""Offline owner picker, confirmation and phase recovery; synthetic accounts only."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import dataclasses
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QDialogButtonBox, QLabel
from shiboken6 import isValid

from maxclient.ui.conversation_owner_transfer_dialog import (
    ConversationOwnerTransferDialog, OwnerTransferConfirmation, owner_transfer_available,
    owner_transfer_notice, owner_transfer_result,
)
from maxclient.ui.conversation_management_dialogs import confirmed_lifecycle_action
import test_chat_row_menu_context as row_base
import test_conversation_actions_shell as info_base


def phase(context, **changes):
    state = dict(context=context, chat_id=context.chat_id, peer_id=None, action='leave',
        lifecycle_kind='owner_transfer', stage='complete', successor_id=202,
        successor_name='Синтетический преемник', transfer_confirmed=True, leave_confirmed=True,
        confirmed=True, pending=False, partial=False, can_continue_leave=False)
    state.update(changes)
    return state


def transferred(context):
    return phase(context, stage='transferred', leave_confirmed=False, confirmed=False,
                 pending=True, partial=True, can_continue_leave=True)


def snapshot(context, state=None, *, kind='group'):
    return dict(context=context, chat_id=context.chat_id, peer_id=None, title='Синтетическая группа',
        leave_private=True, action_pending=state is not None and state['pending'],
        owner_transfer_state=state, row_menu={'chat_status': 'ACTIVE'},
        action_capabilities=dict(supported=True, service=False, kind=kind,
            owner=state is None, participant=True, more=['leave']),
        more_actions=([dict(action='transfer_owner_leave', label='Передать права и выйти',
                            value=None, write=True, available=True)] if state is None else [
            dict(action='continue_owner_leave', label='Выйти из чата', value=None, write=True,
                 available=state['can_continue_leave']),
            dict(action='refresh_actions', label='Проверить состояние', value=None, write=False, available=True)]),
        availability={'more': True})


class SyntheticOwnerService:
    def __init__(self, context):
        self.context, self.current, self.state, self.kind = context, True, None, 'group'
        self.rows = [dict(id=202, name='Синтетический преемник', role='ADMIN', ticket=object()),
                     dict(id=404, name='Другой администратор', role='ADMIN', ticket=object())]
        self.get_owner_transfer_candidates = Mock(side_effect=self.page)
        self.transfer_conversation_owner_and_leave = Mock(side_effect=lambda *_args: self.complete())
        self.continue_conversation_owner_leave = Mock(side_effect=lambda *_args: self.complete())
        self.refresh_conversation_controls = Mock(side_effect=lambda *_args: {
            'context': self.context, 'lifecycle_result': self.state})

    def is_current_conversation(self, context):
        return self.current and context is self.context

    def conversation_snapshot(self, context):
        if not self.is_current_conversation(context):
            raise ValueError('Synthetic stale context')
        return snapshot(context, self.state, kind=self.kind)

    def page(self, context, **_kwargs):
        return dict(context=context, chat_id=context.chat_id, kind=self.kind,
                    candidates=self.rows, marker=None, complete=True)

    def complete(self):
        self.state = phase(self.context)
        return self.state


class OwnerTransferDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.context = SimpleNamespace(owner_id=101, chat_id=303, peer_id=None)
        self.service = SyntheticOwnerService(self.context)
        self.dialogs, self.jobs = [], []
        self.patcher = patch('maxclient.ui.conversation_owner_transfer_dialog.run_async', side_effect=self.queue)
        self.patcher.start()

    def tearDown(self):
        for dialog in reversed(self.dialogs):
            if isValid(dialog):
                dialog.reject()
                dialog.deleteLater()
                QCoreApplication.sendPostedEvents(dialog, QEvent.Type.DeferredDelete)
        self.app.processEvents()
        self.patcher.stop()

    def queue(self, fn, *args, **kwargs):
        self.jobs.append((fn, args, kwargs))

    def run_job(self):
        fn, args, kw = self.jobs.pop(0)
        value = fn(*args)
        kw['on_done'](value)
        return value

    def dialog(self, *, load=True):
        dialog = ConversationOwnerTransferDialog(self.service, self.context,
            self.service.conversation_snapshot(self.context), context_is_current=lambda: True)
        self.dialogs.append(dialog)
        dialog.open()
        self.app.processEvents()
        if load:
            dialog.start_loading()
            self.run_job()
        return dialog

    def confirm(self, dialog, answer=QDialog.DialogCode.Accepted):
        with patch.object(OwnerTransferConfirmation, 'exec', return_value=answer):
            dialog.submit()

    def test_picker_read_only_no_preselection_and_enter_cancels(self):
        dialog = self.dialog()
        self.assertEqual(dialog.candidates.count(), 2)
        self.assertIsNone(dialog._selected())
        self.assertFalse(dialog.submit_button.isEnabled())
        QTest.keyClick(dialog, Qt.Key.Key_Return)
        self.assertEqual(dialog.result(), QDialog.DialogCode.Rejected)
        self.service.transfer_conversation_owner_and_leave.assert_not_called()
        self.service.continue_conversation_owner_leave.assert_not_called()

    def test_confirmations_default_cancel_initial_and_resume_with_plain_names(self):
        for continuation in (False, True):
            dialog = OwnerTransferConfirmation('<b>Group</b>', dict(id=202, name='<b>Name</b>', role='ADMIN'),
                continuation=continuation, context_is_current=lambda: True)
            self.dialogs.append(dialog)
            dialog.open()
            self.app.processEvents()
            self.assertTrue(dialog.buttons.button(QDialogButtonBox.StandardButton.Cancel).isDefault())
            self.assertFalse(dialog.submit_button.isDefault())
            self.assertFalse(dialog.submit_button.autoDefault())
            labels = dialog.findChildren(QLabel)
            self.assertTrue(all(label.textFormat() == Qt.TextFormat.PlainText for label in labels))
            self.assertTrue(any('<b>Name</b>' == label.text() for label in labels))
            self.assertFalse(any('202' in label.text() for label in labels))
            self.assertTrue(any(('только выход' if continuation else 'не отменяется') in label.text()
                                for label in labels))
            QTest.keyClick(dialog, Qt.Key.Key_Return)
            self.assertEqual(dialog.result(), QDialog.DialogCode.Rejected)

    def test_cancel_after_selection_never_sends_and_next_confirm_sends_once(self):
        dialog = self.dialog()
        dialog.candidates.setCurrentRow(0)
        ticket = dialog._selected()['ticket']
        self.confirm(dialog, QDialog.DialogCode.Rejected)
        self.assertFalse(self.jobs)
        self.confirm(dialog)
        dialog.submit()
        self.assertEqual(len(self.jobs), 1)
        self.service.transfer_conversation_owner_and_leave.assert_not_called()
        self.run_job()
        self.service.transfer_conversation_owner_and_leave.assert_called_once_with(self.context, ticket)
        self.assertIn('Вы вышли', dialog.status.text())

    def test_session_changes_during_confirm_or_before_worker_prevent_submit(self):
        dialog = self.dialog()
        dialog.candidates.setCurrentRow(0)
        def changed(_dialog):
            self.service.current = False
            return QDialog.DialogCode.Accepted
        with patch.object(OwnerTransferConfirmation, 'exec', new=changed):
            dialog.submit()
        self.assertFalse(self.jobs)
        self.service.current = True
        self.confirm(dialog)
        self.assertEqual(len(self.jobs), 1)
        self.service.current = False
        self.run_job()
        self.service.transfer_conversation_owner_and_leave.assert_not_called()

    def test_selected_ticket_change_during_confirmation_cannot_send(self):
        dialog = self.dialog()
        dialog.candidates.setCurrentRow(0)
        def changed(_dialog):
            dialog._candidates[202]['ticket'] = object()
            return QDialog.DialogCode.Accepted
        with patch.object(OwnerTransferConfirmation, 'exec', new=changed):
            dialog.submit()
        self.assertFalse(self.jobs)

    def test_ambiguous_names_and_server_ambiguity_are_not_selectable(self):
        self.service.rows[1]['name'] = self.service.rows[0]['name']
        dialog = self.dialog()
        self.assertEqual(dialog._ambiguous, {202, 404})
        dialog.candidates.setCurrentRow(0)
        self.assertIsNone(dialog._selected())
        self.assertFalse(dialog.submit_button.isEnabled())
        self.assertIn('Одноимённые', dialog.status.text())
        self.service.rows = [dict(id=505, name='Other', role='ADMIN', ticket=object(), ambiguous=True)]
        dialog.start_loading()
        self.run_job()
        self.assertEqual(dialog._ambiguous, {505})

    def test_self_and_group_nonadmin_are_not_offered(self):
        self.service.rows.extend([dict(id=101, name='Self', role='ADMIN', ticket=object()),
                                 dict(id=606, name='Member', role='MEMBER', ticket=object())])
        dialog = self.dialog()
        self.assertEqual(set(dialog._candidates), {202, 404})

    def test_partial_empty_search_is_not_claimed_as_no_members(self):
        self.service.get_owner_transfer_candidates.side_effect = lambda context, **_kw: {
            'context': context, 'chat_id': 303, 'kind': 'group', 'candidates': [], 'complete': False, 'marker': 7}
        dialog = self.dialog(load=False)
        dialog.search.setText('Some name')
        dialog.start_loading()
        self.run_job()
        self.assertIn('часть списка', dialog.status.text())
        self.assertNotIn('не найдены', dialog.status.text())
        self.assertTrue(dialog.more_button.isHidden())
        self.assertFalse(dialog.submit_button.isEnabled())

    def test_search_invalidates_selection_and_late_response(self):
        dialog = self.dialog()
        dialog.candidates.setCurrentRow(0)
        dialog.start_loading()
        old = self.jobs.pop()
        dialog.search.setText('New name')
        self.assertFalse(dialog.submit_button.isEnabled())
        old[2]['on_done'](self.service.page(self.context))
        self.assertEqual(dialog.candidates.count(), 0)
        self.assertFalse(self.jobs)

    def test_explicit_channel_next_page_preserves_ticket_and_detects_duplicate_names(self):
        self.service.kind = 'channel'
        self.service.get_owner_transfer_candidates.side_effect = lambda context, **kw: dict(
            context=context, chat_id=303, kind='channel', complete=kw['marker'] is not None,
            marker=9 if kw['marker'] is None else None,
            candidates=[dict(id=202 if kw['marker'] is None else 404, name='Same name',
                             role='MEMBER', ticket=object())])
        dialog = self.dialog()
        self.assertEqual(self.service.get_owner_transfer_candidates.call_count, 1)
        dialog.start_loading(more=True)
        self.run_job()
        self.assertEqual(self.service.get_owner_transfer_candidates.call_args.kwargs['marker'], 9)
        self.assertEqual(dialog._ambiguous, {202, 404})

    def test_partial_stays_open_and_verify_never_continues_leave(self):
        self.service.state = transferred(self.context)
        dialog = self.dialog(load=False)
        self.assertIn('Права переданы', dialog.status.text())
        self.assertIn('Выход не подтверждён', dialog.status.text())
        dialog.verify()
        self.run_job()
        self.assertTrue(dialog.isVisible())
        self.service.refresh_conversation_controls.assert_called_once_with(self.context)
        self.service.transfer_conversation_owner_and_leave.assert_not_called()
        self.service.continue_conversation_owner_leave.assert_not_called()

    def test_continuation_is_separately_confirmed_and_never_repeats_transfer(self):
        self.service.state = transferred(self.context)
        dialog = self.dialog(load=False)
        self.confirm(dialog, QDialog.DialogCode.Rejected)
        self.assertFalse(self.jobs)
        self.confirm(dialog)
        self.run_job()
        self.service.continue_conversation_owner_leave.assert_called_once_with(self.context)
        self.service.transfer_conversation_owner_and_leave.assert_not_called()

    def test_unknown_leave_disables_continue_even_when_fallback_snapshot_fails(self):
        self.service.state = transferred(self.context)
        dialog = self.dialog(load=False)
        self.confirm(dialog)
        job = self.jobs.pop()
        self.service.conversation_snapshot = Mock(side_effect=ValueError('No synthetic cache'))
        job[2]['on_done'](None)
        self.assertFalse(dialog.submit_button.isEnabled())
        self.assertFalse(self.jobs)

    def test_unknown_transfer_and_unknown_leave_offer_only_check(self):
        for stage, transferred_flag in (('transfer_pending', False), ('leave_pending', True)):
            self.service.state = phase(self.context, stage=stage, transfer_confirmed=transferred_flag,
                leave_confirmed=False, confirmed=False, pending=True)
            dialog = self.dialog(load=False)
            self.assertFalse(dialog.submit_button.isEnabled())
            self.assertTrue(dialog.verify_button.isVisible())
            dialog.submit()
            self.assertFalse(self.jobs)

    def test_target_change_in_continuation_snapshot_invalidates_confirmation(self):
        self.service.state = transferred(self.context)
        dialog = self.dialog(load=False)
        self.service.state = dict(self.service.state, successor_id=404)
        self.confirm(dialog)
        self.assertFalse(self.jobs)

    def test_closed_picker_ignores_late_read_and_queued_write(self):
        dialog = self.dialog(load=False)
        dialog.start_loading()
        dialog.reject()
        self.run_job()
        self.assertFalse(dialog._candidates)
        dialog = self.dialog()
        dialog.candidates.setCurrentRow(0)
        self.confirm(dialog)
        dialog.reject()
        self.run_job()
        self.service.transfer_conversation_owner_and_leave.assert_not_called()

    def test_result_gate_requires_exact_context_chat_both_phases_and_valid_successor(self):
        good = phase(self.context)
        self.assertEqual(confirmed_lifecycle_action(good, self.context, 'transfer_owner_leave'), 'leave')
        self.assertEqual(confirmed_lifecycle_action({'lifecycle_result': good}, self.context, 'refresh_actions'), 'leave')
        for changes in ({'context': object()}, {'chat_id': 999}, {'peer_id': 202}, {'successor_id': 101},
                        {'successor_id': True}, {'pending': True}, {'transfer_confirmed': False},
                        {'leave_confirmed': False}, {'confirmed': False}, {'historical': True}):
            self.assertIsNone(confirmed_lifecycle_action(dict(good, **changes), self.context, 'transfer_owner_leave'))

    def test_settled_historical_operation_does_not_replace_new_owner_picker(self):
        historical = phase(self.context, historical=True, confirmed=False, pending=False)
        original = self.service.conversation_snapshot
        self.service.conversation_snapshot = lambda context: dict(original(context), owner_transfer_state=historical)
        dialog = self.dialog()
        self.assertIsNone(dialog._state)
        self.assertFalse(dialog.candidates.isHidden())
        self.assertEqual(dialog.candidates.count(), 2)
        self.assertFalse(dialog._attempted)
        self.assertEqual(owner_transfer_notice(historical), '')


class OwnerTransferMenuTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        row_base.ChatRowMenuContextTests.setUpClass()
        info_base.ConversationActionsShellTests.setUpClass()

    def test_row_routes_owner_to_picker_and_continuation_despite_pending(self):
        fixture = row_base.ChatRowMenuContextTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        service, window = fixture.service, fixture.window
        original_context = service.conversation_context
        service.conversation_context = lambda chat_id, **kw: dataclasses.replace(
            original_context(chat_id, **kw), peer_id=None)
        original = service.conversation_snapshot
        state = {'value': None}
        service.conversation_snapshot = lambda context: dict(original(context), **snapshot(context, state['value']))
        fixture._menu()
        context = window._row_menu_context
        fixture.queue.clear()
        with patch.object(window, '_open_owner_transfer') as opened:
            window._chat_row_menu_action('transfer_owner_leave', None, context)
        opened.assert_called_once_with(context, source='row', continuation=False)
        state['value'] = transferred(context)
        with patch.object(window, '_open_owner_transfer') as opened:
            window._chat_row_menu_action('continue_owner_leave', None, context)
        opened.assert_called_once_with(context, source='row', continuation=True)
        self.assertFalse(fixture.queue)

    def test_info_routes_owner_and_enables_only_explicit_continuation_during_pending(self):
        fixture = info_base.ConversationActionsShellTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        context, window, service = fixture.context, fixture.window, fixture.service
        context.peer_id = None
        context.owner_id = 101
        original = service.conversation_snapshot
        state = {'value': None}
        service.conversation_snapshot = lambda ctx: dict(original(ctx), **snapshot(ctx, state['value']))
        window._info_panel.set_conversation(window._info_with_avatar(service.conversation_snapshot(context)))
        with patch.object(window, '_open_owner_transfer') as opened:
            fixture.action('transfer_owner_leave')
        opened.assert_called_once_with(context, source='info', continuation=False)
        state['value'] = transferred(context)
        window._info_panel.set_conversation(window._info_with_avatar(service.conversation_snapshot(context)))
        row = next(row for row in window._info_snapshot['more_actions'] if row['action'] == 'continue_owner_leave')
        self.assertTrue(window._info_panel._action_pending)
        self.assertTrue(window._info_panel._menu_allowed(row))
        with patch.object(window, '_open_owner_transfer') as opened:
            fixture.action('continue_owner_leave')
        opened.assert_called_once_with(context, source='info', continuation=True)


class OwnerTransferCoreUiIntegrationTests(unittest.TestCase):
    def test_real_core_fake_transport_only_runs_after_final_confirmation(self):
        import test_owner_transfer_core as core
        fixture = core.OwnerTransferCoreTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        app = QApplication.instance() or QApplication([])
        service, context = fixture.s, fixture.context
        jobs = []
        def queue(fn, **kwargs):
            jobs.append((fn, kwargs))
        with patch('maxclient.ui.conversation_owner_transfer_dialog.run_async', side_effect=queue):
            dialog = ConversationOwnerTransferDialog(service, context, service.conversation_snapshot(context),
                                                      context_is_current=lambda: True)
            try:
                dialog.open()
                app.processEvents()
                dialog.start_loading()
                fn, kw = jobs.pop()
                kw['on_done'](fn())
                service.client.transfer_conversation_owner.assert_not_called()
                service.client.leave_conversation.assert_not_called()
                dialog.candidates.setCurrentRow(0)
                with patch.object(OwnerTransferConfirmation, 'exec', return_value=QDialog.DialogCode.Accepted):
                    dialog.submit()
                fn, kw = jobs.pop()
                kw['on_done'](fn())
                service.client.transfer_conversation_owner.assert_called_once()
                service.client.leave_conversation.assert_called_once()
                self.assertIn('Вы вышли', dialog.status.text())
                self.assertIsNotNone(service.store.message(303, 9))
                service.client.request.assert_not_called()
                service.client.change_conversation_lifecycle.assert_not_called()
            finally:
                dialog.reject()
                dialog.deleteLater()
                QCoreApplication.sendPostedEvents(dialog, QEvent.Type.DeferredDelete)


if __name__ == '__main__':
    unittest.main()
