"""Phone lookup/cache routing with synthetic contacts and an in-memory owner."""
import copy
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from maxclient.core.models import Chat, Contact
from maxclient.core.service import MaxService, _clean_name, _person_name
from maxclient.core.store import Store
from maxclient.protocol.client import AuthenticatedSession, ConnectionState, MaxClient, MaxFrame
from maxclient.protocol.errors import MaxError


DEVICE = '00112233-4455-6677-8899-aabbccddeeff'
PHONE = '+79990000000'


class ContactLookupCoreTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(':memory:')
        self.addCleanup(self.store.close)
        self.store.bind_owner(101)
        self.service = MaxService.__new__(MaxService)
        self.service.store = self.store
        self.service.session = SimpleNamespace(my_user_id=101, device_id=DEVICE)
        self.service._metadata_lock = threading.RLock()
        self.service._server_metadata_owner = 101
        self.service._server_chats = {}
        self.service._server_contacts = {}
        self.service._contacts_cache = None
        self.client = MaxClient(device_id=DEVICE, auto_reconnect=False)
        self.client._device_type = 'WEB'
        self.client._state = ConnectionState.CONNECTED
        self.client._sock = object()
        self.context = AuthenticatedSession(101, DEVICE, 'WEB', self.client._sock)
        self.client._auth_session = self.context
        self.client.contact_by_phone = Mock(return_value=self.contact())
        self.client.request = Mock(side_effect=AssertionError('No real RPC in synthetic lookup'))
        self.service.client = self.client

    def contact(self, identity=202):
        return {'id': identity, 'phone': PHONE, 'names': [
            {'type': 'ONEME', 'firstName': 'Synthetic', 'lastName': 'Person'},
            {'type': 'CUSTOM', 'name': 'Later alias'}]}

    def dialog(self, chat_id=303, peer=202):
        self.service._remember_chat_info({'chats': [{'id': chat_id, 'type': 'DIALOG',
            'participants': {101: 1, peer: 1}}]}, my_id=101)

    def switch(self, owner=404, *, same_owner=False):
        if not same_owner:
            self.store.bind_owner(owner)
            self.service.session.my_user_id = owner
            self.service._server_metadata_owner = owner
            self.service._server_contacts.clear()
            self.service._server_chats.clear()
        self.client._auth_session = AuthenticatedSession(
            101 if same_owner else owner, DEVICE, 'WEB', self.client._sock)

    def test_structured_names_never_become_repr_or_reordered_custom_alias(self):
        contact = self.service.add_contact_by_phone(PHONE, context=self.context)
        self.assertEqual(contact.name, 'Synthetic Person')
        self.assertEqual(self.store.list_contacts()[0].name, 'Synthetic Person')
        self.client.contact_by_phone.assert_called_once_with(PHONE, expected_session=self.context)
        self.assertEqual(self.store.list_chats(), [])
        self.client.request.assert_not_called()

    def test_direct_first_last_precedes_name_and_cleaner_rejects_nontext(self):
        self.assertEqual(_person_name({'firstName': 'First', 'lastName': 'Last',
                                      'name': 'Other'}), 'First Last')
        for value in ([{'name': 'Unsafe repr'}], {'name': 'Unsafe repr'}, 42, True):
            self.assertIsNone(_clean_name(value))
        self.assertEqual(_person_name({'name': [{'firstName': 'Unsafe'}],
                                      'names': [{'firstName': 'Actual'}]}), 'Actual')

    def test_lookup_does_not_create_or_conflate_contact_id_chat(self):
        self.store.upsert_chat(Chat(id=202, title='Unrelated group', is_group=True,
                                    chat_type='CHAT'))
        self.dialog()
        self.service.add_contact_by_phone(PHONE)
        self.assertEqual(self.store.get_chat(202).title, 'Unrelated group')
        routed = self.service.dialog_for_contact(202)
        self.assertEqual((routed.id, routed.peer_user_id, routed.title),
                         (303, 202, 'Synthetic Person'))
        self.assertIsNone(self.service.dialog_for_contact(999))

    def test_unconfirmed_legacy_equal_id_row_cannot_be_a_route(self):
        self.store.upsert_chat(Chat(id=202, peer_user_id=202, title='Legacy'))
        self.service.add_contact_by_phone(PHONE)
        self.assertIsNone(self.service.dialog_for_contact(202))
        self.assertEqual(len(self.store.list_chats()), 1)

    def test_malformed_participants_do_not_become_navigation_targets(self):
        self.dialog()
        raw = self.service._server_chats[303]
        for participants in ([202], {202: True}, {202: 'Not timestamp'},
                             {202: 1, 404: 1}, {'bad-id': 1}):
            raw['participants'] = participants
            self.assertIsNone(self.service.dialog_for_contact(202))
        self.dialog(0, 101)
        self.service._server_chats[0]['participants'] = {202: 1}
        self.assertIsNone(self.service.dialog_for_contact(101))

    def test_self_routes_only_existing_saved_zero_and_repairs_display(self):
        self.dialog(0, 101)
        self.store.upsert_chat(Chat(id=0, title="[{'firstName': 'Old repr'}]"))
        self.store.upsert_chat(Chat(id=101, title='Legacy self', peer_user_id=101))
        self.client.contact_by_phone.return_value = self.contact(101)
        contact = self.service.add_contact_by_phone(PHONE)
        self.assertEqual(contact.id, 101)
        saved = self.service.dialog_for_contact(101)
        self.assertEqual((saved.id, saved.title), (0, 'Избранное'))
        self.assertEqual([row.id for row in self.service.list_chats()], [0])
        # The legacy row is hidden, not deleted along with potential history.
        self.assertIsNotNone(self.store.get_chat(101))
        self.client.request.assert_not_called()

    def test_self_without_confirmed_saved_chat_does_not_invent_zero(self):
        self.client.contact_by_phone.return_value = self.contact(101)
        self.service.add_contact_by_phone(PHONE)
        self.assertIsNone(self.service.dialog_for_contact(101))
        self.assertEqual(self.store.list_chats(), [])

    def test_confirmed_nonzero_self_or_group_rows_are_not_hidden(self):
        self.dialog(0, 101)
        self.dialog(101, 101)
        self.assertEqual({chat.id for chat in self.service.list_chats()}, {0, 101})
        self.service._server_chats.pop(101)
        self.store.upsert_chat(Chat(id=101, is_group=True, chat_type='CHAT'))
        self.assertEqual({chat.id for chat in self.service.list_chats()}, {0, 101})

    def test_unlocked_repr_title_repaired_from_actual_contact_not_repr_parsing(self):
        self.dialog()
        self.store.upsert_chat(Chat(id=303, title="[{'name':'Wrong'}]"))
        self.service.add_contact_by_phone(PHONE)
        self.assertEqual(self.service.list_chats()[0].title, 'Synthetic Person')
        self.store.rename_chat(303, 'My explicit title')
        self.assertEqual(self.service.list_chats()[0].title, 'My explicit title')

    def test_explicit_local_lookup_label_locks_actual_dialog_only(self):
        self.dialog()
        self.service.add_contact_by_phone(PHONE, 'Local label')
        chat = self.service.dialog_for_contact(202)
        self.assertEqual((chat.id, chat.title, chat.title_locked), (303, 'Local label', True))
        self.assertIsNone(self.store.get_chat(202))

    def test_expired_supplied_context_makes_no_lookup(self):
        self.switch(same_owner=True)
        with self.assertRaises(ValueError):
            self.service.add_contact_by_phone(PHONE, context=self.context)
        self.client.contact_by_phone.assert_not_called()

    def test_late_lookup_different_owner_or_same_owner_new_session_has_no_writes(self):
        for same_owner in (False, True):
            with self.subTest(same_owner=same_owner):
                # Re-establish the synthetic original owner for each case.
                self.store.bind_owner(101)
                self.service.session.my_user_id = self.service._server_metadata_owner = 101
                self.client._auth_session = self.context
                def reply(*_args, **_kwargs):
                    self.switch(same_owner=same_owner)
                    return self.contact()
                self.client.contact_by_phone.side_effect = reply
                with self.assertRaises(ValueError):
                    self.service.add_contact_by_phone(PHONE, context=self.context)
                self.assertEqual(self.store.list_contacts(), [])
                self.assertEqual(self.service._server_contacts, {})
                self.assertEqual(self.store.list_chats(), [])

    def test_import_shares_lookup_without_fake_chats(self):
        with patch('maxclient.core.service.parse_contacts_file', return_value=[
                ('Local label', PHONE), ('', PHONE)]):
            self.assertEqual(self.service.import_contacts_from_file('synthetic.csv', context=self.context), 2)
        self.assertEqual(self.client.contact_by_phone.call_count, 2)
        self.assertEqual(self.store.list_chats(), [])
        self.assertEqual(self.store.list_contacts()[0].name, 'Synthetic Person')

    def test_import_stops_after_context_switch_even_when_lookup_raises(self):
        def failed(*_args, **_kwargs):
            self.switch(same_owner=True)
            raise MaxError('Synthetic lookup rejection')
        self.client.contact_by_phone.side_effect = failed
        with patch('maxclient.core.service.parse_contacts_file', return_value=[('', PHONE), ('', PHONE)]):
            with self.assertRaises(ValueError):
                self.service.import_contacts_from_file('synthetic.csv', context=self.context)
        self.assertEqual(self.client.contact_by_phone.call_count, 1)
        self.assertEqual(self.store.list_contacts(), [])

    def test_import_skips_one_unavailable_number_but_no_automatic_retry(self):
        self.client.contact_by_phone.side_effect = [MaxError('Synthetic missing'), self.contact()]
        progress = Mock()
        with patch('maxclient.core.service.parse_contacts_file', return_value=[('', PHONE), ('', PHONE)]):
            self.assertEqual(self.service.import_contacts_from_file('synthetic.csv', progress), 1)
        self.assertEqual(self.client.contact_by_phone.call_count, 2)
        self.assertEqual(progress.call_count, 2)

    def test_invalid_contact_identity_is_not_coerced_or_cached(self):
        for identity in ('202', True, 202.5):
            self.client.contact_by_phone.return_value = self.contact(identity)
            with self.assertRaises(ValueError): self.service.add_contact_by_phone(PHONE)
        self.assertEqual(self.store.list_contacts(), [])


