"""Info popups emit data only; actual native-menu input/lifetime is synthetic."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from pathlib import Path
import unittest
from unittest.mock import patch

from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QVBoxLayout, QWidget
from shiboken6 import isValid

from maxclient.ui.conversation_info import ConversationInfo
from maxclient.ui.conversation_actions_menu import ConversationActionsMenu, SOUND_CHOICES
from maxclient.ui.theme import build_stylesheet
from test_conversation_info import snapshot
from test_desktop_shell import _load_offscreen_font


def rows():
    return [
        {'action': 'openSearch', 'label': 'Найти <b>буквально</b> & текст', 'available': True, 'write': False},
        {'action': 'toggleBlock', 'label': 'Заблокировать', 'available': True, 'write': True, 'value': {'contact_id': 7}},
        {'action': 'unsupported', 'label': 'Недоступное действие', 'available': False, 'write': True},
        {'label': 'Папки', 'available': True, 'write': False, 'separator_before': True, 'children': [
            {'action': 'folder', 'label': 'Синтетическая папка', 'available': True, 'write': True,
             'checked': False, 'value': {'folder_id': 'actual-folder', 'include': True}}]},
    ]


class ConversationActionsMenuTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        _load_offscreen_font(cls.app)

    def setUp(self):
        self.host = QWidget()
        self.host.setStyleSheet(build_stylesheet('light', '#0085FF'))
        layout = QVBoxLayout(self.host)
        layout.setContentsMargins(0, 0, 0, 0)
        self.info = ConversationInfo()
        layout.addWidget(self.info)
        self.host.resize(815, 900)
        self.host.show()
        self.context = object()
        self.calls, self.legacy = [], []
        self.info.menu_action_requested.connect(lambda action, value, context: self.calls.append((action, value, context)))
        self.info.action_requested.connect(lambda action, context: self.legacy.append((action, context)))
        self.info.set_conversation(snapshot(self.context, more_actions=rows()))
        self.app.processEvents()

    def tearDown(self):
        if isValid(self.host):
            self.host.close()
            self.host.deleteLater()
            QCoreApplication.sendPostedEvents(self.host, QEvent.Type.DeferredDelete)
        self.app.processEvents()

    def test_sound_direct_click_uses_only_new_signal_and_keeps_confirmed_state(self):
        self.info.buttons['mute'].click()
        self.assertEqual(self.calls, [('mute', -1, self.context)])
        self.assertEqual(self.legacy, [])
        self.assertFalse(self.info._snapshot['muted'])
        self.info.set_conversation(snapshot(self.context, muted=True, more_actions=rows()))
        self.info.buttons['mute'].click()
        self.assertEqual(self.calls[-1], ('mute', 0, self.context))
        self.assertTrue(self.info._snapshot['muted'])

    def test_disabled_row_explains_its_own_capability_limit(self):
        menu = ConversationActionsMenu(self.context, [
            {'action': 'call', 'label': 'Позвонить', 'available': False,
             'unavailable_reason': 'Звонки в этой сборке пока не реализованы.'}], self.host)
        self.assertEqual(menu.actions()[0].toolTip(), 'Звонки в этой сборке пока не реализованы.')
        self.assertFalse(menu.actions()[0].isEnabled())
        menu.deleteLater()

    def test_desktop_hover_has_exact_durations_and_native_click_survives_hide_before_trigger(self):
        QApplication.sendEvent(self.info.buttons['mute'], QEvent(QEvent.Type.Enter))
        menu = self.info._menu
        self.assertIsNotNone(menu)
        self.assertEqual([action.text() for action in menu.actions()], [label for label, _value in SOUND_CHOICES])
        self.app.processEvents()
        target = menu.actions()[1]
        QTest.mouseClick(menu, Qt.MouseButton.LeftButton, pos=menu.actionGeometry(target).center())
        self.assertEqual(self.calls, [('mute', 14400000, self.context)])
        self.assertFalse(self.info._snapshot['muted'])

    def test_unknown_sound_opens_explicit_choices_without_guessing_toggle(self):
        self.info.set_conversation(snapshot(self.context, muted=None, more_actions=rows()))
        self.info.buttons['mute'].click()
        self.assertEqual(self.calls, [])
        menu = self.info._menu
        menu.actions()[0].trigger()
        self.assertEqual(self.calls, [('mute', 3600000, self.context)])

    def test_cached_action_updates_keep_popup_and_tab_data_without_new_reads(self):
        requests = []
        self.info.tab_requested.connect(lambda tab, context: requests.append((tab, context)))
        self.info.set_tab_items(self.context, 'common_chats', [
            {'key': 'actual', 'title': 'Synthetic row', 'subtitle': '', 'available': False}])
        menu = self.info.open_sound_menu()
        epoch = self.info._menu_epoch
        for muted in (False, False, True, None):
            self.assertTrue(self.info.apply_action_state(self.context, muted=muted, available=None))
            self.assertIs(self.info._menu, menu)
            self.assertTrue(menu.isVisible())
            self.assertEqual(self.info._menu_epoch, epoch)
            self.assertEqual(self.info.items.count(), 1)
            self.assertIs(self.info._snapshot['muted'], muted)
        self.assertEqual(requests, [])
        self.assertEqual(self.calls, [])

    def test_cached_pending_and_capability_updates_revalidate_open_popup(self):
        menu = self.info.open_sound_menu()
        self.info.apply_action_state(self.context, muted=False, available=True, pending=True, status='Ожидание')
        self.assertTrue(menu.isVisible())
        self.assertFalse(any(action.isEnabled() for action in menu.actions()))
        menu.actions()[0].trigger()
        self.assertEqual(self.calls, [])
        self.info.apply_action_state(self.context, muted=False, available=False, pending=False)
        self.assertTrue(menu.isVisible())
        self.assertFalse(any(action.isEnabled() for action in menu.actions()))
        self.assertFalse(self.info.buttons['mute'].isEnabled())

    def test_cached_action_update_ignores_foreign_context_and_invalid_types(self):
        self.assertFalse(self.info.apply_action_state(object(), muted=True, pending=True))
        self.assertFalse(self.info._snapshot['muted'])
        self.assertFalse(self.info.apply_action_state(self.context, muted='false'))
        self.assertFalse(self.info.apply_action_state(self.context, available='yes'))
        self.assertFalse(self.info._snapshot['muted'])
        self.info.clear()
        self.assertFalse(self.info.apply_action_state(self.context, muted=True))

    def test_clicking_sound_anchor_after_hover_popup_still_emits_one_desktop_toggle(self):
        button = self.info.buttons['mute']
        QApplication.sendEvent(button, QEvent(QEvent.Type.Enter))
        menu = self.info._menu
        self.app.processEvents()
        # Pointer events target the grabbed popup, not the underlying button.
        location = menu.mapFromGlobal(button.mapToGlobal(button.rect().center()))
        QTest.mouseClick(menu, Qt.MouseButton.LeftButton, pos=location)
        self.app.processEvents()
        self.assertEqual(self.calls, [('mute', -1, self.context)])
        self.assertEqual(self.legacy, [])

    def test_keyboard_enter_delivers_one_navigation_request_after_native_hide(self):
        menu = self.info.open_more_menu()
        self.app.processEvents()
        menu.setActiveAction(menu.actions()[0])
        QTest.keyClick(menu, Qt.Key.Key_Return)
        self.assertEqual(self.calls, [('openSearch', None, self.context)])

    def test_more_order_and_literal_label_are_supplied_not_inferred(self):
        menu = self.info.open_more_menu()
        self.app.processEvents()
        self.assertEqual([action.text() for action in menu.actions() if not action.isSeparator()],
                         ['Найти <b>буквально</b> && текст', 'Заблокировать', 'Недоступное действие', 'Папки'])
        self.assertFalse(menu.actions()[2].isEnabled())
        menu.actions()[2].trigger()
        self.assertEqual(self.calls, [])
        menu.actions()[0].trigger()
        self.assertEqual(self.calls, [('openSearch', None, self.context)])

    def test_pending_disables_writes_in_open_menu_but_search_navigation_remains(self):
        menu = self.info.open_more_menu()
        self.info.set_action_pending(self.context, True, '<b>Ожидание подтверждения</b>')
        self.assertTrue(menu.actions()[0].isEnabled())
        self.assertFalse(menu.actions()[1].isEnabled())
        self.assertFalse(self.info.buttons['mute'].isEnabled())
        self.assertTrue(self.info.buttons['find'].isEnabled())
        self.assertTrue(self.info.buttons['more'].isEnabled())
        menu.actions()[1].trigger()
        self.assertEqual(self.calls, [])
        menu.actions()[0].trigger()
        self.assertEqual(self.calls, [('openSearch', None, self.context)])
        self.assertEqual(self.info.status.text(), '<b>Ожидание подтверждения</b>')

    def test_read_only_keeps_navigation_menu_and_rejects_effectful_items(self):
        self.info.set_read_only(self.context, True)
        menu = self.info.open_more_menu()
        self.assertIsNotNone(menu)
        self.assertTrue(menu.actions()[0].isEnabled())
        self.assertFalse(menu.actions()[1].isEnabled())
        self.assertFalse(menu.actions()[-1].isEnabled())

    def test_menu_close_escape_clear_and_context_swap_revoke_saved_actions(self):
        for method in ('close', 'escape', 'clear', 'context'):
            self.info.set_conversation(snapshot(self.context, more_actions=rows()))
            menu = self.info.open_more_menu()
            action = menu.actions()[1]
            if method == 'close':
                menu.close()
            elif method == 'escape':
                QTest.keyClick(menu, Qt.Key.Key_Escape)
            elif method == 'clear':
                self.info.clear()
            else:
                self.info.set_conversation(snapshot(object(), chat_id=99, peer_id=8, more_actions=rows()))
            action.trigger()
            self.assertEqual(self.calls, [], method)

    def test_submenu_preserves_structured_value_and_does_not_optimistically_check(self):
        menu = self.info.open_more_menu()
        submenu = menu.actions()[-1].menu()
        submenu.popup(menu.pos())
        self.app.processEvents()
        action = submenu.actions()[0]
        self.assertFalse(action.isChecked())
        action.trigger()
        self.assertEqual(self.calls, [('folder', {'folder_id': 'actual-folder', 'include': True}, self.context)])
        self.assertFalse(action.isChecked())
        self.calls[-1][1]['folder_id'] = 'caller mutation'
        self.assertEqual(self.info._snapshot['more_actions'][-1]['children'][0]['value']['folder_id'], 'actual-folder')

    def test_unknown_unsafe_value_is_disabled_without_invoking_object_hooks(self):
        class Unsafe:
            def __deepcopy__(self, _memo):
                raise AssertionError('Object hook must never run')
        menu = ConversationActionsMenu(self.context, [
            {'action': 'unsafe', 'label': '<b>Literal</b>', 'available': True, 'value': Unsafe()}], self.info)
        self.assertFalse(menu.actions()[0].isEnabled())

    def test_parent_gate_also_protects_programmatically_opened_submenu(self):
        values = rows()
        values[-1]['available'] = False
        self.info.set_conversation(snapshot(self.context, more_actions=values))
        menu = self.info.open_more_menu()
        submenu = menu.actions()[-1].menu()
        submenu.popup(menu.pos())
        submenu.actions()[0].trigger()
        self.assertEqual(self.calls, [])

    def test_menu_theme_captures_are_scoped_and_do_not_change_application_style(self):
        original = self.app.styleSheet()
        output = Path('.test-tmp')
        output.mkdir(exist_ok=True)
        for theme in ('light', 'dark'):
            self.host.setStyleSheet(build_stylesheet(theme, '#0085FF'))
            self.info.apply_theme(theme)
            for kind in ('sound', 'more'):
                menu = self.info.open_sound_menu() if kind == 'sound' else self.info.open_more_menu()
                self.app.processEvents()
                self.assertTrue(menu.grab().save(str(output / f'info_{kind}_menu_{theme}.png')))
                menu.close()
        self.assertEqual(self.app.styleSheet(), original)


if __name__ == '__main__':
    unittest.main()
