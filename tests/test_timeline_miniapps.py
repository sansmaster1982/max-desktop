"""Synthetic OPEN_APP routing: never loads a real web page or authenticates."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from PySide6.QtCore import QCoreApplication, QEvent, Signal
from PySide6.QtWidgets import QApplication, QLabel, QPushButton, QVBoxLayout, QWidget

from maxclient.core.models import Attach, Message
from maxclient.ui.theme import palette_for
from maxclient.ui.timeline_miniapps import TimelineMiniApps
from maxclient.ui.widgets.message_bubble import MessageBubble
from test_desktop_shell import _load_offscreen_font


class _View(QWidget):
    def __init__(self):
        super().__init__()
        self.context = object()
        self.valid = True
        self.service = SimpleNamespace(open_message_app=Mock(), is_current_message_app=Mock(return_value=True))
        self.h_sub = QLabel(self)
        self._rows = []

    def file_context_current(self, context):
        return self.valid and context is self.context


class _MiniWindow(QWidget):
    closed = Signal()

    def closeEvent(self, event):
        self.closed.emit()
        super().closeEvent(event)


class TimelineMiniAppsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        _load_offscreen_font(cls.app)

    def setUp(self):
        self.requests = []
        self.network = patch('maxclient.ui.timeline_miniapps.run_async',
                             side_effect=lambda *args, **kwargs: self.requests.append((args, kwargs)))
        self.network.start()
        self.view = _View()
        self.controller = TimelineMiniApps(self.view)
        self.attach = Attach(type='INLINE_KEYBOARD', inline_metadata_confirmed=True,
                             inline_open_app_metadata_confirmed=True,
                             inline_buttons=[[{'type': 'OPEN_APP', 'text': 'Посмотреть код'}]])
        self.message = Message(id=91, chat_id=42, time_ms=123456789, attaches=[self.attach])
        self.bubble = QWidget(self.view)
        self.view._rows.append({'msg': self.message, 'bubble': self.bubble})

    def tearDown(self):
        self.controller.reset()
        self.view.close()
        self.view.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()
        self.network.stop()

    def open(self):
        self.controller.open(self.message, 0, 0, 0, self.view.context)

    def done(self, result=None):
        self.requests[-1][1]['on_done'](result or {'url': 'https://mini.example.test/#synthetic'})

    def test_only_explicit_action_issues_request_with_exact_context_and_indices(self):
        self.assertEqual(self.requests, [])
        self.open()
        self.assertEqual(self.requests[0][0], (self.view.service.open_message_app, self.message, 0, 0, 0))
        self.assertIs(self.requests[0][1]['context'], self.view.context)
        self.open()
        self.assertEqual(len(self.requests), 1)

    def test_success_opens_ephemeral_window_and_reset_closes_it(self):
        window = QWidget()
        with patch('maxclient.ui.timeline_miniapps.create_window', return_value=window) as create:
            self.open()
            self.done()
            self.assertTrue(window.isVisible())
            self.assertIs(create.call_args.args[1], self.view.context)
            self.assertIsNone(self.controller.pending)
            self.controller.reset()
            self.assertFalse(window.isVisible())
        window.deleteLater()

    def test_closed_window_restores_chat_status_without_clobbering_new_status(self):
        for replace in (False, True):
            with self.subTest(replace=replace):
                window = _MiniWindow()
                self.view.h_sub.setText('Сообщения обновлены')
                with patch('maxclient.ui.timeline_miniapps.create_window', return_value=window):
                    self.open()
                    self.done()
                    self.assertEqual(self.view.h_sub.text(), 'Мини-приложение открыто')
                    if replace:
                        self.view.h_sub.setText('Новое состояние')
                    window.close()
                    self.assertEqual(self.view.h_sub.text(), 'Новое состояние' if replace else 'Сообщения обновлены')
                window.deleteLater()

    def test_late_result_after_reset_never_opens(self):
        with patch('maxclient.ui.timeline_miniapps.create_window') as create:
            self.open()
            self.controller.reset()
            self.done()
            create.assert_not_called()

    def test_changed_account_or_context_never_opens(self):
        with patch('maxclient.ui.timeline_miniapps.create_window') as create:
            self.open()
            self.view.valid = False
            self.done()
            create.assert_not_called()

    def test_cache_revision_guard_blocks_result_and_remains_bound_to_window(self):
        with patch('maxclient.ui.timeline_miniapps.create_window') as create:
            self.open()
            self.view.service.is_current_message_app.return_value = False
            self.done()
            create.assert_not_called()

    def test_window_show_failure_closes_allocated_window(self):
        window = QWidget()
        window.close = Mock(wraps=window.close)
        window.show = Mock(side_effect=RuntimeError('private_secret'))
        with patch('maxclient.ui.timeline_miniapps.create_window', return_value=window):
            self.open()
            self.done()
            window.close.assert_called_once()
            self.assertIsNone(self.controller.window)
        window.deleteLater()

    def test_button_edit_or_detached_row_invalidates_pending_result(self):
        for change in ('caption', 'row', 'removed', 'attachment', 'time'):
            with self.subTest(change=change), patch('maxclient.ui.timeline_miniapps.create_window') as create:
                self.message.status = 'sent'
                self.message.attaches = [self.attach]
                self.attach.inline_buttons[0][0]['text'] = 'Посмотреть код'
                self.view._rows = [{'msg': self.message, 'bubble': self.bubble}]
                self.open()
                if change == 'caption':
                    self.attach.inline_buttons[0][0]['text'] = 'Другая кнопка'
                elif change == 'row':
                    self.view._rows = []
                elif change == 'removed':
                    self.message.status = 'removed'
                elif change == 'attachment':
                    self.message.attaches = []
                else:
                    self.message.time_ms += 1
                self.done()
                create.assert_not_called()
                self.assertIsNone(self.controller.pending)

    def test_failure_does_not_echo_url_or_credential_and_allows_explicit_retry(self):
        self.open()
        self.requests[-1][1]['on_error']('https://private.example/?credential=secret')
        self.assertNotIn('secret', self.view.h_sub.text())
        self.assertNotIn('private', self.view.h_sub.text())
        self.open()
        self.assertEqual(len(self.requests), 2)

    def test_invalid_reply_and_window_failure_are_literal_errors(self):
        self.open()
        self.done({'url': 123})
        self.assertIsNone(self.controller.pending)
        with patch('maxclient.ui.timeline_miniapps.create_window', side_effect=ValueError('private_secret')):
            self.open()
            self.done()
        self.assertNotIn('private_secret', self.view.h_sub.text())

    def test_invalid_indices_wrong_button_type_and_unconfirmed_metadata_do_not_request(self):
        for indices in ((True, 0, 0), (-1, 0, 0), (0, 4, 0), (0, 0, 4)):
            self.controller.open(self.message, *indices, self.view.context)
        self.attach.inline_metadata_confirmed = False
        self.open()
        self.attach.inline_metadata_confirmed = True
        self.attach.inline_buttons[0][0]['type'] = 'CALLBACK'
        self.open()
        self.assertEqual(self.requests, [])

    def test_bubble_requires_handler_and_emits_exact_button_position(self):
        self.message.attaches.insert(0, Attach(type='CONTROL'))
        self.attach.inline_buttons.insert(0, [{'type': 'CALLBACK', 'text': 'Недоступно'}])
        callback = Mock()
        bubble = MessageBubble(self.message, '#0085FF', palette_for('light'), on_open_app=callback)
        bubble.show()
        self.app.processEvents()
        button = next(button for button in bubble.findChildren(QPushButton)
                      if button.accessibleName() == 'Посмотреть код')
        self.assertTrue(button.isEnabled())
        button.click()
        callback.assert_called_once_with(self.message, 1, 1, 0)
        self.message.status = 'removed'
        button.click()
        self.assertEqual(callback.call_count, 1)
        bubble.close()
        bubble.deleteLater()


if __name__ == '__main__':
    unittest.main()
