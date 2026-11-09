"""Detached chat names for explicit navigation and toasts; synthetic caches only."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import copy
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from PySide6.QtWidgets import QApplication, QWidget

from maxclient.core.models import Chat, Contact, Message
from maxclient.core.config import Settings
from maxclient.ui.desktop_notifications import DesktopNotifications
from maxclient.ui.main_window import MainWindow
import test_contact_lookup_core as fixtures
from test_search_shell import FakeService


class ChatPresentationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ContactLookupCoreTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.s, self.store, self.client = self.fixture.service, self.fixture.store, self.fixture.client
        self.fixture.dialog()
        self.s._remember_contact_info({'contacts': [{'id': 202, 'flags': 512, 'names': [
            {'type': 'ONEME', 'name': 'Public Name'}, {'type': 'CUSTOM', 'name': 'Saved Alias'}]}]})
        self.context = self.s._history_read_context()
        self.client.set_peer = Mock()

    def test_exact_hidden_chat_uses_current_alias_without_unhiding(self):
        self.store.clear_confirmed_history(101, 303, 100, deleted=True)
        self.assertEqual(self.s.list_chats(), [])
        before = self.store.history_boundary(303)
        result = self.s.present_chat(303, context=self.context)
        self.assertEqual((result.id, result.peer_user_id, result.display), (303, 202, 'Saved Alias'))
        self.assertEqual(self.store.history_boundary(303), before)
        self.assertEqual(self.s.list_chats(), [])

    def test_shared_list_presentation_and_exact_lookup_agree_without_cache_writes(self):
        expected = self.s.list_chats()[0]
        self.client.set_peer.reset_mock()
        stored = self.store.get_chat(303)
        raw = copy.deepcopy((self.s._server_chats, self.s._server_contacts))
        changes = self.store._db.total_changes
        result = self.s.present_chat(303, context=self.context)
        self.assertEqual(result, expected)
        self.assertEqual(self.store._db.total_changes, changes)
        self.assertEqual(self.store.get_chat(303), stored)
        self.assertEqual((self.s._server_chats, self.s._server_contacts), raw)
        self.client.set_peer.assert_not_called()
        self.client.request.assert_not_called()
        result.title = 'Detached mutation'
        self.assertEqual(self.s.present_chat(303).title, 'Saved Alias')

    def test_explicit_local_title_and_contact_badge_are_preserved(self):
        self.store.rename_chat(303, 'Local label')
        self.store.upsert_contact(Contact(202, name='Cached', avatar_url='https://invalid.example/avatar',
                                          is_verified=True))
        result = self.s.present_chat(303)
        self.assertEqual(result.title, 'Local label')
        self.assertTrue(result.title_locked)
        self.assertTrue(result.is_verified)
        self.assertEqual(result.avatar_url, 'https://invalid.example/avatar')

    def test_saved_zero_uses_saved_title_and_keeps_explicit_local_override(self):
        self.fixture.dialog(0, peer=101)
        self.assertEqual(self.s.present_chat(0).display, 'Избранное')
        self.store.rename_chat(0, 'My notes')
        self.assertEqual(self.s.present_chat(0).display, 'My notes')

    def test_contact_id_collision_never_supplies_unrelated_chat_name(self):
        self.store.upsert_chat(Chat(202, chat_type='DIALOG'))
        self.assertEqual(self.s.present_chat(202).display, 'Чат 202')

    def test_groups_and_channels_retain_server_title(self):
        for identity, kind in ((404, 'CHAT'), (505, 'CHANNEL')):
            self.s._remember_chat_info({'chats': [{'id': identity, 'type': kind, 'title': 'Server title'}]}, my_id=101)
            self.assertEqual(self.s.present_chat(identity).display, 'Server title')

    def test_missing_chat_and_invalid_identity_do_not_create_rows(self):
        changes = self.store._db.total_changes
        self.assertIsNone(self.s.present_chat(999))
        for identity in (True, '303', 2**63):
            with self.assertRaises(ValueError):
                self.s.present_chat(identity)
        self.assertEqual(self.store._db.total_changes, changes)

    def test_old_session_and_foreign_owner_are_rejected(self):
        self.fixture.switch(same_owner=True)
        with self.assertRaises(ValueError):
            self.s.present_chat(303, context=self.context)
        self.fixture.switch(owner=404)
        with self.assertRaises(ValueError):
            self.s.present_chat(303, context=self.context)

    def test_unconfirmed_metadata_owner_cannot_supply_another_owners_name(self):
        self.s._server_metadata_owner = 404
        self.s._server_contacts[202]['names'] = [{'name': 'Foreign account alias'}]
        with self.assertRaises(ValueError):
            self.s.present_chat(303, context=self.context)

    def test_reauthentication_during_local_read_cannot_return_old_presentation(self):
        original = self.store.list_contacts
        def switched_read():
            result = original()
            self.fixture.switch(same_owner=True)
            return result
        with patch.object(self.store, 'list_contacts', side_effect=switched_read):
            with self.assertRaises(ValueError):
                self.s.present_chat(303, context=self.context)


class NotificationPresentationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.fixture = ChatPresentationTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.s, self.store = self.fixture.s, self.fixture.store
        self.store.upsert_chat(Chat(303, unread=1, peer_user_id=202, chat_type='DIALOG'))
        self.store.merge_config(101, {'user': {'PUSH_DETAILS': True, 'PUSH_SOUND': '_NONE_'}})
        self.window = QWidget()
        self.window._current_chat_id = None
        self.window.splitter = object()
        self.window._workspace_pages = SimpleNamespace(currentWidget=lambda: self.window.splitter)
        self.window.isActiveWindow = Mock(return_value=False)
        self.notices = DesktopNotifications(self.window, self.s, SimpleNamespace(notifications=True))
        self.notices.policy.started_ms = 1_999_000
        self.notices.toast.present = Mock()
        self.notices._play_sound = Mock()
        self.message = Message(303, id=9, sender=202, text='Synthetic message', time_ms=2_000_000)

    def tearDown(self):
        self.notices.clear()
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()

    def receive(self):
        with patch('maxclient.ui.desktop_notifications.time.time', return_value=2000):
            self.notices.receive(self.message)

    def test_toast_uses_same_contact_title_as_sidebar_on_first_message(self):
        self.receive()
        notice = self.notices.toast.present.call_args.args[0]
        self.assertEqual(notice.title, self.s.list_chats()[0].display)
        self.assertEqual(notice.title, 'Saved Alias')
        self.assertEqual(notice.body, 'Synthetic message')
        self.notices._play_sound.assert_not_called()

    def test_hidden_preview_setting_keeps_name_and_body_private(self):
        self.store.merge_config(101, {'user': {'PUSH_DETAILS': False}})
        self.receive()
        notice = self.notices.toast.present.call_args.args[0]
        self.assertEqual((notice.title, notice.body), ('MAX', 'Новое сообщение'))

    def test_same_owner_session_change_during_preferences_suppresses_toast(self):
        original = self.notices.preferences.read
        def read_then_switch(**kwargs):
            result = original(**kwargs)
            self.fixture.fixture.switch(same_owner=True)
            return result
        with patch.object(self.notices.preferences, 'read', side_effect=read_then_switch):
            self.receive()
        self.notices.toast.present.assert_not_called()

    def test_account_change_during_preferences_suppresses_old_name_and_message(self):
        original = self.notices.preferences.read
        def read_then_switch(**kwargs):
            result = original(**kwargs)
            self.fixture.fixture.switch(owner=404)
            return result
        with patch.object(self.notices.preferences, 'read', side_effect=read_then_switch):
            self.receive()
        self.notices.toast.present.assert_not_called()


class NotificationClickNavigationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.service = FakeService()
        self.binding = (101, self.service.client, self.service.context)
        self.service._history_read_context = lambda: self.binding
        def require(context):
            if context is not self.binding or context[2] is not self.service.context:
                raise ValueError('Synthetic changed session')
            return self.service.client
        self.service._require_history_context = require
        def present(identity, *, context):
            require(context)
            return self.service.chats.get(identity)
        self.service.present_chat = present
        self.service.chats[303].title = 'Synthetic display name'
        self.service.chats[303].unread = 1
        self.worker = patch('maxclient.ui.main_window.run_async')
        self.worker.start()
        self.addCleanup(self.worker.stop)
        self.window = MainWindow(self.service, Settings(theme='light', notifications=True))
        self.window.chat_view.open_chat = Mock()
        self.window.showNormal = Mock()
        self.window.raise_ = Mock()
        self.window.activateWindow = Mock()
        self.window.isActiveWindow = Mock(return_value=False)
        self.notices = self.window._notifications
        self.notices.policy.started_ms = 1_999_000
        self.notices.toast.present = Mock()
        self.notices.preferences.read = Mock(return_value={'available': True, 'pending': False, 'settings': {
            'DONT_DISTURB_UNTIL': 0, 'PUSH_DETAILS': True, 'PUSH_SOUND': '_NONE_'}})
        with patch('maxclient.ui.desktop_notifications.time.time', return_value=2000):
            self.notices.receive(Message(303, id=9, sender=202, text='Synthetic', time_ms=2_000_000))
        self.notices.toast.present.assert_called_once()

    def tearDown(self):
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()

    def test_toast_click_uses_actual_shell_route_and_display_name(self):
        self.notices.toast.opened.emit(303)
        self.window.chat_view.open_chat.assert_called_once_with(
            self.service.chats[303], message_id=None, time_ms=None)
        self.assertEqual(self.window._current_chat_id, 303)
        self.assertIs(self.window._workspace_pages.currentWidget(), self.window.splitter)
        self.window.showNormal.assert_called_once()
        self.notices.toast.opened.emit(303)
        self.assertEqual(self.window.chat_view.open_chat.call_count, 1)

    def test_stale_toast_after_reauthentication_cannot_open_same_id(self):
        self.service.context = object()
        self.notices.toast.opened.emit(303)
        self.window.chat_view.open_chat.assert_not_called()
        self.window.showNormal.assert_not_called()

    def test_wrong_target_or_cleared_notice_cannot_navigate(self):
        self.notices.toast.opened.emit(404)
        self.window.chat_view.open_chat.assert_not_called()
        self.notices.clear()
        self.notices.toast.opened.emit(303)
        self.window.chat_view.open_chat.assert_not_called()

    def test_chat_removed_after_toast_does_not_open_placeholder_or_search_dialog(self):
        self.service.chats.clear()
        with patch('maxclient.ui.main_window.QMessageBox') as box:
            self.notices.toast.opened.emit(303)
        self.window.chat_view.open_chat.assert_not_called()
        box.assert_not_called()


if __name__ == '__main__':
    unittest.main()
