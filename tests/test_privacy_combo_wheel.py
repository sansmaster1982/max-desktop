"""Real Qt wheel delivery on a fake-only privacy panel inside a scroll page."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import unittest
from unittest.mock import Mock, patch

from PySide6.QtCore import QCoreApplication, QEvent, QObject, QPoint, QPointF, Qt
from PySide6.QtGui import QWheelEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QScrollArea, QVBoxLayout, QWidget
from shiboken6 import isValid

from maxclient.ui.privacy_settings_panel import PrivacySettingsPanel


VALUES = {'SEARCH_BY_PHONE': 'ALL', 'INCOMING_CALL': 'ALL', 'CHATS_INVITE': 'ALL',
          'PHONE_NUMBER_PRIVACY': 'CONTACTS', 'HIDDEN': False, 'CONTENT_LEVEL_ACCESS': False}


class _WheelObserver(QObject):
    def __init__(self, viewport):
        super().__init__(viewport)
        self.deliveries = []
        viewport.installEventFilter(self)

    def eventFilter(self, watched, event):  # noqa: N802
        if event.type() == QEvent.Type.Wheel:
            self.deliveries.append((event.pixelDelta(), event.angleDelta()))
        return False


class PrivacyComboWheelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setStyle('Fusion')

    def setUp(self):
        self.service = Mock()
        self.context = (101, object(), object())
        self.service._history_read_context.return_value = self.context
        def require_context(context):
            if context is not self.context:
                raise ValueError('Synthetic stale context')
            return context[1]
        self.service._require_history_context.side_effect = require_context
        self.worker = patch('maxclient.ui.privacy_settings_panel.run_async')
        self.run = self.worker.start()
        self.page = QScrollArea()
        self.page.setWidgetResizable(True)
        self.page.resize(820, 430)
        content = QWidget()
        layout = QVBoxLayout(content)
        self.panel = PrivacySettingsPanel(self.service)
        layout.addWidget(self.panel)
        layout.addSpacing(850)
        self.page.setWidget(content)
        self.observer = _WheelObserver(self.page.viewport())
        self.page.show()
        self.panel._read_done(VALUES)
        self.app.processEvents()
        self.assertGreater(self.page.verticalScrollBar().maximum(), 0)
        self.run.assert_not_called()

    def tearDown(self):
        if isValid(self.page):
            self.page.close()
            self.page.deleteLater()
            QCoreApplication.sendPostedEvents(self.page, QEvent.Type.DeferredDelete)
        self.app.processEvents()
        self.worker.stop()
        # run_async is intercepted before anything can touch a real service.
        self.service.update_privacy_settings.assert_not_called()

    def wheel(self, target, *, angle=-120, pixels=0):
        local = QPointF(target.rect().center())
        event = QWheelEvent(local, QPointF(target.mapToGlobal(local.toPoint())),
                            QPoint(0, pixels), QPoint(0, angle),
                            Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
                            Qt.ScrollPhase.ScrollUpdate, False)
        QApplication.sendEvent(target, event)
        self.app.processEvents()
        return event

    def hidden_row(self, *, focused=True):
        row = self.panel.rows['HIDDEN']
        self.page.ensureWidgetVisible(row)
        if focused:
            row.setFocus()
        else:
            self.panel.refresh_button.setFocus()
        self.app.processEvents()
        return row

    def assert_one_hidden_intent(self):
        self.assertEqual(self.run.call_count, 1)
        self.assertEqual(self.run.call_args.args, (self.service.update_privacy_settings, {'HIDDEN': True}))
        self.assertIs(self.run.call_args.kwargs['context'], self.context)
        self.assertTrue(callable(self.run.call_args.kwargs['on_done']))
        self.assertTrue(callable(self.run.call_args.kwargs['on_error']))
        self.assertTrue(self.panel._busy)
        self.assertIs(self.panel.rows['HIDDEN'].current_value, False)

    def test_closed_wheel_does_not_queue_hidden_write_while_old_value_still_visible(self):
        row = self.hidden_row()
        self.assertIsNone(self.panel._menu)
        self.wheel(row)
        # The original bug restored this old value before scheduling the write;
        # data equality alone would therefore have missed the side effect.
        self.assertIs(row.current_value, False)
        self.run.assert_not_called()
        self.assertFalse(self.panel._busy)
        self.assertNotEqual(self.panel.status.text(), 'Сохранение…')

    def test_all_privacy_rows_ignore_wheel_even_with_keyboard_focus(self):
        for focused in (False, True):
            for key in VALUES:
                for direction in (-120, 120):
                    with self.subTest(key=key, focused=focused, angle=direction):
                        self.panel._read_done(VALUES)
                        row = self.panel.rows[key]
                        self.page.ensureWidgetVisible(row)
                        (row if focused else self.panel.refresh_button).setFocus()
                        self.app.processEvents()
                        before = row.current_value
                        self.wheel(row, angle=direction)
                        self.assertEqual((type(row.current_value), row.current_value), (type(before), before))
                        self.run.assert_not_called()
                        self.assertFalse(self.panel._busy)

    def test_closed_wheel_propagates_to_actual_parent_scroll_page(self):
        row = self.hidden_row()
        self.page.verticalScrollBar().setValue(0)
        before = self.page.verticalScrollBar().value()
        self.wheel(row)
        self.assertGreater(self.page.verticalScrollBar().value(), before)
        self.assertEqual(self.observer.deliveries, [(QPoint(), QPoint(0, -120))])
        self.run.assert_not_called()

    def test_pixel_wheel_reaches_scroll_viewport_without_setting_change(self):
        row = self.hidden_row(focused=False)
        # Platform/style may ignore pixel-only scrolling itself. Forwarding
        # must preserve its ordinary Qt behavior, not invent scroll arithmetic.
        self.page.verticalScrollBar().setValue(0)
        self.wheel(self.page.viewport(), angle=0, pixels=-35)
        native_position = self.page.verticalScrollBar().value()
        self.observer.deliveries.clear()
        self.page.verticalScrollBar().setValue(0)
        self.wheel(row, angle=0, pixels=-35)
        self.assertIs(row.current_value, False)
        self.run.assert_not_called()
        self.assertEqual(self.page.verticalScrollBar().value(), native_position)
        self.assertEqual(self.observer.deliveries, [(QPoint(0, -35), QPoint())])

    def test_intentional_keyboard_choice_still_queues_one_exact_change(self):
        row = self.hidden_row()
        QTest.keyClick(row, Qt.Key.Key_Space)
        self.app.processEvents()
        menu = self.panel._menu
        self.assertTrue(menu.isVisible())
        QTest.keyClick(menu, Qt.Key.Key_Down)
        self.assertIs(menu.activeAction().data(), True)
        self.run.assert_not_called()
        QTest.keyClick(menu, Qt.Key.Key_Return)
        self.app.processEvents()
        self.assert_one_hidden_intent()

    def test_intentional_popup_mouse_choice_still_queues_one_exact_change(self):
        row = self.hidden_row()
        QTest.mouseClick(row, Qt.MouseButton.LeftButton)
        self.app.processEvents()
        menu = self.panel._menu
        self.assertTrue(menu.isVisible())
        target = next(a for a in menu.actions() if a.data() is True)
        position = menu.actionGeometry(target).center()
        QTest.mouseMove(menu, position)
        self.run.assert_not_called()
        QTest.mouseClick(menu, Qt.MouseButton.LeftButton, pos=position)
        self.app.processEvents()
        self.assert_one_hidden_intent()

    def test_row_arrows_and_hover_do_not_select_or_queue_write(self):
        row = self.hidden_row()
        QTest.mouseMove(row, row.rect().center())
        for key in (Qt.Key.Key_Down, Qt.Key.Key_Up, Qt.Key.Key_Left, Qt.Key.Key_Right):
            QTest.keyClick(row, key)
        self.app.processEvents()
        self.run.assert_not_called()
        self.assertIs(row.current_value, False)
        self.assertIsNone(self.panel._menu)

    def test_open_popup_wheel_navigation_and_escape_never_commit(self):
        row = self.panel.rows['PHONE_NUMBER_PRIVACY']
        self.page.ensureWidgetVisible(row)
        QTest.mouseClick(row, Qt.MouseButton.LeftButton)
        self.app.processEvents()
        menu = self.panel._menu
        self.assertTrue(menu.isVisible())
        self.wheel(menu)
        QTest.keyClick(menu, Qt.Key.Key_Down)
        QTest.mouseMove(menu, menu.actionGeometry(menu.actions()[-1]).center())
        QTest.keyClick(menu, Qt.Key.Key_Escape)
        self.app.processEvents()
        self.run.assert_not_called()
        self.assertFalse(menu.isVisible())
        self.assertEqual(row.current_value, 'CONTACTS')

    def test_stale_action_after_popup_escape_cannot_write(self):
        row = self.hidden_row()
        QTest.mouseClick(row, Qt.MouseButton.LeftButton)
        self.app.processEvents()
        menu = self.panel._menu
        action = next(a for a in menu.actions() if a.data() is True)
        QTest.keyClick(menu, Qt.Key.Key_Escape)
        self.assertFalse(menu.isVisible())
        self.assertFalse(menu.selecting)
        action.trigger()
        self.app.processEvents()
        self.run.assert_not_called()
        self.assertIs(row.current_value, False)

    def test_explicit_current_choice_is_noop(self):
        row = self.hidden_row()
        QTest.mouseClick(row, Qt.MouseButton.LeftButton)
        self.app.processEvents()
        menu = self.panel._menu
        action = next(a for a in menu.actions() if a.data() is False)
        self.assertTrue(action.isChecked())
        QTest.mouseClick(menu, Qt.MouseButton.LeftButton, pos=menu.actionGeometry(action).center())
        self.app.processEvents()
        self.run.assert_not_called()
        self.assertIs(row.current_value, False)


if __name__ == '__main__':
    unittest.main()