class ContactLookupProtocolTests(unittest.TestCase):
    def test_optional_contact_fields_keep_actual_presence_and_description(self):
        client = MaxClient(auto_reconnect=False)
        client.request = Mock(return_value=MaxFrame(1, 1, 46, b'', {'contact': {
            'id': 101, 'names': [{'firstName': 'Synthetic'}], 'description': 'Actual about'}}))
        result = client.contact_by_phone(PHONE)
        self.assertNotIn('phone', result)
        self.assertEqual(result['description'], 'Actual about')

    def test_structured_names_and_original_order_preserved_without_name_alias(self):
        client = MaxClient(auto_reconnect=False)
        context = object()
        raw = {'id': 202, 'phone': 79990000000.0, 'names': [
            {'type': 'ONEME', 'firstName': 'Synthetic', 'lastName': 'Person'},
            {'type': 'CUSTOM', 'name': 'Later alias'}]}
        client.request = Mock(return_value=MaxFrame(1, 1, 46, b'', {'contact': raw}))
        result = client.contact_by_phone(PHONE, expected_session=context)
        self.assertIsNone(result['name'])
        self.assertEqual(result['names'], raw['names'])
        self.assertEqual(result['phone'], '79990000000')
        result['names'][0]['firstName'] = 'Detached'
        self.assertEqual(raw['names'][0]['firstName'], 'Synthetic')
        client.request.assert_called_once_with(46, {'phone': PHONE}, expected_session=context)

    def test_malformed_phone_never_stringifies_container_or_boolean(self):
        client = MaxClient(auto_reconnect=False)
        for value in ([123], {'phone': 123}, True, 123.5, float('nan')):
            client.request = Mock(return_value=MaxFrame(1, 1, 46, b'', {'contact': {'id': 202, 'phone': value}}))
            self.assertIsNone(client.contact_by_phone(PHONE)['phone'])

