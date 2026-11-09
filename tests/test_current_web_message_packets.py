"""Offline golden contracts from public DPMqYNCb.js, SHA256 5c95e802...79ea2ed.

Source schema/caller locations are retained in research/CURRENT_WEB_PROTOCOL_EVIDENCE.md.
No message, edit, delete, forward, reaction or authentication is sent by tests.
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from maxclient.protocol.client import MaxClient, MaxError, MaxFrame, MaxTimeout, MaxWriteOutcomeUnknown


class CurrentWebMessageContracts(unittest.TestCase):
    def client(self, opcode=64, response=None):
        client = MaxClient(auto_reconnect=False)
        if response is None:
            response = {'chatId': 42, 'message': {'id': 81, 'text': 'synthetic'}}
        client.request = Mock(return_value=MaxFrame(1, 7, opcode, b'', response))
        return client

    def payload(self, client, opcode):
        self.assertEqual(client.request.call_count, 1)
        args = client.request.call_args.args
        self.assertEqual(args[0], opcode)
        return args[1]

    def test_plain_send_keeps_compatibility_without_unverified_wire_fields(self):
        client = self.client()
        response = client.send_message(42, 'synthetic text')
        payload = self.payload(client, 64)
        self.assertEqual(set(payload), {'chatId', 'message', 'notify'})
        self.assertEqual(payload['chatId'], 42)
        self.assertIs(payload['notify'], True)
        self.assertEqual(set(payload['message']), {'cid', 'text'})
        self.assertEqual(payload['message']['text'], 'synthetic text')
        self.assertEqual(response['message']['id'], 81)

    def test_reply_uses_nested_link_and_exact_known_message(self):
        client = self.client()
        client.reply_to_message(42, 70, 'reply text', post_id=5)
        payload = self.payload(client, 64)
        self.assertEqual(payload['message']['link'], {'type': 'REPLY', 'messageId': 70})
        self.assertEqual(payload['chatId'], 42)
        self.assertEqual(payload['postId'], 5)
        self.assertNotIn('replyTo', payload)
        self.assertNotIn('replyTo', payload['message'])

    def test_forward_keeps_source_and_destination_separate(self):
        client = self.client()
        client.forward_message(12, 70, 42, source_post_id=3, post_id=5)
        payload = self.payload(client, 64)
        self.assertEqual(payload['chatId'], 42)
        self.assertEqual(payload['postId'], 5)
        self.assertEqual(payload['message']['link'], {'type': 'FORWARD', 'chatId': 12,
                                                     'messageId': 70, 'postId': 3})
        self.assertEqual(payload['message']['text'], '')

    def test_forward_peer_target_does_not_rewrite_source_chat(self):
        client = self.client()
        client.forward_message(12, 70, 42, peer_user_id=99)
        payload = self.payload(client, 64)
        self.assertEqual(payload['userId'], 99)
        self.assertNotIn('chatId', payload)
        self.assertEqual(payload['message']['link']['chatId'], 12)

    def test_edit_uses_attachments_only_when_explicitly_changed(self):
        client = self.client(67, {'message': {'id': 81, 'text': 'edited'}})
        self.assertEqual(client.edit_message(42, 81, 'edited')['message']['text'], 'edited')
        self.assertEqual(self.payload(client, 67), {'chatId': 42, 'messageId': 81, 'text': 'edited'})
        client.request.reset_mock()
        client.edit_message(42, 81, 'edited', attachments=[], elements=[], post_id=5)
        self.assertEqual(self.payload(client, 67), {'chatId': 42, 'messageId': 81, 'text': 'edited',
                                                   'attachments': [], 'elements': [], 'postId': 5})

    def test_delete_for_me_and_for_all_follow_primary_inverse_flag(self):
        response = {'chatId': 42, 'messageIds': [81]}
        client = self.client(66, response)
        self.assertEqual(client.delete_messages(42, [81, 82]), response)
        self.assertEqual(self.payload(client, 66), {'chatId': 42, 'messageIds': [81, 82], 'forMe': True})
        client.request.reset_mock()
        client.delete_messages(42, [81], for_all=True, post_id=5)
        self.assertEqual(self.payload(client, 66), {'chatId': 42, 'messageIds': [81], 'forMe': False, 'postId': 5})

    def test_contextual_send_does_not_retry_finder_rejection(self):
        for call in [lambda client: client.reply_to_message(42, 70, 'reply'),
                     lambda client: client.forward_message(12, 70, 42)]:
            client = self.client()
            client.request.return_value = MaxFrame(3, 7, 64, b'common.finder[User,999]', {'error': 'not.found'})
            with self.assertRaises(MaxError):
                call(client)
            self.assertEqual(client.request.call_count, 1)

    def test_write_timeout_is_unknown_and_never_retried(self):
        for call in [lambda client: client.send_message(42, 'plain'),
                     lambda client: client.reply_to_message(42, 70, 'reply'),
                     lambda client: client.forward_message(12, 70, 42),
                     lambda client: client.edit_message(42, 81, 'edited'),
                     lambda client: client.delete_messages(42, [81])]:
            client = self.client()
            client.request.side_effect = MaxTimeout('private synthetic exception detail')
            with self.assertRaises(MaxWriteOutcomeUnknown) as caught:
                call(client)
            self.assertNotIn('private synthetic exception detail', str(caught.exception))
            self.assertEqual(client.request.call_count, 1)

    def test_incomplete_write_ack_is_unknown_not_a_fabricated_success(self):
        calls = [(64, {}, lambda client: client.send_message(42, 'plain')),
                 (67, {'message': {}}, lambda client: client.edit_message(42, 81, 'edited')),
                 (67, {'message': {'id': 82}}, lambda client: client.edit_message(42, 81, 'edited')),
                 (66, {'chatId': 43, 'messageIds': [81]}, lambda client: client.delete_messages(42, [81]))]
        for opcode, reply, call in calls:
            client = self.client(opcode, reply)
            with self.assertRaises(MaxWriteOutcomeUnknown):
                call(client)
            self.assertEqual(client.request.call_count, 1)

    def test_reaction_write_uses_literal_unicode_and_preserves_unknown_outcome(self):
        client = self.client(178, {})
        client.set_reaction(42, 81, '❤️')
        self.assertEqual(self.payload(client, 178)['reaction'], {'reactionType': 'EMOJI', 'id': '❤️'})
        for call in [lambda instance: instance.set_reaction(42, 81, '👍'),
                     lambda instance: instance.remove_reaction(42, 81)]:
            instance = self.client(178, {})
            instance.request.side_effect = MaxTimeout('private synthetic exception detail')
            with self.assertRaises(MaxWriteOutcomeUnknown):
                call(instance)
            self.assertEqual(instance.request.call_count, 1)
            instance.request.side_effect = None
            instance.request.return_value = MaxFrame(1, 7, 178, b'', None)
            with self.assertRaises(MaxWriteOutcomeUnknown):
                call(instance)

    def test_client_cids_are_distinct_even_with_equal_wall_clock(self):
        client = self.client()
        with patch('maxclient.protocol.client.time.time', return_value=12345):
            client.send_message(42, 'one')
            client.send_message(42, 'two')
        first, second = [call.args[1]['message']['cid'] for call in client.request.call_args_list]
        self.assertGreater(second, first)

    def test_invalid_link_combinations_stop_before_any_request(self):
        client = self.client()
        with self.assertRaises(ValueError):
            client.send_message(42, 'text', reply_to_message_id=70,
                                forward_source_chat_id=12, forward_source_message_id=70)
        with self.assertRaises(ValueError):
            client.send_message(42, 'text', forward_source_chat_id=12)
        with self.assertRaises(ValueError):
            client.delete_messages(42, [81], for_all='yes')
        client.request.assert_not_called()

    def test_reaction_reads_use_structured_map_and_user_request_without_post_id(self):
        response = {'messagesReactions': {'81': {'totalCount': 2}}}
        client = self.client(180, response)
        self.assertEqual(client.get_reactions(42, [81]), response)
        self.assertEqual(self.payload(client, 180), {'chatId': 42, 'messageIds': [81]})
        client.request.reset_mock()
        client.request.return_value = MaxFrame(1, 8, 181, b'', {'reactions': []})
        self.assertEqual(client.reaction_users(42, 81, reaction='👍', marker=7), {'reactions': []})
        self.assertEqual(self.payload(client, 181), {'chatId': 42, 'messageId': 81, 'reaction': '👍',
                                                    'marker': 7, 'count': 100})


if __name__ == '__main__':
    unittest.main()
