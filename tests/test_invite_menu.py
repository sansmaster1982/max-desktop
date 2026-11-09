"""Invitation menu behavior with synthetic text; no server/share/account access."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import unittest
from unittest.mock import Mock, patch
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication, QLabel
from maxclient.core.config import Settings
from maxclient.ui.main_window import MainWindow
from maxclient.ui.invite_link import copy_invitation
from test_settings_workspace import SyntheticService


class InviteMenuTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.context = object()
        self.service = SyntheticService()
        self.service.search_context = lambda: self.context
        self.service.invite_link_text = Mock(return_value='Synthetic invitation\nhttps://max.ru/test-only\n')
        self.clipboard = Mock()
        self.clipboard_patch = patch('maxclient.ui.invite_link.QApplication.clipboard', return_value=self.clipboard)
        self.clipboard_patch.start()
        self.patches = [patch('maxclient.ui.main_window.run_async'),
                        patch('maxclient.ui.settings_workspace.run_async'),
                        patch('maxclient.ui.chat_view.run_async'), patch.object(Settings, 'save')]
        for p in self.patches:
            p.start()
        self.window = MainWindow(self.service, Settings())
        self.window.show()
        self.app.processEvents()

    def tearDown(self):
        self.window.close()
        self.window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()
        for p in reversed(self.patches):
            p.stop()
        self.clipboard_patch.stop()

    def _invite(self, contacts):
        menu = self.window._build_new_menu(contacts=contacts)
        return next(a for a in menu.actions() if a.text() == 'Пригласить по ссылке')

    def test_both_plus_menus_copy_exact_server_text_without_dialog(self):
        for contacts in (False, True):
            self._invite(contacts).trigger()
            self.clipboard.setText.assert_called_with('Synthetic invitation\nhttps://max.ru/test-only\n')
            self.assertEqual(self.window._menu_notice.findChild(QLabel, 'MenuNoticeText').text(),
                             'Вы скопировали ссылку')
            label = self.window._menu_notice.findChild(QLabel, 'MenuNoticeText')
            self.assertGreaterEqual(label.width(), label.fontMetrics().horizontalAdvance(label.text()))
        self.assertEqual(self.service.invite_link_text.call_count, 2)
        self.service.invite_link_text.assert_called_with(context=self.context)

    def test_settings_invite_copies_without_leaving_current_page(self):
        self.window._open_settings()
        workspace = self.window._settings_workspace
        workspace.select_page('language')
        page = workspace.pages.currentWidget()
        workspace.nav_buttons['invite'].click()
        self.assertIs(workspace.pages.currentWidget(), page)
        self.assertEqual(workspace.current_page, 'language')
        self.assertTrue(workspace.nav_buttons['language'].isChecked())
        self.assertFalse(workspace.nav_buttons['invite'].isChecked())
        self.clipboard.setText.assert_called_once_with('Synthetic invitation\nhttps://max.ru/test-only\n')

    def test_stale_menu_never_copies_new_owner_invitation(self):
        action = self._invite(True)
        self.context = object()
        action.trigger()
        self.service.invite_link_text.assert_not_called()
        self.clipboard.setText.assert_not_called()

    def test_session_switch_during_local_config_read_never_copies(self):
        def switching(**kwargs):
            self.context = object()
            return 'Other owner text'
        self.service.invite_link_text.side_effect = switching
        self._invite(True).trigger()
        self.clipboard.setText.assert_not_called()

    def test_missing_config_does_not_fabricate_link_or_report_success(self):
        self.service.invite_link_text.side_effect = ValueError('not supplied')
        self._invite(True).trigger()
        self.clipboard.setText.assert_not_called()
        notice = self.window._menu_notice.findChild(QLabel, 'MenuNoticeText').text()
        self.assertIn('Не удалось', notice)
        self.assertNotIn('скопировали', notice)

    def test_disconnect_disables_menu_and_old_menu_is_ignored(self):
        old = self._invite(True)
        self.window._stop_search()
        self.assertFalse(self._invite(False).isEnabled())
        old.trigger()
        self.clipboard.setText.assert_not_called()

    def test_empty_malformed_text_never_overwrites_clipboard(self):
        for value in ('', ' \n', None, ['https://max.ru/']):
            self.service.invite_link_text.return_value = value
            with self.assertRaises(ValueError):
                copy_invitation(self.service, context=self.context)
        self.clipboard.setText.assert_not_called()

    def test_settings_error_preserves_navigation_and_clipboard(self):
        self.window._open_settings()
        workspace = self.window._settings_workspace
        old_page = workspace.pages.currentWidget()
        self.service.invite_link_text.side_effect = ValueError('no supplied invitation')
        workspace.nav_buttons['invite'].click()
        self.assertIs(workspace.pages.currentWidget(), old_page)
        self.clipboard.setText.assert_not_called()


if __name__ == '__main__':
    unittest.main()
