"""Pure validators for retained public41/32 contracts; no account/network."""
import copy
import unittest

from maxclient.protocol.contact_create_contracts import (
    contact_create_payload, contact_create_reply, contact_create_readback,
)


PHONE = '+79990000000'


def record(identity=202):
    return {'id': identity, 'updateTime': 123, 'phone': 79990000000, 'flags': 512,
            'names': [{'type': 'CUSTOM', 'firstName': 'Synthetic', 'lastName': 'Person'}]}


class ContactCreateContractTests(unittest.TestCase):
    def test_exact_official_payload_omits_empty_surname_and_generic_name(self):
        self.assertEqual(contact_create_payload('8 (999) 000-00-00', ' First ', ' '),
                         {'phone': PHONE, 'firstName': 'First'})
        self.assertEqual(contact_create_payload(PHONE, ' First ', ' Last '),
                         {'phone': PHONE, 'firstName': 'First', 'lastName': 'Last'})

    def test_invalid_phone_and_names_never_make_a_payload(self):
        for phone in (None, True, 79990000000, '', '+', '+0000000', 'abc79990000000', '+12', '+１２３４５６７８９'):
            with self.subTest(phone=phone), self.assertRaises(ValueError):
                contact_create_payload(phone, 'Name')
        for first in (None, True, [], '', ' ', 'x' * 61, '\ud800', 'x\ny'):
            with self.subTest(first=repr(first)), self.assertRaises(ValueError):
                contact_create_payload(PHONE, first)

    def test_utf16_form_limit(self):
        self.assertEqual(contact_create_payload(PHONE, '\U0001f600' * 30)['firstName'], '\U0001f600' * 30)
        with self.assertRaises(ValueError):
            contact_create_payload(PHONE, '\U0001f600' * 31)

    def test_typed_new_and_signed_int64_identity(self):
        for identity in (-(2**63), 0, 2**63-1):
            raw = record(identity)
            self.assertEqual(contact_create_reply({'contact': raw, 'new': False}), (raw, False))
        for identity in (True, '202', 202.0, 2**63, -(2**63)-1):
            with self.subTest(identity=identity), self.assertRaises(ValueError):
                contact_create_reply({'contact': record(identity), 'new': True})
        for new in (None, 0, 1, 'true'):
            with self.subTest(new=new), self.assertRaises(ValueError):
                contact_create_reply({'contact': record(), 'new': new})

    def test_malformed_dto_cannot_confirm_success(self):
        variants = [dict(record(), flags=True), dict(record(), flags=2**31),
                    dict(record(), phone='79990000000'), dict(record(), phone=True),
                    dict(record(), phone=float('nan')), dict(record(), phone=10**1000),
                    dict(record(), names=[{'firstName': []}]),
                    dict(record(), status='ADDED'), dict(record(), updateTime=True)]
        for raw in variants:
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                contact_create_reply({'contact': raw, 'new': True})

    def test_readback_requires_matching_identity_membership_and_actual_custom_name(self):
        payload = contact_create_payload(PHONE, 'Synthetic', 'Person')
        raw = record()
        result = contact_create_readback({'contacts': [raw]}, 202, payload)
        result['names'][0]['firstName'] = 'Detached'
        self.assertEqual(raw['names'][0]['firstName'], 'Synthetic')
        bad = [[], [record(303)], [raw, copy.deepcopy(raw)], [dict(raw, flags=0)],
               [dict(raw, status='REMOVED')], [dict(raw, status='HIDDEN')],
               [dict(raw, phone=79990000001)],
               [dict(raw, names=[{'type': 'ONEME', 'firstName': 'Synthetic', 'lastName': 'Person'}])],
               [dict(raw, names=[{'type': 'CUSTOM', 'firstName': 'Old', 'lastName': 'Person'}])]]
        for rows in bad:
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                contact_create_readback({'contacts': rows}, 202, payload)

    def test_absent_phone_and_absent_optional_surname_are_supported(self):
        raw = record()
        raw.pop('phone')
        raw['names'][0].pop('lastName')
        self.assertEqual(contact_create_readback({'contacts': [raw]}, 202,
                         contact_create_payload(PHONE, 'Synthetic')), raw)
