"""Regressions derived from current WEB + native binary10 producer/consumer."""
import unittest
import msgpack
from maxclient.protocol.codec import decompress_body,pack_payload,unpack_payload


class BinaryEnvelopeTests(unittest.TestCase):
    def test_coefficient_255_is_lz4_for_current_web_native(self):
        self.assertEqual(decompress_body(b'\x30abc',255),b'abc')

    def test_lz4_budget_is_not_an_exact_output_length(self):
        self.assertEqual(decompress_body(b'\x30abc',2),b'abc')

    def test_lz4_cannot_exceed_coefficient_budget(self):
        with self.assertRaises(ValueError):
            decompress_body(b'\x1fA\x01\x00\x14',1)

    def test_payload_does_not_skip_an_unrecognized_prefix_or_accept_trailing_bytes(self):
        valid=pack_payload({'chatId':7,'messageId':9})
        self.assertEqual(unpack_payload(valid),{'chatId':7,'messageId':9})
        for prefix in (b'\xc1',b'\xc1\xc1',b'\xc1\xc1\xc1\xc1'):
            self.assertIsNone(unpack_payload(prefix+valid))
        self.assertIsNone(unpack_payload(valid+b'\x00'))

    def test_compound_map_keys_are_not_converted_to_invented_string_keys(self):
        self.assertIsNone(unpack_payload(b'\x81\x91\x01\x02'))
        self.assertEqual(unpack_payload(msgpack.packb({b'id':7})),{b'id':7})

    def test_invalid_compression_coefficient_is_rejected(self):
        for coefficient in (-1,256,True,'1'):
            with self.assertRaises(ValueError):decompress_body(b'abc',coefficient)


if __name__=='__main__':unittest.main()
