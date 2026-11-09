"""Offline request contracts from the bounded historical protocol evidence.

These tests verify local serialization and response handling, not live MAX
acceptance, hidden schema aliases, newest-history semantics or call execution.
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from maxclient.protocol.client import MaxClient, MaxError, MaxFrame


class MessagePacketContractTests(unittest.TestCase):
    def client_with_response(self, opcode, response):
        client = MaxClient(auto_reconnect=False)
        client.request = Mock(return_value=MaxFrame(1, 7, opcode, b'', response))
        return client

    def test_read_mark_uses_known_message_and_mark_without_send_message(self):
        response = {'mark': 1793459502608, 'unread': 0, 'success': True}
        client = self.client_with_response(50, response)
        self.assertEqual(client.mark_read(42, message_id=81, mark=1793459502608), response)
        client.request.assert_called_once_with(50, {'chatId': 42, 'type': 'READ_MESSAGE',
                                                   'messageId': 81, 'mark': 1793459502608}, timeout=30)

    def test_typing_and_stop_use_separate_documented_commands(self):
        client = self.client_with_response(65, None)
        client.typing(42, True)
        client.request.assert_called_once_with(65, {'chatId': 42, 'type': 'TEXT'}, timeout=10)
        client.request.reset_mock()
        client.typing(42, False)
        client.request.assert_called_once_with(196, {'chatId': 42}, timeout=10)

    def test_read_mark_keeps_fractional_timestamp_and_expected_transport_binding(self):
        response = {'mark': 1793459502608.5, 'unread': 0}
        client = self.client_with_response(50, response)
        context = object()
        client.mark_read(-(2**63),message_id=2**63-1,mark=response['mark'],expected_session=context)
        client.request.assert_called_once_with(50,{'chatId':-(2**63),'type':'READ_MESSAGE',
            'messageId':2**63-1,'mark':response['mark']},timeout=30,expected_session=context)

    def test_reaction_set_and_remove_preserve_server_response(self):
        response = {'reactionInfo': {'totalCount': 2, 'yourReaction': '👍'}}
        client = self.client_with_response(178, response)
        self.assertEqual(client.set_reaction(42, 81, '👍', post_id=7), response)
        client.request.assert_called_once_with(178, {'chatId': 42, 'messageId': 81, 'postId': 7,
                                                     'reaction': {'reactionType': 'EMOJI', 'id': '👍'}})
        client.request.reset_mock()
        self.assertEqual(client.remove_reaction(42, 81), response)
        client.request.assert_called_once_with(179, {'chatId': 42, 'messageId': 81})

    def test_read_by_message_ids_is_scoped_to_one_chat(self):
        response = {'messages': [{'id': 81, 'text': 'synthetic'}]}
        client = self.client_with_response(71, response)
        self.assertEqual(client.get_messages(42, [81]), response)
        client.request.assert_called_once_with(71, {'chatId': 42, 'messageIds': [81]}, timeout=30)
        client.request.reset_mock()
        client.request.return_value = MaxFrame(1, 7, 180, b'', {'messagesReactions': {}})
        client.get_reactions(42, [81], post_id=7)
        client.request.assert_called_once_with(180, {'chatId': 42, 'messageIds': [81], 'postId': 7}, timeout=30)

    def test_history_window_has_explicit_cursor_both_directions_and_keeps_metadata(self):
        response = {'chat': {'id': 42, 'lastMessage': {'id': 81}}, 'messages': [{'id': 81}], 'messageIds': [81]}
        client = self.client_with_response(49, response)
        self.assertEqual(client.chat_history_window(42, from_cursor=1793459502608, backward=50), response)
        client.request.assert_called_once_with(49, {'chatId': 42, 'from': 1793459502608,
                                                    'forward': 0, 'backward': 50,
                                                    'getMessages': True, 'getChat': True}, timeout=30)

    def test_unstructured_history_is_not_reported_as_an_empty_success(self):
        client = self.client_with_response(49, None)
        with self.assertRaises(MaxError):
            client.chat_history_window(42, from_cursor=0)

    def test_primary_history_schema_allows_omitted_messages(self):
        response = {'chat': {'id': 42}, 'messageIds': []}
        client = self.client_with_response(49, response)
        self.assertEqual(client.chat_history_window(42, from_cursor=1793459502608), response)

    def test_call_history_only_reads_163_and_preserves_unknown_item_fields(self):
        response = {'callHistoryItems': [{'opaqueSyntheticField': 123}], 'callHistorySync': 77, 'reset': False}
        client = self.client_with_response(163, response)
        self.assertEqual(client.call_history(), response)
        client.request.assert_called_once_with(163, {}, timeout=30)
        client.request.reset_mock()
        self.assertEqual(client.call_history(77), response)
        client.request.assert_called_once_with(163, {'callHistorySync': 77}, timeout=30)
        client.request.return_value = MaxFrame(1, 8, 163, b'', {})
        with self.assertRaises(MaxError):
            client.call_history()

    def test_chat_list_uses_server_marker_and_channel_enum_not_fake_new_filter(self):
        response = {'chats': [], 'marker': 123}
        client = self.client_with_response(53, response)
        self.assertEqual(client.list_chats(77, 40, chat_types=['CHANNEL'], no_messages=True), response)
        client.request.assert_called_once_with(53, {'marker': 77, 'count': 40, 'chatTypes': ['CHANNEL'],
                                                    'noMessages': True}, timeout=30)
        client.request.reset_mock()
        with self.assertRaises(ValueError):
            client.list_chats(chat_types=['NEW'])
        client.request.assert_not_called()

    def test_contact_photos_returns_exact_urls_without_building_auth_urls(self):
        response = {'urls': ['https://image.invalid/synthetic.png'], 'ids': [9], 'total': 1}
        client = self.client_with_response(39, response)
        self.assertEqual(client.contact_photos(42, from_cursor=9, count=20), response)
        client.request.assert_called_once_with(39, {'contactId': 42, 'from': 9, 'count': 20}, timeout=30)

    def test_invalid_targets_empty_id_lists_and_unknown_enums_never_reach_network(self):
        client = self.client_with_response(1, {})
        calls = [lambda: client.mark_read(True), lambda: client.get_messages(42, []),
                 lambda: client.set_reaction(42, 81, 'x', reaction_type='UNVERIFIED'),
                 lambda: client.send_typing(42, 'UNVERIFIED'),
                 lambda: client.chat_history_window(42, from_cursor=0, backward=101),
                 lambda: client.chat_history_window(42, from_cursor=0, backward=0, forward=0)]
        for call in calls:
            with self.assertRaises(ValueError):
                call()
        client.request.assert_not_called()

    def test_rejected_packet_does_not_turn_into_optimistic_state(self):
        client = self.client_with_response(178, {})
        client.request.return_value = MaxFrame(3, 7, 178, b'private synthetic payload',
                                                {'error': 'synthetic.denied', 'message': 'private synthetic payload'})
        with self.assertRaisesRegex(MaxError, r'REACTION_SET: synthetic\.denied') as caught:
            client.set_reaction(42, 81, '👍')
        self.assertNotIn('private synthetic payload', str(caught.exception))


if __name__ == '__main__':
    unittest.main()
