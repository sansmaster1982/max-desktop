"""Embedded contact navigation uses synthetic records; no account or network."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtGui import QKeyEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLabel, QListWidget, QListWidgetItem
from shiboken6 import isValid

from maxclient.core.models import Contact
from maxclient.ui.contacts_workspace import ContactsWorkspace, ContactWorkspaceRow
from maxclient.ui.widgets.avatar import Avatar


class ContactsWorkspaceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.context = object()
        self.rows = [Contact(2**40 + 1, 'Synthetic Person', '+70000000001'), Contact(202, 'Second')]
        self.service = SimpleNamespace(search_context=lambda: self.context,
            list_contacts=Mock(side_effect=lambda query='': [c for c in self.rows if not query
                or query.casefold() in ((c.name or '') + (c.phone or '')).casefold()]))
        self.photo_patch = patch('maxclient.ui.contacts_workspace.Avatar.set_photo', autospec=True)
        self.photo = self.photo_patch.start()
        self.widget = ContactsWorkspace(self.service)
        self.widget.resize(360, 720)
        self.widget.show()
        self.opened = []
        self.widget.open_chat.connect(lambda identity, name: self.opened.append((identity, name)))

    def tearDown(self):
        if isValid(self.widget):
            self.widget.close()
            self.widget.deleteLater()
            QCoreApplication.sendPostedEvents(self.widget, QEvent.Type.DeferredDelete)
        self.app.processEvents()
        self.photo_patch.stop()

    def test_constructor_does_not_read_contacts_or_emit_navigation(self):
        self.service.list_contacts.assert_not_called()
        self.assertEqual(self.opened, [])
        self.assertFalse(self.widget.plus_button.isEnabled())

    def test_explicit_activation_search_and_full_int64_navigation(self):
        self.assertTrue(self.widget.activate_context())
        self.assertEqual(self.widget.list.count(), 2)
        self.widget.search.setText('person')
        self.assertEqual(self.widget.list.count(), 1)
        self.service.list_contacts.assert_called_with('person')
        self.widget._open_item(self.widget.list.item(0))
        self.assertEqual(self.opened, [(2**40 + 1, 'Synthetic Person')])

    def test_query_no_match_differs_from_empty_book_and_is_local(self):
        self.widget.activate_context()
        self.widget.search.setText('missing')
        self.assertEqual(self.widget.empty.text(), 'Ничего не найдено')
        self.assertEqual(self.widget.list.count(), 0)
        self.rows.clear()
        self.widget.search.clear()
        self.assertEqual(self.widget.empty.text(), 'Пока нет контактов')

    def test_rows_do_not_fabricate_presence_or_missing_phone(self):
        self.widget.activate_context()
        row = self.widget.list.itemWidget(self.widget.list.item(1))
        labels = [label.text() for label in row.findChildren(QLabel)]
        self.assertFalse(any('недавно' in label or 'онлайн' in label for label in labels))
        self.assertIsNone(row.findChild(QLabel, 'ContactPhone'))

    def test_literal_fields_preserve_avatar_and_verified_metadata(self):
        contact = Contact(303, '<b>Name</b>', '<i>phone</i>', 'https://example.test/avatar', True)
        self.rows[:] = [contact]
        self.widget.activate_context()
        row = self.widget.list.itemWidget(self.widget.list.item(0))
        self.assertEqual(row.name._full_text, '<b>Name</b>')
        self.assertEqual(row.name.textFormat(), Qt.TextFormat.PlainText)
        phone = row.findChild(QLabel, 'ContactPhone')
        self.assertEqual(phone._full_text, '<i>phone</i>')
        self.assertEqual(phone.textFormat(), Qt.TextFormat.PlainText)
        self.assertEqual(row.avatar._identity, 303)
        self.photo.assert_called_once_with(row.avatar, 'https://example.test/avatar')
        self.assertIsNotNone(row.findChild(QLabel, 'ContactVerified'))

    def test_phone_fallback_is_not_duplicated_as_second_row(self):
        self.rows[:] = [Contact(303, None, '+70000000003')]
        self.widget.activate_context()
        row = self.widget.list.itemWidget(self.widget.list.item(0))
        self.assertEqual(row.name._full_text, '+70000000003')
        self.assertIsNone(row.findChild(QLabel, 'ContactPhone'))

    def test_change_during_read_discards_old_rows_before_avatar_requests(self):
        def changed(query=''):
            self.context = object()
            return self.rows
        self.service.list_contacts.side_effect = changed
        self.assertFalse(self.widget.activate_context())
        self.assertEqual(self.widget.list.count(), 0)
        self.photo.assert_not_called()
        self.assertFalse(self.widget.plus_button.isEnabled())

    def test_refresh_error_and_reentry_can_bind_new_session(self):
        self.service.list_contacts.side_effect = ValueError('Changed owner')
        self.assertFalse(self.widget.activate_context())
        self.assertIn('Сессия изменилась', self.widget.empty.text())
        self.context = object()
        self.service.list_contacts.side_effect = lambda query='': self.rows
        self.assertTrue(self.widget.activate_context())
        self.assertEqual(self.widget.list.count(), 2)

    def test_cache_failure_is_not_reported_as_empty_book_or_raw_exception(self):
        self.service.list_contacts.side_effect = RuntimeError('Synthetic private diagnostic')
        self.assertFalse(self.widget.activate_context())
        self.assertIn('Не удалось загрузить', self.widget.empty.text())
        self.assertNotIn('Synthetic', self.widget.empty.text())
        self.service.list_contacts.side_effect = lambda query='': self.rows
        self.assertTrue(self.widget.activate_context())

    def test_deactivate_erases_rows_query_and_blocks_old_callbacks(self):
        self.widget.activate_context()
        self.widget.search.setText('person')
        item = self.widget.list.item(0)
        self.widget.deactivate()
        self.widget._open_item(item)
        self.assertEqual(self.opened, [])
        self.assertEqual(self.widget.list.count(), 0)
        self.assertEqual(self.widget.search.text(), '')
        self.assertIsNone(self.widget._context)

    def test_context_change_blocks_contact_and_menu_signal(self):
        self.widget.activate_context()
        item = self.widget.list.item(0)
        menu = Mock()
        self.widget.plus_requested.connect(menu)
        self.context = object()
        self.widget._open_item(item)
        self.widget._request_plus()
        self.assertEqual(self.opened, [])
        menu.assert_not_called()

    def test_unrelated_item_cannot_navigate(self):
        self.widget.activate_context()
        foreign = QListWidget()
        item = QListWidgetItem(foreign)
        item.setData(Qt.ItemDataRole.UserRole, self.rows[0])
        self.widget._open_item(item)
        self.assertEqual(self.opened, [])
        foreign.deleteLater()

    def test_malformed_contact_ids_are_not_shown_or_navigable(self):
        self.rows[:] = [Contact(True, 'Boolean'), Contact('202', 'Text'),
                        Contact(-(2**63) - 1, 'Underflow'), Contact(2**63, 'Overflow'), 'bad']
        self.service.list_contacts.side_effect = lambda query='': self.rows
        self.widget.activate_context()
        self.assertEqual(self.widget.list.count(), 0)

    def test_entire_signed_identity_domain_is_passed_without_chat_id_conversion(self):
        self.rows[:] = [Contact(-(2**63), 'Signed'), Contact(0, 'Zero actual contact')]
        self.widget.activate_context()
        self.widget._open_item(self.widget.list.item(0))
        self.widget._open_item(self.widget.list.item(1))
        self.assertEqual(self.opened, [(-(2**63), 'Signed'), (0, 'Zero actual contact')])

    def test_same_context_refresh_keeps_selected_contact(self):
        self.widget.activate_context()
        self.widget.list.setCurrentItem(self.widget.list.item(1))
        row = self.widget.list.itemWidget(self.widget.list.item(1))
        self.photo.reset_mock()
        self.widget.refresh()
        self.assertEqual(self.widget.list.currentItem().data(Qt.ItemDataRole.UserRole).id, 202)
        self.assertIs(self.widget.list.itemWidget(self.widget.list.item(1)), row)
        self.photo.assert_not_called()

    def test_changed_metadata_refreshes_row_and_expiry_drops_search_query(self):
        self.widget.activate_context()
        self.widget.search.setText('person')
        self.rows[0].name = 'Synthetic Person Updated'
        self.widget.refresh()
        row = self.widget.list.itemWidget(self.widget.list.item(0))
        self.assertEqual(row.name._full_text, 'Synthetic Person Updated')
        self.context = object()
        self.widget.refresh()
        self.assertEqual(self.widget.search.text(), '')
        self.assertIsNone(self.widget._rows_snapshot)

    def test_enter_opens_once_and_item_activation_does_not_duplicate_click(self):
        self.widget.activate_context()
        item = self.widget.list.item(0)
        self.widget.list.setCurrentItem(item)
        QTest.keyClick(self.widget.list, Qt.Key.Key_Return)
        self.assertEqual(self.opened, [(2**40 + 1, 'Synthetic Person')])
        self.widget.list.itemActivated.emit(item)
        self.assertEqual(len(self.opened), 1)

    def test_modified_and_repeated_enter_do_not_open_contacts(self):
        self.widget.activate_context()
        self.widget.list.setCurrentItem(self.widget.list.item(0))
        QTest.keyClick(self.widget.list, Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier)
        event = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Return,
                          Qt.KeyboardModifier.NoModifier, '\r', True)
        QApplication.sendEvent(self.widget.list, event)
        self.assertEqual(self.opened, [])


if __name__ == '__main__':
    unittest.main()
