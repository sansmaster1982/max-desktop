"""Actual desktop layout, synthetic inputs, offscreen and no real service calls."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from PySide6.QtCore import QPoint
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from maxclient.core.config import Settings
from maxclient.ui.main_window import MainWindow
from maxclient.ui.theme import build_stylesheet
from test_desktop_shell import _load_offscreen_font
from test_folders_blocks_ui import folders, contacts
from test_settings_workspace import SyntheticService, immediate


class IntegratedManagementPagesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        _load_offscreen_font(cls.app)
        cls.app.setStyle('Fusion')

    def test_first_contact_block_action_prompts_before_the_first_list_read_is_busy(self):
        patches = [patch('maxclient.ui.main_window.run_async'),
                   patch('maxclient.ui.settings_workspace.run_async', side_effect=immediate),
                   patch('maxclient.ui.notification_settings_panel.run_async'),
                   patch('maxclient.ui.folders_panel.run_async'),
                   patch('maxclient.ui.blocked_contacts_panel.run_async')]
        runners = [item.start() for item in patches]
        window = None
        try:
            for accepted in (False, True):
                synthetic = SyntheticService()
                synthetic.list_blocked_contacts = lambda: contacts(42)
                synthetic.block_contact = Mock(side_effect=AssertionError('Never execute a real action in QA'))
                window = MainWindow(synthetic, Settings(theme='light', notifications=False))
                with patch('maxclient.ui.blocked_contacts_panel.confirm_action', return_value=accepted) as confirmation:
                    window._block_contact(42, '<b>Тест</b>')
                confirmation.assert_called_once()
                panel = window._settings_workspace.blocked_contacts
                self.assertTrue(panel._busy)
                self.assertFalse(panel._loaded)
                # Async runners are captured and deliberately NOT completed:
                # neither a fast read nor a fake ACK can hide this race.
                self.assertEqual(runners[-1].call_count, 1)
                if accepted:
                    self.assertEqual(panel._pending, {'id': 42, 'blocked': True})
                    self.assertTrue(panel._refresh_queued)
                    self.assertIs(runners[-1].call_args.args[0], synthetic.block_contact)
                else:
                    self.assertIsNone(panel._pending)
                    self.assertIs(runners[-1].call_args.args[0], synthetic.list_blocked_contacts)
                synthetic.block_contact.assert_not_called()
                window.close()
                window = None
                runners[-1].reset_mock()
        finally:
            if window is not None:
                window.close()
            for item in reversed(patches):
                item.stop()
            self.app.processEvents()

    def test_normal_and_minimum_settings_pages_keep_below_fold_controls_reachable(self):
        patches = [patch('maxclient.ui.main_window.run_async'),
                   patch('maxclient.ui.settings_workspace.run_async', side_effect=immediate),
                   patch('maxclient.ui.notification_settings_panel.run_async'),
                   patch('maxclient.ui.folders_panel.run_async'),
                   patch('maxclient.ui.blocked_contacts_panel.run_async')]
        for item in patches:
            item.start()
        window = None
        try:
            self.app.setStyleSheet(build_stylesheet('light', '#0085FF'))
            synthetic = SyntheticService()
            synthetic.list_blocked_contacts = lambda: contacts(42, 81)
            window = MainWindow(synthetic, Settings(theme='light', notifications=False))
            window.show()
            window._open_settings()
            workspace = window._settings_workspace
            for width, height in [(1180, 760), (880, 560)]:
                window.resize(width, height)
                for key in ('folders', 'blocked', 'notifications'):
                    workspace.select_page(key)
                    if key == 'folders':
                        workspace.folder_list._read_done(folders())
                        workspace.folder_list.items.setCurrentRow(1)
                        # The ordinary editor now fits without scrolling at
                        # normal sizes. Exercise the expanded controls that
                        # actually require a below-the-fold layout.
                        workspace.folder_list.advanced_button.click()
                        self.assertFalse(workspace.folder_list.advanced.isHidden())
                    elif key == 'blocked':
                        workspace.blocked_contacts._read_done(contacts(42, 81))
                    else:
                        workspace.notification_panel._done({'available': True, 'pending': False, 'settings': {
                            'DONT_DISTURB_UNTIL': 0, 'PUSH_DETAILS': True, 'PUSH_SOUND': 'DEFAULT',
                            'CHATS_PUSH_NOTIFICATION': 'ON', 'CHATS_PUSH_SOUND': '_NONE_'}})
                    self.app.processEvents()
                    self.app.processEvents()
                    QTest.qWait(25)  # Settle nested resize/reflow layout requests before measuring scroll limits.
                    page = workspace.page_widgets[key]
                    self.assertEqual(page.horizontalScrollBar().maximum(), 0, (key, width))
                    output = Path('.test-tmp') / f'integrated_{key}_{width}.png'
                    output.parent.mkdir(exist_ok=True)
                    page.verticalScrollBar().setValue(0)
                    self.app.processEvents()
                    self.assertTrue(window.grab().save(str(output)))
                    page.verticalScrollBar().setValue(page.verticalScrollBar().maximum())
                    self.app.processEvents()
                    self.assertTrue(window.grab().save(str(output.with_name(output.stem + '_bottom.png'))))
                    if key == 'folders':
                        self.assertGreater(page.verticalScrollBar().maximum(), 0)
                        button = workspace.folder_list.save_button
                        position = button.mapTo(page.viewport(), QPoint(0, 0))
                        self.assertGreaterEqual(position.y(), 0)
                        self.assertLessEqual(position.y() + button.height(), page.viewport().height())
            self.assertEqual(window.service.profile_writes, [])
            self.assertEqual(window.service.termination_writes, 0)
        finally:
            if window is not None:
                window.close()
            for item in reversed(patches):
                item.stop()
            self.app.processEvents()


if __name__ == '__main__':
    unittest.main()
