"""Info action orchestration with fake services; no workers or account I/O."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import unittest
from unittest.mock import Mock, patch

from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication, QDialog
from shiboken6 import isValid

from maxclient.core.config import Settings
from maxclient.ui.main_window import MainWindow
from test_conversation_info_shell import FakeService as InfoFakeService


class FakeService(InfoFakeService):
    def __init__(self):
        super().__init__()
        self.muted, self.pending = False, False
        for name in ('get_conversation_folders', 'set_conversation_folder_included',
                     'set_conversation_contact_blocked', 'set_conversation_mute',
                     'set_conversation_stories_hidden', 'stop_conversation_bot',
                     'refresh_conversation_controls', 'clear_conversation_history',
                     'delete_conversation', 'share_conversation_contact'):
            setattr(self, name, Mock(side_effect=AssertionError('No worker or network')))
        self.get_folder_view = Mock(side_effect=lambda row, chats=None: {
            'hidden': row.get('hidden', False), 'chatIds': list(self.chats),
            'complete': True, 'unreadCount': 0,
        })

    def conversation_context(self, chat_id, *, activate_files=True):
        return super().conversation_context(chat_id)

    def share_conversation_destinations(self, context):
        if not self.is_current_conversation(context):
            raise ValueError('Changed session')
        return list(self.chats.values())

    def conversation_action_status(self, context):
        return {'context': context, 'available': True, 'muted': self.muted, 'pending': self.pending}

    def conversation_snapshot(self, context):
        result = super().conversation_snapshot(context)
        result['availability'].update(mute=True, more=True)
        result.update(muted=self.muted, action_pending=self.pending, action_capabilities={'mute': True})
        result['more_actions'] = [
            {'action': action, 'label': action, 'available': enabled, 'write': write, 'value': value}
            for action, enabled, write, value in (
                ('folder', True, False, None), ('search', True, False, None),
                ('block', True, True, None), ('hide_stories', True, True, True),
                ('stop_bot', True, True, None), ('delete', False, True, None),
                ('refresh_actions', True, False, None))]
        return result


class ConversationActionsShellTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.service = FakeService()
        self.patches = [patch('maxclient.ui.main_window.run_async'),
                        patch('maxclient.ui.main_window.DesktopNotifications'),
                        patch('maxclient.ui.main_window.confirm_action', return_value=False)]
        self.jobs, _, self.confirm = [item.start() for item in self.patches]
        settings = Settings(theme='light')
        settings.save = Mock()
        self.window = MainWindow(self.service, settings)
        self.window.chat_view.chat = self.service.chats[303]
        self.window.chat_view.open_chat = Mock()
        self.window._open_conversation_info(self.service.chats[303])
        self.context = self.window._info_context

    def tearDown(self):
        if isValid(self.window):
            self.window.close()
            self.window.deleteLater()
        self.app.processEvents()
        for item in reversed(self.patches): item.stop()

    def job(self, method):
        return next(call for call in reversed(self.jobs.call_args_list) if call.args[0] is method)

    def action(self, name, value=None):
        self.window._info_menu_action(name, value, self.context)

    def folders(self, rows, **extra):
        self.job(self.service.get_conversation_folders).kwargs['on_done']({'orderedFolders': rows, **extra})
        return next(row['children'] for row in self.window._info_snapshot['more_actions']
                    if row['action'] == 'folder')

    def test_mute_is_single_bound_job_and_no_optimistic_echo(self):
        self.action('mute', -1)
        job = self.job(self.service.set_conversation_mute)
        self.assertEqual(job.args, (self.service.set_conversation_mute, self.context, -1))
        before = self.jobs.call_count
        self.action('mute', 0)
        self.assertEqual(self.jobs.call_count, before)
        self.assertFalse(self.window._info_snapshot['muted'])
        self.service.pending = True
        job.kwargs['on_done']({'accepted': True, 'confirmed': False})
        self.assertFalse(self.window._info_snapshot['muted'])
        self.assertTrue(self.window._info_panel._action_pending)
        self.service.set_conversation_mute.assert_not_called()

    def enable_lifecycle(self, action):
        rows = self.window._info_snapshot['more_actions']
        rows[:] = [row for row in rows if row.get('action') != action]
        rows.append({'action': action, 'label': action, 'available': True, 'write': True, 'value': None})

    def test_clear_requires_confirmation_with_explicit_scope(self):
        self.enable_lifecycle('clear')
        before = self.jobs.call_count
        with patch('maxclient.ui.main_window.confirm_conversation_removal', return_value=None):
            self.action('clear')
        self.assertEqual(self.jobs.call_count, before)
        with patch('maxclient.ui.main_window.confirm_conversation_removal', return_value=False):
            self.action('clear')
        job = self.job(self.service.clear_conversation_history)
        self.assertEqual(job.args, (self.service.clear_conversation_history, self.context))
        self.assertIs(job.kwargs['for_all'], False)
        self.service.clear_conversation_history.assert_not_called()

    def test_lifecycle_confirmation_account_change_cannot_queue_write(self):
        self.enable_lifecycle('delete')
        def changed(*_args):
            self.service.context = object()
            return True
        before = self.jobs.call_count
        with patch('maxclient.ui.main_window.confirm_conversation_removal', side_effect=changed):
            self.action('delete')
        self.assertEqual(self.jobs.call_count, before)

    def test_lifecycle_capability_revoked_during_confirmation_cannot_queue(self):
        self.enable_lifecycle('delete')
        def changed(*_args):
            self.window._info_snapshot['more_actions'] = []
            return False
        before = self.jobs.call_count
        with patch('maxclient.ui.main_window.confirm_conversation_removal', side_effect=changed):
            self.action('delete')
        self.assertEqual(self.jobs.call_count, before)

    def test_confirmed_delete_closes_only_its_view(self):
        self.enable_lifecycle('delete')
        with patch('maxclient.ui.main_window.confirm_conversation_removal', return_value=False):
            self.action('delete')
        self.job(self.service.delete_conversation).kwargs['on_done'](
            {'context': self.context, 'accepted': True, 'confirmed': True, 'action': 'delete'})
        self.assertIsNone(self.window._info_context)
        self.assertIsNone(self.window.chat_view.chat)
        self.assertEqual(self.window.statusBar().currentMessage(), 'Чат удалён.')

    def test_unconfirmed_delete_does_not_close_view_or_claim_success(self):
        self.enable_lifecycle('delete')
        with patch('maxclient.ui.main_window.confirm_conversation_removal', return_value=False):
            self.action('delete')
        self.service.pending = True
        self.job(self.service.delete_conversation).kwargs['on_done'](
            {'context': self.context, 'accepted': True, 'confirmed': False, 'pending': True})
        self.assertIs(self.window._info_context, self.context)
        self.assertTrue(self.window._info_panel._action_pending)
        self.assertNotEqual(self.window.statusBar().currentMessage(), 'Чат удалён.')

    def test_readback_of_delete_closes_view_only_on_matching_confirmed_result(self):
        self.action('refresh_actions')
        self.job(self.service.refresh_conversation_controls).kwargs['on_done']({
            'context': self.context, 'lifecycle_result': {
                'context': self.context, 'action': 'delete', 'confirmed': True}})
        self.assertIsNone(self.window._info_context)
        self.assertIsNone(self.window.chat_view.chat)

    def test_acknowledged_delete_pending_shows_readback_notice_not_refusal(self):
        self.enable_lifecycle('delete')
        with patch('maxclient.ui.main_window.confirm_conversation_removal', return_value=False):
            self.action('delete')
        self.service.pending = True
        with patch.object(self.window._info_panel, 'set_action_pending') as notice:
            self.job(self.service.delete_conversation).kwargs['on_done']({
                'context': self.context, 'chat_id': self.context.chat_id, 'action': 'delete',
                'accepted': True, 'confirmed': False, 'pending': True})
        self.assertIn('MAX принял команду', notice.call_args.args[2])
        self.assertIn('Проверить состояние', notice.call_args.args[2])
        self.assertIs(self.window._info_context, self.context)
        self.service.delete_conversation.assert_not_called()

    def test_pending_notice_rejects_wrong_or_unacknowledged_result(self):
        from maxclient.ui.conversation_management_dialogs import accepted_lifecycle_pending_notice
        valid = dict(context=self.context, chat_id=self.context.chat_id, action='delete',
                     accepted=True, confirmed=False, pending=True)
        for change in ({'context': object()}, {'chat_id': -999}, {'chat_id': True},
                       {'accepted': False}, {'confirmed': True}, {'pending': False},
                       {'action': 'share_contact'}, {'lifecycle_kind': 'bot_delete'}):
            with self.subTest(change=change):
                self.assertIsNone(accepted_lifecycle_pending_notice({**valid, **change}, self.context, 'delete'))
        self.assertIsNotNone(accepted_lifecycle_pending_notice(
            {'lifecycle_result': valid}, self.context, 'refresh_actions'))

    def test_share_contact_cancel_does_not_queue_send(self):
        self.enable_lifecycle('share_contact')
        before = self.jobs.call_count
        with patch('maxclient.ui.main_window.ShareContactDialog') as factory:
            factory.return_value.exec.return_value = QDialog.DialogCode.Rejected
            factory.return_value.selected_chat_id = None
            self.action('share_contact')
        self.assertEqual(self.jobs.call_count, before)

    def test_share_has_no_picker_or_send_when_no_verified_writable_recipients(self):
        self.enable_lifecycle('share_contact')
        self.service.share_conversation_destinations = Mock(return_value=[])
        before = self.jobs.call_count
        with patch('maxclient.ui.main_window.ShareContactDialog') as factory:
            self.action('share_contact')
            factory.assert_not_called()
        self.assertEqual(self.jobs.call_count, before)
        self.assertEqual(self.window.statusBar().currentMessage(), 'Нет доступных чатов для отправки контакта.')

    def test_share_rejects_destination_not_in_verified_picker_snapshot(self):
        self.enable_lifecycle('share_contact')
        before = self.jobs.call_count
        with patch('maxclient.ui.main_window.ShareContactDialog') as factory:
            factory.return_value.exec.return_value = QDialog.DialogCode.Accepted
            factory.return_value.selected_chat_id = 987654321
            self.action('share_contact')
        self.assertEqual(self.jobs.call_count, before)

    def test_share_contact_explicit_destination_is_captured(self):
        self.enable_lifecycle('share_contact')
        with patch('maxclient.ui.main_window.ShareContactDialog') as factory:
            factory.return_value.exec.return_value = QDialog.DialogCode.Accepted
            factory.return_value.selected_chat_id = 303
            self.action('share_contact')
        job = self.job(self.service.share_conversation_contact)
        self.assertEqual(job.args[:2], (self.service.share_conversation_contact, self.context))
        self.assertEqual(job.args[2].chat_id, 303)
        self.assertIs(job.args[2].auth, self.context.auth)
        self.service.share_conversation_contact.assert_not_called()

    def test_share_picker_account_change_cannot_capture_new_destination(self):
        self.enable_lifecycle('share_contact')
        before = self.jobs.call_count
        with patch('maxclient.ui.main_window.ShareContactDialog') as factory:
            def changed():
                self.service.context = object()
                return QDialog.DialogCode.Accepted
            factory.return_value.exec.side_effect = changed
            factory.return_value.selected_chat_id = 303
            self.action('share_contact')
        self.assertEqual(self.jobs.call_count, before)

    def test_mute_unknown_cached_capability_can_offer_explicit_duration(self):
        self.service.conversation_action_status = Mock(return_value={
            'context': self.context, 'available': False, 'mute_transport_available': True,
            'muted': None, 'pending': False})
        self.window._poll_info_action_state()
        self.assertTrue(self.window._info_panel.buttons['mute'].isEnabled())
        before = self.jobs.call_count
        self.window._info_panel.buttons['mute'].click()
        self.assertEqual(self.jobs.call_count, before)
        self.assertIsNotNone(self.window._info_panel._menu)
        self.window._info_panel._menu.cancel()

    def test_late_profile_and_folder_refresh_preserve_inflight_guard(self):
        profile = self.job(self.service.refresh_conversation)
        self.action('mute', -1)
        profile.kwargs['on_done'](self.service.conversation_snapshot(self.context))
        self.assertTrue(self.window._info_panel._action_pending)
        self.folders([])
        self.assertTrue(self.window._info_panel._action_pending)

    def test_cached_state_tick_changes_sound_without_requests_or_render(self):
        before = self.jobs.call_count
        self.service.muted = True
        with patch.object(self.window._info_panel, 'set_conversation') as render:
            self.window._poll_info_action_state()
            render.assert_not_called()
        self.assertTrue(self.window._info_panel._snapshot['muted'])
        self.assertEqual(self.jobs.call_count, before)

    def test_disabled_unknown_and_mismatching_value_cannot_dispatch(self):
        before = self.jobs.call_count
        for action, value in [('delete', None), ('missing', None), ('hide_stories', False)]:
            self.action(action, value)
        self.assertEqual(self.jobs.call_count, before)

    def test_block_requires_confirmation_then_exact_context_peer_path(self):
        before = self.jobs.call_count
        self.action('block')
        self.assertEqual(self.jobs.call_count, before)
        self.confirm.return_value = True
        self.action('block')
        self.assertEqual(self.job(self.service.set_conversation_contact_blocked).args,
                         (self.service.set_conversation_contact_blocked, self.context, True))

    def test_account_changed_during_confirmation_does_not_queue_write(self):
        def changed(*_args):
            self.service.context = object()
            return True
        self.confirm.side_effect = changed
        before = self.jobs.call_count
        self.action('block')
        self.assertEqual(self.jobs.call_count, before)

    def test_folder_checkboxes_are_explicit_include_not_filter_membership(self):
        children = self.folders([
            {'id': 'all.chat.folder', 'title': 'All', 'include': [303]},
            {'id': 'suggested', 'title': 'Suggested', 'options': 16, 'include': []},
            {'id': 'explicit', 'title': 'Explicit', 'include': [303]},
            {'id': 'hidden', 'title': 'Hidden', 'hidden': True, 'include': []},
            {'id': 'unknown', 'title': 'Unknown', 'hidden': None, 'include': []},
        ])
        selectable = [row for row in children if row['action'] == 'folder_include']
        self.assertEqual([row['label'] for row in selectable], ['Suggested', 'Explicit'])
        self.assertEqual([row['checked'] for row in selectable], [False, True])
        self.action('folder_include', selectable[0]['value'])
        self.assertEqual(self.job(self.service.set_conversation_folder_included).args,
                         (self.service.set_conversation_folder_included, self.context, 'suggested', True))
        self.service.set_conversation_folder_included.assert_not_called()

    def test_pending_folder_snapshot_disables_membership_changes(self):
        children = self.folders([{'id': 'a', 'title': 'A', 'include': []}], writePending=True)
        entry = children[0]
        self.assertFalse(entry['available'])
        before = self.jobs.call_count
        self.action('folder_include', entry['value'])
        self.assertEqual(self.jobs.call_count, before)

    def test_folder_changes_invalidate_choices_and_ignore_old_read(self):
        old_read = self.job(self.service.get_conversation_folders).kwargs['on_done']
        self.folders([{'id': 'a', 'title': 'Old', 'include': []}])
        complete = {'folderSync': 7, 'foldersOrder': ['b'],
                    'orderedFolders': [{'id': 'b', 'title': 'New', 'include': []}]}
        self.window._on_folders(complete)
        new_read = self.job(self.service.get_conversation_folders).kwargs['on_done']
        self.assertIsNot(new_read, old_read)
        old_read({'orderedFolders': [{'id': 'a', 'title': 'Old', 'include': []}]})
        self.assertIsNone(self.window._info_folder_rows)
        new_read(complete)
        self.assertEqual(self.window._info_folder_rows[0]['label'], 'New')

    def test_malformed_folder_result_offers_explicit_retry(self):
        self.job(self.service.get_conversation_folders).kwargs['on_done'](None)
        self.assertEqual(self.window._info_folder_rows[0]['action'], 'folder_retry')
        before = self.jobs.call_count
        self.action('folder_retry')
        self.assertEqual(self.jobs.call_count, before + 1)

    def test_manual_refresh_queues_only_bound_read_path(self):
        self.service.pending = True
        self.window._info_panel.set_conversation(self.window._info_with_avatar(
            self.service.conversation_snapshot(self.context)))
        self.action('refresh_actions')
        self.assertEqual(self.job(self.service.refresh_conversation_controls).args,
                         (self.service.refresh_conversation_controls, self.context))
        self.service.set_conversation_mute.assert_not_called()

    def test_error_does_not_echo_private_details(self):
        self.action('hide_stories', True)
        self.job(self.service.set_conversation_stories_hidden).kwargs['on_error']('PRIVATE TOKEN SECRET')
        self.assertNotIn('PRIVATE', self.window._info_panel.status.text())
        self.assertIn('Не удалось', self.window._info_panel.status.text())

    def test_owner_changed_or_window_destroyed_ignores_action_completion(self):
        self.action('mute', -1)
        callback = self.job(self.service.set_conversation_mute).kwargs['on_done']
        self.service.context = object()
        with patch.object(self.window._info_panel, 'set_conversation') as render:
            callback({})
            render.assert_not_called()
        self.window.deleteLater()
        QCoreApplication.sendPostedEvents(self.window, QEvent.Type.DeferredDelete)
        self.assertFalse(isValid(self.window))
        callback({})

    def test_create_folder_navigates_without_mutation(self):
        self.folders([])
        workspace = Mock()
        self.window._settings_workspace = workspace
        with patch.object(self.window, '_open_settings') as opening:
            self.action('folder_create')
        opening.assert_called_once_with()
        workspace.select_page.assert_called_once_with('folders')
        self.service.set_conversation_folder_included.assert_not_called()

    def test_rail_folder_callback_rejects_old_owner_closed_and_deleted_window(self):
        snapshot = {'folderSync': 7, 'foldersOrder': ['b'],
                    'orderedFolders': [{'id': 'b', 'title': 'New', 'include': []}]}
        self.window._on_folders(snapshot, context=object())
        self.assertFalse(self.window._folders)
        self.window.close()
        self.window._on_folders(snapshot)
        self.assertFalse(self.window._folders)
        self.window.deleteLater()
        QCoreApplication.sendPostedEvents(self.window, QEvent.Type.DeferredDelete)
        self.assertFalse(isValid(self.window))
        self.window._on_folders(snapshot)


if __name__ == '__main__': unittest.main()
