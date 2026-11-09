"""Read/pin row operations use synthetic identities and an explicit local queue."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from dataclasses import dataclass
import unittest
from unittest.mock import patch

from PySide6.QtCore import QPoint, Qt
from PySide6.QtWidgets import QApplication
from shiboken6 import isValid

import test_chat_row_menu_context as context_fixture
from test_chat_row_menu_context import RowService


@dataclass(frozen=True, eq=False)
class ReadContext:
    conversation: object
    anchor: int


@dataclass(frozen=True, eq=False)
class PinContext:
    conversation: object
    folder_id: str


class ReadPinService(RowService):
    def __init__(self):
        super().__init__()
        self.read_contexts = []
        self.pin_contexts = []
        self.pin_reads = []
        self.anchor = 20
        self.unread = False
        self.read_pending = False
        self.read_available = True
        self.pinned = False
        self.pin_pending = False
        self.pin_available = True
        self.pin_limit = 5

    def get_folder_view(self, folder, chats=None):
        return {'hidden': False, 'chatIds': [1], 'complete': True, 'unreadCount': 0}

    def capture_conversation_read(self, context):
        result = ReadContext(context, self.anchor)
        self.read_contexts.append(result)
        return result

    def get_conversation_read_state(self, context):
        if not self.is_current_conversation(context.conversation):
            raise ValueError('Stale read context')
        return dict(context=context, unread=self.unread, pending=self.read_pending,
                    available=self.read_available and not self.read_pending and context.anchor == self.anchor)

    def set_conversation_read(self, context, read):
        state = self.get_conversation_read_state(context)
        if not state['available']:
            raise ValueError('Unavailable read state')
        self.writes.append(('read', context, read))
        self.unread = not read
        return dict(self.get_conversation_read_state(context), confirmed=True, accepted=True)

    def refresh_conversation_read_state(self, context):
        self.writes.append(('readback', context))
        return self.get_conversation_read_state(context)

    def capture_conversation_pin(self, context, folder_id):
        result = PinContext(context, folder_id)
        self.pin_contexts.append(result)
        return result

    def is_current_conversation_pin(self, context):
        return self.is_current_conversation(context.conversation)

    def conversation_pin_status(self, context):
        if not self.is_current_conversation_pin(context):
            raise ValueError('Stale pin context')
        return dict(context=context, folder_id=context.folder_id, pending=self.pin_pending,
                    inflight=False, available=self.pin_available and not self.pin_pending, limit=self.pin_limit)

    def get_conversation_pin_state(self, context):
        self.pin_reads.append(context)
        return dict(self.conversation_pin_status(context), pinned=self.pinned,
                    can_pin=self.pin_available, can_unpin=True,
                    folder=dict(id=context.folder_id, title=context.folder_id, include=[], filters=[],
                                favorites=[1] if self.pinned else []))

    def set_conversation_pinned(self, context, pinned):
        if not self.conversation_pin_status(context)['available']:
            raise ValueError('Unavailable pin')
        self.writes.append(('pin', context, pinned))
        self.pinned = pinned
        return dict(self.get_conversation_pin_state(context), confirmed=True, accepted=True)


class ChatRowReadPinTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.case = context_fixture.ChatRowMenuContextTests()
        self.case.app = self.app
        with patch('test_chat_row_menu_context.RowService', ReadPinService):
            self.case.setUp()
        self.service = self.case.service
        self.window = self.case.window
        self.queue = self.case.queue
        records = [dict(id=value, title=value, include=[], filters=[], favorites=[]) for value in ('a', 'b')]
        self.window._on_folders(dict(folderSync=1, foldersOrder=['a', 'b'], orderedFolders=records),
                                context=self.service.security_session)
        self.queue.clear()

    def tearDown(self):
        self.case.tearDown()

    def menu(self, *, load=True):
        menu = self.window._build_chat_row_menu(1)
        self.assertIsNotNone(menu)
        menu.popup(self.window.mapToGlobal(QPoint(120, 100)))
        self.app.processEvents()
        if load:
            for job in list(self.queue):
                self.queue.remove(job)
                self.case._run(job)
        return menu

    def action(self, menu, name):
        return next(action for action, row, _child in menu._rows if row['action'] == name)

    def open_chat(self):
        self.window.chat_view.open_chat(self.service.chats[0])
        self.window._current_chat_id = 1
        self.window.chat_view.input.setPlainText('Synthetic draft')

    def test_popup_only_captures_read_and_exact_selected_folder_without_writes(self):
        self.open_chat()
        self.menu()
        self.assertEqual(self.service.writes, [])
        self.assertEqual(self.window.chat_view.chat.id, 1)
        self.assertEqual(self.window.chat_view.input.toPlainText(), 'Synthetic draft')
        self.assertEqual(self.service.pin_contexts[-1].folder_id, 'a')
        self.assertEqual(self.service.read_contexts[-1].anchor, 20)

    def test_pin_is_disabled_until_authoritative_folder_read_returns(self):
        menu = self.menu(load=False)
        self.assertFalse(self.action(menu, 'toggle_pin').isEnabled())
        for job in list(self.queue):
            self.queue.remove(job)
            self.case._run(job)
        self.assertEqual(self.action(menu, 'toggle_pin').text(), 'Закрепить')
        self.assertTrue(self.action(menu, 'toggle_pin').isEnabled())

    def test_explicit_pin_dispatch_uses_captured_folder_context_and_no_optimistic_value(self):
        menu = self.menu()
        context = self.window._row_pin_context
        self.action(menu, 'toggle_pin').trigger()
        self.assertFalse(self.service.pinned)
        self.assertEqual(self.window._folders['folder:a']['favorites'], [])
        self.assertEqual(len(self.queue), 1)
        self.case._run(self.queue.pop())
        self.assertEqual(self.service.writes, [('pin', context, True)])
        self.assertEqual(self.window._folders['folder:a']['favorites'], [1])
        self.assertEqual(self.window._folders['folder:b']['favorites'], [])

    def test_unpin_uses_false_target_from_confirmed_pinned_state(self):
        self.service.pinned = True
        menu = self.menu()
        self.assertEqual(self.action(menu, 'toggle_pin').text(), 'Открепить')
        self.action(menu, 'toggle_pin').trigger()
        self.case._run(self.queue.pop())
        self.assertEqual(self.service.writes[0][2], False)

    def test_selected_folder_switch_revokes_old_popup_and_queued_pin(self):
        menu = self.menu()
        self.action(menu, 'toggle_pin').trigger()
        job = self.queue.pop()
        self.window._select_section('folder:b')
        self.assertIsNone(self.window._row_menu_context)
        self.case._run(job)
        self.assertEqual(self.service.writes, [])

    def test_selected_folder_removed_revokes_context_without_guessing_a_replacement(self):
        menu = self.menu()
        old_action = self.action(menu, 'toggle_pin')
        self.window._on_folders(dict(folderSync=2, foldersOrder=['b'], orderedFolders=[
            dict(id='b', title='b', filters=[], include=[], favorites=[])]))
        if isValid(old_action):
            old_action.trigger()
        self.assertIsNone(self.window._row_menu_context)
        self.assertEqual(self.service.writes, [])

    def test_no_pin_context_is_guessed_for_local_fallback_all_tab(self):
        self.window._cancel_chat_row_menu()
        self.window._folders = {}
        self.window._section = 'all'
        menu = self.menu()
        self.assertIsNone(self.window._row_pin_context)
        self.assertFalse(self.action(menu, 'toggle_pin').isEnabled())

    def test_late_pin_read_after_folder_switch_does_not_relabel_new_menu(self):
        self.menu(load=False)
        jobs = list(self.queue)
        self.queue.clear()
        pin_job = jobs[-1]
        result = self.case._run(pin_job, callbacks=False)
        self.window._select_section('folder:b')
        self.menu(load=False)
        pin_job[3](result)
        self.assertEqual(self.window._row_pin_context.folder_id, 'b')
        self.assertIsNone(self.window._row_pin_state)

    def test_explicit_unread_closes_only_matching_active_chat_and_keeps_draft(self):
        self.open_chat()
        menu = self.menu()
        context = self.window._row_read_context
        self.action(menu, 'toggle_read').trigger()
        self.assertIsNone(self.window.chat_view.chat)
        self.assertEqual(self.window.chat_view._drafts[1], 'Synthetic draft')
        self.assertFalse(self.window.chat_view.read_receipts.timer.isActive())
        self.assertFalse(self.service.unread)
        self.case._run(self.queue.pop())
        self.assertEqual(self.service.writes, [('read', context, False)])

    def test_read_all_does_not_close_matching_chat(self):
        self.service.unread = True
        self.open_chat()
        menu = self.menu()
        self.assertEqual(self.action(menu, 'toggle_read').text(), 'Прочитать всё')
        self.action(menu, 'toggle_read').trigger()
        self.assertEqual(self.window.chat_view.chat.id, 1)
        self.case._run(self.queue.pop())
        self.assertEqual(self.service.writes[0][2], True)

    def test_unread_for_another_row_does_not_close_current_chat(self):
        self.open_chat()
        self.window.chat_view.chat.id = 2
        # Keep the selected row identity in the store separate from the view.
        from dataclasses import replace
        self.service.chats[0] = replace(self.service.chats[0], id=1)
        menu = self.menu()
        self.action(menu, 'toggle_read').trigger()
        self.assertEqual(self.window.chat_view.chat.id, 2)
        self.case._run(self.queue.pop())
        self.assertEqual(self.service.writes[0][0], 'read')

    def test_new_last_message_before_worker_prevents_stale_frontier_write(self):
        menu = self.menu()
        self.action(menu, 'toggle_read').trigger()
        job = self.queue.pop()
        self.service.anchor = 21
        self.case._run(job)
        self.assertEqual(self.service.writes, [])

    def test_changed_read_state_before_click_does_not_invert_newer_state(self):
        menu = self.menu()
        action = self.action(menu, 'toggle_read')
        self.service.unread = True
        action.trigger()
        self.assertEqual(self.queue, [])
        self.assertEqual(self.service.writes, [])

    def test_unknown_read_outcome_disables_writes_and_only_explicit_readback_runs(self):
        menu = self.menu()
        self.action(menu, 'toggle_read').trigger()
        job = self.queue.pop()
        self.service.read_pending = True
        job[4](RuntimeError('Synthetic unknown outcome'))
        menu = self.menu()
        self.assertFalse(self.action(menu, 'toggle_read').isEnabled())
        self.assertFalse(self.action(menu, 'toggle_pin').isEnabled())
        context = self.window._row_read_context
        self.action(menu, 'refresh_row_read').trigger()
        self.case._run(self.queue.pop())
        self.assertEqual(self.service.writes, [('readback', context)])
        self.assertTrue(self.service.read_pending)

    def test_changed_pin_capability_before_worker_stops_write(self):
        menu = self.menu()
        self.action(menu, 'toggle_pin').trigger()
        job = self.queue.pop()
        self.service.pin_available = False
        self.case._run(job)
        self.assertEqual(self.service.writes, [])

    def test_late_pin_confirmation_does_not_reorder_after_folder_switch(self):
        menu = self.menu()
        self.action(menu, 'toggle_pin').trigger()
        job = self.queue.pop()
        result = self.case._run(job, callbacks=False)
        self.window._select_section('folder:b')
        job[3](result)
        self.assertEqual(self.window._folders['folder:a']['favorites'], [])
        self.assertEqual(self.window._folders['folder:b']['favorites'], [])

    def test_foreign_folder_dto_cannot_overwrite_selected_folder(self):
        self.menu()
        context, pin_context = self.window._row_menu_context, self.window._row_pin_context
        result = self.service.get_conversation_pin_state(pin_context)
        result['folder'] = dict(result['folder'], id='b', favorites=[1])
        self.window._apply_chat_row_pin_folder(context, pin_context, result)
        self.assertEqual(self.window._folders['folder:a']['favorites'], [])

    def test_pin_limit_blocked_state_never_enables_pin(self):
        self.service.pin_available = False
        menu = self.menu()
        self.assertFalse(self.action(menu, 'toggle_pin').isEnabled())
        self.action(menu, 'toggle_pin').trigger()
        self.assertEqual(self.queue, [])


if __name__ == '__main__':
    unittest.main()
