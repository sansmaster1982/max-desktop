"""Device labels from synthetic session DTOs; no transport or account data."""
import copy
import unittest

from maxclient.core.service import MaxService
from maxclient.protocol.client import MaxFrame


STAMP = 1_700_000_000_000


class SessionDisplayLabelTests(unittest.TestCase):
    def parse(self, raw):
        frame = MaxFrame(1, 1, 96, b'', {'sessions': [raw]})
        before = copy.deepcopy(frame.decoded)
        result = MaxService._sessions_from_frame(frame)
        self.assertEqual(frame.decoded, before)
        return result[0]

    def test_observed_web_null_null_label_uses_actual_client_only(self):
        row = self.parse({'time': STAMP, 'client': 'WEB', 'info': 'null, null'})
        self.assertEqual(row['label'], 'WEB')
        self.assertEqual(row['client'], 'WEB')
        self.assertEqual(row['info'], 'null, null')

    def test_only_empty_or_null_only_info_is_omitted_from_display(self):
        for info in ('', ' \t\n ', 'null', ' null , null ', 'NULL, null',
                     'null, ', ', ,'):
            with self.subTest(info=info):
                row = self.parse({'time': STAMP, 'client': 'WEB', 'info': info})
                self.assertEqual(row['label'], 'WEB')
                self.assertEqual(row['info'], info)

    def test_mixed_descriptions_and_null_words_are_not_rewritten(self):
        for info in ('Windows 11, Chrome 142', 'Windows 11, null',
                     'null, Firefox 142', 'Null Island', 'null-compatible client',
                     'undefined', 'None', '(null)'):
            with self.subTest(info=info):
                row = self.parse({'time': STAMP, 'client': 'WEB', 'info': info})
                self.assertEqual(row['label'], 'WEB · ' + info)
                self.assertEqual(row['info'], info)

    def test_missing_client_display_uses_real_info_or_neutral_device(self):
        for client, info, expected in (
                ('null', 'Firefox 142', 'Firefox 142'),
                (' null, NULL ', 'Windows 11, null', 'Windows 11, null'),
                ('', 'null, null', 'Устройство'),
                (' \t ', '', 'Устройство')):
            with self.subTest(client=client, info=info):
                row = self.parse({'time': STAMP, 'client': client, 'info': info,
                                  'location': 'Synthetic location'})
                self.assertEqual(row['label'], expected)
                self.assertEqual(row['client'], client)
                self.assertEqual(row['info'], info)
                self.assertEqual(row['location'], 'Synthetic location')

    def test_precomposed_labels_drop_only_standalone_placeholder_parts(self):
        for client, info, expected in (
                ('WEB · null, null', '', 'WEB'),
                ('WEB', 'Windows 11 · null, null', 'WEB · Windows 11'),
                ('null · WEB · null, null', 'Firefox', 'WEB · Firefox'),
                ('WEB · Null Island', 'Windows, null', 'WEB · Null Island · Windows, null'),
                ('WEB', 'Chrome·null', 'WEB · Chrome·null')):
            with self.subTest(client=client, info=info):
                row = self.parse({'time': STAMP, 'client': client, 'info': info})
                self.assertEqual(row['label'], expected)

    def test_identity_fields_and_optional_field_presence_are_preserved(self):
        raw = {'time': 0, 'client': 'WEB', 'info': 'null, null',
               'location': 'null, null', 'current': False, 'extra': {'synthetic': 1}}
        row = self.parse(raw)
        self.assertEqual(row, {'time': 0, 'client': 'WEB', 'info': 'null, null',
                              'location': 'null, null', 'current': False, 'label': 'WEB'})
        raw['current'] = True
        self.assertIs(self.parse(raw)['current'], True)
        minimal = self.parse({'time': STAMP, 'client': 'WEB'})
        self.assertEqual(minimal, {'time': STAMP, 'client': 'WEB', 'label': 'WEB'})
        self.assertNotIn('id', minimal)

    def test_wrongly_typed_raw_values_are_still_rejected(self):
        changes = ({'time': None}, {'time': True}, {'time': 1.5},
                   {'time': 2**63}, {'client': None}, {'client': 1},
                   {'info': None}, {'info': ['null', 'null']},
                   {'location': None}, {'location': 42},
                   {'current': 0}, {'current': 'false'})
        for change in changes:
            with self.subTest(change=change):
                data = {'sessions': [{'time': STAMP, 'client': 'WEB', **change}]}
                before = copy.deepcopy(data)
                with self.assertRaises(ValueError):
                    MaxService._sessions_from_frame(MaxFrame(1, 1, 96, b'', data))
                self.assertEqual(data, before)

    def test_bad_ack_list_or_cursor_does_not_become_an_empty_success(self):
        for cmd, data in ((0, {'sessions': []}), (3, {'sessions': []}),
                          (1, None), (1, {}), (1, {'sessions': None}),
                          (1, {'sessions': [None]}),
                          (1, {'sessions': [], 'marker': None})):
            with self.subTest(cmd=cmd, data=data):
                with self.assertRaises(ValueError):
                    MaxService._sessions_from_frame(MaxFrame(cmd, 1, 96, b'', data))
        self.assertEqual(MaxService._sessions_from_frame(
            MaxFrame(1, 1, 96, b'', {'sessions': [], 'marker': 'synthetic-next'})), [])


if __name__ == '__main__':
    unittest.main()
