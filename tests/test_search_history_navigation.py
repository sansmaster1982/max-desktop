"""Search result -> real timestamp history window, with no service execution."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from maxclient.core.config import Settings
from maxclient.core.models import Chat, Message
from maxclient.protocol.client import AuthenticatedSession, ConnectionState
from maxclient.ui.chat_view import ChatView
from maxclient.ui.theme import build_stylesheet
from test_desktop_shell import _load_offscreen_font


def auth(owner=7):
    return AuthenticatedSession(owner, 'synthetic-device', 'WEB', object())


def message(identity, chat_id=42, text=None):
    return Message(chat_id=chat_id, id=identity, time_ms=1793459500000+identity,
                   text=text or f'Синтетическое сообщение {identity}')


class SearchHistoryNavigationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        _load_offscreen_font(cls.app)
        cls.app.setStyle('Fusion')

    def setUp(self):
        self.old_style = self.app.styleSheet()
        self.app.setStyleSheet(build_stylesheet('light', '#0085FF'))
        self.client = SimpleNamespace(authenticated_session=auth(), state=ConnectionState.CONNECTED)
        self.service = SimpleNamespace(
            client=self.client, my_id=7, local_history=Mock(return_value=[]),
            open_chat=Mock(), open_chat_window=Mock(), calls_available=lambda _chat: False,
            mark_chat_read=Mock(), send_typing=Mock(), stop_typing=Mock(), upload=Mock(),
        )
        self.callbacks = []

        def capture(method, chat_id, *, on_done, on_error, **kwargs):
            self.callbacks.append({'method': method, 'chat_id': chat_id, 'params': kwargs,
                                   'done': on_done, 'error': on_error})

        self.runner = patch('maxclient.ui.chat_view.run_async', side_effect=capture)
        self.run = self.runner.start()
        self.view = ChatView(self.service, Settings(theme='light'))
        self.view.resize(740, 620)
        self.view.show()
        self.app.processEvents()

    def tearDown(self):
        for name in ('mark_chat_read', 'send_typing', 'stop_typing', 'upload'):
            getattr(self.service, name).assert_not_called()
        self.service.open_chat.assert_not_called()
        self.service.open_chat_window.assert_not_called()
        self.view.close()
        self.view.deleteLater()
        self.app.processEvents()
        self.runner.stop()
        self.app.setStyleSheet(self.old_style)

    def open_target(self, identity=99, timestamp=1793459500099, chat_id=42):
        self.view.open_chat(Chat(id=chat_id, title='Синтетический чат'),
                            message_id=identity, time_ms=timestamp)
        return self.callbacks[-1]

    def test_old_callers_still_request_latest_without_target_or_extra_parameters(self):
        self.view.open_chat(Chat(id=42))
        request = self.callbacks[-1]
        self.assertIs(request['method'], self.service.open_chat)
        self.assertEqual(request['params'], {})
        request['done']([message(11)])
        self.assertEqual(self.view.h_sub.text(), 'Сообщения обновлены')
        self.assertIsNone(self.view._history_target)
        self.assertIsNone(self.view._spotlight_id)

    def test_source_time_is_passed_exactly_and_window_uses_proven_15back_30forward(self):
        for timestamp in (1793459500099, 1793459500099.5, 2**62):
            request = self.open_target(timestamp=timestamp)
            self.assertIs(request['method'], self.service.open_chat_window)
            self.assertEqual(request['params'], {'from_cursor': timestamp, 'backward': 15, 'forward': 30})
            self.assertIs(type(request['params']['from_cursor']), type(timestamp))
        self.assertEqual(len(self.callbacks), 3)

    def test_only_matching_actual_bubble_is_highlighted_after_accepted_generation(self):
        cached = message(11, text='Одинаковый текст')
        self.service.local_history.return_value = [cached]
        request = self.open_target()
        self.assertFalse(any(row['bubble'].property('searchSpotlight') for row in self.view._rows))
        self.assertIsNone(self.view._spotlight_id)
        target = message(99, text='Одинаковый текст')
        with patch.object(self.view.scroll, 'ensureWidgetVisible') as ensure:
            request['done']([cached, target, message(100)])
            self.app.processEvents()
            QTest.qWait(25)
            selected = [row for row in self.view._rows if row['bubble'].property('searchSpotlight')]
            self.assertEqual([row['msg'].id for row in selected], [99])
            ensure.assert_called_once_with(selected[0]['bubble'], 0, 24)
        self.assertEqual(self.view.h_sub.text(), 'Найденное сообщение')

    def test_missing_message_reports_unavailable_and_never_substitutes_latest(self):
        request = self.open_target()
        with patch.object(self.view.scroll, 'ensureWidgetVisible') as ensure:
            request['done']([message(11), message(12)])
            self.app.processEvents()
            ensure.assert_not_called()
        self.assertIn('не найдено', self.view.h_sub.text())
        self.assertFalse(any(row['bubble'].property('searchSpotlight') for row in self.view._rows))
        self.assertEqual(len(self.callbacks), 1)
        self.assertIs(self.callbacks[0]['method'], self.service.open_chat_window)

    def test_invalid_identity_or_time_never_falls_back_to_latest_or_executes_read(self):
        cases = [(None, 100), (True, 100), ('99', 100), (2**64, 100),
                 (99, None), (99, True), (99, float('nan')), (99, float('inf'))]
        for identity, timestamp in cases:
            with self.subTest(identity=identity, timestamp=timestamp):
                self.view.open_chat(Chat(id=42), message_id=identity, time_ms=timestamp)
                self.assertEqual(len(self.callbacks), 0)
                self.assertIn('подтверждённые ID', self.view.h_sub.text())

    def test_window_failure_and_foreign_messages_preserve_cache_without_raw_error(self):
        cached = message(11)
        self.service.local_history.return_value = [cached]
        request = self.open_target()
        request['error']('<b>secret payload must never display</b>')
        self.assertEqual([row['msg'].id for row in self.view._rows], [11])
        self.assertEqual(self.view.h_sub.text(), 'Не удалось загрузить найденное сообщение')
        request = self.open_target()
        request['done']([message(99, chat_id=999, text='foreign account text')])
        self.assertEqual([row['msg'].id for row in self.view._rows], [11])
        self.assertNotIn('secret', self.view.h_sub.text())
        self.assertNotIn('foreign', self.view.h_sub.text())

    def test_previous_chat_same_chat_newer_request_and_placeholder_reject_old_results(self):
        old = self.open_target(99)
        current = self.open_target(100)
        old['done']([message(99)])
        self.assertEqual(self.view._rows, [])
        current['done']([message(100)])
        self.assertEqual([row['msg'].id for row in self.view._rows], [100])
        self.view.open_chat(Chat(id=43))
        current['error']('stale')
        old['done']([message(99)])
        self.assertEqual(self.view.chat.id, 43)
        self.assertEqual(self.view._rows, [])
        pending = self.open_target(99)
        self.view.show_placeholder()
        pending['done']([message(99)])
        self.assertIsNone(self.view.chat)
        self.assertEqual(self.view._rows, [])

    def test_changed_auth_owner_client_and_disconnected_state_reject_delayed_response(self):
        for mutation in ('auth', 'owner', 'client', 'disconnect'):
            with self.subTest(mutation=mutation):
                self.service.client = self.client
                self.service.my_id = 7
                self.client.authenticated_session = auth()
                self.client.state = ConnectionState.CONNECTED
                self.view.show_placeholder()
                request = self.open_target()
                if mutation == 'auth':
                    self.client.authenticated_session = auth()
                elif mutation == 'owner':
                    self.service.my_id = 8
                elif mutation == 'client':
                    self.service.client = SimpleNamespace(authenticated_session=auth(), state=ConnectionState.CONNECTED)
                else:
                    self.client.state = ConnectionState.DISCONNECTED
                request['done']([message(99)])
                request['error']('stale error')
                self.assertEqual(self.view._rows, [])
                self.assertEqual(self.view.h_sub.text(), 'Загрузка найденного сообщения…')

    def test_deferred_spotlight_scroll_cannot_move_a_new_chat_or_new_context(self):
        for switch in ('chat', 'context'):
            self.service.client = self.client
            self.client.authenticated_session = auth()
            request = self.open_target()
            with patch.object(self.view.scroll, 'ensureWidgetVisible') as ensure:
                request['done']([message(99)])
                if switch == 'chat':
                    self.view.open_chat(Chat(id=43))
                else:
                    self.client.authenticated_session = auth()
                self.app.processEvents()
                ensure.assert_not_called()

    def test_no_current_connection_does_not_launch_a_window_read(self):
        self.client.state = ConnectionState.DISCONNECTED
        self.service.local_history.return_value = [message(11)]
        self.view.open_chat(Chat(id=42), message_id=99, time_ms=100)
        self.assertEqual(self.callbacks, [])
        self.assertIn('Нет подтверждённого соединения', self.view.h_sub.text())
        self.assertEqual([row['msg'].id for row in self.view._rows], [11])

    def test_target_jump_preserves_draft_reply_and_normal_latest_clears_spotlight(self):
        self.view.open_chat(Chat(id=42))
        self.view.input.setPlainText('Синтетический черновик <literal>')
        reply = message(11)
        self.view._set_reply_target(reply)
        request = self.open_target()
        request['done']([message(99)])
        self.assertEqual(self.view.input.toPlainText(), 'Синтетический черновик <literal>')
        self.assertIs(self.view._reply_target, reply)
        self.view.apply_appearance()
        self.assertTrue(self.view._rows[0]['bubble'].property('searchSpotlight'))
        self.view.open_chat(Chat(id=42))
        self.assertIs(self.callbacks[-1]['method'], self.service.open_chat)
        self.assertIsNone(self.view._history_target)
        self.assertFalse(any(row['bubble'].property('searchSpotlight') for row in self.view._rows))

    def test_target_jump_clears_old_local_search_to_avoid_competing_auto_scroll(self):
        self.view.open_chat(Chat(id=42))
        self.callbacks[-1]['done']([message(11, text='локальный поиск')])
        self.view.history_search.setText('локальный')
        self.assertTrue(self.view._search_matches)
        request = self.open_target()
        self.assertEqual(self.view.history_search.text(), '')
        request['done']([message(11, text='локальный поиск'), message(99)])
        self.assertEqual(self.view._search_matches, [])
        self.assertEqual(self.view._spotlight_id, 99)

    def test_target_scroll_reaches_older_bubble_and_literal_synthetic_capture(self):
        request = self.open_target(identity=11)
        request['done']([message(i, text=('<b>Найденное сообщение</b>' if i == 11 else None))
                         for i in range(1, 42)])
        self.app.processEvents()
        QTest.qWait(40)
        selected = next(row['bubble'] for row in self.view._rows if row['msg'].id == 11)
        point = selected.mapTo(self.view.scroll.viewport(), selected.rect().center())
        self.assertGreaterEqual(point.y(), 0)
        self.assertLessEqual(point.y(), self.view.scroll.viewport().height())
        self.assertLess(self.view.scroll.verticalScrollBar().value(), self.view.scroll.verticalScrollBar().maximum())
        output = Path('.test-tmp/search_target_history.png')
        output.parent.mkdir(exist_ok=True)
        self.assertTrue(self.view.grab().save(str(output)))


if __name__ == '__main__':
    unittest.main()
