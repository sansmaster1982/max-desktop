"""Synthetic decoded binary48 maps retain int64 identity without schema bypass."""
import copy
import unittest

import msgpack

from maxclient.protocol.codec import unpack_payload
from maxclient.protocol.chat_record_maps import normalize_chat_id_maps
from maxclient.protocol.conversation_lifecycle_contracts import lifecycle_readback
from maxclient.protocol.direct_dialog_contracts import direct_dialog_reply

import test_conversation_lifecycle as fixtures


class ChatRecordMapsTests(unittest.TestCase):
    def test_real_decoder_int64_extension_keys_reach_typed48(self):
        raw = fixtures.chat(participants={msgpack.ExtType(1, msgpack.packb(101)): 1,
                                         msgpack.ExtType(1, msgpack.packb(202)): 2})
        decoded = unpack_payload(msgpack.packb({'chats': [raw]}, use_bin_type=True))
        self.assertEqual(set(decoded['chats'][0]['participants']), {101, 202})
        result = lifecycle_readback(decoded, 303)
        self.assertEqual(result['participants'], {'101': 1, '202': 2})
        self.assertEqual(set(decoded['chats'][0]['participants']), {101, 202})

    def test_direct_dialog48_accepts_integer_participant_keys_exactly(self):
        raw = fixtures.chat(participants={101: 1, 202: 1})
        result = direct_dialog_reply({'chats': [raw]}, 101, 202, 303)
        self.assertEqual(result['participants'], {'101': 1, '202': 1})
        with self.assertRaises(ValueError):
            direct_dialog_reply({'chats': [fixtures.chat(participants={101: 1, 404: 1})]}, 101, 202, 303)

    def test_large_integer_keys_preserve_full_precision(self):
        owner, peer = 2**62 + 123, 2**61 + 202
        raw = fixtures.chat(id=owner ^ peer, participants={owner: 1, str(peer): 2})
        result = direct_dialog_reply({'chats': [raw]}, owner, peer, owner ^ peer)
        self.assertEqual(set(result['participants']), {str(owner), str(peer)})

    def test_same_self_participant_reaches_saved_readback(self):
        result = direct_dialog_reply({'chats': [fixtures.chat(id=0, participants={101: 1})]}, 101, 101, 0)
        self.assertEqual(result['participants'], {'101': 1})

    def test_conflicting_or_malformed_participant_keys_are_never_coerced(self):
        for participants in ({101: 1, '101': 2}, {'0101': 1}, {True: 1},
                             {1.5: 1}, {-101: 1}, {2**63: 1}, {b'101': 1}):
            with self.subTest(participants=participants), self.assertRaises(ValueError):
                lifecycle_readback({'chats': [fixtures.chat(participants=participants)]}, 303)

    def test_admin_record_identity_is_preserved_and_conflicts_rejected(self):
        raw = fixtures.chat(adminParticipants={101: {'id': 101, 'permissions': 256}})
        before = copy.deepcopy(raw)
        result = normalize_chat_id_maps(raw)
        self.assertEqual(result['adminParticipants'], {'101': {'id': 101, 'permissions': 256}})
        self.assertEqual(raw, before)
        raw['adminParticipants']['101'] = {'id': 101, 'permissions': 0}
        with self.assertRaises(ValueError):
            normalize_chat_id_maps(raw)

    def test_unrelated_full_dto_validation_is_not_weakened(self):
        for raw in (fixtures.chat(participants={101: True, 202: 1}),
                    fixtures.chat(lastMessage={'id': 9, 'time': 90}),
                    fixtures.chat(created=None), fixtures.chat(id=404)):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                lifecycle_readback({'chats': [raw]}, 303)


if __name__ == '__main__':
    unittest.main()
