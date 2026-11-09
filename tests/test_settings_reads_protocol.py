"""Synthetic contracts for public read-only folder/archive operations."""
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from maxclient.protocol.client import MaxClient, MaxError, MaxFrame, MaxTimeout


STAMP = 1_700_000_000_000


class SettingsReadProtocolTests(unittest.TestCase):
    def client(self, opcode, response):
        client = MaxClient(auto_reconnect=False)
        client.request = Mock(return_value=MaxFrame(1, 7, opcode, b"", response))
        return client

    def test_folder_sync_is_omitted_only_when_not_supplied(self):
        client = self.client(272, {"folderSync": STAMP})
        self.assertEqual(client.get_folders(), {"folderSync": STAMP})
        client.request.assert_called_once_with(272, {}, timeout=30)
        client.request.reset_mock()
        client.get_folders(folder_sync=0)
        client.request.assert_called_once_with(272, {"folderSync": 0}, timeout=30)

    def test_folder_order_empty_and_omitted_are_not_conflated(self):
        response = {"folderSync": STAMP, "foldersOrder": [], "allFilterExcludeFolders": ["synthetic"]}
        client = self.client(272, response)
        self.assertEqual(client.get_folders(), response)
        self.assertNotIn("folders", client.get_folders())

    def test_folder_detail_request_uses_only_supplied_string_ids(self):
        response = {"folders": [{"id": "synthetic", "title": "Synthetic folder"}]}
        client = self.client(273, response)
        self.assertEqual(client.get_folder_details(["synthetic"]), response)
        client.request.assert_called_once_with(273, {"folderIds": ["synthetic"]}, timeout=30)

    def test_invalid_folder_input_does_not_make_any_rpc(self):
        client = self.client(272, {"folderSync": STAMP})
        for value in (True, "0", 2 ** 63):
            with self.assertRaises(ValueError):
                client.get_folders(value)
        for value in ([], [True], [1], [""], ["x"] * 1001, "synthetic"):
            with self.assertRaises(ValueError):
                client.get_folder_details(value)
        client.request.assert_not_called()

    def test_malformed_folder_response_is_not_an_empty_success(self):
        for response in ({}, {"folderSync": True}, {"folderSync": STAMP, "folders": None},
                         {"folderSync": STAMP, "foldersOrder": [7]}):
            client = self.client(272, response)
            with self.assertRaises(MaxError):
                client.get_folders()
            self.assertEqual(client.request.call_count, 1)
        client = self.client(273, {})
        with self.assertRaises(MaxError):
            client.get_folder_details(["synthetic"])

    def test_own_archive_initial_and_next_page_use_timestamp_markers(self):
        client = self.client(219, {"stories": [], "marker": STAMP})
        result = client.story_archive(-42)
        client.request.assert_called_once_with(
            219, {"owner": {"type": 0, "ownerId": -42}, "marker": 0, "count": 15}, timeout=30)
        client.request.reset_mock()
        client.story_archive(-42, marker=result["marker"], count=30)
        client.request.assert_called_once_with(
            219, {"owner": {"type": 0, "ownerId": -42}, "marker": STAMP, "count": 30}, timeout=30)

    def test_invalid_archive_parameters_do_not_make_rpc(self):
        client = self.client(219, {"stories": []})
        for args in ({"owner_type": True}, {"owner_type": 3}, {"marker": "0"},
                     {"marker": True}, {"count": 0}, {"count": 101}):
            with self.assertRaises(ValueError):
                client.story_archive(42, **args)
        with self.assertRaises(ValueError):
            client.story_archive(True)
        client.request.assert_not_called()

    def test_archive_malformed_nack_and_timeout_propagate_without_retry(self):
        for response in ({}, {"stories": None}, {"stories": [1]}, {"stories": [], "marker": "0"}):
            client = self.client(219, response)
            with self.assertRaises(MaxError):
                client.story_archive(42)
            self.assertEqual(client.request.call_count, 1)
        client = self.client(219, {})
        client.request.return_value = MaxFrame(3, 7, 219, b"", {"error": "synthetic.denied"})
        with self.assertRaisesRegex(MaxError, "synthetic.denied"):
            client.story_archive(42)
        self.assertEqual(client.request.call_count, 1)
        client.request.reset_mock(side_effect=True)
        client.request.side_effect = MaxTimeout("Synthetic read timeout")
        with self.assertRaises(MaxTimeout):
            client.story_archive(42)
        self.assertEqual(client.request.call_count, 1)


if __name__ == "__main__":
    unittest.main()
