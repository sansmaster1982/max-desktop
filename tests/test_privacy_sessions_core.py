"""Owner-bound supplied settings, partial config and explicit session actions."""
import copy
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from maxclient.core.config import AppPaths, Session, Settings
from maxclient.core.service import MaxService
from maxclient.core.store import Store
from maxclient.protocol.client import MaxError, MaxFrame, MaxWriteOutcomeUnknown
from test_core_parity import CoreClient, STAMP, response
from test_token_service import confirm_fake_transport, login_frame


class PrivacyClient(CoreClient):
    def __init__(self, **options):
        super().__init__(**options)
        self.settings_reply = None
        self.settings_error = None
        self.close_reply = {"token": "synthetic-opaque-ack"}
        self.close_error = None
        self.sessions_reply = response(96, {"sessions": []})

    def update_user_settings(self, values):
        self.calls.append((22, copy.deepcopy(values)))
        if self.settings_error:
            raise self.settings_error
        return self.settings_reply() if callable(self.settings_reply) else copy.deepcopy(self.settings_reply)

    def close_other_sessions(self):
        self.calls.append((97, {}))
        if self.close_error:
            raise self.close_error
        return copy.deepcopy(self.close_reply)

    def sessions_info(self):
        self.calls.append((96, {}))
        return self.sessions_reply


class PrivacySessionsCoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="max-privacy-core-")
        self.environment = patch.dict(os.environ, {"MAX_DESKTOP_DATA_DIR": self.temporary.name})
        self.environment.start()
        self.paths = AppPaths()
        with patch("maxclient.core.service.MaxClient", PrivacyClient):
            self.service = MaxService(self.paths, Settings.load(self.paths), Session.load(self.paths))
        self.service._finish_login(login_frame(config={
            "hash": "synthetic-login-hash", "user": {"HIDDEN": False, "SEARCH_BY_PHONE": "ALL"},
            "server": {"sessions-end-disabled": False}}), None)
        confirm_fake_transport(self.service)

    def tearDown(self):
        self.assertFalse(self.service.client.login_calls)
        self.assertFalse(self.service.client.connect_calls)
        self.service.shutdown()
        self.environment.stop()
        self.temporary.cleanup()

    def test_getter_returns_only_supplied_values_without_rpc_or_defaults(self):
        self.assertEqual(self.service.get_privacy_settings(), {"HIDDEN": False, "SEARCH_BY_PHONE": "ALL"})
        self.assertNotIn("PHONE_NUMBER_PRIVACY", self.service.get_privacy_settings())
        self.assertFalse(self.service.client.calls)

    def test_config_push_merges_partial_values_and_notifies_without_rpc(self):
        observed = []
        self.service.on_config_changed = lambda: observed.append(True)
        self.service._on_push(response(134, {"config": {"user": {"HIDDEN": True,
            "PHONE_NUMBER_PRIVACY": "CONTACTS", "unrelated": "ignored"}}}, cmd=0,
            auth_context=self.service.client.authenticated_session))
        self.assertEqual(self.service.get_privacy_settings(), {
            "HIDDEN": True, "SEARCH_BY_PHONE": "ALL", "PHONE_NUMBER_PRIVACY": "CONTACTS"})
        self.assertEqual(observed, [True])
        self.assertFalse(self.service.client.calls)

    def test_invalid_known_push_field_does_not_partially_poison_settings(self):
        before = self.service.get_privacy_settings()
        with self.assertRaises(ValueError):
            self.service._on_push(response(134, {"config": {"user": {
                "HIDDEN": True, "SEARCH_BY_PHONE": "unverified"}}}, cmd=0,
                auth_context=self.service.client.authenticated_session))
        self.assertEqual(self.service.get_privacy_settings(), before)

    def test_owner_scoped_config_survives_store_reopen(self):
        other = Store(self.paths.db_file)
        try:
            self.assertEqual(other.load_config(123)["user"]["HIDDEN"], False)
            self.assertEqual(other.load_config(999), {})
        finally:
            other.close()

    def test_unknown_or_different_owner_clears_settings_before_new_login(self):
        self.service._finish_login(login_frame(profile={"contact": {"id": 456}},
            config={"user": {"PHONE_NUMBER_PRIVACY": "NOBODY"}}), 123)
        self.assertEqual(self.service.get_privacy_settings(), {"PHONE_NUMBER_PRIVACY": "NOBODY"})
        self.assertEqual(self.service.store.load_config(123), {})

    def test_optional_ack_is_accepted_pending_without_saving_input(self):
        result = self.service.update_privacy_settings({"HIDDEN": True})
        self.assertEqual(result, {"accepted": True, "pending": True,
                                  "settings": {"HIDDEN": False, "SEARCH_BY_PHONE": "ALL"}})
        self.assertEqual(self.service.client.calls, [(22, {"HIDDEN": True})])

    def test_only_actual_returned_values_become_confirmed(self):
        self.service.client.settings_reply = {"hash": "synthetic-updated", "user": {"HIDDEN": True}}
        result = self.service.update_privacy_settings({"HIDDEN": True})
        self.assertFalse(result["pending"])
        self.assertTrue(result["settings"]["HIDDEN"])
        self.service.client.settings_reply = {"hash": "synthetic-other", "user": {"HIDDEN": False}}
        result = self.service.update_privacy_settings({"HIDDEN": True})
        self.assertTrue(result["pending"])
        self.assertFalse(result["settings"]["HIDDEN"])

    def test_family_safemode_and_unsupported_writes_stop_before_rpc(self):
        for values in ({"SAFE_MODE": False}, {"FAMILY_PROTECTION": "OFF"},
                       {"HIDDEN": "NOBODY"}, {"SEARCH_BY_PHONE": "NOBODY"}):
            with self.assertRaises(ValueError):
                self.service.update_privacy_settings(values)
        self.assertFalse(self.service.client.calls)

    def test_guarded_controls_obey_managed_and_safe_flags_but_not_guessed_global_lock(self):
        for settings in ({"SAFE_MODE": True}, {"FAMILY_PROTECTION": "MANAGEABLE"}):
            self.service._remember_config({"user": settings})
            with self.assertRaises(ValueError):
                self.service.update_privacy_settings({"INCOMING_CALL": "CONTACTS"})
        self.assertFalse(self.service.client.calls)
        self.service.update_privacy_settings({"PHONE_NUMBER_PRIVACY": "CONTACTS"})
        self.assertEqual(self.service.client.calls, [(22, {"PHONE_NUMBER_PRIVACY": "CONTACTS"})])

    def test_unknown_and_explicit_failure_preserve_settings_without_retry(self):
        before = self.service.get_privacy_settings()
        for error in (MaxWriteOutcomeUnknown("Synthetic unknown"), MaxError("Synthetic denied")):
            self.service.client.settings_error = error
            with self.assertRaises(type(error)):
                self.service.update_privacy_settings({"HIDDEN": True})
        self.assertEqual(len(self.service.client.calls), 2)
        self.assertEqual(self.service.get_privacy_settings(), before)

    def test_profile_change_during_write_discards_returned_config(self):
        def switched():
            self.service.session.my_user_id = 456
            self.service._server_metadata_owner = 456
            return {"hash": "synthetic-new", "user": {"HIDDEN": True}}
        self.service.client.settings_reply = switched
        with self.assertRaises(MaxWriteOutcomeUnknown):
            self.service.update_privacy_settings({"HIDDEN": True})
        self.assertFalse(self.service.store.load_config(123)["user"]["HIDDEN"])

    def test_local_cache_failure_after_ack_remains_unknown_without_retry(self):
        self.service.client.settings_reply = {"hash": "synthetic-updated", "user": {"HIDDEN": True}}
        with patch.object(self.service.store, "merge_config", side_effect=OSError("Synthetic local failure")):
            with self.assertRaises(MaxWriteOutcomeUnknown):
                self.service.update_privacy_settings({"HIDDEN": True})
        self.assertEqual(self.service.client.calls, [(22, {"HIDDEN": True})])
        self.assertFalse(self.service.get_privacy_settings()["HIDDEN"])

    def test_ui_callback_failure_does_not_invalidate_actual_saved_ack(self):
        self.service.client.settings_reply = {"hash": "synthetic-updated", "user": {"HIDDEN": True}}
        self.service.on_config_changed = lambda: (_ for _ in ()).throw(RuntimeError("Synthetic UI failure"))
        result = self.service.update_privacy_settings({"HIDDEN": True})
        self.assertFalse(result["pending"])
        self.assertTrue(result["settings"]["HIDDEN"])

    def test_sessions_have_no_manufactured_id_current_or_empty_fallback(self):
        self.service.client.sessions_reply = response(96, {"sessions": [{"time": STAMP, "client": "Synthetic client"}]})
        row = self.service.list_sessions()[0]
        self.assertEqual(row["time"], STAMP)
        self.assertNotIn("id", row)
        self.assertNotIn("current", row)
        self.service.client.sessions_reply = response(96, {"sessions": []})
        self.assertEqual(self.service.list_sessions(), [])
        for data in ({}, {"sessions": [{"time": True, "client": "Synthetic"}]},
                     {"sessions": [{"time": STAMP, "client": "Synthetic", "current": "false"}]}):
            self.service.client.sessions_reply = response(96, data)
            with self.assertRaises(ValueError):
                self.service.list_sessions()

    def test_close_others_keeps_saved_credential_and_does_not_return_ack_token(self):
        self.service.session.set_token("synthetic-original-login", "web")
        before = self.paths.token_file.read_bytes()
        self.assertIs(self.service.terminate_other_sessions(), True)
        self.assertEqual(self.service.client.calls, [(97, {})])
        self.assertEqual(self.paths.token_file.read_bytes(), before)
        self.assertEqual(self.service.session.token, "synthetic-original-login")

    def test_server_close_gate_and_bad_ack_do_not_fake_revocation(self):
        self.service._remember_config({"server": {"sessions-end-disabled": True}})
        with self.assertRaises(ValueError):
            self.service.terminate_other_sessions()
        self.assertFalse(self.service.client.calls)
        self.service._remember_config({"server": {"sessions-end-disabled": False}})
        self.service.client.close_reply = {}
        with self.assertRaises(MaxWriteOutcomeUnknown):
            self.service.terminate_other_sessions()
        self.assertEqual(self.service.client.calls, [(97, {})])


if __name__ == "__main__":
    unittest.main()
