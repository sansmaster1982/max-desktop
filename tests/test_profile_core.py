"""Current PROFILE16 metadata/update contracts with synthetic server replies."""

import copy
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from maxclient.core.config import AppPaths, Session, Settings
from maxclient.core.models import Profile
from maxclient.core.service import MaxService
from maxclient.core.store import Store
from maxclient.protocol.client import MaxWriteOutcomeUnknown, MaxLoginFailed
from maxclient.protocol.errors import MaxError
from maxclient.protocol.uploader import AttachKind
from test_core_parity import CoreClient
from test_token_service import login_frame


def rich_profile(identity=123, **extra):
    return {"contact": {"id": identity, "phone": 79990000001,
                          "names": [{"type": "CUSTOM", "name": "Local display alias"},
                                    {"type": "ONEME", "firstName": "Sample", "lastName": "User"}],
                          "description": "Server profile description",
                          "baseUrl": "https://cdn.example.test/actual-avatar", "photoId": 5,
                          "link": "https://max.ru/synthetic-profile", **extra}}


class ProfileClient(CoreClient):
    def __init__(self, **options):
        super().__init__(**options)
        self.profile_result = {"profile": rich_profile()}
        self.profile_error = None

    def update_profile(self, **kwargs):
        self.calls.append((16, kwargs))
        if self.profile_error:
            raise self.profile_error
        return copy.deepcopy(self.profile_result)


class ProfileCoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="max-profile-core-")
        self.environment = patch.dict(os.environ, {"MAX_DESKTOP_DATA_DIR": self.temporary.name})
        self.environment.start()
        self.paths = AppPaths()
        with patch("maxclient.core.service.MaxClient", ProfileClient):
            self.service = MaxService(self.paths, Settings.load(self.paths), Session.load(self.paths))
        self.service._finish_login(login_frame(profile=rich_profile()), None)
        self.service.session.save()

    def tearDown(self):
        self.assertFalse(self.service.client.login_calls)
        self.assertFalse(self.service.client.connect_calls)
        self.service.shutdown()
        self.environment.stop()
        self.temporary.cleanup()

    def test_canonical_profile_fields_are_not_guessed_from_a_custom_alias(self):
        profile = self.service.my_profile()
        self.assertEqual((profile.first_name, profile.last_name, profile.name), ("Sample", "User", "Sample User"))
        self.assertEqual(profile.description, "Server profile description")
        self.assertEqual(profile.avatar_url, "https://cdn.example.test/actual-avatar")
        self.assertEqual(profile.photo_id, 5)

    def test_rich_profile_survives_store_restart_for_the_confirmed_owner(self):
        original = self.service.my_profile()
        self.service.store.close()
        self.service.store = Store(self.paths.db_file)
        self.assertEqual(self.service.my_profile(), original)

    def test_profile_update_persists_only_the_returned_server_profile(self):
        self.service.client.profile_result = {"profile": rich_profile(
            names=[{"type": "ONEME", "firstName": "Server", "lastName": "Normalized"}],
            description="Server normalized description")}
        result = self.service.update_my_profile(first_name="Requested", last_name="Name",
                                                  description="Requested description")
        self.assertEqual(result.name, "Server Normalized")
        self.assertEqual(result.description, "Server normalized description")
        self.assertEqual(self.service.my_profile(), result)
        self.assertEqual(self.service.session.my_name, "Server Normalized")
        self.assertEqual(len(self.service.client.calls), 1)

    def test_rejected_profile_change_does_not_optimistically_change_the_cache(self):
        original = self.service.my_profile()
        self.service.client.profile_error = MaxError("Synthetic refusal")
        with self.assertRaises(MaxError):
            self.service.update_my_profile(first_name="Requested")
        self.assertEqual(self.service.my_profile(), original)

    def test_wrong_profile_ack_is_unknown_and_cannot_replace_the_current_account(self):
        original = self.service.my_profile()
        session_bytes = self.paths.session_file.read_bytes()
        self.service.client.profile_result = {"profile": rich_profile(identity=999)}
        with self.assertRaises(MaxWriteOutcomeUnknown):
            self.service.update_my_profile(description="Requested description")
        self.assertEqual(self.service.my_profile(), original)
        self.assertEqual(self.paths.session_file.read_bytes(), session_bytes)
        self.assertEqual(self.service.store.cache_owner, 123)

    def test_unknown_profile_write_is_not_repeated(self):
        self.service.client.profile_error = MaxWriteOutcomeUnknown("Synthetic unknown outcome")
        with self.assertRaises(MaxWriteOutcomeUnknown):
            self.service.update_my_profile(description="Requested description")
        self.assertEqual(len(self.service.client.calls), 1)
        self.assertEqual(self.service.my_profile().description, "Server profile description")

    def test_full_display_name_is_not_split_into_unproven_first_last_fields(self):
        profile = rich_profile(names=[{"type": "ONEME", "name": "Single stored full display name"}])
        self.service._finish_login(login_frame(profile=profile), 123)
        self.assertIsNone(self.service.my_profile().first_name)
        self.assertIsNone(self.service.my_profile().last_name)

    def test_avatar_uses_profile_upload_then_the_returned_token_and_real_profile_ack(self):
        self.service.client.profile_result = {"profile": rich_profile(baseUrl="https://cdn.example.test/new-avatar")}
        with patch("maxclient.core.service.upload_and_build_attach",
                   return_value={"_type": "PHOTO", "photoToken": "synthetic-upload-token"}) as upload:
            profile = self.service.set_my_avatar("synthetic-photo-path")
        upload.assert_called_once_with(self.service.client, "synthetic-photo-path", AttachKind.PHOTO,
                                       on_progress=None, profile=True)
        self.assertEqual(profile.avatar_url, "https://cdn.example.test/new-avatar")
        sent = self.service.client.calls[0][1]
        self.assertEqual(sent["photo_token"], "synthetic-upload-token")
        self.assertEqual(sent["avatar_type"], "USER_AVATAR")

    def test_missing_photo_token_prevents_profile_write(self):
        with patch("maxclient.core.service.upload_and_build_attach", return_value={"_type": "PHOTO"}):
            with self.assertRaises(ValueError):
                self.service.set_my_avatar("synthetic-photo-path")
        self.assertFalse(self.service.client.calls)

    def test_owner_switch_clears_previous_cached_profile_as_well_as_messages(self):
        self.service.store.bind_owner(999)
        self.service.store.save_profile(Profile(id=999, name="Other owner", description="Other owner description"))
        self.service.session.my_user_id = None
        self.service._finish_login(login_frame(profile=rich_profile()), None)
        self.assertEqual(self.service.store.cache_owner, 123)
        self.assertEqual(self.service.my_profile().description, "Server profile description")


if __name__ == "__main__":
    unittest.main()
