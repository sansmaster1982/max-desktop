"""Contact-header navigation and delayed Info callbacks; no service workers."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from shiboken6 import isValid

from maxclient.core.config import Settings
from maxclient.protocol.client import ConnectionState
from maxclient.ui.main_window import MainWindow
from test_search_shell import FakeService as SearchFakeService


class FakeService(SearchFakeService):
    def __init__(self):
        super().__init__()
        self.refresh_conversation = Mock(side_effect=AssertionError('No worker or network'))
        self.load_conversation_tab = Mock(side_effect=AssertionError('No worker or network'))

    def conversation_context(self, chat_id):
        return SimpleNamespace(chat_id=chat_id, peer_id=202, auth=self.context)

    def is_current_conversation(self, context):
        return context.auth is self.context and self.client.state == ConnectionState.CONNECTED

    def conversation_snapshot(self, context):
        return {'context': context, 'chat_id': context.chat_id, 'peer_id': context.peer_id,
                'title': 'Синтетический контакт', 'phone': '+70000000000',
                'availability': {'chat': True, 'find': True, 'edit': False, 'call': False, 'video': False},
                'tabs': {key: True for key in ('common_chats', 'media', 'files', 'voice', 'links')}}


class ConversationInfoShellTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.service = FakeService()
        self.patches = [patch('maxclient.ui.main_window.run_async'),
                        patch('maxclient.ui.main_window.DesktopNotifications')]
        self.jobs, self.notifications = [item.start() for item in self.patches]
        settings = Settings(theme='light')
        settings.save = Mock()
        self.window = MainWindow(self.service, settings)
        self.window.chat_view.chat = self.service.chats[303]
        self.window.chat_view.h_title.setText('Синтетический контакт')
        self.window.chat_view.open_chat = Mock()
        self.window.show()
        self.app.processEvents()
        self.jobs.reset_mock()

    def tearDown(self):
        if isValid(self.window):
            self.window.close()
            self.window.deleteLater()
        self.app.processEvents()
        for item in reversed(self.patches): item.stop()

    def open_info(self):
        QTest.mouseClick(self.window.chat_view.h_title, Qt.MouseButton.LeftButton)
        self.assertIsNotNone(self.window._info_context)
        self.assertIs(self.window._conversation_pages.currentWidget(), self.window._info_panel)
        return self.window._info_context

    def job(self, method):
        return next(call.kwargs for call in reversed(self.jobs.call_args_list) if call.args[0] is method)

    @staticmethod
    def rows(key='media:1'):
        return [{'key': key, 'title': 'Тестовый файл', 'subtitle': 'Синтетические данные',
                 'chat_id': 303, 'message_id': 505, 'time_ms': 1793459502608.5, 'action': 'message'}]

    def test_click_and_keyboard_open_info_without_calls_or_messages(self):
        context = self.open_info()
        self.assertEqual(context.chat_id, 303)
        self.assertEqual(len(self.jobs.call_args_list), 2)  # First tab + profile read, workers mocked.
        self.service.refresh_conversation.assert_not_called()
        self.service.load_conversation_tab.assert_not_called()
        self.service.store.ensure_chat.assert_not_called()
        self.window._close_conversation_info()
        self.window.chat_view.h_avatar.setFocus()
        QTest.keyClick(self.window.chat_view.h_avatar, Qt.Key.Key_Return)
        self.assertIsNotNone(self.window._info_context)

    def test_back_preserves_open_chat_and_discards_delayed_profile(self):
        context = self.open_info()
        callback = self.job(self.service.refresh_conversation)['on_done']
        self.window._info_panel.back_requested.emit()
        callback(self.service.conversation_snapshot(context))
        self.assertIs(self.window._conversation_pages.currentWidget(), self.window.chat_view)
        self.assertIsNone(self.window._info_context)
        self.assertIsNone(self.window._info_panel._context)
        self.window.chat_view.open_chat.assert_not_called()

    def test_refresh_keeps_existing_tab_and_does_not_reload_it(self):
        context = self.open_info()
        tab_callback = self.job(self.service.load_conversation_tab)['on_done']
        tab_callback({'items': [], 'marker': None, 'complete': True})
        before = self.jobs.call_count
        snapshot = self.service.conversation_snapshot(context)
        snapshot['title'] = 'Обновлённое имя'
        self.job(self.service.refresh_conversation)['on_done'](snapshot)
        self.assertEqual(self.jobs.call_count, before)
        self.assertEqual(self.window._info_panel.name.text(), 'Обновлённое имя')

    def test_owner_change_rejects_profile_and_tab_data(self):
        context = self.open_info()
        refresh = self.job(self.service.refresh_conversation)['on_done']
        tab = self.job(self.service.load_conversation_tab)['on_done']
        self.service.context = object()
        refresh({**self.service.conversation_snapshot(context), 'title': 'Старый владелец'})
        tab({'items': self.rows(), 'marker': None})
        self.assertNotEqual(self.window._info_panel.name.text(), 'Старый владелец')
        self.window._on_state(ConnectionState.CONNECTED)
        self.assertIsNone(self.window._info_context)

    def test_destroyed_window_rejects_late_info_profile_and_tab_callbacks(self):
        context = self.open_info()
        refresh = self.job(self.service.refresh_conversation)
        tab = self.job(self.service.load_conversation_tab)
        self.window.deleteLater()
        QCoreApplication.sendPostedEvents(self.window, QEvent.Type.DeferredDelete)
        self.assertFalse(isValid(self.window))
        self.assertFalse(self.window._info_current(context))
        refresh['on_done'](self.service.conversation_snapshot(context))
        refresh['on_error']('synthetic late error')
        tab['on_done']({'items': self.rows(), 'marker': None})
        tab['on_error']('synthetic late error')

    def test_media_more_preserves_rows_dedupes_and_stops_cursor_cycle(self):
        context = self.open_info()
        self.window._request_info_tab('files', context)
        self.job(self.service.load_conversation_tab)['on_done']({'items': self.rows(), 'marker': 505})
        self.window._request_info_tab('files', context, more=True)
        call = self.jobs.call_args
        self.assertEqual(call.kwargs['marker'], 505)
        call.kwargs['on_done']({'items': self.rows() + self.rows('media:2'), 'marker': 505})
        state = self.window._info_pages['files']
        self.assertEqual(len(state['items']), 2)
        self.assertIsNone(state['marker'])
        before = self.jobs.call_count
        self.window._request_info_tab('files', context, more=True)
        self.assertEqual(self.jobs.call_count, before)

    def test_failed_page_keeps_previous_results(self):
        context = self.open_info()
        self.window._request_info_tab('media', context)
        self.job(self.service.load_conversation_tab)['on_done']({'items': self.rows(), 'marker': 505})
        self.window._request_info_tab('media', context, more=True)
        self.job(self.service.load_conversation_tab)['on_error']('raw private message')
        self.assertEqual(self.window._info_pages['media']['items'], self.rows())

    def test_find_returns_to_same_chat_without_refresh_or_send(self):
        context = self.open_info()
        before = self.jobs.call_count
        self.window._info_action('find', context)
        self.assertIs(self.window._conversation_pages.currentWidget(), self.window.chat_view)
        self.assertTrue(self.window.chat_view.search_button.isChecked())
        self.window.chat_view.open_chat.assert_not_called()
        self.assertEqual(self.jobs.call_count, before)

    def test_copy_uses_displayed_number_only_on_explicit_action_and_current_context(self):
        context = self.open_info()
        with patch('maxclient.ui.main_window.QApplication') as app:
            app.clipboard.assert_not_called()
            self.window._info_action('copy_phone', context)
            app.clipboard.return_value.setText.assert_called_once_with('+70000000000')
            self.service.context = object()
            self.window._info_action('copy_phone', context)
            self.assertEqual(app.clipboard.return_value.setText.call_count, 1)

    def test_item_navigates_to_actual_message_without_downloading_attachment(self):
        context = self.open_info()
        self.window._open_info_item(self.rows()[0], context)
        self.window.chat_view.open_chat.assert_called_once_with(
            self.service.chats[303], message_id=505, time_ms=1793459502608.5)
        self.service.store.ensure_chat.assert_not_called()

    def test_foreign_message_or_switched_context_cannot_navigate(self):
        context = self.open_info()
        self.window._open_info_item({**self.rows()[0], 'chat_id': 909}, context)
        self.window.chat_view.open_chat.assert_not_called()
        self.service.context = object()
        self.window._info_action('chat', context)
        self.window.chat_view.open_chat.assert_not_called()


if __name__ == '__main__': unittest.main()
