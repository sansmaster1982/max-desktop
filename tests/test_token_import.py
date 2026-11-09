"""Local credential import never executes source or follows remote paths."""
import json
import tempfile
import unittest
from pathlib import Path

from maxclient.core.token_import import read_web_session


class TokenImportTests(unittest.TestCase):
    def test_saved_metadata_loads_token_and_original_device(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            (root / 'token.txt').write_text('A' * 120, encoding='utf-8')
            (root / 'session.json').write_text(json.dumps({'tokenFile':'token.txt','deviceId':'12345678-1234-1234-1234-123456789abc','deviceType':'WEB','transport':'wss'}), encoding='utf-8')
            value = read_web_session(root / 'session.json')
            self.assertEqual(value.token, 'A' * 120)
            self.assertEqual(value.device_id, '12345678-1234-1234-1234-123456789abc')
            self.assertNotIn('A' * 120, repr(value))

    def test_token_reference_cannot_escape_selected_directory(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            (root / 'inner').mkdir()
            (root / 'token.txt').write_text('A' * 120, encoding='utf-8')
            selected = root / 'inner' / 'session.json'
            selected.write_text(json.dumps({'tokenFile':'../token.txt','deviceId':'12345678-1234-1234-1234-123456789abc'}), encoding='utf-8')
            with self.assertRaises(ValueError):
                read_web_session(selected)

    def test_script_is_rejected_without_execution(self):
        with tempfile.TemporaryDirectory() as name:
            selected = Path(name) / 'session.json'
            selected.write_text("localStorage.setItem('token','anything');", encoding='utf-8')
            with self.assertRaises(ValueError):
                read_web_session(selected)


if __name__ == '__main__':
    unittest.main()
