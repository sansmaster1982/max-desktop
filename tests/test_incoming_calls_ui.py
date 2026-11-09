"""Synthetic widgets only: no WebEngine/server/SDK/auth RPC or media is opened."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from dataclasses import replace
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QApplication, QPushButton, QWidget

from maxclient.core.config import Settings
from maxclient.core.incoming_calls import IncomingCallNotice
from maxclient.protocol.client import AuthenticatedSession, ConnectionState
from maxclient.ui.main_window import MainWindow


class FakeService:
    def __init__(self):
        context = AuthenticatedSession(101, 'synthetic-device', 'WEB', object())
        self.client = SimpleNamespace(state=ConnectionState.CONNECTED, device_id='synthetic-device',
                                      authenticated_session=context)
        self.session = SimpleNamespace(my_user_id=101, my_name='Тест', device_id='synthetic-device')
        self.my_id = 101
        self.store = SimpleNamespace(get_chat=lambda _id: None, get_contact=lambda _id: None)
        self.logout = Mock()
        self.backend_requests = []

    def list_chats(self): return []
    def sync_chats(self): raise AssertionError('No network in incoming UI tests')
    def calls_available(self, chat): return False

    def is_current_incoming_call(self, notice):
        return (isinstance(notice, IncomingCallNotice) and notice.owner_id == self.my_id
                and notice.source_client is self.client
                and notice.session_context is self.client.authenticated_session
                and self.client.state == ConnectionState.CONNECTED)

    def notice(self, conversation='synthetic-conversation', call_type='AUDIO'):
        return IncomingCallNotice(101, self.client, self.client.authenticated_session,
                                  77, 55, conversation, call_type, 'synthetic-private-vcp')


class FakeBackend:
    def __init__(self, service): self.service = service
    def config(self): return SimpleNamespace()
    def is_authorized(self): return True
    def rpc(self, opcode, payload):
        self.service.backend_requests.append(opcode)
        raise AssertionError('A passive incoming notice must never request a call token or RPC')


class FakeCallWindow(QWidget):
    closure_unknown = Signal(str)
    created = []
    on_create = None

    def __init__(self, runtime, config, **kwargs):
        super().__init__(kwargs.get('parent'))
        self.runtime, self.incoming, self.caption = runtime, kwargs.get('incoming'), kwargs.get('title')
        self.audio_call, self.video_call = QPushButton(self), QPushButton(self)
        self.shown = False
        self.local_closes = 0
        type(self).created.append(self)
        if type(self).on_create:
            type(self).on_create()

    def show(self): self.shown = True; super().show()
    def closeEvent(self, event):
        self.local_closes += 1
        self.runtime.close()
        event.accept()


class IncomingCallsUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.service = FakeService()
        FakeCallWindow.created, FakeCallWindow.on_create = [], None
        self.patches = [
            patch('maxclient.ui.main_window.run_async'),
            patch('maxclient.ui.main_window.DesktopNotifications'),
            patch('maxclient.ui.main_window.QMessageBox.information'),
            patch('maxclient.core.call_backend.MaxCallBackend', FakeBackend),
            patch('maxclient.ui.call_window.CallWindow', FakeCallWindow),
        ]
        for item in self.patches: item.start()
        settings = Settings(theme='light')
        settings.save = Mock()  # No settings/account files, including .local/AppData.
        self.window = MainWindow(self.service, settings)
        self.window.show()
        self.app.processEvents()

    def tearDown(self):
        FakeCallWindow.on_create = None
        self.window.close()
        for call in FakeCallWindow.created:
            call.close()
        self.app.processEvents()
        for item in reversed(self.patches): item.stop()

    def test_notice_opens_one_passive_window_without_sdk_auth_or_rpc(self):
        notice = self.service.notice(call_type='VIDEO')
        self.service.on_incoming_call(notice)
        self.app.processEvents()
        self.assertEqual(len(FakeCallWindow.created), 1)
        call = FakeCallWindow.created[0]
        self.assertTrue(call.shown)
        self.assertEqual(call.incoming, {'conversationId':notice.conversation_id, 'vcp':notice.vcp})
        self.assertEqual(call.caption, 'Входящий видеозвонок MAX')
        self.assertIsNone(call.runtime._intent)
        self.assertEqual(self.service.backend_requests, [])

    def test_duplicate_and_locally_dismissed_invite_do_not_reopen(self):
        notice = self.service.notice()
        self.window._on_incoming_call(notice)
        self.window._on_incoming_call(replace(notice))
        call = self.window._call_window
        call.close()
        self.window._release_call_window(call)
        self.window._on_incoming_call(replace(notice))
        self.assertEqual(len(FakeCallWindow.created), 1)
        self.assertEqual(self.service.backend_requests, [])

    def test_queued_old_context_is_rechecked_after_gui_delivery(self):
        notice = self.service.notice()
        thread = threading.Thread(target=lambda:self.service.on_incoming_call(notice))
        thread.start();thread.join()
        self.service.client.authenticated_session = AuthenticatedSession(101, 'synthetic-device', 'WEB', object())
        self.app.processEvents()
        self.assertEqual(FakeCallWindow.created, [])

    def test_owner_source_client_and_missing_checker_are_rejected(self):
        notice = self.service.notice()
        self.window._on_incoming_call(replace(notice, owner_id=999))
        self.window._on_incoming_call(replace(notice, source_client=object()))
        self.service.is_current_incoming_call = None
        self.window._on_incoming_call(notice)
        self.assertEqual(FakeCallWindow.created, [])

    def test_hidden_and_logout_closed_main_window_ignore_queued_invites(self):
        self.window.hide()
        self.window._on_incoming_call(self.service.notice('hidden'))
        self.window.show()
        self.window._handle_logout()
        self.window._on_incoming_call(self.service.notice('after-logout'))
        self.assertEqual(FakeCallWindow.created, [])

    def test_auth_invalid_disables_presentation_without_real_dialog(self):
        self.window._on_auth_invalid()
        self.window._on_incoming_call(self.service.notice())
        self.assertEqual(FakeCallWindow.created, [])

    def test_occupied_call_slot_is_preserved_without_busy_or_decline_rpc(self):
        occupied = SimpleNamespace(close=Mock())
        self.window._call_window = occupied
        self.window._on_incoming_call(self.service.notice())
        self.assertIs(self.window._call_window, occupied)
        self.assertEqual(FakeCallWindow.created, [])
        self.assertEqual(self.service.backend_requests, [])

    def test_changed_context_during_construction_cannot_show_a_stale_window(self):
        notice = self.service.notice()
        def change_context():
            self.service.client.authenticated_session = AuthenticatedSession(101, 'synthetic-device', 'WEB', object())
        FakeCallWindow.on_create = change_context
        self.window._on_incoming_call(notice)
        call = FakeCallWindow.created[0]
        self.assertFalse(call.shown)
        self.assertTrue(call.runtime._closed)
        self.assertEqual(self.service.backend_requests, [])

    def test_main_hiding_during_construction_cannot_show_a_late_invite(self):
        FakeCallWindow.on_create = self.window.hide
        self.window._on_incoming_call(self.service.notice())
        call = FakeCallWindow.created[0]
        self.assertFalse(call.shown)
        self.assertTrue(call.runtime._closed)

    def test_context_change_after_display_invalidates_runtime_action_guard(self):
        self.window._on_incoming_call(self.service.notice())
        runtime = self.window._call_window.runtime
        self.service.client.authenticated_session = AuthenticatedSession(101, 'synthetic-device', 'WEB', object())
        self.assertFalse(runtime._authorized())

    def test_old_window_destroyed_callback_does_not_clear_a_new_slot(self):
        old, current = object(), object()
        self.window._call_window = current
        self.window._release_call_window(old)
        self.assertIs(self.window._call_window, current)
        self.window._release_call_window(current)
        self.assertIsNone(self.window._call_window)

    def test_ui_dedupe_memory_is_bounded_per_current_context(self):
        self.window._call_window = SimpleNamespace(close=Mock())
        for i in range(150):
            self.window._on_incoming_call(self.service.notice('synthetic-'+str(i)))
        self.assertEqual(len(self.window._incoming_seen), 128)
        self.assertEqual(len(self.window._incoming_order), 128)
        self.assertEqual(self.service.backend_requests, [])


if __name__ == '__main__': unittest.main()
