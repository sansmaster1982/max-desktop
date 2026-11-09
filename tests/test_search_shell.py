"""Search shell adapters, fake async completions and no authenticated I/O."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtWidgets import QApplication
from shiboken6 import isValid

from maxclient.core.config import Settings
from maxclient.core.models import Chat
from maxclient.protocol.client import AuthenticatedSession, ConnectionState
from maxclient.ui.main_window import MainWindow


class FakeService:
    def __init__(self):
        self.context = AuthenticatedSession(101, 'synthetic-device', 'WEB', object())
        self.client = SimpleNamespace(state=ConnectionState.CONNECTED)
        self.session = SimpleNamespace(my_user_id=101, device_id='synthetic-device')
        self.my_id = 101
        self.chats = {303: Chat(303, title='Тестовый чат', peer_user_id=202)}
        self.store = SimpleNamespace(get_chat=self.chats.get, ensure_chat=Mock(), get_contact=lambda _id: None)
        self.search_all = Mock(side_effect=AssertionError('No real worker/network in this suite'))

    def list_chats(self): return list(self.chats.values())
    def calls_available(self, _chat): return False
    def sync_chats(self): raise AssertionError('No real worker/network')
    def search_context(self):
        if self.client.state != ConnectionState.CONNECTED: raise ValueError('disconnected')
        return self.context


class SearchShellTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.service = FakeService()
        self.patches = [patch('maxclient.ui.main_window.run_async'),
                        patch('maxclient.ui.main_window.DesktopNotifications'),
                        patch('maxclient.ui.main_window.QMessageBox')]
        self.jobs, self.notifications, self.box = [item.start() for item in self.patches]
        settings = Settings(theme='light')
        settings.save = Mock()
        self.window = MainWindow(self.service, settings)
        self.window.chat_view.open_chat = Mock()
        self.jobs.reset_mock()

    def tearDown(self):
        if isValid(self.window):
            self.window.close()
            self.window.deleteLater()
        self.app.processEvents()
        for item in reversed(self.patches): item.stop()

    def start(self, query='Тест'):
        self.window.search.setText(query)
        self.window.search_panel._timer.stop()
        self.window.search_panel._start(None)
        request = self.window.search_panel._request
        self.assertIsNotNone(request)
        return dict(request), self.jobs.call_args.kwargs

    def page(self, request):
        return {'query': request['query'], 'context': request['context'], 'items': [self.row()],
                'marker': None, 'total': 1, 'counters': {}}

    @staticmethod
    def row(**values):
        row = {'key': 'message:303:505', 'section': 'MESSAGES', 'title': 'Тестовый чат',
               'preview': 'Синтетическое сообщение', 'chat_id': 303,
               'message_id': 505, 'user_id': None, 'time_ms': 1793459502608.5}
        row.update(values)
        return row

    def test_search_is_async_bound_read_and_push_reload_preserves_results(self):
        request, callbacks = self.start()
        self.assertIs(self.jobs.call_args.args[0], self.service.search_all)
        self.assertIs(self.jobs.call_args.kwargs['context'], self.service.context)
        callbacks['on_done'](self.page(request))
        self.assertEqual(len(self.window.search_panel._items), 1)
        self.window._reload_chats()
        self.assertIs(self.window._chat_pages.currentWidget(), self.window.search_panel)
        self.assertEqual(self.jobs.call_count, 1)
        self.service.search_all.assert_not_called()

    def test_changed_query_rejects_late_result(self):
        request, callbacks = self.start('старый')
        self.window.search.setText('новый')
        callbacks['on_done'](self.page(request))
        self.assertEqual(self.window.search_panel._items, [])
        self.assertEqual(self.window.search_panel._query, 'новый')

    def test_reconnect_or_new_context_rejects_old_owner_results(self):
        request, callbacks = self.start()
        self.service.context = AuthenticatedSession(101, 'synthetic-device', 'WEB', object())
        callbacks['on_done'](self.page(request))
        self.assertEqual(self.window.search_panel._items, [])
        self.assertIs(self.window._search_context, self.service.context)
        self.service.client.state = ConnectionState.DISCONNECTED
        self.window._on_state(ConnectionState.DISCONNECTED)
        self.assertIsNone(self.window._search_context)

    def test_closed_window_never_accepts_delayed_search(self):
        request, callbacks = self.start()
        self.window.close()
        callbacks['on_done'](self.page(request))
        callbacks['on_error']('private raw error must not be shown')
        self.assertEqual(self.window.search_panel._items, [])
        self.assertIsNone(self.window._search_context)

    def test_message_navigation_preserves_canonical_id_and_timestamp(self):
        self.window._bind_search_context()
        self.window._open_search_result(self.row())
        self.window.chat_view.open_chat.assert_called_once_with(
            self.service.chats[303], message_id=505, time_ms=1793459502608.5)
        self.service.store.ensure_chat.assert_not_called()

    def test_destroyed_window_rejects_late_search_without_needing_close_event(self):
        request, callbacks = self.start()
        self.window.deleteLater()
        QCoreApplication.sendPostedEvents(self.window, QEvent.Type.DeferredDelete)
        self.assertFalse(isValid(self.window))
        callbacks['on_done'](self.page(request))
        callbacks['on_error']('synthetic late error')
        self.assertIsNone(self.window._bind_search_context())

    def test_contact_uses_known_peer_relation_not_user_id_as_chat_id(self):
        self.window._bind_search_context()
        self.window._open_search_result(self.row(section='CONTACTS', key='contact:202',
                                               chat_id=None, user_id=202, message_id=None, time_ms=None))
        self.assertEqual(self.window.chat_view.open_chat.call_args.args[0].id, 303)
        self.service.store.ensure_chat.assert_not_called()

    def test_contacts_dialog_route_does_not_create_chat_with_user_id(self):
        self.window._open_contact_by_id(202, 'Тестовый контакт')
        self.assertEqual(self.window.chat_view.open_chat.call_args.args[0].id, 303)
        self.window.chat_view.open_chat.reset_mock()
        self.window._open_contact_by_id(808, 'Другой контакт')
        self.window.chat_view.open_chat.assert_not_called()
        self.service.store.ensure_chat.assert_not_called()

    def test_unjoined_directory_hit_only_previews_and_does_not_write_cache(self):
        self.window._bind_search_context()
        self.window._open_search_result(self.row(section='BOTS', key='contact:808', chat_id=None,
                                               user_id=808, message_id=None, preview='<b>Literal</b>'))
        self.window.chat_view.open_chat.assert_not_called()
        self.service.store.ensure_chat.assert_not_called()
        self.box.return_value.setTextFormat.assert_called_with(Qt.TextFormat.PlainText)
        self.box.return_value.exec.assert_called_once()

    def test_owner_change_before_activation_cannot_open_cached_conversation(self):
        self.window._bind_search_context()
        self.service.context = AuthenticatedSession(404, 'synthetic-device', 'WEB', object())
        self.window._open_search_result(self.row())
        self.window.chat_view.open_chat.assert_not_called()
        self.box.assert_not_called()

    def test_explicit_foreign_chat_id_cannot_be_replaced_by_known_contact_dialog(self):
        self.window._bind_search_context()
        self.window._open_search_result(self.row(chat_id=909, user_id=202))
        self.window.chat_view.open_chat.assert_not_called()
        self.service.store.ensure_chat.assert_not_called()
        self.box.return_value.exec.assert_called_once()

    def test_owner_swap_during_cache_lookup_cannot_open_old_chat(self):
        self.window._bind_search_context()
        def replaced(_identity):
            self.service.context = AuthenticatedSession(404, 'synthetic-device', 'WEB', object())
            return self.service.chats[303]
        self.service.store.get_chat = replaced
        self.window._open_search_result(self.row())
        self.window.chat_view.open_chat.assert_not_called()
        self.box.assert_not_called()

    def test_query_clear_restores_sidebar_without_rpc_and_partial_is_visible(self):
        request, callbacks = self.start()
        page = self.page(request)
        page['unavailable_count'] = 2
        callbacks['on_done'](page)
        self.assertIn('Часть результатов', self.window.search_panel.status.text())
        self.window.search.clear()
        self.assertIs(self.window._chat_pages.currentWidget(), self.window.chat_list)
        self.assertEqual(self.window.search_panel._items, [])
        self.assertEqual(self.jobs.call_count, 1)


if __name__ == '__main__': unittest.main()
