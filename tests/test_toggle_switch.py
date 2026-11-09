"""Switch semantics and rendering use only synthetic widgets, no settings writes."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import unittest
from pathlib import Path

from PySide6.QtCore import QPoint, QSignalBlocker, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QVBoxLayout, QWidget

from maxclient.ui.theme import build_stylesheet
from maxclient.ui.widgets.toggle_switch import ToggleSwitch
from test_desktop_shell import _load_offscreen_font


class ToggleSwitchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        _load_offscreen_font(cls.app)

    def test_keyboard_track_click_blocked_signals_and_disabled_semantics(self):
        control = ToggleSwitch('Уведомления', theme='light')
        control.resize(360, 44)
        changes = []
        control.toggled.connect(changes.append)
        control.show()
        control.setFocus()
        self.app.processEvents()
        QTest.keyClick(control, Qt.Key.Key_Space)
        self.assertTrue(control.isChecked())
        self.assertEqual(changes, [True])
        blocker = QSignalBlocker(control)
        control.setChecked(False)
        del blocker
        self.assertEqual(changes, [True])
        QTest.mouseClick(control, Qt.MouseButton.LeftButton, pos=QPoint(control.width() - 20, control.height() // 2))
        self.assertTrue(control.isChecked())
        self.assertEqual(changes, [True, True])
        control.setEnabled(False)
        QTest.keyClick(control, Qt.Key.Key_Space)
        self.assertTrue(control.isChecked())
        self.assertEqual(changes, [True, True])
        control.close()

    def test_wrapped_labels_and_both_schemes_render(self):
        for theme in ('light', 'dark'):
            root = QWidget()
            root.setStyleSheet(build_stylesheet(theme, '#0085FF'))
            layout = QVBoxLayout(root)
            for checked, enabled in [(False, True), (True, True), (False, False), (True, False)]:
                switch = ToggleSwitch('Уведомления в групповых чатах', theme=theme)
                switch.setChecked(checked)
                switch.setEnabled(enabled)
                layout.addWidget(switch)
                self.assertGreaterEqual(switch.heightForWidth(200), 44)
            root.resize(390, 260)
            root.show()
            self.app.processEvents()
            output = Path('.test-tmp') / f'notification_switches_{theme}.png'
            output.parent.mkdir(exist_ok=True)
            self.assertTrue(root.grab().save(str(output)))
            root.close()


if __name__ == '__main__':
    unittest.main()
