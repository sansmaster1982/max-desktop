"""Pure destination viewerCanWrite projection against observed WEB rules."""
import copy
import unittest

from maxclient.core.conversation_write_capabilities import conversation_can_write


def raw(kind='DIALOG', **fields):
    return {'id': 303, 'type': kind, 'owner': 0, 'status': 'ACTIVE',
            'participants': {'101': 1, '202': 1}, 'lastEventTime': 200, 'created': 1, **fields}


class ConversationWriteCapabilitiesTests(unittest.TestCase):
    def can(self, chat, contact=None, owner=101):
        return conversation_can_write(chat, {'id': 202} if contact is None else contact, owner)

    def test_direct_owner_zero_and_unadded_active_recipient_are_supported(self):
        self.assertTrue(self.can(raw(), {'id': 202, 'flags': 0}))
        self.assertTrue(self.can(raw(status='REMOVED'), {'id': 202, 'status': 'REMOVED'}))
        self.assertTrue(self.can(raw(status='HIDDEN'), {'id': 202, 'status': 'HIDDEN', 'accountStatus': 0}))

    def test_direct_input_restriction_and_contact_account_gates(self):
        self.assertFalse(self.can(raw(restrictions=64)))
        self.assertTrue(self.can(raw(restrictions=32)))
        for status in (1, 2, 3, True, '0', None):
            with self.subTest(accountStatus=status):
                self.assertFalse(self.can(raw(), {'id': 202, 'accountStatus': status}))
        self.assertFalse(self.can(raw(), {'id': 202, 'status': 'BLOCKED'}))
        self.assertFalse(self.can(raw(), {'id': 202, 'status': 'UNKNOWN'}))

    def test_direct_recipient_must_match_raw_participants(self):
        for contact in ({'id': 404}, {'id': 101}, {'id': True}, {'id': -202}, {}):
            with self.subTest(contact=contact):
                self.assertFalse(self.can(raw(), contact))
        self.assertFalse(self.can(raw(participants={'101': 1, '202': 1, '404': 1})))
        self.assertTrue(self.can(raw(participants={'202': 1})))  # uM participant override

    def test_saved_has_explicit_source_override_but_never_foreign_participants(self):
        self.assertTrue(self.can(raw(id=0, participants={'101': 1}, restrictions=64), {}))
        self.assertFalse(self.can(raw(id=0), {}))
        self.assertFalse(self.can(raw(id=0, participants={}), {}))

    def test_bot_suspension_gates_and_bad_types(self):
        self.assertTrue(self.can(raw(hasBots=True)))
        self.assertFalse(self.can(raw(hasBots=True, isSuspended=True)))
        self.assertFalse(self.can(raw(hasBots=True, lastEventTime=0)))
        self.assertFalse(self.can(raw(hasBots=True, isSuspended=0)))
        self.assertFalse(self.can(raw(hasBots=1)))

    def test_group_member_requires_positive_join_or_owner_or_admin(self):
        self.assertFalse(self.can(raw('CHAT')))
        self.assertTrue(self.can(raw('CHAT', joinTime=1)))
        self.assertTrue(self.can(raw('CHAT', owner=101)))
        self.assertTrue(self.can(raw('CHAT', participants={'202': 1},
                                     adminParticipants={'101': {'id': 101}})))
        for joined in (0, None, -1, True, float('inf'), float('nan'), 10**1000):
            with self.subTest(joined=repr(joined)[:30]):
                self.assertFalse(self.can(raw('CHAT', joinTime=joined)))

    def test_group_only_active_hidden_and_no_input_restriction(self):
        for state in ('REMOVED', 'BLOCKED', 'LEFT', 'CLOSED', 'UNKNOWN'):
            with self.subTest(status=state):
                self.assertFalse(self.can(raw('CHAT', owner=101, status=state)))
        self.assertTrue(self.can(raw('CHAT', owner=101, status='HIDDEN')))
        self.assertFalse(self.can(raw('CHAT', owner=101, restrictions=64)))

    def test_channel_owner_alone_is_not_post_permission(self):
        self.assertFalse(self.can(raw('CHANNEL', owner=101)))
        for bits in (0, 2, 8, 128):
            with self.subTest(bits=bits):
                self.assertFalse(self.can(raw('CHANNEL', owner=101,
                    adminParticipants={'101': {'id': 101, 'permissions': bits}})))
        for bits in (1, 256, 257):
            with self.subTest(bits=bits):
                self.assertTrue(self.can(raw('CHANNEL', adminParticipants={'101': {'id': 101, 'permissions': bits}})))

    def test_channel_foreign_or_malformed_admin_grants_nothing(self):
        for admins in ({'202': {'id': 202, 'permissions': 256}},
                       {'101': {'id': 202, 'permissions': 256}},
                       {'101': {'id': 101, 'permissions': True}},
                       {'101': {'id': 101, 'permissions': 2**31}}, []):
            with self.subTest(admins=admins):
                self.assertFalse(self.can(raw('CHANNEL', owner=101, adminParticipants=admins)))

    def test_unknown_or_malformed_dto_is_closed_without_exception(self):
        for chat in (None, {}, raw(restrictions=True), raw(restrictions=2**31),
                     raw(participants={'0101': 1, '202': 1}), raw(participants={'101': True, '202': 1}),
                     raw('CHAT', owner=101, postId=2), raw('CHAT', owner=101, videoConversation={'type': 0})):
            with self.subTest(chat=chat):
                self.assertFalse(self.can(chat))
        for owner in (True, 0, -101, '101', 2**63):
            self.assertFalse(self.can(raw(), owner=owner))

    def test_helper_never_mutates_source_dtos(self):
        chat = raw('CHANNEL', adminParticipants={'101': {'id': 101, 'permissions': 256}})
        contact = {'id': 202}
        before = copy.deepcopy((chat, contact))
        self.assertTrue(self.can(chat, contact))
        self.assertEqual((chat, contact), before)

    def test_nested_msgpack_integer_participants_match_transport_cache(self):
        self.assertTrue(self.can(raw(participants={101: 1, 202: 1})))
        self.assertTrue(self.can(raw(id=0, participants={101: 1}), {}))

    def test_permission_projection_does_not_depend_on_message_body_or_created_time(self):
        chat = raw(lastMessage={'id': 8, 'time': 9, 'text': 'Partial push message'})
        chat.pop('created')
        self.assertTrue(self.can(chat))
        chat.update(id=0, participants={101: 1}, lastMessage={'id': 8})
        self.assertTrue(self.can(chat, {}))

    def test_nested_integer_admin_key_uses_same_permission_record(self):
        self.assertTrue(self.can(raw('CHANNEL', participants={101: 1, 202: 1},
                                    adminParticipants={101: {'id': 101, 'permissions': 256}})))

    def test_duplicate_numeric_key_spellings_are_ambiguous_not_permission(self):
        self.assertFalse(self.can(raw(participants={101: 1, '101': 2, 202: 1})))
        self.assertFalse(self.can(raw('CHANNEL', owner=101,
            adminParticipants={101: {'id': 101, 'permissions': 256}, '101': {'id': 101, 'permissions': 0}})))


if __name__ == '__main__':
    unittest.main()
