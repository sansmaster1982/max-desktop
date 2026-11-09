"""Offscreen viewport/focus receipt scheduling with intercepted async calls."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import unittest
from unittest.mock import Mock, patch

from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog
from shiboken6 import isValid

import test_history_reconnect_review as fixture
from maxclient.core.config import Settings
from maxclient.core.models import Message
from maxclient.ui.chat_view import ChatView
from test_desktop_shell import _load_offscreen_font


class VisibleReadReceiptTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        _load_offscreen_font(cls.app)

    def setUp(self):
        self.case = fixture.HistoryReconnectReviewTests()
        self.case.setUp()
        self.service = self.case.service
        self.service._server_chats[303]['participants'][101] = fixture.STAMP - 1
        self.service.calls_available = Mock(return_value=False)
        self.service.mark_chat_read = Mock(side_effect=AssertionError('No real read RPC'))
        self.calls = []
        self.work = patch('maxclient.ui.read_receipts.run_async',
                          side_effect=lambda method,*args,**kw:self.calls.append((method,args,kw)))
        self.history = patch('maxclient.ui.chat_view.run_async')
        self.work.start(); self.history.start()
        self.view = ChatView(self.service, Settings(theme='light'))
        self.view.resize(700, 480)
        self.view.show()
        self.view.activateWindow()
        self.view.open_chat(self.case.store.get_chat(303))
        self.receipts = self.view.read_receipts
        self.messages = [Message(chat_id=303,id=i,time_ms=fixture.STAMP+i,
            text=f'Synthetic row {i}',status='sent') for i in range(1,21)]
        self.view._set_messages(self.messages)
        self.settle()
        self.foreground = patch.object(self.receipts, '_foreground', return_value=True)
        self.foreground.start()

    def tearDown(self):
        self.foreground.stop()
        if isValid(self.view):
            self.view.close(); self.view.deleteLater()
            QCoreApplication.sendPostedEvents(self.view, QEvent.Type.DeferredDelete)
        self.app.processEvents()
        self.history.stop(); self.work.stop()
        self.service.mark_chat_read.assert_not_called()
        self.case.tearDown()

    def settle(self):
        for _ in range(5): self.app.processEvents()
        if hasattr(self, 'receipts'): self.receipts.timer.stop()

    def test_visible_bottom_selects_actual_latest_message_and_pinned_session(self):
        self.receipts.flush()
        self.assertEqual(len(self.calls),1)
        method,args,kw=self.calls[0]
        self.assertTrue(callable(method))
        self.assertEqual(args,(303,))
        self.assertEqual((kw['message_id'],kw['mark']),(20,fixture.STAMP+20))
        self.assertIs(kw['context'],self.case.context)

    def _run_queued(self):
        method, args, kwargs = self.calls[0]
        return method(*args, **{key: value for key, value in kwargs.items()
                                if key not in ('on_done', 'on_error')})

    def test_queued_receipt_is_cancelled_after_placeholder_closes_chat(self):
        self.receipts.flush()
        self.view.show_placeholder()
        self.assertTrue(self._run_queued()['cancelled'])
        self.calls[0][2]['on_done']({'success': False, 'cancelled': True})
        self.assertEqual(self.receipts._attempted, {})

    def test_queued_receipt_is_cancelled_after_same_chat_reopen_changes_generation(self):
        self.receipts.flush()
        self.view.open_chat(self.case.store.get_chat(303))
        self.assertTrue(self._run_queued()['cancelled'])

    def test_queued_receipt_is_cancelled_after_session_change(self):
        self.receipts.flush()
        self.case.client._auth_session = self.case.context_for(self.case.client)
        self.assertTrue(self._run_queued()['cancelled'])

    def test_queued_receipt_is_cancelled_if_visibility_ended(self):
        self.receipts.flush()
        self.receipts._foreground = Mock(return_value=False)
        self.receipts.schedule()
        self.assertTrue(self._run_queued()['cancelled'])

    def test_queued_worker_never_queries_qwidget_foreground_state(self):
        self.receipts.flush()
        with patch.object(self.receipts, '_foreground', side_effect=AssertionError('GUI query in worker')):
            with patch.object(self.service, 'mark_chat_read', return_value={'success': True}) as send:
                self._run_queued()
        send.assert_called_once()

    def test_queued_receipt_is_cancelled_after_widget_deletion(self):
        self.receipts.flush()
        self.view.deleteLater()
        QCoreApplication.sendPostedEvents(self.view, QEvent.Type.DeferredDelete)
        self.assertTrue(self._run_queued()['cancelled'])

    def test_current_queued_receipt_calls_exact_original_frontier_once(self):
        self.receipts.flush()
        with patch.object(self.service, 'mark_chat_read', return_value={'success': True}) as send:
            self._run_queued()
        send.assert_called_once_with(303, message_id=20, mark=fixture.STAMP+20,
                                     context=self.case.context)

    def test_scrolled_history_marks_only_last_visible_not_loaded_latest(self):
        self.view.scroll.verticalScrollBar().setValue(0)
        self.settle()
        self.receipts.flush()
        self.assertEqual(len(self.calls),1)
        self.assertLess(self.calls[0][2]['message_id'],20)
        self.assertGreater(self.calls[0][2]['message_id'],0)

    def test_partial_long_message_bottom_must_be_visible_before_read(self):
        self.view._set_messages([Message(chat_id=303,id=50,time_ms=fixture.STAMP+50,
            text='Long synthetic line\n'*100)])
        self.settle()
        self.view.scroll.verticalScrollBar().setValue(0)
        self.settle()
        self.receipts.flush()
        self.assertEqual(self.calls,[])
        self.view.scroll.verticalScrollBar().setValue(self.view.scroll.verticalScrollBar().maximum())
        self.settle(); self.receipts.flush()
        self.assertEqual(self.calls[0][2]['message_id'],50)

    def test_hidden_inactive_minimized_and_modal_views_do_not_mark(self):
        self.foreground.stop()
        self.view.isActiveWindow=Mock(return_value=True)
        self.assertTrue(self.receipts._foreground())
        self.view.hide(); self.receipts.flush()
        self.assertFalse(self.receipts._foreground())
        self.view.show(); self.view.isActiveWindow.return_value=False
        self.receipts.flush(); self.assertFalse(self.receipts._foreground())
        self.view.isActiveWindow.return_value=True
        self.view.showMinimized(); self.receipts.flush()
        self.assertFalse(self.receipts._foreground())
        self.view.showNormal()
        with patch.object(QApplication,'activeModalWidget',return_value=QDialog()):
            self.receipts.flush(); self.assertFalse(self.receipts._foreground())
        self.assertEqual(self.calls,[])

    def test_search_target_never_marks_current_or_unseen_history(self):
        self.foreground.stop()
        self.view.isActiveWindow=Mock(return_value=True)
        self.view._history_target=(303,1,7)
        self.receipts.flush()
        self.assertEqual(self.calls,[])

    def test_changed_session_before_timer_never_submits(self):
        self.case.client._auth_session=self.case.context_for(self.case.client)
        # Old metadata may bind same-owner reconnect; core read context must
        # still be the newly confirmed session rather than the old object.
        self.case.client._auth_session=None
        self.receipts.flush()
        self.assertEqual(self.calls,[])

    def test_pending_failed_removed_and_unknown_are_not_frontiers(self):
        for status in ('pending','failed','removed','unknown'):
            with self.subTest(status=status):
                self.view._set_messages([Message(chat_id=303,id=50,time_ms=fixture.STAMP+50,
                                                text='Unconfirmed',status=status)])
                self.settle(); self.receipts.flush()
        self.assertEqual(self.calls,[])

    def test_outgoing_confirmed_message_is_a_visible_read_frontier(self):
        self.view._set_messages([Message(chat_id=303,id=51,time_ms=fixture.STAMP+51,
                                        text='Confirmed own message',outgoing=True,status='sent')])
        self.settle(); self.receipts.flush()
        self.assertEqual(self.calls[0][2]['message_id'],51)

    def test_single_pending_and_no_automatic_retry_for_unknown_outcome(self):
        self.receipts.flush(); self.receipts.flush()
        self.assertEqual(len(self.calls),1)
        self.calls[0][2]['on_error']('Synthetic unknown outcome')
        self.receipts.timer.stop(); self.receipts.flush()
        self.assertEqual(len(self.calls),1)

    def test_new_explicit_unread_after_success_can_read_same_visible_message_again(self):
        self.receipts.flush()
        raw=self.service._server_chats[303]
        raw['participants'][101]=fixture.STAMP+20
        self.case.store.set_unread(303,0)
        self.calls[0][2]['on_done']({'mark':fixture.STAMP+20,'unread':0})
        self.receipts.timer.stop(); self.receipts.flush()
        self.assertEqual(len(self.calls),1)
        raw['participants'][101]=fixture.STAMP+19
        raw['markedAsUnread']=True
        self.case.store.set_unread(303,1)
        self.receipts.flush()
        self.assertEqual(len(self.calls),2)
        self.assertEqual(self.calls[1][2]['message_id'],20)

    def test_new_visible_push_is_queued_after_previous_receipt_finishes(self):
        self.receipts.flush()
        self.view._set_messages(self.messages+[Message(chat_id=303,id=21,
            time_ms=fixture.STAMP+21,text='New visible arrival')])
        self.settle(); self.receipts.flush()
        self.assertEqual(len(self.calls),1)
        self.calls[0][2]['on_done']({'mark':fixture.STAMP+20,'unread':1})
        self.receipts.timer.stop(); self.receipts.flush()
        self.assertEqual(len(self.calls),2)
        self.assertEqual(self.calls[1][2]['message_id'],21)

    def test_cache_or_disconnect_exceptions_fail_closed(self):
        for exc in (ConnectionError('offline'),RuntimeError('closed cache')):
            self.service.read_receipt_state=Mock(side_effect=exc)
            self.receipts.flush()
        self.assertEqual(self.calls,[])

    def test_already_read_frontier_does_not_repeat_rpc(self):
        self.service._server_chats[303]['participants'][101]=fixture.STAMP+20
        self.receipts.flush()
        self.assertEqual(self.calls,[])

    def test_marked_unread_does_not_mark_older_visible_history_or_loop_equal_ack(self):
        self.service._server_chats[303]['participants'][101]=fixture.STAMP+100
        self.service._server_chats[303]['markedAsUnread']=True
        self.receipts.flush(); self.receipts.flush()
        self.assertEqual(self.calls,[])

    def test_fractional_time_keeps_exact_frontier(self):
        self.messages[-1].time_ms=fixture.STAMP+20.5
        self.receipts.flush()
        self.assertEqual(self.calls[0][2]['mark'],fixture.STAMP+20.5)

    def test_short_visibility_has_no_receipt_until_debounce(self):
        self.receipts.schedule()
        self.assertEqual(self.receipts.timer.interval(),200)
        QTest.qWait(40)
        self.assertEqual(self.calls,[])
        self.receipts._foreground=Mock(return_value=False)
        self.receipts.schedule()
        QTest.qWait(210)
        self.assertEqual(self.calls,[])

    def test_deleted_view_discards_late_callbacks(self):
        self.receipts.flush()
        callback=self.calls[0][2]['on_done']
        self.view.deleteLater()
        QCoreApplication.sendPostedEvents(self.view,QEvent.Type.DeferredDelete)
        callback({'mark':fixture.STAMP,'unread':0})
        self.assertEqual(len(self.calls),1)

    def test_destroyed_scroll_with_live_view_does_not_touch_deleted_qt(self):
        self.foreground.stop()
        self.view.scroll.deleteLater()
        QCoreApplication.sendPostedEvents(self.view.scroll,QEvent.Type.DeferredDelete)
        self.assertTrue(isValid(self.view))
        self.receipts.schedule(); self.receipts.flush(); self.app.processEvents()
        self.assertEqual(self.calls,[])

    def test_manual_read_uses_the_menu_session_and_rejects_stale_binding(self):
        binding=self.view.chat_metadata_binding()
        self.view._mark_read(self.messages[-1],binding)
        # The separately intercepted ChatView runner captures only UI dispatch.
        from maxclient.ui import chat_view
        self.assertIs(chat_view.run_async.call_args.kwargs['context'],self.case.context)
        chat_view.run_async.reset_mock()
        self.case.client._auth_session=None
        self.view._mark_read(self.messages[-1],binding)
        chat_view.run_async.assert_not_called()
