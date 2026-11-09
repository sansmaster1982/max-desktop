"""Offline regressions for importing an existing MAX token and device UUID."""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from maxclient.core.config import AppPaths, Session, Settings
from maxclient.core.models import Chat
from maxclient.core.service import MaxService
from maxclient.protocol.client import AuthenticatedSession, ConnectionState, MaxFrame, MaxLoginFailed


ORIGINAL_DEVICE = "10000000-0000-4000-8000-000000000001"
IMPORTED_DEVICE = "20000000-0000-4000-8000-000000000002"


def confirm_fake_transport(service):
    """Model an already accepted synthetic LOGIN; never execute auth or network."""
    client = service.client
    client.state = ConnectionState.CONNECTED
    client._device_type = "WEB"
    client._sock = object()
    client.authenticated_session = AuthenticatedSession(
        service.my_id, service.session.device_id, "WEB", client._sock)


def login_frame(profile=None, **extra):
    if profile is None:
        profile = {"contact": {"id": 123, "phone": 79990000001,
                                "names": [{"firstName": "Sample", "lastName": "User"}]}}
    return MaxFrame(cmd=1, seq=4, opcode=19,
                    body=b"\xa6chatId\xd2\x00\x00\x05\x39",
                    decoded={"profile": profile, **extra})


class FakeClient:
    def __init__(self, **options):
        self.device_id = options["device_id"]
        self.app_version = options["app_version"]
        self.auto_reconnect = options["auto_reconnect"]
        self.is_connected = False
        self.connect_calls = []
        self.login_calls = []
        self.close_calls = 0
        self.frame = login_frame()
        self.error = None

    def connect(self, device_type):
        self.connect_calls.append((device_type, self.device_id, self.app_version,
                                   self.auto_reconnect))
        self.is_connected = True

    def login(self, token):
        self.login_calls.append(token)
        if self.error is not None:
            raise self.error
        return self.frame

    def current_profile(self):
        raise AssertionError("Confirmed LOGIN already contains the profile")

    def chat_info(self, ids):
        raise AssertionError("LOGIN ingestion must use decoded chats")

    def close(self):
        self.close_calls += 1
        self.is_connected = False


class TokenServiceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="max-token-service-")
        self.root = Path(self.temporary.name)
        self.environment = patch.dict(os.environ, {
            "MAX_DESKTOP_DATA_DIR": str(self.root / "isolated"),
            "APPDATA": str(self.root / "original-appdata"),
        })
        self.environment.start()
        self.paths = AppPaths()
        self.settings = Settings.load(self.paths)
        # Token import must honor its configured WEB version independently of
        # whichever public build is the current product default.
        self.settings.web_app_version = "26.10.9"
        self.session = Session.load(self.paths)
        self.session.set_device_id(ORIGINAL_DEVICE)
        with patch("maxclient.core.service.MaxClient", FakeClient):
            self.service = MaxService(self.paths, self.settings, self.session)

    def tearDown(self):
        self.service.shutdown()
        self.environment.stop()
        self.temporary.cleanup()

    def seed_saved_session(self):
        self.session.my_user_id = 7
        self.session.my_name = "Previous user"
        self.session.phone = "+79990000007"
        self.session.set_token("synthetic-previous-token", "web")

    def saved_files(self):
        return {path.name: path.read_bytes() for path in
                (self.paths.token_file, self.paths.session_file, self.paths.device_file)}

    def test_data_override_does_not_touch_original_appdata(self):
        self.assertEqual(self.paths.dir, self.root / "isolated")
        self.assertFalse((self.root / "original-appdata").exists())

    def test_import_uses_original_device_and_persists_only_confirmed_profile(self):
        self.service.client.frame = login_frame(
            chats=[{"id": 42, "title": "Verification codes", "type": "DIALOG"}],
            contacts=[{"id": 77, "names": [{"firstName": "Example", "lastName": "Contact"}],
                       "phone": 79990000077}],
        )
        self.service.login_with_token("synthetic-new-token", device_id=IMPORTED_DEVICE)
        self.assertEqual(self.service.client.connect_calls,
                         [("WEB", IMPORTED_DEVICE, self.settings.web_app_version, False)])
        self.assertEqual(self.paths.device_file.read_text(encoding="utf-8"), IMPORTED_DEVICE)
        self.assertEqual(self.session.token, "synthetic-new-token")
        self.assertEqual(self.session.my_user_id, 123)
        self.assertEqual(self.session.my_name, "Sample User")
        self.assertEqual(self.session.phone, "+79990000001")
        self.assertEqual([chat.id for chat in self.service.list_chats()], [42])
        self.assertEqual(self.service.list_contacts()[0].name, "Example Contact")
        self.assertEqual(self.service.client.login_calls, ["synthetic-new-token"])
        self.assertTrue(self.service.client.auto_reconnect)

    def test_failed_candidate_preserves_previous_session_and_device_files(self):
        self.seed_saved_session()
        original = self.saved_files()
        self.service.client.error = MaxLoginFailed("Synthetic server refusal")
        with self.assertRaises(MaxLoginFailed):
            self.service.login_with_token("synthetic-rejected-token", IMPORTED_DEVICE)
        self.assertEqual(self.saved_files(), original)
        self.assertEqual(self.session.my_user_id, 7)
        self.assertEqual(self.session.token, "synthetic-previous-token")
        self.assertEqual(self.session.device_id, ORIGINAL_DEVICE)
        self.assertEqual(self.service.client.device_id, ORIGINAL_DEVICE)
        self.assertFalse(self.service.client.is_connected)
        self.assertEqual(len(self.service.client.login_calls), 1)

    def test_unconfirmed_profile_does_not_replace_saved_token_or_cache(self):
        self.seed_saved_session()
        original = self.saved_files()
        self.service.store.upsert_chat(Chat(id=9, title="Existing local chat"))
        self.service.client.frame = login_frame(profile={"contact": {"name": "No ID"}})
        with self.assertRaises(MaxLoginFailed):
            self.service.login_with_token("synthetic-unconfirmed-token", IMPORTED_DEVICE)
        self.assertEqual(self.saved_files(), original)
        self.assertEqual([chat.id for chat in self.service.store.list_chats()], [9])
        self.assertEqual(self.session.my_user_id, 7)

    def test_restore_rejected_web_token_once_without_type_probe_or_deletion(self):
        self.seed_saved_session()
        original = self.saved_files()
        self.service.client.error = MaxLoginFailed("Synthetic server refusal")
        self.assertFalse(self.service.try_restore())
        self.assertIsInstance(self.service.last_restore_error, MaxLoginFailed)
        self.assertEqual(self.service.client.connect_calls,
                         [("WEB", ORIGINAL_DEVICE, self.settings.web_app_version, False)])
        self.assertEqual(self.saved_files(), original)
        self.assertEqual(len(self.service.client.login_calls), 1)

    def test_network_error_is_exposed_and_preserves_saved_credentials(self):
        self.seed_saved_session()
        original = self.saved_files()
        self.service.client.error = ConnectionError("Synthetic network failure")
        self.assertFalse(self.service.try_restore())
        self.assertIsInstance(self.service.last_restore_error, ConnectionError)
        self.assertEqual(self.saved_files(), original)
        self.assertEqual(len(self.service.client.connect_calls), 1)

    def test_saved_android_kind_is_used_without_web_probe(self):
        self.session.set_token("synthetic-android-token", "android")
        self.assertTrue(self.service.try_restore())
        self.assertEqual(self.service.client.connect_calls,
                         [("ANDROID", ORIGINAL_DEVICE, self.settings.app_version, False)])
        self.assertEqual(self.session.token_kind, "android")

    def test_invalid_device_is_rejected_before_closing_existing_connection(self):
        self.service.client.is_connected = True
        with self.assertRaises(ValueError):
            self.service.login_with_token("synthetic-new-token", "invalid-device")
        self.assertTrue(self.service.client.is_connected)
        self.assertEqual(self.service.client.close_calls, 0)
        self.assertFalse(self.service.client.connect_calls)

    def test_expected_qr_profile_mismatch_does_not_write_credentials_or_bind_cache(self):
        self.seed_saved_session()
        original = self.saved_files()
        self.service.store.upsert_chat(Chat(id=9, title="Previous cache"))
        with self.assertRaises(MaxLoginFailed):
            self.service.login_with_token("synthetic-qr-token", IMPORTED_DEVICE,
                                             expected_profile_id=999)
        self.assertEqual(self.saved_files(), original)
        self.assertEqual([chat.id for chat in self.service.store.list_chats()], [9])
        self.assertIsNone(self.service.store.cache_owner)
        self.assertEqual(self.session.my_user_id, 7)
        self.assertEqual(len(self.service.client.login_calls), 1)

    def test_explicit_profile_rpc_failure_is_not_swallowed(self):
        self.seed_saved_session()
        original = self.saved_files()
        self.service.client.current_profile = lambda: (_ for _ in ()).throw(ConnectionError())
        with self.assertRaises(ConnectionError):
            self.service._load_my_profile()
        self.assertEqual(self.saved_files(), original)

    def test_auth_invalid_notification_preserves_saved_token(self):
        self.seed_saved_session()
        original = self.saved_files()
        self.service._on_auth_invalid()
        self.assertEqual(self.saved_files(), original)
        self.assertIsInstance(self.service.last_restore_error, MaxLoginFailed)


if __name__ == "__main__":
    unittest.main()
