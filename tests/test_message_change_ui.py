"""Actual memory service push -> queued Qt bridge -> real visible rows, no RPC."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from dataclasses import replace
import unittest
from unittest.mock import Mock, patch

from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtTest import QSignalSpy
from PySide6.QtWidgets import QApplication, QLabel, QPushButton
from shiboken6 import isValid

import test_history_reconnect_review as fixture
from maxclient.core.config import Settings
from maxclient.core.models import Message
from maxclient.protocol.client import MaxFrame
from maxclient.ui.main_window import MainWindow
from test_desktop_shell import _load_offscreen_font


class MessageChangeUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        _load_offscreen_font(cls.app)

    def setUp(self):
        self.case = fixture.HistoryReconnectReviewTests()
        self.case.setUp()
        self.service, self.store, self.client = self.case.service, self.case.store, self.case.client
        self.raw = dict(self.case.raw(text='Synthetic original'), updateTime=100)
        self.service._on_push(self.case.push(self.raw))
        self.service.calls_available = Mock(return_value=False)
        self.effects = {}
        for name in ('mark_chat_read', 'send_typing', 'stop_typing', 'send_text', 'reply_to_message'):
            self.effects[name] = Mock(side_effect=AssertionError('No effect permitted'))
            setattr(self.service, name, self.effects[name])
        self.callbacks = []
        def capture(method, *args, on_done=None, on_error=None, **kwargs):
            self.callbacks.append((method, args, kwargs, on_done, on_error))
        self.patches = [patch('maxclient.ui.main_window.run_async'),
                        patch('maxclient.ui.chat_view.run_async', side_effect=capture),
                        patch('maxclient.ui.main_window.DesktopNotifications')]
        for value in self.patches: value.start()
        settings = Settings(theme='light'); settings.save = Mock()
        self.window = MainWindow(self.service, settings)
        self.window.resize(1100, 700)
        self.window.show()
        self.view = self.window.chat_view
        self.view.open_chat(self.store.get_chat(303))
        self.spy = QSignalSpy(self.window.sig_message_changed)
        self.settle()

    def tearDown(self):
        if isValid(self.window):
            self.window.close(); self.window.deleteLater()
            QCoreApplication.sendPostedEvents(self.window, QEvent.Type.DeferredDelete)
        self.app.processEvents()
        for value in reversed(self.patches): value.stop()
        for effect in self.effects.values(): effect.assert_not_called()
        self.client.chat_info.assert_not_called()
        self.client.contact_info.assert_not_called()
        self.client.chat_history_window.assert_not_called()
        self.case.tearDown()

    def settle(self):
        for _ in range(4): self.app.processEvents()

    def push(self, opcode, data):
        self.service._on_push(MaxFrame(0, 8, opcode, b'', data, auth_context=self.case.context))

    def ids(self):
        return [row['msg'].id for row in self.view._rows]

    def test_actual_edit_cache_commit_precedes_queued_repaint_without_new_arrival(self):
        edited = dict(self.raw, text='<b>Actual synthetic edit</b>', updateTime=200)
        self.push(128, {'chatId': 303, 'message': edited})
        self.assertEqual(self.store.message(303, 7).text, edited['text'])
        self.assertEqual(self.view._rows[0]['msg'].text, 'Synthetic original')
        self.assertEqual(self.spy.count(), 1)
        self.settle()
        self.assertEqual(self.view._rows[0]['msg'].text, edited['text'])
        self.assertEqual(self.view._rows[0]['bubble'].findChild(QLabel, 'MessageText').text(), edited['text'])
        self.window._notifications.receive.assert_not_called()

    def test_actual_delete_removes_row_and_unused_divider_without_arrival(self):
        self.push(142, {'chat': self.case.chat(), 'messageIds': [7], 'ttl': False})
        self.assertEqual(self.ids(), [7])
        self.settle()
        self.assertEqual(self.ids(), [])
        self.assertEqual(self.view.msg_layout.count(), 1)
        self.window._notifications.receive.assert_not_called()

    def test_actual_reaction_snapshot_and_clear_repaint_existing_chips(self):
        self.push(155, {'chatId': 303, 'messageId': 7, 'totalCount': 2,
                        'counters': [{'reaction': 'A', 'count': 2}]})
        self.settle()
        self.assertEqual([chip.text() for chip in self.view._rows[0]['bubble'].findChildren(QPushButton, 'MessageReaction')], ['A 2'])
        self.push(156, {'chatId': 303, 'messageId': 7})
        self.settle()
        self.assertEqual(self.view._rows[0]['bubble'].findChildren(QPushButton, 'MessageReaction'), [])
        self.window._notifications.receive.assert_not_called()

    def test_old_owner_queued_event_is_dropped_before_exact_cache_read(self):
        original = self.service.cached_messages_for_change
        with patch.object(self.service, 'cached_messages_for_change', wraps=original) as read:
            self.push(155, {'chatId': 303, 'messageId': 7, 'totalCount': 2})
            self.case.change_account()
            self.settle()
            read.assert_not_called()
        self.assertEqual(self.view._rows[0]['msg'].reaction_info, {})
        self.window._notifications.receive.assert_not_called()

    def test_owner_or_chat_change_during_exact_read_cannot_patch_old_rows(self):
        original = self.service.cached_messages_for_change
        def changed(event):
            result = original(event)
            self.view.show_placeholder()
            return result
        with patch.object(self.service, 'cached_messages_for_change', side_effect=changed):
            self.push(155, {'chatId': 303, 'messageId': 7, 'totalCount': 2})
            self.settle()
        self.assertIsNone(self.view.chat)
        self.assertEqual(self.ids(), [])

    def test_session_change_after_exact_read_cannot_patch_old_owner_rows(self):
        original = self.service.cached_messages_for_change
        def changed(event):
            result = original(event)
            self.client._auth_session = self.case.context_for(self.client)
            return result
        with patch.object(self.service, 'cached_messages_for_change', side_effect=changed):
            self.push(155, {'chatId': 303, 'messageId': 7, 'totalCount': 2})
            self.settle()
        self.assertEqual(self.view._rows[0]['msg'].reaction_info, {})

    def test_foreign_chat_change_updates_sidebar_only(self):
        raw = dict(self.case.raw(20, text='Other chat'), updateTime=100)
        self.service.on_message = Mock()  # Seed another cache row without an arrival UI path.
        self.push(128, {'chatId': 909, 'message': raw})
        self.settle()
        with patch.object(self.service, 'cached_messages_for_change') as read:
            self.push(155, {'chatId': 909, 'messageId': 20, 'totalCount': 1})
            self.settle()
            read.assert_not_called()
        self.assertEqual(self.ids(), [7])
        self.assertEqual(self.view._rows[0]['msg'].reaction_info, {})

    def prepare_many(self, count=50):
        self.store.delete_messages(303, [7])
        self.store.insert_messages([Message.from_server(303, self.case.raw(i, fixture.STAMP + i,
                                   text=f'Synthetic row {i}'), 101) for i in range(count)])
        self.view.reload(); self.settle()

    def test_edit_and_delete_preserve_draft_reply_query_and_scroll_anchor(self):
        self.prepare_many()
        self.view.input.setPlainText('<b>Unsent synthetic draft</b>')
        reply = self.view._rows[7]['msg']; self.view._set_reply_target(reply)
        self.view.history_search.setText('Synthetic')
        self.settle()
        bar = self.view.scroll.verticalScrollBar(); bar.setValue(bar.maximum() // 2)
        self.settle()
        anchor = next(row for row in self.view._rows if row['bubble'].mapTo(self.view.scroll.viewport(), row['bubble'].rect().topLeft()).y() >= 0)
        identity = anchor['msg'].id
        before = anchor['bubble'].mapTo(self.view.scroll.viewport(), anchor['bubble'].rect().topLeft()).y()
        with patch.object(self.view.scroll, 'ensureWidgetVisible') as navigate:
            self.push(155, {'chatId': 303, 'messageId': 1, 'totalCount': 2,
                            'counters': [{'reaction': 'A', 'count': 2}]})
            self.push(142, {'chat': self.case.chat(), 'messageIds': [0], 'ttl': False})
            self.settle()
            navigate.assert_not_called()
        after = next(row for row in self.view._rows if row['msg'].id == identity)
        self.assertLessEqual(abs(after['bubble'].mapTo(self.view.scroll.viewport(), after['bubble'].rect().topLeft()).y() - before), 2)
        self.assertEqual(self.view.input.toPlainText(), '<b>Unsent synthetic draft</b>')
        self.assertIs(self.view._reply_target, reply)
        self.assertEqual(self.view.history_search.text(), 'Synthetic')
        self.assertNotIn(0, self.ids())
        self.assertEqual(sum(row['divider'] is not None for row in self.view._rows), 1)

    def test_target_older_than_last300_patches_exact_rows_without_adding_latest(self):
        self.prepare_many(330)
        old = [self.store.message(303, i) for i in (0, 1)]
        self.view.open_chat(self.store.get_chat(303), message_id=0, time_ms=fixture.STAMP)
        self.callbacks[-1][3](old); self.settle()
        target = self.view._history_target
        with patch.object(self.service, 'local_history', wraps=self.service.local_history) as latest:
            self.push(155, {'chatId': 303, 'messageId': 0, 'totalCount': 1,
                            'counters': [{'reaction': 'A', 'count': 1}]})
            self.settle()
            latest.assert_not_called()
        self.assertEqual(self.ids(), [0, 1])
        self.assertEqual(self.view._history_target, target)
        self.push(142, {'chat': self.case.chat(), 'messageIds': [0], 'ttl': False})
        self.settle()
        self.assertEqual(self.ids(), [1])
        self.assertEqual(self.view._history_target, target)
        self.assertIsNone(self.view._spotlight_id)

    def test_pending_target_reply_overlays_changed_ids_without_expanding_membership(self):
        old = self.store.message(303, 7)
        self.view.open_chat(self.store.get_chat(303), message_id=7, time_ms=fixture.STAMP)
        done = self.callbacks[-1][3]
        edited = dict(self.raw, text='Fresh while target pending', updateTime=200)
        self.push(128, {'chatId': 303, 'message': edited}); self.settle()
        done([old]); self.settle()
        self.assertEqual(self.ids(), [7])
        self.assertEqual(self.view._rows[0]['msg'].text, edited['text'])
        self.assertEqual(self.view._spotlight_id, 7)

    def test_late_target_reply_cannot_resurrect_deleted_target(self):
        old = self.store.message(303, 7)
        self.view.open_chat(self.store.get_chat(303), message_id=7, time_ms=fixture.STAMP)
        done = self.callbacks[-1][3]
        self.push(142, {'chat': self.case.chat(), 'messageIds': [7], 'ttl': False}); self.settle()
        done([old]); self.settle()
        self.assertEqual(self.ids(), [])
        self.assertIn('не найдено', self.view.h_sub.text())

    def test_latest_queued_reply_cannot_undo_an_edit_or_reaction(self):
        old = self.store.message(303, 7)
        done = self.callbacks[-1][3]
        self.push(155, {'chatId': 303, 'messageId': 7, 'totalCount': 1,
                        'counters': [{'reaction': 'A', 'count': 1}]})
        self.settle(); done([old]); self.settle()
        self.assertEqual(self.view._rows[0]['msg'].reaction_info['totalCount'], 1)

    def test_replaced_session_discards_pending_scroll_state_before_new_change(self):
        self.push(155, {'chatId': 303, 'messageId': 7, 'totalCount': 1})
        event = self.spy.at(0)[0]
        self.window._on_message_changed(event)
        stale = self.view._change_scroll_pending
        self.client._auth_session = self.case.context_for(self.client)
        current = replace(event, session_context=self.client._auth_session)
        binding = self.view.message_change_binding(303)
        self.assertTrue(self.view.apply_message_change(current, self.service.cached_messages_for_change(current), binding))
        self.assertIsNot(self.view._change_scroll_pending, stale)
        self.assertIs(self.view._change_scroll_pending['event'], current)

    def test_queued_change_and_scroll_callback_after_window_deletion_are_safe(self):
        self.push(155, {'chatId': 303, 'messageId': 7, 'totalCount': 1})
        self.window._on_message_changed(self.spy.at(0)[0])
        self.assertIsNotNone(self.view._change_scroll_pending)
        self.window.deleteLater()
        QCoreApplication.sendPostedEvents(self.window, QEvent.Type.DeferredDelete)
        self.settle()
        self.assertFalse(isValid(self.window))


if __name__ == '__main__':
    unittest.main()
