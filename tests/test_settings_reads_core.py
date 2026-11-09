"""No-network folder metadata and own archive identity/cursor regressions."""
import sys
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from maxclient.core.service import MaxService
from maxclient.protocol.client import MaxError


STAMP = 1_700_000_000_000


def story(**overrides):
    return {"id": 501, "updateTime": STAMP, "owner": {"type": 0, "ownerId": -42},
            "settings": 0, "time": STAMP - 1000, "expiration": 86400,
            "media": {"_type": "PHOTO", "id": 9}, "viewsCount": 7,
            "reactionsCount": 2, **overrides}


class SettingsReadCoreTests(unittest.TestCase):
    def setUp(self):
        self.service = MaxService.__new__(MaxService)
        self.service.session = SimpleNamespace(my_user_id=-42)
        self.service._metadata_lock = threading.RLock()
        self.service._server_metadata_owner = -42
        self.service.client = SimpleNamespace(
            get_folders=Mock(return_value={"folderSync": STAMP}),
            get_folder_details=Mock(return_value={"folders": []}),
            story_archive=Mock(return_value={"stories": []}),
            request=Mock(), login=Mock(), auth_request=Mock(), send_message=Mock())

    def tearDown(self):
        self.service.client.request.assert_not_called()
        self.service.client.login.assert_not_called()
        self.service.client.auth_request.assert_not_called()
        self.service.client.send_message.assert_not_called()

    def test_folder_details_resolve_once_in_actual_server_order(self):
        first = {"id": "one", "title": "First", "favorites": [12, 10], "options": [1]}
        second = {"id": "two", "title": "Second", "filters": [0, 2]}
        self.service.client.get_folders.return_value = {
            "folderSync": STAMP, "folders": [first], "foldersOrder": ["two", "one"],
            "allFilterExcludeFolders": ["two"]}
        self.service.client.get_folder_details.return_value = {"folders": [second]}
        result = self.service.get_folders()
        self.service.client.get_folders.assert_called_once_with(folder_sync=None)
        self.service.client.get_folder_details.assert_called_once_with(["two"])
        self.assertEqual([folder["id"] for folder in result["orderedFolders"]], ["two", "one"])
        self.assertEqual(result["orderedFolders"][1]["favorites"], [12, 10])
        self.assertEqual(result["missingFolderIds"], [])
        self.assertEqual(result["allFilterExcludeFolders"], ["two"])
        self.assertNotIn("unreadCount", result["folders"][0])

    def test_unavailable_folder_is_none_not_a_manufactured_empty_folder(self):
        self.service.client.get_folders.return_value = {
            "folderSync": STAMP, "foldersOrder": ["unavailable", "unavailable"]}
        result = self.service.get_folders()
        self.service.client.get_folder_details.assert_called_once_with(["unavailable"])
        self.assertEqual(result["orderedFolders"], [None, None])
        self.assertEqual(result["missingFolderIds"], ["unavailable"])
        self.assertEqual(result["folders"], [])

    def test_omitted_delta_fields_remain_omitted_without_made_up_order(self):
        self.assertEqual(self.service.get_folders(folder_sync=STAMP - 1), {"folderSync": STAMP})
        self.service.client.get_folder_details.assert_not_called()
        self.service.client.get_folders.assert_called_once_with(folder_sync=STAMP - 1)

    def test_explicit_empty_folder_order_does_not_trigger_detail_reads(self):
        self.service.client.get_folders.return_value = {"folderSync": STAMP, "foldersOrder": [], "folders": []}
        result = self.service.get_folders()
        self.assertEqual(result["orderedFolders"], [])
        self.assertEqual(result["missingFolderIds"], [])
        self.service.client.get_folder_details.assert_not_called()

    def test_malformed_folder_dto_and_unrequested_details_raise(self):
        for folder in ({"id": "x"}, {"id": "x", "title": "X", "favorites": [True]},
                       {"id": "x", "title": "X", "filters": [15]},
                       {"id": "x", "title": "X", "options": [True]}):
            self.service.client.get_folders.return_value = {"folderSync": STAMP, "folders": [folder]}
            with self.assertRaises(ValueError):
                self.service.get_folders()
        self.service.client.get_folders.return_value = {"folderSync": STAMP, "foldersOrder": ["requested"]}
        self.service.client.get_folder_details.return_value = {"folders": [{"id": "other", "title": "Other"}]}
        with self.assertRaisesRegex(ValueError, "другой папке"):
            self.service.get_folders()

    def test_folder_read_failures_propagate_instead_of_becoming_empty(self):
        self.service.client.get_folders.side_effect = MaxError("Synthetic denied read")
        with self.assertRaises(MaxError):
            self.service.get_folders()
        self.service.client.get_folders.side_effect = None
        self.service.client.get_folders.return_value = {"folderSync": STAMP, "foldersOrder": ["missing"]}
        self.service.client.get_folder_details.side_effect = MaxError("Synthetic denied details")
        with self.assertRaises(MaxError):
            self.service.get_folders()
        self.assertEqual(self.service.client.get_folder_details.call_count, 1)

    def test_own_archive_uses_confirmed_signed_profile_and_supplied_timestamp(self):
        raw = story(expiration=123, layers=[], reaction={"reactionType": 0, "id": "❤"})
        self.service.client.story_archive.return_value = {"stories": [raw], "marker": STAMP - 2}
        result = self.service.list_own_stories(marker=STAMP, count=20)
        self.service.client.story_archive.assert_called_once_with(-42, owner_type=0, marker=STAMP, count=20)
        self.assertEqual(result, {"stories": [raw], "marker": STAMP - 2})
        self.assertEqual(result["stories"][0]["expiration"], 123)
        self.assertNotIn("expiresAt", result["stories"][0])
        result["stories"][0]["media"]["id"] = 100
        self.assertEqual(raw["media"]["id"], 9)

    def test_archive_empty_success_preserves_absent_and_zero_marker(self):
        self.assertEqual(self.service.list_own_stories(), {"stories": []})
        self.service.client.story_archive.return_value = {"stories": [], "marker": 0}
        self.assertEqual(self.service.list_own_stories(), {"stories": [], "marker": 0})

    def test_unconfirmed_profile_stops_all_new_reads_before_rpc(self):
        self.service._server_metadata_owner = None
        for method in (self.service.get_folders, self.service.list_own_stories):
            with self.assertRaises(ValueError):
                method()
        self.service.client.get_folders.assert_not_called()
        self.service.client.story_archive.assert_not_called()

    def test_other_owner_invalid_counters_and_marker_are_not_displayed_as_own(self):
        for raw in (story(owner={"type": 1, "ownerId": -42}), story(owner={"type": 0, "ownerId": 99}),
                    story(time=True), story(viewsCount="7"), story(media={"_type": "unverified"})):
            self.service.client.story_archive.return_value = {"stories": [raw]}
            with self.assertRaises(ValueError):
                self.service.list_own_stories()
        self.service.client.story_archive.return_value = {"stories": [], "marker": "0"}
        with self.assertRaises(ValueError):
            self.service.list_own_stories()

    def test_archive_rpc_failure_is_not_reported_as_an_empty_archive(self):
        self.service.client.story_archive.side_effect = MaxError("Synthetic denied archive")
        with self.assertRaises(MaxError):
            self.service.list_own_stories()
        self.assertEqual(self.service.client.story_archive.call_count, 1)

    def test_profile_switch_during_read_discards_result(self):
        def changed_profile(**_kwargs):
            self.service.session.my_user_id = 77
            self.service._server_metadata_owner = 77
            return {"folderSync": STAMP}
        self.service.client.get_folders.side_effect = changed_profile
        with self.assertRaisesRegex(ValueError, "Аккаунт изменился"):
            self.service.get_folders()


if __name__ == "__main__":
    unittest.main()
