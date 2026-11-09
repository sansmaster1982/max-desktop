"""Short native-painted submenus must use their visible width for text/input."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import unittest

from PySide6.QtCore import QPoint, Qt, QCoreApplication, QEvent
from PySide6.QtGui import QFontMetrics
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from maxclient.core.i18n import SUPPORTED_LANGUAGES
from maxclient.ui.chat_row_menu import chat_row_entries
from maxclient.ui.conversation_actions_menu import ConversationActionsMenu
from maxclient.ui.theme import build_stylesheet
from test_chat_row_menu_layout import snapshot
from test_desktop_shell import _load_offscreen_font


class ChatRowMenuGeometryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        _load_offscreen_font(cls.app)

    def setUp(self):
        self.menus = []
        self.previous_style = self.app.styleSheet()

    def tearDown(self):
        for menu in self.menus:
            menu.cancel()
            menu.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.setStyleSheet(self.previous_style)
        self.app.processEvents()

    def submenu(self, theme='light', language='ru'):
        self.app.setStyleSheet(build_stylesheet(theme, '#0085FF'))
        menu = ConversationActionsMenu(object(), chat_row_entries(snapshot(), language=language),
                                       theme=theme, native_rows=True)
        self.menus.append(menu)
        menu.popup(QPoint(10, 10))
        self.app.processEvents()
        submenu = next(child for _action, row, child in menu._rows
                       if row['action'] == 'mute_choices')
        submenu.popup(QPoint(280, 10))
        self.app.processEvents()
        return submenu

    def test_mute_submenu_text_and_hit_rect_use_minimum_content_width(self):
        for theme in ('light', 'dark'):
            for language in SUPPORTED_LANGUAGES:
                with self.subTest(theme=theme, language=language):
                    submenu = self.submenu(theme, language)
                    for action in submenu.actions():
                        rect = submenu.actionGeometry(action)
                        self.assertGreaterEqual(rect.width(), 208)
                        self.assertEqual(rect.right(), submenu.width() - 8)
                        # The actual style/device font plus row paddings must fit.
                        metrics = QFontMetrics(action.font().resolve(submenu.font()), submenu)
                        self.assertLessEqual(metrics.horizontalAdvance(action.text()),
                                             rect.width() - 37)

    def test_click_at_far_right_of_24h_row_dispatches_exactly_one_action(self):
        submenu = self.submenu()
        calls = []
        submenu.requested.connect(lambda action, value, context: calls.append((action, value)))
        target = submenu.actions()[2]
        pos = QPoint(submenu.width() - 12, submenu.actionGeometry(target).center().y())
        self.assertIs(submenu.actionAt(pos), target)
        QTest.mouseClick(submenu, Qt.MouseButton.LeftButton, pos=pos)
        self.assertEqual(calls, [('mute', 86400000)])


if __name__ == '__main__':
    unittest.main()
