"""Contacts display/search projection with synthetic records and no real RPC."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import copy
import unittest
from unittest.mock import patch

from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtWidgets import QApplication, QLabel
from shiboken6 import isValid

import test_contact_lookup_core as fixture
from maxclient.core.models import Contact, Profile
from maxclient.ui.contacts_dialog import ContactsDialog, _ContactRow
from maxclient.ui.widgets.avatar import Avatar


NAME = 'Synthetic First Family'
PHONE = '+70000000001'
PHOTO = 'https://example.test/synthetic-avatar.png'


class ContactsDisplayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.case = fixture.ContactLookupCoreTests()
        self.case.setUp()
        self.service, self.store, self.client = self.case.service, self.case.store, self.case.client
        self.widgets = []
        self.photo_patch = patch('maxclient.ui.contacts_dialog.Avatar.set_photo', autospec=True)
        self.photo = self.photo_patch.start()
        self.worker_patch = patch('maxclient.ui.contacts_dialog.run_async')
        self.worker = self.worker_patch.start()
        self.record = {'id': 202, 'names': [
            {'type': 'ONEME', 'firstName': 'Synthetic First', 'lastName': 'Family'},
            {'type': 'CUSTOM', 'name': 'Later source record'}]}

    def tearDown(self):
        for widget in self.widgets:
            if isValid(widget):
                widget.close()
                widget.deleteLater()
                QCoreApplication.sendPostedEvents(widget, QEvent.Type.DeferredDelete)
        self.app.processEvents()
        self.worker_patch.stop()
        self.photo_patch.stop()
        self.worker.assert_not_called()
        self.client.request.assert_not_called()
        self.client.contact_by_phone.assert_not_called()
        self.case.doCleanups()

    def legacy(self):
        self.service._server_contacts[202] = copy.deepcopy(self.record)
        raw = Contact(202, str(self.record['names']), PHONE, PHOTO, True)
        self.store.upsert_contact(raw)
        return raw

    def dialog(self):
        widget = ContactsDialog(self.service)
        self.widgets.append(widget)
        widget.show()
        self.app.processEvents()
        return widget

    def row(self, contact):
        widget = _ContactRow(contact)
        self.widgets.append(widget)
        widget.show()
        self.app.processEvents()
        return widget

    def test_exact_known_legacy_names_are_projected_and_search_uses_resolved_name(self):
        raw = self.legacy()
        projected = self.service.list_contacts()
        self.assertEqual([contact.name for contact in projected], [NAME])
        self.assertEqual(self.service.list_contacts('first family')[0].id, 202)
        self.assertEqual(self.service.list_contacts(PHONE)[0].id, 202)
        self.assertEqual(self.service.list_contacts('not-found'), [])
        self.assertEqual(self.store.list_contacts()[0], raw)  # Display is not migration.
        self.assertEqual(self.service._server_contacts[202], self.record)

    def test_explicit_normal_alias_is_retained_in_projection_and_search(self):
        self.legacy()
        alias = '[My genuine local label]'
        self.store.upsert_contact(Contact(202, alias, PHONE))
        self.assertEqual(self.service.list_contacts('GENUINE')[0].name, alias)
        self.assertEqual(self.store.list_contacts()[0].name, alias)

    def test_repr_string_without_exact_same_id_owner_metadata_is_not_guessed(self):
        raw = self.legacy()
        for change in ('missing', 'wrong_id', 'wrong_metadata_owner', 'different_names'):
            with self.subTest(change=change):
                self.service._server_metadata_owner = 101
                self.service._server_contacts[202] = copy.deepcopy(self.record)
                if change == 'missing': self.service._server_contacts.clear()
                elif change == 'wrong_id': self.service._server_contacts[202]['id'] = 303
                elif change == 'wrong_metadata_owner': self.service._server_metadata_owner = 404
                else: self.service._server_contacts[202]['names'].reverse()
                self.assertEqual(self.service.list_contacts()[0].name, raw.name)
                self.assertEqual(self.store.list_contacts()[0].name, raw.name)

    def test_valid_structured_names_share_normalizer_without_custom_priority_reorder(self):
        forms = [self.record['names'], self.record['names'][0], {'names': self.record['names']}]
        for value in forms:
            with self.subTest(value_type=type(value).__name__):
                contact = Contact(202, value, PHONE)
                with patch.object(self.store, 'list_contacts', return_value=[contact]):
                    projected = self.service.list_contacts()[0]
                self.assertEqual(projected.name, NAME)
                self.assertIs(contact.name, value)
                row = self.row(contact)
                self.assertEqual(row.findChild(QLabel, 'ContactName').text(), NAME)

    def test_cold_malformed_name_fields_fall_back_to_phone_or_id(self):
        for value in (None, 42, True, [], [{'firstName': 42}], {'name': ['invalid']}):
            with self.subTest(value_type=type(value).__name__):
                raw = Contact(202, value, PHONE)
                with patch.object(self.store, 'list_contacts', return_value=[raw]):
                    projected = self.service.list_contacts()[0]
                self.assertIsNone(projected.name)
                self.assertEqual(projected.display, PHONE)
                row = self.row(Contact(202, value))
                self.assertEqual(row.findChild(QLabel, 'ContactName').text(), 'Контакт 202')
                self.assertIsNone(row.findChild(QLabel, 'ContactPhone'))

    def test_name_phone_avatar_and_verification_fields_remain_literal_and_bound(self):
        name, phone = '<b>Synthetic & Name</b>', '<i>synthetic-phone</i>'
        contact = Contact(202, [{'name': name, 'type': 'ONEME'}], phone, PHOTO, True)
        row = self.row(contact)
        self.assertEqual(row.findChild(QLabel, 'ContactName').text(), name)
        self.assertEqual(row.findChild(QLabel, 'ContactPhone').text(), phone)
        self.assertEqual(row.findChild(QLabel, 'ContactName').textFormat(), Qt.TextFormat.PlainText)
        self.assertEqual(row.findChild(QLabel, 'ContactPhone').textFormat(), Qt.TextFormat.PlainText)
        avatar = row.findChild(Avatar, 'ContactAvatar')
        self.assertEqual(avatar._identity, 202)
        self.assertEqual(avatar.accessibleName(), name)
        self.photo.assert_called_once_with(avatar, PHOTO)
        self.assertTrue(contact.is_verified)

    def test_missing_or_nontext_phone_avatar_do_not_become_repr_labels_or_urls(self):
        row = self.row(Contact(202, {'firstName': 'Only name'}, {'phone': 'bad'}, ['bad-url']))
        self.assertEqual(row.findChild(QLabel, 'ContactName').text(), 'Only name')
        self.assertIsNone(row.findChild(QLabel, 'ContactPhone'))
        self.photo.assert_called_once_with(row.findChild(Avatar, 'ContactAvatar'), None)

    def test_dialog_filters_and_sorts_by_projected_display_and_keeps_actual_id(self):
        self.legacy()
        self.store.upsert_contact(Contact(303, 'Alpha'))
        dialog = self.dialog()
        self.assertEqual([dialog.list.item(i).data(Qt.ItemDataRole.UserRole).id
                          for i in range(dialog.list.count())], [303, 202])
        dialog.search.setText('first family')
        self.assertEqual(dialog.list.count(), 1)
        item = dialog.list.item(0)
        row = dialog.list.itemWidget(item)
        self.assertEqual(row.findChild(QLabel, 'ContactName').text(), NAME)
        opened = []
        dialog.open_chat.connect(lambda identity, name: opened.append((identity, name)))
        dialog._on_click(item)
        self.assertEqual(opened, [(202, NAME)])

    def test_refresh_discards_projection_when_session_changes_during_read(self):
        self.legacy()
        dialog = self.dialog()
        original = self.service.list_contacts
        def switch(query=''):
            rows = original(query)
            self.client._auth_session = fixture.AuthenticatedSession(101, fixture.DEVICE, 'WEB', self.client._sock)
            return rows
        self.photo.reset_mock()
        with patch.object(self.service, 'list_contacts', side_effect=switch):
            dialog._refresh()
        self.assertEqual(dialog.list.count(), 0)
        self.assertIn('Сессия изменилась', dialog.empty.text())
        self.photo.assert_not_called()

    def test_account_switch_during_core_read_rejects_old_rows(self):
        self.legacy()
        original = self.store.list_contacts
        def switch(*args):
            rows = original(*args)
            self.service.session.my_user_id = 404
            self.store.bind_owner(404)
            self.service._server_metadata_owner = 404
            return rows
        with patch.object(self.store, 'list_contacts', side_effect=switch):
            with self.assertRaises(ValueError):
                self.service.list_contacts()

    def test_cache_owner_mismatch_never_projects_foreign_contact(self):
        self.legacy()
        self.store.bind_owner(404)
        self.store.upsert_contact(Contact(202, 'Other owner'))
        with self.assertRaises(ValueError):
            self.service.list_contacts()

    def test_lookup_result_uses_same_display_normalizer_without_stringifying_containers(self):
        dialog = self.dialog()
        contact = Contact(202, self.record['names'], PHONE)
        dialog._found_context = dialog._dialog_context
        dialog._on_added(contact)
        self.assertIn(NAME, dialog.result_label.text())
        self.assertNotIn("'firstName'", dialog.result_label.text())
        opened = []
        dialog.open_chat.connect(lambda identity, name: opened.append((identity, name)))
        dialog.open_result_btn.click()
        self.assertEqual(opened, [(202, NAME)])

    def test_self_legacy_name_uses_same_owner_profile_without_contact_metadata(self):
        names = [{'name': NAME, 'firstName': 'Synthetic First', 'lastName': 'Family', 'type': 'ONEME'}]
        raw = Contact(101, str(names), PHONE, PHOTO, True)
        self.store.upsert_contact(raw)
        self.store.save_profile(Profile(101, NAME))
        self.service._server_contacts.clear()
        projected = self.service.list_contacts('first family')
        self.assertEqual([(contact.id, contact.name) for contact in projected], [(101, NAME)])
        self.assertEqual((projected[0].phone, projected[0].avatar_url, projected[0].is_verified),
                         (PHONE, PHOTO, True))
        self.assertEqual(self.store.list_contacts(), [raw])
        self.assertEqual(self.service._server_contacts, {})
        dialog = self.dialog()
        row = dialog.list.itemWidget(dialog.list.item(0))
        self.assertEqual(row.findChild(QLabel, 'ContactName').text(), NAME)

    def test_self_profile_fallback_does_not_require_current_names_repr_to_match(self):
        raw = Contact(101, str([{'firstName': 'Synthetic First', 'lastName': 'Family', 'type': 'ONEME'}]))
        self.store.upsert_contact(raw)
        self.store.save_profile(Profile(101, '  ' + NAME + '  '))
        current = {'id': 101, 'names': [{'type': 'CUSTOM', 'name': 'Different current source'}]}
        self.service._server_contacts[101] = copy.deepcopy(current)
        self.assertEqual(self.service.list_contacts()[0].name, NAME)
        self.assertEqual(self.store.list_contacts()[0], raw)
        self.assertEqual(self.service._server_contacts[101], current)

    def test_profile_fallback_never_changes_other_id_or_ordinary_alias(self):
        literal = str([{'name': NAME, 'type': 'ONEME'}])
        self.store.save_profile(Profile(101, NAME))
        for identity, alias in ((202, literal), (0, literal),
                                (101, 'My local label'), (101, '[My genuine local label]')):
            with self.subTest(identity=identity, alias=alias):
                self.store.delete_contact(101)
                self.store.delete_contact(202)
                self.store.delete_contact(0)
                self.store.upsert_contact(Contact(identity, alias))
                self.assertEqual(self.service.list_contacts()[0].name, alias)

    def test_profile_fallback_requires_matching_integer_profile_id_and_normalized_name(self):
        literal = str([{'name': NAME, 'type': 'ONEME'}])
        self.store.upsert_contact(Contact(101, literal))
        for profile in (None, Profile(404, NAME), Profile('101', NAME), Profile(True, NAME),
                        Profile(101, 'Different Name'), Profile(101, {'name': NAME}), Profile(101, '')):
            with self.subTest(profile=profile):
                with patch.object(self.store, 'load_profile', return_value=profile):
                    self.assertEqual(self.service.list_contacts()[0].name, literal)

    def test_self_fallback_rejects_unproven_literals_without_executing_or_changing_them(self):
        self.store.save_profile(Profile(101, NAME))
        literals = [
            str([{'name': 'Different Name', 'type': 'ONEME'}]),
            '[{"name": "' + NAME + '", "type": "ONEME"}]',  # Not historical Python str.
            "[{'name': '" + NAME + "', 'name': '" + NAME + "'}]",  # Duplicate keys.
            str([{'name': NAME, 'extra': 'unproven'}]),
            str([{'name': NAME, 'type': 'UNKNOWN'}]),
            str([{'name': NAME, 'firstName': None}]),
            str([{'name': NAME, 'lastName': True}]),
            str([{'name': NAME, 'firstName': 1}]),
            str([{'name': [NAME]}]),
            str([{'name': {'nested': NAME}}]),
            str([{'name': NAME}])[:-1],
            "[{'name': __import__('builtins').str('" + NAME + "')}]",
            "[{'name': value.name}]", "[{'name': b'" + NAME + "'}]",
            "[{'name': '" + NAME + "'} for item in []]",
            str([{'name': NAME}] * 17),
            str([{'name': NAME, 'firstName': 'x' * 257}]),
            str([{'name': NAME, 'firstName': 'x' * 4096}]),
        ]
        for literal in literals:
            with self.subTest(literal=literal[:110]):
                raw = Contact(101, literal)
                self.store.upsert_contact(raw)
                self.assertEqual(self.service.list_contacts()[0].name, literal)
                self.assertEqual(self.store.list_contacts()[0], raw)

    def test_oversize_self_literal_is_rejected_before_ast_parse(self):
        self.store.save_profile(Profile(101, NAME))
        literal = str([{'name': NAME, 'firstName': 'x' * 4096}])
        self.store.upsert_contact(Contact(101, literal))
        with patch('maxclient.core.service.ast.parse', side_effect=AssertionError('Oversize parse')) as parse:
            self.assertEqual(self.service.list_contacts()[0].name, literal)
        parse.assert_not_called()

    def test_owner_change_during_profile_read_discards_self_projection(self):
        self.store.upsert_contact(Contact(101, str([{'name': NAME}])))
        self.store.save_profile(Profile(101, NAME))
        original = self.store.load_profile
        def switch():
            profile = original()
            self.service.session.my_user_id = 404
            self.store.bind_owner(404)
            return profile
        with patch.object(self.store, 'load_profile', side_effect=switch):
            with self.assertRaises(ValueError):
                self.service.list_contacts()


if __name__ == '__main__':
    unittest.main()
