"""Local-list removal through real Qt menus with synthetic identities only."""
import unittest
from unittest.mock import Mock, patch

from PySide6.QtCore import QPoint

import test_contact_profile_ui as fixture
from maxclient.core.i18n import SUPPORTED_LANGUAGES, tr, translation_available
from maxclient.core.models import Chat
from maxclient.ui.contact_profile import contact_menu_entries
from maxclient.ui.contacts_dialog import ContactsDialog


LABEL = 'Убрать из локального списка'
CONFIRM = 'Убрать «{name}» из локального списка этого приложения? Переписка сохранится.'
SUCCESS = 'Контакт убран из локального списка. Переписка сохранена.'
FAILURE = 'Не удалось убрать контакт из локального списка. Попробуйте ещё раз.'


class LocalContactVisibilityUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture.ContactProfileUiTests.setUpClass()

    def setUp(self):
        self.case = fixture.ContactProfileUiTests()
        self.case.setUp()
        self.addCleanup(self.case.tearDown)
        self.window, self.service = self.case.window, self.case.service
        self.alice, self.bob = self.case.alice, self.case.bob
        self.service.server_added.discard(self.alice)

    def open_row(self):
        self.window._contact_context_menu(self.alice, QPoint(100, 100))
        context = self.window._contact_context
        self.case.finish()
        return context

    def local_action(self, menu, language='ru'):
        return next(action for action in menu.actions() if action.text() == tr(LABEL, language))

    def test_row_action_hides_only_local_list_and_refreshes_visible_manager(self):
        chat = Chat(819, 'Conversation retained', peer_user_id=self.alice)
        self.service.chats.append(chat)
        context = self.open_row()
        menu = self.window._contact_row_menu
        action = self.local_action(menu)
        self.assertTrue(action.isEnabled())
        self.assertFalse(next(row for row in menu.actions() if row.text() == 'Удалить контакт').isEnabled())
        with patch('maxclient.ui.contact_profile.confirm_action', return_value=True) as confirm:
            action.trigger()
        self.assertEqual(confirm.call_args.args,
                         (self.window, LABEL, tr(CONFIRM, name='Alice')))
        self.assertEqual(len(self.case.queue), 1)
        self.assertEqual(self.case.queue[0][0].__name__, 'hide_contact_profile')
        manager = ContactsDialog(self.service, self.window)
        manager.show()
        self.case.app.processEvents()
        self.assertEqual(manager.list.count(), 2)
        self.window._contact_action('hide_local', context)
        self.assertEqual(len(self.case.queue), 1)
        self.case.finish()
        self.assertEqual(self.window._contacts_workspace.list.count(), 1)
        self.assertEqual(manager.list.count(), 1)
        self.assertEqual(self.service.local_actions, [('hide', self.alice)])
        self.assertEqual(self.service.writes, [])
        self.assertEqual(self.service.dialog_for_contact(self.alice), chat)
        self.assertEqual(len(self.service.contacts), 2)
        self.assertEqual(self.window._contact_panel.status.text(), SUCCESS)
        self.assertNotIn('hide_local', [row['action'] for row in contact_menu_entries(self.window._contact_snapshot)])

    def test_profile_more_menu_dispatches_same_local_operation(self):
        self.case.select(self.alice)
        self.case.finish()
        menu = self.window._contact_panel.open_more_menu()
        with patch('maxclient.ui.contact_profile.confirm_action', return_value=True):
            self.local_action(menu).trigger()
        self.case.finish()
        self.assertEqual(self.service.local_actions, [('hide', self.alice)])
        self.assertEqual(self.service.writes, [])
        reopened = self.window._contact_panel.open_more_menu()
        self.assertNotIn(LABEL, [action.text() for action in reopened.actions()])

    def test_cancelled_confirmation_keeps_row_and_sends_nothing(self):
        context = self.open_row()
        with patch('maxclient.ui.contact_profile.confirm_action', return_value=False):
            self.window._contact_action('hide_local', context)
        self.assertEqual(self.case.queue, [])
        self.assertEqual(self.service.local_hidden, set())
        self.assertEqual(self.window._contacts_workspace.list.count(), 2)
        self.assertEqual(self.service.writes, [])

    def test_session_change_during_confirmation_cannot_queue_old_action(self):
        context = self.open_row()

        def change_session(*_args):
            self.service.auth = object()
            return True

        with patch('maxclient.ui.contact_profile.confirm_action', side_effect=change_session):
            self.window._contact_action('hide_local', context)
        self.assertEqual(self.case.queue, [])
        self.assertEqual(self.service.local_actions, [])
        self.assertEqual(self.service.writes, [])

    def test_busy_pending_and_nonfresh_states_cannot_dispatch_local_action(self):
        context = self.case.select(self.alice)
        with patch('maxclient.ui.contact_profile.confirm_action', return_value=True) as confirm:
            self.window._contact_action('hide_local', context)
            confirm.assert_not_called()
        self.assertEqual(len(self.case.queue), 1)  # Only the original refresh.
        self.case.finish()
        self.service.pending = True
        self.window._refresh_contact_profile_state()
        menu = self.window._contact_panel.open_more_menu()
        self.assertFalse(self.local_action(menu).isEnabled())
        with patch('maxclient.ui.contact_profile.confirm_action', return_value=True) as confirm:
            self.window._contact_action('hide_local', context)
            confirm.assert_not_called()
        self.service.pending = False
        self.service.fresh.discard(self.alice)
        self.window._refresh_contact_profile_state()
        self.assertNotIn('hide_local', [row['action'] for row in contact_menu_entries(self.window._contact_snapshot)])
        self.window._contact_action('hide_local', context)
        self.assertEqual(self.case.queue, [])
        self.assertEqual(self.service.local_actions, [])

    def test_membership_changed_during_confirmation_revokes_local_action(self):
        context = self.open_row()

        def add_elsewhere(*_args):
            self.service.server_added.add(self.alice)
            self.window._refresh_contact_profile_state()
            return True

        with patch('maxclient.ui.contact_profile.confirm_action', side_effect=add_elsewhere):
            self.window._contact_action('hide_local', context)
        self.assertEqual(self.case.queue, [])
        self.assertEqual(self.service.local_actions, [])
        self.assertTrue(self.window._contact_snapshot['availability']['delete'])

    def test_old_queued_worker_and_late_callback_cannot_change_replacement_session(self):
        context = self.open_row()
        with patch('maxclient.ui.contact_profile.confirm_action', return_value=True):
            self.window._contact_action('hide_local', context)
        self.service.auth = object()
        self.window._open_contacts()
        self.case.finish()
        self.assertEqual(self.service.local_actions, [])
        self.assertEqual(self.service.local_hidden, set())
        self.assertEqual(self.window._contacts_workspace.list.count(), 2)
        self.assertEqual(self.service.writes, [])

    def test_failure_preserves_row_and_reports_local_error_without_retry(self):
        context = self.open_row()
        self.service.hide_contact_profile = Mock(side_effect=ValueError('Synthetic local failure'))
        with patch('maxclient.ui.contact_profile.confirm_action', return_value=True):
            self.window._contact_action('hide_local', context)
        self.case.finish()
        self.assertEqual(self.window._contact_panel.status.text(), FAILURE)
        self.assertEqual(self.window._contacts_workspace.list.count(), 2)
        self.assertEqual(self.case.queue, [])
        self.assertEqual(self.service.local_hidden, set())
        self.assertEqual(self.service.writes, [])

    def test_already_hidden_profile_has_no_duplicate_local_action(self):
        self.service.local_hidden.add(self.alice)
        self.case.select(self.alice)
        self.case.finish()
        for profile in (False, True):
            rows = contact_menu_entries(self.window._contact_snapshot, profile=profile)
            self.assertNotIn('hide_local', [row['action'] for row in rows])
        with patch('maxclient.ui.contact_profile.confirm_action', return_value=True) as confirm:
            self.window._contact_action('hide_local', self.window._contact_context)
            confirm.assert_not_called()
        self.assertEqual(self.case.queue, [])

    def test_new_action_confirmation_and_result_follow_selected_language(self):
        self.window.settings.ui_language = 'en'
        self.service.contacts[0].name = '<b>Alice & Bob</b>'
        self.open_row()
        action = self.local_action(self.window._contact_row_menu, 'en')
        with patch('maxclient.ui.contact_profile.confirm_action', return_value=True) as confirm:
            action.trigger()
        self.assertEqual(confirm.call_args.args[1], 'Remove from local list')
        self.assertEqual(confirm.call_args.args[2], tr(CONFIRM, 'en', name='<b>Alice & Bob</b>'))
        self.case.finish()
        self.assertEqual(self.window._contact_panel.status.text(), tr(SUCCESS, 'en'))

    def test_all_supported_languages_cover_new_application_owned_strings(self):
        for language in SUPPORTED_LANGUAGES:
            for source in (LABEL, CONFIRM, SUCCESS, FAILURE):
                with self.subTest(language=language, source=source):
                    self.assertTrue(translation_available(source, language))
                    self.assertIn('Literal {name}', tr(CONFIRM, language, name='Literal {name}'))


if __name__ == '__main__':
    unittest.main()
