"""Info FILE gestures only; no download, local file or service execution."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from shiboken6 import isValid
import unittest

from maxclient.ui.conversation_info import ConversationInfo
from test_desktop_shell import _load_offscreen_font


def snapshot(context, **extra):
    value = {'context': context, 'chat_id': 42, 'peer_id': 7, 'title': 'Синтетический профиль',
             'availability': {}, 'tabs': {'files': True}}
    value.update(extra)
    return value


def row(**extra):
    value = {'key': 'attachment:42:91:0', 'title': '<b>Синтетический файл & приложение.pdf</b>',
             'subtitle': '10 МБ', 'kind': 'FILE', 'file_name': '<b>Синтетический файл & приложение.pdf</b>',
             'file_id': 52.25, 'item_type': 'REGULAR', 'download_available': True,
             'available': True, 'chat_id': 42, 'message_id': 91, 'time_ms': 1793459500091.5,
             'action': 'message'}
    value.update(extra)
    return value


class ConversationFileRowsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        _load_offscreen_font(cls.app)

    def setUp(self):
        self.panel = ConversationInfo()
        self.panel.resize(815, 900)
        self.panel.show()
        self.context = object()
        self.actions, self.messages = [], []
        self.panel.file_action_requested.connect(lambda action, value, context: self.actions.append((action, value, context)))
        self.panel.item_requested.connect(lambda value, context: self.messages.append((value, context)))
        self.panel.set_conversation(snapshot(self.context))

    def tearDown(self):
        if isValid(self.panel):
            self.panel.close()
            self.panel.deleteLater()
            QCoreApplication.sendPostedEvents(self.panel, QEvent.Type.DeferredDelete)
        self.app.processEvents()

    def load(self, value):
        self.panel.set_tab_items(self.context, 'files', [value])
        self.app.processEvents()

    def test_primary_file_click_requests_save_with_exact_outer_identity(self):
        value = row()
        self.load(value)
        self.panel._activate_item(self.panel.items.item(0))
        self.assertEqual(self.actions, [('save', value, self.context)])
        self.assertEqual(self.messages, [])
        self.assertEqual(self.actions[0][1]['file_id'], 52.25)
        self.assertEqual(self.actions[0][1]['time_ms'], 1793459500091.5)

    def test_deleted_missing_unknown_id_or_permission_fall_back_to_message(self):
        for fields in ({'file_id': None}, {'file_id': True}, {'file_id': float('nan')},
                       {'deleted': True}, {'download_available': False}, {'download_available': None}):
            value = row(**fields)
            self.load(value)
            self.panel._activate_item(self.panel.items.item(0))
        self.assertEqual(self.actions, [])
        self.assertEqual(len(self.messages), 6)

    def test_file_menu_save_and_message_are_explicit_and_plain(self):
        value = row()
        self.load(value)
        menu = self.panel.open_file_menu(value)
        self.assertEqual([action.text() for action in menu.actions()], ['Сохранить файл', 'Перейти к сообщению'])
        menu.actions()[1].trigger()
        self.assertEqual(self.actions, [('message', value, self.context)])
        menu = self.panel.open_file_menu(value)
        menu.actions()[0].trigger()
        self.assertEqual(self.actions[-1], ('save', value, self.context))

    def test_unavailable_file_menu_disables_save_but_retains_original_message(self):
        value = row(deleted=True)
        self.load(value)
        menu = self.panel.open_file_menu(value)
        self.assertFalse(menu.actions()[0].isEnabled())
        self.assertTrue(menu.actions()[1].isEnabled())
        menu.actions()[0].trigger()
        self.assertEqual(self.actions, [])
        menu.actions()[1].trigger()
        self.assertEqual(self.actions, [('message', value, self.context)])

    def test_context_and_same_key_source_change_revoke_old_menu(self):
        value = row()
        self.load(value)
        old = self.panel.open_file_menu(value)
        action = old.actions()[0]
        self.panel.set_tab_items(self.context, 'files', [row(file_id=99.5, file_name='Новый файл')])
        action.trigger()
        self.assertEqual(self.actions, [])
        self.assertIsNone(self.panel.open_file_menu(value))
        current = row(file_id=99.5, file_name='Новый файл')
        menu = self.panel.open_file_menu(current)
        other = object()
        self.panel.set_conversation(snapshot(other, chat_id=43))
        menu.actions()[1].trigger()
        self.assertEqual(self.actions, [])

    def test_busy_clear_and_dismissed_menu_cannot_emit_stale_file_actions(self):
        value = row()
        self.load(value)
        menu = self.panel.open_file_menu(value)
        action = menu.actions()[0]
        menu.close()
        action.trigger()
        self.assertEqual(self.actions, [])
        self.panel.set_tab_busy(self.context, 'files', True)
        self.assertIsNone(self.panel.open_file_menu(value))
        self.panel._activate_item(self.panel.items.item(0))
        self.assertEqual(self.actions, [])
        self.panel.clear()
        self.assertIsNone(self.panel.open_file_menu(value))

    def test_file_primary_keyboard_activation_matches_mouse_and_no_fake_non_file_save(self):
        self.load(row())
        self.panel.items.setCurrentRow(0)
        QTest.keyClick(self.panel.items, Qt.Key.Key_Return)
        self.assertEqual(len(self.actions), 1)
        self.load(row(kind=None))
        self.panel._activate_item(self.panel.items.item(0))
        self.assertEqual(len(self.actions), 1)
        self.assertEqual(len(self.messages), 1)

    def test_receiver_can_clear_and_delete_popup_during_file_action_dispatch(self):
        value = row()
        self.load(value)
        menu = self.panel.open_file_menu(value)
        def close_during_dispatch(_action, _row, _context):
            self.panel.clear()
            QCoreApplication.sendPostedEvents(menu, QEvent.Type.DeferredDelete)
        self.panel.file_action_requested.connect(close_during_dispatch)
        menu.actions()[0].trigger()
        self.assertFalse(isValid(menu))
        self.assertEqual(self.actions, [('save', value, self.context)])


if __name__ == '__main__':
    unittest.main()
