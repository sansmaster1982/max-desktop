"""Contact-only navigation/actions: synthetic identities, no RPC or account files."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from PySide6.QtCore import QCoreApplication, QEvent, QPoint, Qt
from PySide6.QtWidgets import QApplication

from maxclient.core.config import Settings
from maxclient.core.models import Chat, Contact
from maxclient.ui.main_window import MainWindow
from maxclient.ui.contact_profile import ContactEditPanel, contact_menu_entries
from test_settings_workspace import SyntheticService


class ContactService(SyntheticService):
    def __init__(self):
        super().__init__()
        self.auth = object()
        self.contacts = [Contact(2**40+1, 'Alice', '+70000000001'), Contact(2**40+2, 'Bob')]
        self.server_added = {row.id for row in self.contacts}
        self.fresh = set()
        self.pending = False
        self.blocked = set()
        self.local_hidden = set()
        self.local_actions = []
        self.writes = []
        self.dialog_reads = []

    def search_context(self):
        return self.auth

    def list_contacts(self, query=''):
        return [row for row in self.contacts if row.id not in self.local_hidden and query.lower() in row.name.lower()]

    def contact_context(self, identity):
        return SimpleNamespace(contact_id=identity, session_context=self.auth)

    def is_current_contact(self, context):
        return context.session_context is self.auth

    def dialog_for_contact(self, identity):
        return next((row for row in self.chats if row.peer_user_id == identity), None)

    def contact_snapshot(self, context):
        identity = context.contact_id
        person = next((row for row in self.contacts if row.id == identity), Contact(identity, 'Removed'))
        fresh = identity in self.fresh
        added = identity in self.server_added
        chat = self.dialog_for_contact(identity)
        return {'context': context, 'contact_id': identity, 'title': person.name,
                'first_name': person.name, 'last_name': '', 'phone': person.phone,
                'is_added': added, 'fresh': fresh, 'pending': self.pending,
                'is_local_hidden': identity in self.local_hidden,
                'blocked': identity in self.blocked, 'chat_id': chat.id if chat else None,
                'availability': {'edit': fresh and added and not self.pending,
                                 'delete': fresh and added and not self.pending,
                                 'hide_local': fresh and not added and identity not in self.local_hidden and not self.pending,
                                 'add': fresh and not added and not self.pending,
                                 'block': fresh and identity not in self.blocked and not self.pending,
                                 'unblock': fresh and identity in self.blocked and not self.pending,
                                 'chat': True}}

    def open_contact_dialog(self, context):
        if not self.is_current_contact(context):
            raise ValueError('Session changed')
        self.dialog_reads.append(context)
        chat = self.dialog_for_contact(context.contact_id)
        return {'context': context, 'chat': chat or Chat(9001, 'Alice', peer_user_id=context.contact_id),
                'local_draft': chat is None}

    def refresh_contact_profile(self, context):
        self.fresh.add(context.contact_id)
        return self.contact_snapshot(context)

    def rename_contact_profile(self, context, first, last=''):
        self.writes.append(('edit', context.contact_id, first, last))
        person = next(row for row in self.contacts if row.id == context.contact_id)
        person.name = first + (' '+last if last else '')
        return self.contact_snapshot(context)

    def delete_contact_profile(self, context):
        self.writes.append(('delete', context.contact_id))
        self.contacts = [row for row in self.contacts if row.id != context.contact_id]
        self.server_added.discard(context.contact_id)
        return self.contact_snapshot(context)

    def add_contact_profile(self, context):
        self.writes.append(('add', context.contact_id))
        self.server_added.add(context.contact_id)
        self.local_hidden.discard(context.contact_id)
        return self.contact_snapshot(context)

    def hide_contact_profile(self, context):
        if not self.is_current_contact(context) or not self.contact_snapshot(context)['availability']['hide_local']:
            raise ValueError('Local action unavailable')
        self.local_actions.append(('hide', context.contact_id))
        self.local_hidden.add(context.contact_id)
        return self.contact_snapshot(context)

    def set_contact_profile_blocked(self, context, value):
        self.writes.append(('block', context.contact_id, value))
        if value:
            self.blocked.add(context.contact_id)
        else:
            self.blocked.discard(context.contact_id)
        return self.contact_snapshot(context)


class ContactProfileUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.service = ContactService()
        self.queue = []
        def queued(function, *args, on_done=None, on_error=None, **kwargs):
            self.queue.append((function, args, kwargs, on_done, on_error))
        self.patches = [patch('maxclient.ui.main_window.run_async'),
                        patch('maxclient.ui.chat_view.run_async'),
                        patch('maxclient.ui.contact_profile.run_async', side_effect=queued),
                        patch('maxclient.ui.widgets.avatar.Avatar.set_photo'),
                        patch.object(Settings, 'save')]
        for item in self.patches:
            item.start()
        self.window = MainWindow(self.service, Settings())
        self.window.show()
        self.window._open_contacts()
        self.app.processEvents()
        self.alice, self.bob = [row.id for row in self.service.contacts]

    def tearDown(self):
        self.window.close()
        self.window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()
        for item in reversed(self.patches):
            item.stop()

    def finish(self, index=0):
        function, args, kwargs, done, error = self.queue.pop(index)
        try:
            result = function(*args, **kwargs)
        except Exception as exc:
            if error:
                error(str(exc))
            else:
                raise
        else:
            if done:
                done(result)

    def select(self, identity):
        self.window._select_contact(identity, 'Untrusted title')
        return self.window._contact_context

    def test_missing_dialog_clears_old_recipient_body_and_saves_draft(self):
        self.window.chat_view.open_chat(self.service.chats[0])
        self.window.chat_view.input.setPlainText('unfinished')
        self.window._current_chat_id = 1
        context = self.select(self.alice)
        self.assertIsNone(self.window._current_chat_id)
        self.assertIsNone(self.window.chat_view.chat)
        self.assertEqual(self.window.chat_view._drafts[1], 'unfinished')
        self.assertEqual(self.window._contact_panel.name.text(), 'Alice')
        self.assertIs(self.window._conversation_pages.currentWidget(), self.window._contact_panel)
        self.assertFalse(self.window._contact_panel.edit_button.isEnabled())
        self.assertIs(self.window._sidebar_pages.currentWidget(), self.window._contacts_workspace)
        self.assertEqual(context.contact_id, self.alice)
        self.assertEqual(self.service.writes, [])

    def test_known_dialog_keeps_contacts_sidebar_and_real_chat_id(self):
        actual = Chat(819, 'Actual', peer_user_id=self.alice)
        self.service.chats.append(actual)
        self.window.chat_view.open_chat = Mock()
        self.select(self.alice)
        self.assertEqual(self.window._current_chat_id, 819)
        self.window.chat_view.open_chat.assert_called_once_with(actual)
        self.assertIs(self.window._sidebar_pages.currentWidget(), self.window._contacts_workspace)
        self.assertEqual(self.queue, [])

    def test_failed_contact_capture_clears_previous_chat_without_rebinding(self):
        self.window.chat_view.open_chat(self.service.chats[0])
        self.window._current_chat_id = 1
        self.service.contact_context = Mock(side_effect=ValueError('Session changed'))
        self.service.dialog_for_contact = Mock()
        self.assertFalse(self.window._select_contact(self.alice, 'Alice'))
        self.assertIsNone(self.window.chat_view.chat)
        self.assertIsNone(self.window._current_chat_id)
        self.service.dialog_for_contact.assert_not_called()
        self.assertEqual(self.queue, [])

    def test_explicit_write_opens_resolved_empty_dialog_without_add_or_send(self):
        context = self.select(self.alice)
        self.finish()
        self.window.chat_view.open_chat = Mock()
        self.window._contact_action('chat', context)
        self.assertEqual(self.service.dialog_reads, [])
        self.assertEqual(len(self.queue), 1)
        self.finish()
        self.assertEqual(self.service.dialog_reads, [context])
        opened = self.window.chat_view.open_chat.call_args.args[0]
        self.assertEqual(opened.peer_user_id, self.alice)
        self.assertEqual(self.window._current_chat_id, 9001)
        self.assertEqual(self.window._contact_local_draft_id, 9001)
        self.assertEqual(self.service.writes, [])
        self.assertIs(self.window._conversation_pages.currentWidget(), self.window.chat_view)
        self.assertIs(self.window._sidebar_pages.currentWidget(), self.window._contacts_workspace)
        self.window._open_chat_header_info(opened)
        self.assertIs(self.window._conversation_pages.currentWidget(), self.window._contact_panel)

    def test_pending_dialog_open_cannot_navigate_after_selection_changes(self):
        context = self.select(self.alice)
        self.finish()
        self.window._contact_action('chat', context)
        self.select(self.bob)
        self.window.chat_view.open_chat = Mock()
        self.finish()
        self.window.chat_view.open_chat.assert_not_called()
        self.assertEqual(self.window._contact_panel.name.text(), 'Bob')
        self.assertIsNone(self.window._current_chat_id)

    def test_dialog_open_rejects_wrong_result_identity_and_allows_retry(self):
        context = self.select(self.alice)
        self.finish()
        self.window._contact_action('chat', context)
        job = self.queue.pop()
        job[3]({'context': context, 'chat': Chat(19, 'Wrong', peer_user_id=self.bob), 'local_draft': True})
        self.assertIsNone(self.window._current_chat_id)
        self.assertIsNone(self.window._contact_job)
        self.assertIn('Не удалось открыть', self.window._contact_panel.status.text())

    def test_unadded_and_deferred_controls_have_specific_reasons(self):
        self.service.server_added.remove(self.alice)
        self.select(self.alice)
        self.finish()
        self.assertIn('Добавить в контакты', self.window._contact_panel.edit_button.toolTip())
        self.assertIn('не реализованы', self.window._contact_panel.buttons['call'].toolTip())
        rows = contact_menu_entries(self.window._contact_snapshot)
        self.assertIn('Добавить в контакты', next(row for row in rows if row['action'] == 'delete')['unavailable_reason'])

    def test_dialog_failure_logs_only_allowlisted_stage_and_exception_class(self):
        context = self.select(self.alice)
        self.finish()
        self.service.log = Mock()
        error = ValueError('secret-token-123 response with phone +70000000001')
        error.contact_dialog_stage = 'parse48'
        self.service.open_contact_dialog = Mock(side_effect=error)
        self.window._contact_action('chat', context)
        self.finish()
        self.service.log.assert_called_once_with(
            'contact-dialog: stage=parse48; error=ValueError; category=operation_failed')
        self.assertIsNone(self.window._current_chat_id)
        self.assertIn('Не удалось открыть', self.window._contact_panel.status.text())
        self.assertNotIn('secret', self.window._contact_panel.status.text())

    def test_dialog_failure_sanitizes_unknown_class_and_stage(self):
        context = self.select(self.alice)
        self.finish()
        self.service.log = Mock()
        error = type('SecretAccountToken', (Exception,), {})('private-content')
        error.contact_dialog_stage = 'secret-id-and-token'
        self.service.open_contact_dialog = Mock(side_effect=error)
        self.window._contact_action('chat', context)
        self.finish()
        self.service.log.assert_called_once_with(
            'contact-dialog: stage=unknown; error=OtherError; category=operation_failed')

    def test_fresh_profile_enables_edit_delete_and_block(self):
        context = self.select(self.alice)
        self.finish()
        self.assertTrue(self.window._contact_panel.edit_button.isEnabled())
        self.assertEqual(self.window._contact_panel.phone.text(), '+70000000001')
        rows = contact_menu_entries(self.window._contact_snapshot)
        self.assertEqual([row['label'] for row in rows],
                         ['Написать','Позвонить','Видеозвонок','Заблокировать','Удалить контакт'])
        self.assertTrue(rows[-1]['available'])
        self.assertTrue(rows[-2]['available'])
        self.assertFalse(rows[1]['available'])
        self.assertEqual(self.service.writes, [])

    def test_late_profile_result_does_not_replace_selected_person(self):
        old = self.select(self.alice)
        current = self.select(self.bob)
        self.finish(0)
        self.assertIs(self.window._contact_context, current)
        self.assertEqual(self.window._contact_panel.name.text(), 'Bob')
        self.finish(0)
        self.assertEqual(self.window._contact_panel.name.text(), 'Bob')
        self.assertFalse(self.window._contact_current(old))

    def test_account_switch_discards_result_and_contact_actions(self):
        context = self.select(self.alice)
        self.service.auth = object()
        self.finish()
        self.window._contact_action('block', context)
        self.assertEqual(self.service.writes, [])
        self.window._on_state(self.service.client.state)
        self.assertIsNone(self.window._contact_context)
        self.assertIsNone(self.window.chat_view.chat)

    def test_row_menu_captures_right_clicked_person_without_history(self):
        self.window.chat_view.open_chat(self.service.chats[0])
        self.window._contact_context_menu(self.bob, QPoint(100,100))
        context = self.window._contact_context
        self.assertEqual(context.contact_id, self.bob)
        self.assertIsNone(self.window.chat_view.chat)
        self.finish()
        menu = self.window._contact_row_menu
        self.assertEqual([a.text() for a in menu.actions() if not a.isSeparator()],
                         ['Написать','Позвонить','Видеозвонок','Заблокировать','Удалить контакт'])
        self.assertTrue(next(a for a in menu.actions() if a.text() == 'Удалить контакт').isEnabled())
        self.assertEqual(self.service.writes, [])

    def test_cancelled_delete_does_not_write(self):
        context = self.select(self.alice)
        self.finish()
        with patch('maxclient.ui.contact_profile.confirm_action', return_value=False):
            self.window._contact_action('delete', context)
        self.assertEqual(self.queue, [])
        self.assertEqual(self.service.writes, [])

    def test_block_and_unblock_apply_to_selected_id_only(self):
        context = self.select(self.bob)
        self.finish()
        with patch('maxclient.ui.contact_profile.confirm_action', return_value=True):
            self.window._contact_action('block', context)
        self.assertEqual(len(self.queue), 1)
        self.window._contact_action('block', context)
        self.assertEqual(len(self.queue), 1)
        self.finish()
        self.assertEqual(self.service.writes, [('block',self.bob,True)])
        self.assertEqual(contact_menu_entries(self.window._contact_snapshot)[3]['label'], 'Разблокировать')
        with patch('maxclient.ui.contact_profile.confirm_action', return_value=True):
            self.window._contact_action('unblock', context)
        self.finish()
        self.assertEqual(self.service.writes[-1], ('block',self.bob,False))

    def test_confirmation_account_change_rejects_write(self):
        context = self.select(self.alice)
        self.finish()
        def changed(*_args):
            self.service.auth = object()
            return True
        with patch('maxclient.ui.contact_profile.confirm_action', side_effect=changed):
            self.window._contact_action('delete', context)
        self.assertEqual(self.queue, [])

    def test_embedded_edit_requires_explicit_save_and_preserves_contact_scope(self):
        context = self.select(self.alice)
        self.finish()
        self.window._contact_action('edit', context)
        panel = self.window._contact_edit_panel
        self.assertIsInstance(panel, ContactEditPanel)
        self.assertIs(self.window._conversation_pages.currentWidget(), panel)
        self.assertFalse(panel.save_button.isVisible())
        panel.first_name.setText('New Alice')
        self.assertTrue(panel.save_button.isEnabled())
        self.assertEqual(self.queue, [])
        panel._save()
        self.assertEqual(len(self.queue), 1)
        panel._save()
        self.assertEqual(len(self.queue), 1)
        self.finish()
        self.assertEqual(self.service.writes, [('edit',self.alice,'New Alice','')])
        self.assertEqual(self.window._contact_panel.name.text(), 'New Alice')
        self.assertIs(self.window._conversation_pages.currentWidget(), self.window._contact_panel)

    def test_edit_empty_first_name_and_stale_signals_do_not_write(self):
        context = self.select(self.alice)
        self.finish()
        self.window._contact_action('edit', context)
        panel = self.window._contact_edit_panel
        panel.first_name.setText(' ')
        panel._save()
        self.assertEqual(self.queue, [])
        self.select(self.bob)
        panel.submitted.emit('Wrong contact', '', context)
        self.assertEqual(len(self.queue), 1)  # only Bob profile read
        self.assertEqual(self.service.writes, [])

    def test_rejected_name_save_retains_editor_values_and_explains_failure(self):
        context = self.select(self.alice)
        self.finish()
        self.window._contact_action('edit', context)
        panel = self.window._contact_edit_panel
        panel.first_name.setText('Unsaved Alice')
        self.service.rename_contact_profile = Mock(side_effect=ValueError('Rejected before write'))
        panel._save()
        self.finish()
        self.assertIs(self.window._conversation_pages.currentWidget(), panel)
        self.assertEqual(panel.first_name.text(), 'Unsaved Alice')
        self.assertTrue(panel.save_button.isEnabled())
        self.assertIn('Не удалось сохранить', panel.status.text())
        self.assertEqual(self.service.writes, [])

    def test_unknown_write_only_offers_readback_and_does_not_repeat(self):
        context = self.select(self.alice)
        self.finish()
        def unknown(owner):
            self.service.writes.append(('delete', owner.contact_id))
            self.service.pending = True
            raise RuntimeError('Unknown outcome')
        self.service.delete_contact_profile = unknown
        with patch('maxclient.ui.contact_profile.confirm_action', return_value=True):
            self.window._contact_action('delete', context)
        self.finish()
        self.window._contact_action('delete', context)
        self.assertEqual(self.queue, [])
        self.assertTrue(self.window._contact_snapshot['pending'])
        self.window._contact_action('refresh', context)
        self.finish()
        self.assertEqual(self.service.writes, [('delete',self.alice)])

    def test_delete_refreshes_contacts_without_deleting_chat(self):
        self.service.chats.append(Chat(819,'Actual',peer_user_id=self.alice))
        self.window._contact_context_menu(self.alice, QPoint(100,100))
        context = self.window._contact_context
        self.finish()
        with patch('maxclient.ui.contact_profile.confirm_action', return_value=True):
            self.window._contact_action('delete', context)
        self.finish()
        self.assertEqual(self.window._contacts_workspace.list.count(), 1)
        self.assertEqual(self.service.dialog_for_contact(self.alice).id, 819)
        self.assertEqual(self.service.writes, [('delete',self.alice)])

    def test_profile_more_never_calls_delete_contact_as_delete_chat(self):
        self.select(self.alice)
        self.finish()
        rows = contact_menu_entries(self.window._contact_snapshot, profile=True)
        self.assertNotIn('Удалить контакт', [row['label'] for row in rows])
        self.assertFalse(next(row for row in rows if row['label']=='Удалить чат')['available'])
        self.window._contact_action('delete_chat', self.window._contact_context)
        self.assertEqual(self.queue, [])

    def test_header_keeps_full_existing_conversation_info(self):
        chat = Chat(819,'Actual',peer_user_id=self.alice)
        self.service.chats.append(chat)
        self.select(self.alice)
        self.window._open_conversation_info = Mock()
        self.window._open_chat_header_info(chat)
        self.window._open_conversation_info.assert_called_once_with(chat)

    def test_contact_editor_from_existing_conversation_reads_before_editing(self):
        self.window._edit_conversation_contact(self.alice)
        self.assertIsNone(self.window._contact_edit_panel)
        self.finish()
        self.assertIs(self.window._conversation_pages.currentWidget(), self.window._contact_edit_panel)
        self.assertEqual(self.service.writes, [])

    def test_lookup_only_card_adds_only_after_explicit_action_and_confirmation(self):
        self.service.server_added.remove(self.alice)
        context = self.select(self.alice)
        self.assertEqual(self.service.writes, [])
        self.assertFalse(self.window._contact_panel.add_button.isEnabled())
        self.finish()
        panel = self.window._contact_panel
        self.assertTrue(panel.add_button.isEnabled())
        self.assertFalse(panel.edit_button.isEnabled())
        self.assertFalse(next(row for row in contact_menu_entries(self.window._contact_snapshot)
                              if row['action'] == 'delete')['available'])
        self.assertEqual(self.service.writes, [])
        panel.add_button.click()
        self.assertEqual(len(self.queue), 1)
        panel.add_button.click()
        self.assertEqual(len(self.queue), 1)
        self.assertFalse(panel.edit_button.isEnabled())
        self.finish()
        self.assertEqual(self.service.writes, [('add', self.alice)])
        self.assertTrue(panel.edit_button.isEnabled())
        self.assertTrue(next(row for row in contact_menu_entries(self.window._contact_snapshot)
                             if row['action'] == 'delete')['available'])
        self.assertTrue(panel.add_button.isHidden())

    def test_late_add_button_of_previous_contact_cannot_dispatch(self):
        self.service.server_added.remove(self.alice)
        old = self.select(self.alice)
        self.finish()
        current = self.select(self.bob)
        self.window._contact_panel.add_requested.emit(old)
        self.assertEqual(len(self.queue), 1)  # only Bob read
        self.finish()
        self.assertEqual(self.service.writes, [])
        self.assertIs(self.window._contact_context, current)

    def test_local_capability_update_preserves_form_but_revokes_edit(self):
        context = self.select(self.alice)
        self.finish()
        self.window._contact_action('edit', context)
        editor = self.window._contact_edit_panel
        editor.first_name.setText('Draft Alice')
        self.service.server_added.remove(self.alice)
        self.window._refresh_contact_profile_state()
        self.assertEqual(editor.first_name.text(), 'Draft Alice')
        self.assertFalse(editor.save_button.isEnabled())
        self.assertFalse(editor.delete_button.isEnabled())
        editor._save()
        self.assertEqual(self.queue, [])
        self.assertEqual(self.service.writes, [])

    def test_local_capability_update_disables_open_delete_menu(self):
        self.window._contact_context_menu(self.alice, QPoint(100,100))
        self.finish()
        self.assertTrue(next(a for a in self.window._contact_row_menu.actions()
                             if a.text() == 'Удалить контакт').isEnabled())
        self.service.server_added.remove(self.alice)
        self.window._refresh_contact_profile_state()
        self.assertFalse(next(a for a in self.window._contact_row_menu.actions()
                              if a.text() == 'Удалить контакт').isEnabled())
        self.assertEqual(self.queue, [])

    def test_editor_has_gray_body_and_distinct_white_fields(self):
        from maxclient.ui.theme import build_stylesheet
        previous = self.app.styleSheet()
        try:
            self.app.setStyleSheet(build_stylesheet('light', '#0085FF'))
            context = self.select(self.alice)
            self.finish()
            self.window._contact_action('edit', context)
            editor = self.window._contact_edit_panel
            self.app.processEvents()
            image = editor.grab().toImage()
            self.assertEqual(image.pixelColor(14, 90).name(), '#edeef3')
            self.assertEqual(image.pixelColor(14, 14).name(), '#ffffff')
            point = editor.first_name.mapTo(editor, QPoint(editor.first_name.width()-20, editor.first_name.height()//2))
            self.assertEqual(image.pixelColor(point).name(), '#ffffff')
        finally:
            self.app.setStyleSheet(previous)


if __name__ == '__main__':
    unittest.main()
