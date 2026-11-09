"""Synthetic wire fixtures; no application state or network."""
import copy
import unittest

from maxclient.protocol.conversation_create_contracts import (
    conversation_create_values, conversation_creation_limits, conversation_create_payload,
    conversation_create_reply, conversation_create_readback, conversation_description_reply,
)


def chat_record(kind='group', description=''):
    return {'id': -701, 'type': 'CHAT' if kind == 'group' else 'CHANNEL', 'status': 'ACTIVE',
            'owner': 101, 'participants': {'101': 1}, 'lastEventTime': 10, 'created': 10,
            'title': 'Synthetic title', 'description': description}


def create_reply(kind='group', cid=700):
    return {'chatId': -701, 'chat': chat_record(kind),
            'message': {'id': 800, 'time': 10, 'type': 'USER', 'cid': cid}}


class ConversationCreateContractTests(unittest.TestCase):
    def test_exact_control_new_group_with_selected_ids_and_no_target(self):
        values = conversation_create_values('group', ' Synthetic title ', [202, 303])
        self.assertEqual(conversation_create_payload(values, 700), {'message': {'cid': 700,
            'attaches': [{'_type': 'CONTROL', 'event': 'new', 'chatType': 'CHAT',
                         'title': 'Synthetic title', 'userIds': [202, 303]}]}, 'notify': True})

    def test_channel_never_invites_and_description_is_not_part_of64(self):
        values = conversation_create_values('channel', 'Synthetic title', description=' Info ')
        payload = conversation_create_payload(values, 700)
        self.assertEqual(values['description'], 'Info')
        self.assertEqual(payload['message']['attaches'][0]['chatType'], 'CHANNEL')
        self.assertEqual(payload['message']['attaches'][0]['userIds'], [])
        self.assertNotIn('description', payload)
        with self.assertRaises(ValueError):
            conversation_create_values('channel', 'title', [202])

    def test_empty_group_is_allowed_by_official_next_step(self):
        self.assertEqual(conversation_create_values('group', 'title')['member_ids'], [])

    def test_title_and_description_utf16_lengths(self):
        self.assertEqual(len(conversation_create_values('group', '😀' * 100)['title']), 100)
        for title in ('', '  ', 'a' * 201, '😀' * 101):
            with self.subTest(title_length=len(title)), self.assertRaises(ValueError):
                conversation_create_values('group', title)
        with self.assertRaises(ValueError):
            conversation_create_values('channel', 'title', description='😀' * 201)

    def test_supplied_limits_and_member_minimum_bound(self):
        limits = conversation_creation_limits({'max-cname-length': 5, 'max-participants': 2})
        self.assertEqual(limits['max-description-length'], 400)
        conversation_create_values('group', 'title', [202], limits=limits)
        for title, ids in [('longer', []), ('title', [202, 303])]:
            with self.assertRaises(ValueError):
                conversation_create_values('group', title, ids, limits=limits)

    def test_invalid_members_and_limits_fail_closed(self):
        for ids in ([True], [0], [-1], [2, 2], ['2'], {2}, [2 ** 64]):
            with self.subTest(ids=ids), self.assertRaises(ValueError):
                conversation_create_values('group', 'title', ids)
        for value in (True, 0, -1, '200', 2.5):
            with self.assertRaises(ValueError):
                conversation_creation_limits({'max-cname-length': value})

    def test_group_rejects_channel_description(self):
        with self.assertRaises(ValueError):
            conversation_create_values('group', 'title', description='description')

    def test_creation_requires_real_typed_chat_and_matching_ack_id_cid(self):
        values = conversation_create_values('group', 'Synthetic title')
        self.assertEqual(conversation_create_reply(create_reply(), values, 101, 700)['id'], -701)
        for mutate in [lambda d: d.pop('chat'), lambda d: d.update(chatId=-702),
                       lambda d: d['message'].update(cid=701), lambda d: d['chat'].update(owner=202),
                       lambda d: d['chat'].update(type='CHANNEL'), lambda d: d['chat'].update(id=0),
                       lambda d: d['chat'].update(title='Other'), lambda d: d['chat'].update(status='LEFT'),
                       lambda d: d.pop('message')]:
            data = create_reply(); mutate(data)
            with self.assertRaises(ValueError):
                conversation_create_reply(data, values, 101, 700)

    def test_readback_requires_unique_actual_id_not_a_title_match(self):
        values = conversation_create_values('group', 'Synthetic title')
        for rows in ([], [chat_record(), chat_record()], [{**chat_record(), 'id': -702}]):
            with self.assertRaises(ValueError):
                conversation_create_readback({'chats': rows}, values, 101, -701)

    def test_description_requires_confirmed_actual_channel(self):
        values = conversation_create_values('channel', 'Synthetic title', description='Info')
        raw = chat_record('channel', 'Info')
        self.assertEqual(conversation_description_reply({'chat': raw}, values, 101, -701), raw)
        self.assertEqual(conversation_create_readback({'chats': [raw]}, values, 101, -701, description=True), raw)
        with self.assertRaises(ValueError):
            conversation_description_reply({'chat': chat_record('channel')}, values, 101, -701)

    def test_parser_copy_does_not_mutate_server_input(self):
        source = create_reply(); before = copy.deepcopy(source)
        raw = conversation_create_reply(source, conversation_create_values('group', 'Synthetic title'), 101, 700)
        raw['participants']['202'] = 3
        self.assertEqual(source, before)


if __name__ == '__main__':
    unittest.main()
