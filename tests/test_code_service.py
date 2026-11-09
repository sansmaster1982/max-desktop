"""Offline checks of the bounded, read-only verification-message viewer."""

import copy
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from maxclient.core.config import AppPaths, Session, Settings
from maxclient.core.models import Message
from maxclient.core.service import MaxService, _extract_chats
from maxclient.protocol.client import MaxFrame
from test_token_service import FakeClient, confirm_fake_transport, login_frame


STAMP = 1_700_000_000_000


def code_contact(name="Коды подтверждения", flags=None, **extra):
    return {"id": 77, "names": [{"name": name}],
            "options": ["SERVICE_ACCOUNT", "OFFICIAL", "BOT"] if flags is None else flags,
            **extra}


def code_chat(**extra):
    return {"id": 42, "type": "DIALOG", "participants": {123: STAMP, 77: STAMP},
            "options": {"SERVICE_CHAT": True},
            "lastMessage": {"id": 101, "sender": 77, "time": STAMP,
                            "text": "Synthetic visible verification message"}, **extra}


class CodeClient(FakeClient):
    def __init__(self, **options):
        super().__init__(**options)
        self.is_connected = True
        self.read_calls = []
        self.chat_response = {"chats": []}
        self.contact_response = {"contacts": []}
        self.chat_error = self.contact_error = None

    def chat_info(self, ids, *, expected_session=None):
        assert expected_session is not None and expected_session is self.authenticated_session
        self.read_calls.append(("CHAT_INFO", ids))
        if self.chat_error:
            raise self.chat_error
        return copy.deepcopy(self.chat_response)

    def contact_info(self, ids, *, expected_session=None):
        assert expected_session is not None and expected_session is self.authenticated_session
        self.read_calls.append(("CONTACT_INFO", ids))
        if self.contact_error:
            raise self.contact_error
        return copy.deepcopy(self.contact_response)

    def chat_history(self, *args, **kwargs):
        raise AssertionError("The code viewer must not guess history pagination")

    def start_auth_sms(self, *args, **kwargs):
        raise AssertionError("The code viewer must not request authentication")

    def send_message(self, *args, **kwargs):
        raise AssertionError("The code viewer must not send a message")

    def set_peer(self, *args):
        pass


class CodeServiceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="max-code-service-")
        self.environment = patch.dict(os.environ, {"MAX_DESKTOP_DATA_DIR": self.temporary.name})
        self.environment.start()
        paths = AppPaths()
        with patch("maxclient.core.service.MaxClient", CodeClient):
            self.service = MaxService(paths, Settings.load(paths), Session.load(paths))

    def tearDown(self):
        self.assertFalse(self.service.client.login_calls)
        self.assertFalse(self.service.client.connect_calls)
        self.service.shutdown()
        self.environment.stop()
        self.temporary.cleanup()

    def seed(self, chat=None, contacts=None):
        chat = code_chat() if chat is None else chat
        contacts = [code_contact()] if contacts is None else contacts
        self.service.client.chat_response = {"chats": [chat]}
        self.service.client.contact_response = {"contacts": contacts}
        self.service._finish_login(login_frame(chats=[chat], contacts=contacts), prev_uid=None)
        confirm_fake_transport(self.service)

    def test_participant_map_keys_and_confirmed_profile_determine_peer(self):
        self.service.session.my_user_id = 999
        self.seed()
        self.assertEqual(self.service.store.get_chat(42).peer_user_id, 77)
        self.assertEqual(self.service._server_metadata_owner, 123)
        listed = _extract_chats({"chats": [code_chat(participants=[{"userId": 123}, {"id": 77}])]}, 123)
        self.assertEqual(listed[0].peer_user_id, 77)
        self.assertIsNone(_extract_chats({"chats": [code_chat()]}, 999)[0].peer_user_id)

    def test_trusted_contact_shows_exact_latest_text_and_timestamp_without_auth(self):
        self.seed()
        result = self.service.verification_code_messages()
        self.assertEqual(result, {"status": "available", "chat_id": 42, "messages": [
            {"id": 101, "time_ms": STAMP, "text": "Synthetic visible verification message"}]})
        self.assertEqual(self.service.client.read_calls, [("CHAT_INFO", [42])])
        self.assertEqual(self.service.store.get_chat(42).last_time_ms, STAMP)
        self.assertFalse(self.service.store.messages(42))
        self.service.verification_code_messages()
        self.assertFalse(self.service.store.messages(42))

    def test_name_only_service_only_and_security_contact_are_rejected(self):
        examples = [
            code_contact(flags=[]),
            code_contact(flags=["SERVICE_ACCOUNT"]),
            code_contact(name="Безопасность", username="verificationcodes_bot"),
            code_contact(name="Other bot", link="https://max.ru.evil.example/verificationcodes_bot"),
        ]
        for contact in examples:
            with self.subTest(contact_case=examples.index(contact)):
                self.seed(chat=code_chat(title="Коды подтверждения"), contacts=[contact])
                self.assertEqual(self.service.verification_code_messages(),
                                 {"status": "chat_not_available", "messages": []})

    def test_code_bot_contact_without_service_chat_marker_is_rejected(self):
        self.seed(chat=code_chat(options={}))
        self.assertEqual(self.service.verification_code_messages()["status"], "chat_not_available")
        self.assertEqual(self.service.client.read_calls, [("CHAT_INFO", [42])])

    def test_custom_address_book_alias_cannot_turn_security_bot_into_code_bot(self):
        contact = code_contact()
        contact["names"] = [{"type": "CUSTOM", "name": "Коды подтверждения"},
                            {"type": "ONEME", "name": "Безопасность"}]
        self.seed(contacts=[contact])
        self.assertEqual(self.service.verification_code_messages()["status"], "chat_not_available")

    def test_canonical_code_bot_remains_valid_with_a_different_custom_alias(self):
        contact = code_contact()
        contact["names"] = [{"type": "CUSTOM", "name": "Local alias"},
                            {"type": "ONEME", "name": "Коды подтверждения"}]
        self.seed(contacts=[contact])
        self.assertEqual(self.service.verification_code_messages()["status"], "available")

    def test_only_missing_service_peer_metadata_is_looked_up(self):
        self.seed(contacts=[])
        self.service.client.contact_response = {"contacts": [code_contact()]}
        self.service.client.chat_response["chats"].append(code_chat(
            id=88, participants={123: STAMP, 99: STAMP}, options={}))
        self.assertEqual(self.service.verification_code_messages()["status"], "available")
        self.assertEqual(self.service.client.read_calls,
                         [("CHAT_INFO", [42]), ("CONTACT_INFO", [77])])

    def test_exact_bot_username_with_official_markers_is_accepted(self):
        self.seed(contacts=[code_contact(name="Localized official bot", username="verificationcodes_bot")])
        self.assertEqual(self.service.verification_code_messages()["status"], "available")

    def test_current_numeric_contact_flags_are_accepted_and_show_verified_badge(self):
        contact = code_contact()
        contact.pop("options")
        contact["flags"] = 11  # OFFICIAL=1, BOT=2, SERVICE_ACCOUNT=8.
        self.seed(contacts=[contact])
        self.assertEqual(self.service.verification_code_messages()["status"], "available")
        stored = next(item for item in self.service.store.list_contacts() if item.id == 77)
        self.assertTrue(stored.is_verified)

    def test_numeric_mask_is_not_completed_by_legacy_options_or_coerced(self):
        for flags in (0, 1, 2, 3, 8, 9, 10, -1, True, False, None, "11", 11.0, 2 ** 31, [], {}):
            with self.subTest(flags=flags):
                contact = code_contact()  # Contradictory legacy positive options.
                contact["flags"] = flags
                self.seed(contacts=[contact])
                self.assertEqual(self.service.verification_code_messages()["status"], "chat_not_available")

    def test_flags_only_update_can_clear_the_persisted_official_badge(self):
        self.seed()
        contact = code_contact()
        contact.pop("options")
        contact["flags"] = 0
        self.service._remember_contact_info({"contacts": [contact]})
        stored = next(item for item in self.service.store.list_contacts() if item.id == 77)
        self.assertFalse(stored.is_verified)

    def test_numeric_markers_still_require_canonical_identity_and_service_chat(self):
        contact = code_contact(name="Безопасность", username="verificationcodes_bot")
        contact.pop("options")
        contact["flags"] = 11
        self.seed(contacts=[contact])
        self.assertEqual(self.service.verification_code_messages()["status"], "chat_not_available")
        contact["names"] = [{"type": "ONEME", "name": "Коды подтверждения"}]
        self.seed(chat=code_chat(options={}, flags=11), contacts=[contact])
        self.assertEqual(self.service.verification_code_messages()["status"], "chat_not_available")
        self.seed(chat=code_chat(flags=0), contacts=[contact])
        self.assertEqual(self.service.verification_code_messages()["status"], "available")

    def test_missing_or_invalid_timestamp_is_not_manufactured(self):
        for timestamp in (None, True, '1700000000000', float('nan'), float('inf')):
            with self.subTest(timestamp=timestamp):
                message = {"id": 101, "sender": 77, "text": "Visible synthetic text"}
                if timestamp is not None:
                    message["time"] = timestamp
                self.seed(chat=code_chat(lastMessage=message))
                self.assertEqual(self.service.verification_code_messages()["status"], "empty")
                self.assertFalse(self.service.store.messages(42))

    def test_actual_finite_timestamp_has_no_guessed_epoch_window_or_rounding(self):
        for timestamp in (0, 1700000000, STAMP * 1000, STAMP + 0.25):
            with self.subTest(timestamp=timestamp):
                self.seed(chat=code_chat(lastMessage={"id": 101, "time": timestamp,
                                                     "text": "Visible synthetic text"}))
                result = self.service.verification_code_messages()
                self.assertEqual(result['status'], 'available')
                self.assertEqual(result['messages'][0]['time_ms'], timestamp)
                self.assertFalse(self.service.store.messages(42))

    def test_hidden_fields_are_not_decoded_and_redacted_latest_is_not_recovered_from_cache(self):
        self.seed(chat=code_chat(lastMessage={"id": 101, "time": STAMP,
                                            "secret": "synthetic-secret", "code": "synthetic-code"}))
        self.service.store.insert_message(Message(chat_id=42, id=101, sender=77,
                                                  text="Earlier cached text", time_ms=STAMP))
        self.assertEqual(self.service.verification_code_messages(),
                         {"status": "empty", "chat_id": 42, "messages": []})

    def test_general_cached_messages_are_not_treated_as_current_server_codes(self):
        self.seed()
        self.service.store.insert_message(Message(chat_id=42, id=100, sender=77,
                                                  text="Earlier visible text", time_ms=STAMP - 1000))
        self.service.store.insert_message(Message(chat_id=42, id=99, sender=77,
                                                  text="Undated text", time_ms=0))
        self.service.store.insert_message(Message(chat_id=42, id=98, sender=123,
                                                  text="Outgoing text", time_ms=STAMP, outgoing=True))
        result = self.service.verification_code_messages()
        self.assertEqual([item["id"] for item in result["messages"]], [101])

    def test_rpc_errors_propagate_instead_of_becoming_empty_results(self):
        self.seed()
        self.service.client.chat_error = ConnectionError("Synthetic refresh failure")
        with self.assertRaises(ConnectionError):
            self.service.verification_code_messages()
        self.service.client.chat_error = None
        self.service.client.chat_response = {}
        with self.assertRaises(ValueError):
            self.service.verification_code_messages()

    def test_unavailable_contact_metadata_error_propagates(self):
        self.seed(contacts=[])
        self.service.client.contact_error = ConnectionError("Synthetic contact failure")
        with self.assertRaises(ConnectionError):
            self.service.verification_code_messages()

    def test_fresh_empty_chat_info_does_not_reuse_stale_verified_identity(self):
        self.seed()
        self.service.client.chat_response = {"chats": []}
        self.assertEqual(self.service.verification_code_messages()["status"], "chat_not_available")

    def test_newer_hidden_message_is_not_replaced_with_an_older_visible_code_chat(self):
        older = code_chat()
        newer = code_chat(id=43, participants={123: STAMP, 88: STAMP},
                          lastMessage={"id": 201, "time": STAMP + 1000, "secret": "synthetic"})
        contacts = [code_contact(), code_contact(id=88)]
        self.service._finish_login(login_frame(chats=[older, newer], contacts=contacts), prev_uid=None)
        confirm_fake_transport(self.service)
        self.service.client.chat_response = {"chats": [older, newer]}
        self.assertEqual(self.service.verification_code_messages(),
                         {"status": "empty", "chat_id": 43, "messages": []})

    def test_actual_clipboard_button_is_literal_but_unrelated_payloads_are_ignored(self):
        message = {'id': 101, 'sender': 77, 'type': 'USER', 'time': STAMP,
                   'text': 'Synthetic visible text', 'code': 'hidden synthetic value',
                   'attaches': [{'_type': 'INLINE_KEYBOARD', 'callbackId': 'synthetic callback',
                                'keyboard': {'buttons': [[
                                    {'type': 'CLIPBOARD', 'text': 'Copy supplied text', 'payload': '000123'},
                                    {'type': 'CALLBACK', 'text': 'Trust device', 'payload': 'hidden callback'}
                                ]]}}]}
        self.seed(chat=code_chat(lastMessage=message))
        actual = self.service.verification_code_messages()['messages'][0]
        self.assertEqual(actual['text'], 'Synthetic visible text\n\nCopy supplied text\n000123')
        self.assertEqual(actual['time_ms'], STAMP)
        self.assertEqual(self.service.store.get_chat(42).last_preview, 'Synthetic visible text')
        self.assertEqual(self.service.client.read_calls, [('CHAT_INFO', [42])])

    def test_clipboard_does_not_bypass_trust_or_removed_message(self):
        message = {'id': 101, 'sender': 77, 'time': STAMP, 'status': 'REMOVED',
                   'attaches': [{'_type': 'INLINE_KEYBOARD', 'callbackId': 'synthetic', 'keyboard': {
                       'buttons': [[{'type': 'CLIPBOARD', 'text': '000123', 'payload': '000123'}]]}}]}
        self.seed(chat=code_chat(lastMessage=message))
        self.assertEqual(self.service.verification_code_messages()['status'], 'empty')
        message.pop('status')
        self.seed(chat=code_chat(lastMessage=message), contacts=[code_contact(flags=[])])
        self.assertEqual(self.service.verification_code_messages()['status'], 'chat_not_available')

    def test_late_chat_response_cannot_populate_a_switched_account(self):
        self.seed()
        def late(ids, *, expected_session):
            self.service.store.bind_owner(456)
            return {'chats': [code_chat()]}
        with patch.object(self.service.client, 'chat_info', side_effect=late):
            with self.assertRaises(ValueError):
                self.service.verification_code_messages()
        self.assertEqual(self.service.store.list_chats(), [])
        self.assertEqual(self.service.store.list_contacts(), [])

    def test_late_contact_response_after_transport_replacement_is_rejected(self):
        self.seed(contacts=[])
        def late(ids, *, expected_session):
            confirm_fake_transport(self.service)
            return {'contacts': [code_contact()]}
        with patch.object(self.service.client, 'contact_info', side_effect=late):
            with self.assertRaises(ValueError):
                self.service.verification_code_messages()
        self.assertNotIn(77, self.service._server_contacts)

    def test_unconfirmed_session_never_sends_a_code_chat_query(self):
        self.seed()
        self.service.client.authenticated_session = None
        with self.assertRaises(ValueError):
            self.service.verification_code_messages()
        self.assertEqual(self.service.client.read_calls, [])


if __name__ == "__main__":
    unittest.main()
