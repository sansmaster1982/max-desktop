"""Typed MessagePack CONFIG.chats IDs; synthetic, local and without RPCs."""
import copy
import math
import unittest
from unittest.mock import Mock

import msgpack
import test_contact_lookup_core as fixture
from maxclient.core.models import Contact
from maxclient.protocol.client import MaxFrame
from maxclient.protocol.codec import pack_payload, unpack_payload
from maxclient.protocol.settings_contracts import chat_notification_values


class ChatConfigIdentityTests(unittest.TestCase):
    def test_integer_and_string_int64_keys_normalize_without_changing_values(self):
        data = {0: {'dontDisturbUntil': -1}, -1: {'dontDisturbUntil': 0},
                -(2**63): {'dontDisturbUntil': 10.25},
                str(2**63 - 1): {'dontDisturbUntil': 10.5},
                '0002': {'dontDisturbUntil': -.5}}
        before = copy.deepcopy(data)
        self.assertEqual(chat_notification_values(data), {
            '0': {'dontDisturbUntil': -1}, '-1': {'dontDisturbUntil': 0},
            str(-(2**63)): {'dontDisturbUntil': 10.25},
            str(2**63 - 1): {'dontDisturbUntil': 10.5},
            '2': {'dontDisturbUntil': -.5}})
        self.assertEqual(data, before)

    def test_invalid_keys_and_non_dict_fields_still_reject(self):
        for identity in (True, False, 0.0, 1.25, None, b'1', 2**63, -(2**63)-1,
                         '9223372036854775808', '', '-', '--1', '+1', '1.0', ' 1', '١'):
            with self.subTest(identity=identity), self.assertRaises(ValueError):
                chat_notification_values({identity: {'dontDisturbUntil': 0}})
        for fields in (None, [], 'invalid', False, 0):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                chat_notification_values({0: fields})
        with self.assertRaises(ValueError):
            chat_notification_values([])

    def test_canonical_collisions_reject_even_when_one_entry_has_no_mute_value(self):
        for data in ({1: {'dontDisturbUntil': 0}, '1': {'dontDisturbUntil': -1}},
                     {'01': {}, 1: {'dontDisturbUntil': 0}},
                     {'-0': {'dontDisturbUntil': 0}, '0': {}},
                     {'1': {}, '001': {}}):
            with self.subTest(data=data), self.assertRaises(ValueError):
                chat_notification_values(data)

    def test_timestamp_guards_remain_for_both_accepted_key_types(self):
        for identity in (0, '0'):
            for value in (True, None, '0', [], math.nan, math.inf, -math.inf, -1.01, 10**400):
                with self.subTest(identity=identity, value_type=type(value).__name__), self.assertRaises(ValueError):
                    chat_notification_values({identity: {'dontDisturbUntil': value}})
        self.assertEqual(chat_notification_values({0: {}}), {})

    def test_transport_preserves_plain_and_code_one_integer_map_keys(self):
        extension_key = msgpack.ExtType(1, msgpack.packb(303))
        decoded = unpack_payload(pack_payload({'chats': {
            0: {'dontDisturbUntil': 0}, extension_key: {'dontDisturbUntil': -1}}}))
        self.assertTrue(all(type(identity) is int for identity in decoded['chats']))
        self.assertEqual(chat_notification_values(decoded['chats']), {
            '0': {'dontDisturbUntil': 0}, '303': {'dontDisturbUntil': -1}})

    def test_login_integer_chat_config_reaches_existing_self_contact_repair(self):
        case = fixture.ContactLookupCoreTests()
        case.setUp()
        self.addCleanup(case.doCleanups)
        service = case.service
        service.log = Mock()
        legacy = [{'type': 'ONEME', 'firstName': 'Synthetic', 'lastName': 'Owner'}]
        service.store.upsert_contact(Contact(101, str(legacy)))
        decoded = unpack_payload(pack_payload({
            'profile': {'contact': {'id': 101, 'names': legacy}},
            'config': {'chats': {0: {'dontDisturbUntil': -1}}},
            'contacts': [{'id': 202, 'names': [{'firstName': 'Synthetic Peer'}]}],
        }))
        service._finish_login(MaxFrame(1, 1, 19, b'', decoded), 101)
        actual = {contact.id: contact for contact in service.store.list_contacts()}
        self.assertEqual(actual[101].name, 'Synthetic Owner')
        self.assertEqual(actual[202].name, 'Synthetic Peer')
        self.assertEqual(service.store.load_config(101)['chats'], {'0': {'dontDisturbUntil': -1}})
        service.log.assert_not_called()
        service.client.request.assert_not_called()
        service.client.contact_by_phone.assert_not_called()

    def test_colliding_config_does_not_commit_partially(self):
        case = fixture.ContactLookupCoreTests()
        case.setUp()
        self.addCleanup(case.doCleanups)
        service = case.service
        service._remember_config({'chats': {3: {'dontDisturbUntil': -1}}}, owner_id=101)
        before = service.store.load_config(101)
        with self.assertRaises(ValueError):
            service._remember_config({'chats': {
                1: {'dontDisturbUntil': 0}, '1': {'dontDisturbUntil': -1}}}, owner_id=101)
        self.assertEqual(service.store.load_config(101), before)
        service.client.request.assert_not_called()


if __name__ == '__main__':
    unittest.main()
