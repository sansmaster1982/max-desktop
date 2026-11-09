"""Handle-only folder reordering with deferred, wholly synthetic workers."""
import copy
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import unittest
from unittest.mock import Mock, patch

from PySide6.QtCore import QEvent, QMimeData, QPoint, QPointF, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QAbstractItemView, QApplication

from maxclient.protocol.client import AuthenticatedSession, MaxError, MaxWriteNotStarted, MaxWriteOutcomeUnknown
from maxclient.ui.folders_panel import ALL_FOLDER, FoldersPanel, _DRAG_MIME
from maxclient.ui.theme import build_stylesheet
from test_folders_blocks_ui import folders, service


def snapshot(order=None):
    value = folders()
    value['writePending'] = False
    if order is not None:
        records = dict(zip(value['foldersOrder'], value['orderedFolders']))
        value['foldersOrder'] = list(order)
        value['orderedFolders'] = [records[key] for key in order]
    return value


class DragEvent:
    def __init__(self, source, token, point=QPoint(20, 130), actions=Qt.DropAction.MoveAction):
        self._source, self._point, self._actions = source, point, actions
        self._mime = QMimeData()
        self._mime.setData(_DRAG_MIME, token.encode('ascii'))
        self.accepted = False

    def source(self): return self._source
    def mimeData(self): return self._mime
    def possibleActions(self): return self._actions
    def position(self): return QPointF(self._point)
    def setDropAction(self, action): self.action = action
    def accept(self): self.accepted = True
    def ignore(self): self.accepted = False


class FolderDragTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        # Exercise the shipped list padding instead of inheriting whichever
        # application's stylesheet a previous test class happened to leave.
        self.addCleanup(self.app.setStyleSheet, self.app.styleSheet())
        self.app.setStyleSheet(build_stylesheet('light', '#0085FF'))
        self.runner = patch('maxclient.ui.folders_panel.run_async')
        self.run = self.runner.start()
        self.service = service()
        self.service.context = (101, object(), AuthenticatedSession(101, 'synthetic', 'WEB', object()))
        self.service._history_read_context.side_effect = lambda: self.service.context
        def require(context):
            current = self.service.context
            if context[0] != current[0] or context[1] is not current[1] or context[2] is not current[2]:
                raise MaxWriteNotStarted('Synthetic session changed')
        self.service._require_history_context.side_effect = require
        self.panel = FoldersPanel(self.service)
        self.panel._read_done(snapshot())
        self.panel.resize(640, 580)
        self.panel.show()
        self.app.processEvents()
        self.original = [ALL_FOLDER, 'work', 'locked']
        self.moved = [ALL_FOLDER, 'locked', 'work']

    def tearDown(self):
        self.panel.close()
        self.panel.deleteLater()
        self.app.processEvents()
        self.runner.stop()

    def displayed(self):
        return [self.panel.items.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.panel.items.count())]

    def perform_worker(self):
        call = self.run.call_args
        function, *args = call.args
        kwargs = dict(call.kwargs)
        done, failed = kwargs.pop('on_done'), kwargs.pop('on_error')
        try:
            result = function(*args, **kwargs)
        except Exception as error:
            failed(str(error))
        else:
            done(result)

    def start(self, identity='work', target=2):
        token = self.panel._begin_folder_drag(identity)
        self.assertIsNotNone(token)
        self.assertTrue(self.panel._drop_folder_drag(token, target))
        return token

    def test_drop_passes_exact_snapshot_context_and_full_order_without_optimism(self):
        producing = self.panel._snapshot_context
        token = self.start()
        self.assertEqual(self.displayed(), self.original)
        self.assertIs(self.panel._snapshot, token.snapshot)
        self.service.reorder_folders.return_value = dict(accepted=True, pending=False, snapshot=snapshot(self.moved))
        self.perform_worker()
        self.service.reorder_folders.assert_called_once_with(self.moved, context=producing, expected_order=self.original)
        self.assertEqual(self.displayed(), self.moved)
        self.assertIsNone(self.panel._pending)

    def test_all_noop_cancel_and_invalid_slots_do_not_queue_a_worker(self):
        self.assertIsNone(self.panel._begin_folder_drag(ALL_FOLDER))
        for index in (0, -1, 3, True, '2'):
            token = self.panel._begin_folder_drag('work')
            self.assertFalse(self.panel._drop_folder_drag(token, index))
        token = self.panel._begin_folder_drag('work')
        self.assertFalse(self.panel._drop_folder_drag(token, 1))
        token = self.panel._begin_folder_drag('work')
        self.panel._cancel_folder_drag()
        self.assertFalse(self.panel._drop_folder_drag(token, 2))
        self.run.assert_not_called()

    def test_locked_folder_can_move_and_unavailable_other_ids_are_preserved(self):
        value = snapshot()
        value['foldersOrder'].insert(2, 'missing')
        value['orderedFolders'].insert(2, None)
        self.panel._read_done(value)
        self.assertIsNone(self.panel._begin_folder_drag('missing'))
        self.start('locked', 1)
        self.assertEqual(self.panel._pending['order'], [ALL_FOLDER, 'locked', 'work', 'missing'])
        self.assertEqual(self.displayed(), [ALL_FOLDER, 'work', 'missing', 'locked'])

    def test_read_only_busy_pending_and_noncanonical_all_order_block_start(self):
        for field in ('_busy', '_pending', '_read_ok'):
            old = getattr(self.panel, field)
            setattr(self.panel, field, False if field == '_read_ok' else {'kind': 'order'} if field == '_pending' else True)
            self.assertIsNone(self.panel._begin_folder_drag('work'), field)
            setattr(self.panel, field, old)
        self.panel._snapshot['writePending'] = True
        self.assertIsNone(self.panel._begin_folder_drag('work'))
        self.panel._snapshot['writePending'] = False
        self.panel._snapshot['foldersOrder'] = ['work', ALL_FOLDER, 'locked']
        self.assertIsNone(self.panel._begin_folder_drag('work'))
        self.run.assert_not_called()

    def test_locale_theme_editor_hide_and_refresh_invalidate_gesture(self):
        actions = [lambda: self.panel.set_language('en'), lambda: self.panel.apply_theme('dark'),
                   lambda: self.panel._load_editor(self.panel._records['locked']), self.panel.hide, self.panel.refresh]
        for action in actions:
            with self.subTest(action=action):
                token = self.panel._begin_folder_drag('work')
                self.assertIsNotNone(token)
                action()
                self.assertFalse(self.panel._drop_folder_drag(token, 2))
        self.service.reorder_folders.assert_not_called()

    def test_same_owner_new_session_invalidates_display_and_drag(self):
        token = self.panel._begin_folder_drag('work')
        old = self.service.context
        self.service.context = (old[0], old[1], AuthenticatedSession(101, 'synthetic', 'WEB', object()))
        self.assertFalse(self.panel._drop_folder_drag(token, 2))
        self.assertIsNone(self.panel._begin_folder_drag('work'))
        self.run.assert_not_called()

    def test_reopening_after_reconnect_reads_new_snapshot_without_writing(self):
        old = self.service.context
        self.service.context = (old[0], old[1], AuthenticatedSession(101, 'synthetic', 'WEB', object()))
        self.service.get_folders.return_value = snapshot()
        self.panel.ensure_loaded()
        self.assertEqual(self.run.call_count, 1)
        self.assertTrue(self.panel._busy)
        self.perform_worker()
        self.assertIs(self.panel._snapshot_context, self.service.context)
        self.assertIsNotNone(self.panel._begin_folder_drag('work'))
        self.panel._cancel_folder_drag()
        self.panel.ensure_loaded()
        self.assertEqual(self.run.call_count, 1)
        self.service.reorder_folders.assert_not_called()

    def test_stale_handle_press_reveals_refresh_without_silent_disabled_state(self):
        self.assertTrue(self.panel.refresh_button.isHidden())
        old = self.service.context
        self.service.context = (old[0], old[1], AuthenticatedSession(101, 'synthetic', 'WEB', object()))
        grip = self.panel._row_widgets['work'].grip
        QTest.mouseClick(grip, Qt.MouseButton.LeftButton, pos=QPoint(5, 5))
        self.assertIsNone(self.panel._drag_ticket)
        self.assertFalse(self.panel._read_ok)
        self.assertFalse(self.panel.refresh_button.isHidden())
        self.assertTrue(self.panel.refresh_button.isEnabled())
        self.assertFalse(self.panel.status.isHidden())
        self.run.assert_not_called()

    def test_refresh_context_captured_before_worker_and_late_callback_is_rejected(self):
        old_snapshot = self.panel._snapshot
        self.panel.refresh()
        call = self.run.call_args
        old = self.service.context
        self.service.context = (old[0], old[1], AuthenticatedSession(101, 'synthetic', 'WEB', object()))
        with self.assertRaises(MaxWriteNotStarted):
            call.args[0]()
        self.service.get_folders.assert_not_called()
        call.kwargs['on_done'](snapshot(self.moved))
        self.assertIs(self.panel._snapshot, old_snapshot)
        self.assertFalse(self.panel._read_ok)
        self.assertIsNone(self.panel._begin_folder_drag('work'))

    def test_read_context_change_during_worker_does_not_adopt_result(self):
        def changed():
            old = self.service.context
            self.service.context = (202, old[1], AuthenticatedSession(202, 'synthetic', 'WEB', object()))
            return snapshot(self.moved)
        self.service.get_folders.side_effect = changed
        self.panel.refresh()
        self.perform_worker()
        self.assertEqual(self.displayed(), self.original)
        self.assertFalse(self.panel._read_ok)

    def test_old_callback_cannot_override_later_read_with_same_context_object(self):
        self.panel.refresh()
        old_done = self.run.call_args.kwargs['on_done']
        old_done(snapshot())
        self.panel.refresh()
        new_done = self.run.call_args.kwargs['on_done']
        old_done(snapshot(self.moved))
        self.assertTrue(self.panel._busy)
        self.assertEqual(self.displayed(), self.original)
        new_done(snapshot())
        self.assertFalse(self.panel._busy)

    def test_pending_ack_does_not_display_proposed_order_and_refresh_never_writes(self):
        token = self.start()
        value = snapshot(self.moved)
        value['writePending'] = True
        self.service.reorder_folders.return_value = dict(accepted=True, pending=True, snapshot=value)
        self.perform_worker()
        self.assertEqual(self.displayed(), self.original)
        self.assertFalse(self.panel._drop_folder_drag(token, 2))
        self.assertIsNone(self.panel._begin_folder_drag('locked'))
        self.service.get_folders.return_value = snapshot(self.moved)
        self.panel.refresh()
        self.perform_worker()
        self.assertEqual(self.displayed(), self.moved)
        self.assertIsNone(self.panel._pending)
        self.assertEqual(self.service.reorder_folders.call_count, 1)

    def test_unknown_result_blocks_repeat_after_unmatched_read(self):
        self.service.reorder_folders.side_effect = MaxWriteOutcomeUnknown('Synthetic timeout')
        self.start()
        self.perform_worker()
        pending = self.panel._pending
        self.service.get_folders.return_value = snapshot()
        self.panel.refresh()
        self.perform_worker()
        self.assertIs(self.panel._pending, pending)
        self.assertIsNone(self.panel._begin_folder_drag('work'))
        self.assertEqual(self.service.reorder_folders.call_count, 1)

    def test_unknown_drag_keeps_overview_when_disabling_focused_create_button(self):
        self.panel.activateWindow()
        self.panel.new_button.setFocus()
        self.app.processEvents()
        self.assertTrue(self.panel.new_button.hasFocus())
        self.assertTrue(self.panel.editor.isHidden())
        self.service.reorder_folders.side_effect = MaxWriteOutcomeUnknown('Synthetic unknown order')
        self.start()
        self.assertTrue(self.panel.editor.isHidden())
        self.perform_worker()
        self.app.processEvents()
        self.assertTrue(self.panel.editor.isHidden())
        self.assertIsNone(self.panel._selected)
        self.assertIsNotNone(self.panel._pending)
        self.assertEqual(self.displayed(), self.original)
        self.assertTrue(self.panel.refresh_button.isEnabled())
        self.assertFalse(self.panel.status.isHidden())
        self.assertFalse(self.panel.new_button.isEnabled())
        self.assertFalse(self.panel._row_widgets['work'].grip.isEnabled())
        self.assertFalse(self.panel.save_button.isEnabled())

    def test_restored_pending_order_does_not_open_an_unrelated_editor(self):
        self.panel.activateWindow()
        self.panel.new_button.setFocus()
        self.app.processEvents()
        self.assertTrue(self.panel.new_button.hasFocus())
        self.panel._pending = dict(kind='order', id='work', order=self.moved, _drag_context=self.service.context)
        self.panel._write_error('Synthetic restored pending order')
        self.app.processEvents()
        self.assertTrue(self.panel.editor.isHidden())
        self.assertIsNone(self.panel._selected)
        self.assertEqual(self.displayed(), self.original)
        self.assertIsNotNone(self.panel._pending)
        self.assertTrue(self.panel.refresh_button.isEnabled())

    def test_confirmed_drag_from_focused_create_keeps_overview(self):
        self.panel.activateWindow()
        self.panel.new_button.setFocus()
        self.app.processEvents()
        self.assertTrue(self.panel.new_button.hasFocus())
        self.service.reorder_folders.return_value = dict(accepted=True, pending=False, snapshot=snapshot(self.moved))
        self.start()
        self.assertTrue(self.panel.editor.isHidden())
        self.perform_worker()
        self.app.processEvents()
        self.assertIsNone(self.panel._pending)
        self.assertEqual(self.displayed(), self.moved)
        self.assertTrue(self.panel.editor.isHidden())
        self.assertIsNone(self.panel._selected)
        self.assertTrue(self.panel.new_button.isEnabled())
        self.assertTrue(self.panel.items.isEnabled())

    def test_confirmed_drag_preserves_previously_open_folder_editor(self):
        self.panel.items.setCurrentRow(2)
        self.assertEqual(self.panel._selected, 'locked')
        self.assertFalse(self.panel.editor.isHidden())
        self.service.reorder_folders.return_value = dict(accepted=True, pending=False, snapshot=snapshot(self.moved))
        self.start()
        self.perform_worker()
        self.app.processEvents()
        self.assertIsNone(self.panel._pending)
        self.assertEqual(self.displayed(), self.moved)
        self.assertEqual(self.panel._selected, 'locked')
        self.assertEqual(self.panel.title.text(), 'Папка MAX')
        self.assertFalse(self.panel.editor.isHidden())

    def test_known_not_started_and_explicit_rejection_require_refresh_then_allow_new_drag(self):
        for error in (MaxWriteNotStarted('Changed before write'), MaxError('Explicit negative acknowledgement')):
            with self.subTest(error=type(error).__name__):
                self.service.reorder_folders.side_effect = error
                self.start()
                self.perform_worker()
                self.assertIsNone(self.panel._pending)
                self.assertFalse(self.panel._read_ok)
                self.assertEqual(self.displayed(), self.original)
                self.assertIsNone(self.panel._begin_folder_drag('work'))
                self.service.get_folders.return_value = snapshot()
                self.panel.refresh()
                self.perform_worker()
                self.assertIsNotNone(self.panel._begin_folder_drag('work'))

    def test_untyped_valueerror_is_not_silently_treated_as_known_rejection(self):
        self.service.reorder_folders.side_effect = ValueError('Potential post-write error')
        self.start()
        self.perform_worker()
        self.assertIsNotNone(self.panel._pending)

    def test_known_not_started_can_clear_intent_after_session_change_but_cannot_bless_snapshot(self):
        self.start()
        old = self.service.context
        self.service.context = (old[0], old[1], AuthenticatedSession(101, 'synthetic', 'WEB', object()))
        self.service.reorder_folders.side_effect = MaxWriteNotStarted('Before transport')
        self.perform_worker()
        self.assertIsNone(self.panel._pending)
        self.assertFalse(self.panel._read_ok)
        self.assertIsNone(self.panel._begin_folder_drag('work'))

    def test_old_write_callback_cannot_move_rows_after_session_change(self):
        self.start()
        callback = self.run.call_args.kwargs['on_done']
        old = self.service.context
        self.service.context = (old[0], old[1], AuthenticatedSession(101, 'synthetic', 'WEB', object()))
        callback(dict(accepted=True, pending=False, snapshot=snapshot(self.moved)))
        self.assertEqual(self.displayed(), self.original)
        self.assertIsNotNone(self.panel._pending)
        self.assertFalse(self.panel._read_ok)

    def test_handle_threshold_and_cancel_use_move_only_without_selecting_editor(self):
        grip = self.panel._row_widgets['work'].grip
        QTest.mousePress(grip, Qt.MouseButton.LeftButton, pos=QPoint(5, 5))
        self.assertIsNotNone(self.panel._drag_ticket)
        self.assertIsNone(self.panel._selected)
        self.assertTrue(self.panel.editor.isHidden())
        with patch('maxclient.ui.folders_panel.QDrag') as drag:
            small = QMouseEvent(QEvent.Type.MouseMove, QPointF(6, 5), QPointF(6, 5),
                Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
            grip.mouseMoveEvent(small)
            drag.assert_not_called()
            large = QMouseEvent(QEvent.Type.MouseMove, QPointF(30, 5), QPointF(30, 5),
                Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
            grip.mouseMoveEvent(large)
            drag.return_value.exec.assert_called_once_with(Qt.DropAction.MoveAction)
            self.assertTrue(drag.return_value.setMimeData.call_args.args[0].hasFormat(_DRAG_MIME))
        self.assertIsNone(self.panel._drag_ticket)
        self.run.assert_not_called()

    def test_external_copy_tampered_and_other_panel_payloads_are_rejected(self):
        token = self.panel._begin_folder_drag('work')
        grip = self.panel._row_widgets['work'].grip
        grip._ticket = token
        good = DragEvent(grip, token.nonce)
        self.panel.items.dragEnterEvent(good)
        self.assertTrue(good.accepted)
        for source, nonce, action in ((None, token.nonce, Qt.DropAction.MoveAction),
                (grip, 'not-a-ticket', Qt.DropAction.MoveAction), (grip, token.nonce, Qt.DropAction.CopyAction),
                (self.panel._row_widgets['locked'].grip, token.nonce, Qt.DropAction.MoveAction)):
            event = DragEvent(source, nonce, actions=action)
            self.panel.items.dropEvent(event)
            self.assertFalse(event.accepted)
        other = FoldersPanel(self.service)
        try:
            other._read_done(snapshot())
            other._begin_folder_drag('work')
            event = DragEvent(grip, token.nonce)
            other.items.dropEvent(event)
            self.assertFalse(event.accepted)
        finally:
            other.close()
            other.deleteLater()
        self.run.assert_not_called()

    def test_drop_hit_testing_locks_all_and_indicator_overlay_does_not_reorder(self):
        token = self.panel._begin_folder_drag('work')
        grip = self.panel._row_widgets['work'].grip
        grip._ticket = token
        items = self.panel.items
        all_rect = items.visualItemRect(items.item(0))
        self.assertIsNone(items._drop_target(QPoint(20, all_rect.top() + 2), token))
        self.assertIsNone(items._drop_target(QPoint(-20, 50), token))
        items.scrollToItem(items.item(2), QAbstractItemView.ScrollHint.PositionAtBottom)
        self.app.processEvents()
        last = items.visualItemRect(items.item(2))
        visible = last.intersected(items.viewport().rect())
        point = QPoint(visible.center().x(), visible.bottom() - 3)
        self.assertTrue(items.viewport().rect().contains(point))
        self.assertGreater(point.y(), last.center().y())
        self.assertEqual(items._drop_target(point, token)[0], 2)
        event = DragEvent(grip, token.nonce, point)
        items.dragMoveEvent(event)
        self.assertTrue(event.accepted)
        self.assertTrue(items._indicator.isVisible())
        self.assertEqual(self.displayed(), self.original)
        items.dropEvent(event)
        self.assertTrue(event.accepted)
        self.assertTrue(items._indicator.isHidden())
        self.assertEqual(self.panel._pending['order'], self.moved)

    def test_edge_scroll_moves_viewport_only_and_stops_on_invalidation(self):
        value = snapshot()
        for i in range(20):
            value['foldersOrder'].append(f'extra-{i}')
            value['orderedFolders'].append(dict(id=f'extra-{i}', title=f'Folder {i}'))
        self.panel._read_done(value)
        self.app.processEvents()
        token = self.panel._begin_folder_drag('work')
        grip = self.panel._row_widgets['work'].grip
        grip._ticket = token
        items = self.panel.items
        event = DragEvent(grip, token.nonce, QPoint(20, items.viewport().height() - 3))
        items.dragMoveEvent(event)
        start = items.verticalScrollBar().value()
        items._scroll_drag()
        self.assertGreater(items.verticalScrollBar().value(), start)
        self.assertEqual(self.displayed(), value['foldersOrder'])
        self.panel.set_language('en')
        self.assertFalse(items._scroll_timer.isActive())
        self.assertTrue(items._indicator.isHidden())
        self.run.assert_not_called()

    def test_keyboard_move_buttons_bind_snapshot_context_and_order(self):
        context = self.service.context
        self.panel.items.setCurrentRow(1)
        self.panel.down_button.click()
        self.service.reorder_folders.assert_not_called()
        self.assertEqual(self.displayed(), self.original)
        self.perform_worker()
        self.service.reorder_folders.assert_called_once_with(
            self.moved, context=context, expected_order=self.original)

