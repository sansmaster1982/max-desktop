"""Pinned WEB model/Info gates, pure synthetic metadata; no Qt or account I/O."""
import copy
import unittest

from maxclient.core.conversation_action_capabilities import conversation_action_capabilities


STAMP = 1_790_000_000_000.25


def chat(**fields):
    return {'id': 303, 'type': 'DIALOG', 'status': 'ACTIVE', 'owner': 101,
            'participants': {'101': 0, '202': 0}, 'lastEventTime': STAMP,
            'created': 0, 'lastMessage': {'id': 7, 'time': STAMP, 'type': 'USER'}, **fields}


def contact(**fields):
    return {'id': 202, 'updateTime': STAMP, 'names': [{'name': 'Synthetic peer'}], 'flags': 0, **fields}


def capabilities(raw=None, person=None, config=None):
    return conversation_action_capabilities(chat() if raw is None else raw,
        contact() if person is None else person, 101, {} if config is None else config, peer_id=202)


class ConversationActionCapabilitiesTests(unittest.TestCase):
    def test_partial_chat_without_status_never_authorizes_actions(self):
        for kind in ('DIALOG', 'CHAT', 'CHANNEL'):
            for missing in (True, False):
                with self.subTest(kind=kind, missing=missing):
                    raw = chat(type=kind)
                    if missing:
                        raw.pop('status')
                    else:
                        raw['status'] = None
                    result = capabilities(raw)
                    self.assertFalse(result['supported'])
                    self.assertFalse(result['mute'])
                    self.assertEqual(result['more'], [])

    def test_admin_records_require_actual_id_and_typed_fields(self):
        for row in (0, None, {}, {'id': 202}, {'id': True}, {'id': 101, 'permissions': True},
                    {'id': 101, 'inviterId': '202'}, {'id': 101, 'alias': 4}):
            with self.subTest(row=row):
                result = capabilities(chat(type='CHAT', owner=0, participants={},
                    adminParticipants={'101': row}), person={})
                self.assertFalse(result['supported'])
                self.assertFalse(result['participant'])

    def test_invalid_participant_timestamps_never_enable_group_actions(self):
        for value in (True, None, 'invalid', float('nan'), float('inf'), 10**400):
            with self.subTest(value=str(value)[:24]):
                result = capabilities(chat(type='CHAT', owner=0, joinTime=100,
                    participants={'101': value}), person={})
                self.assertFalse(result['supported'])
                self.assertFalse(result['participant'])
                self.assertEqual(result['more'], [])

    def test_normal_dialog_has_info_specific_order_and_no_report(self):
        result = capabilities()
        self.assertTrue(result['supported'])
        self.assertEqual(result['kind'], 'dialog')
        self.assertTrue(result['mute'])
        self.assertTrue(result['participant'])
        self.assertEqual(result['more'], ['share_contact', 'folder', 'search', 'clear',
            'hide_stories', 'block', 'delete'])
        self.assertNotIn('report', result['more'])  # Base W comma-expression returns false.
        self.assertFalse(result['clear_for_all_optional'])

    def test_blocked_recipient_disables_mute_but_selects_unblock_from_active_chat(self):
        result = capabilities(person=contact(status='BLOCKED'))
        self.assertFalse(result['mute'])
        self.assertIn('unblock', result['more'])
        self.assertNotIn('block', result['more'])
        self.assertIn('clear', result['more'])

    def test_blocked_chat_status_filters_more_without_inventing_recipient_block(self):
        result = capabilities(chat(status='BLOCKED'))
        self.assertEqual(result['more'], ['delete'])
        self.assertTrue(result['mute'])  # Normal dialog's getter tests recipient status separately.
        self.assertFalse(result['recipient_blocked'])

    def test_saved_dialog_uses_actual_zero_chat_id_and_has_no_delete_or_mute(self):
        result = capabilities(chat(id=0), person={})
        self.assertEqual(result['kind'], 'saved')
        self.assertTrue(result['participant'])
        self.assertFalse(result['mute'])
        self.assertEqual(result['more'], ['folder', 'search', 'clear'])

    def test_group_membership_replays_join_filter_and_owner_admin_additions(self):
        ordinary = chat(type='CHAT', owner=202, joinTime=1)
        result = capabilities(ordinary, person={})
        self.assertTrue(result['participant'])
        self.assertIn('leave', result['more'])
        self.assertNotIn('report', result['more'])
        missing_join = copy.deepcopy(ordinary)
        missing_join.pop('joinTime')
        result = capabilities(missing_join, person={})
        self.assertFalse(result['participant'])
        self.assertNotIn('leave', result['more'])
        self.assertNotIn('folder', result['more'])
        for added in ({'owner': 101, 'participants': {}},
                      {'adminParticipants': {'101': {'id': 101}}, 'participants': {}}):
            with self.subTest(added=added):
                result = capabilities({**missing_join, **added}, person={})
                self.assertTrue(result['participant'])

    def test_group_owner_delete_clear_scope_and_transfer_gate(self):
        raw = chat(type='CHAT', participants={}, participantsCount=3)
        result = capabilities(raw, person={})
        self.assertTrue(result['owner'])
        self.assertIn('delete', result['more'])
        self.assertTrue(result['clear_for_all_optional'])
        self.assertTrue(result['leave_requires_transfer'])
        self.assertFalse(capabilities({**raw, 'participantsCount': 1}, person={})['leave_requires_transfer'])
        raw.pop('participantsCount')
        self.assertTrue(capabilities(raw, person={})['leave_requires_transfer'])

    def test_leave_restriction_is_bit8_not_a_generic_permission_boolean(self):
        result = capabilities(chat(type='CHAT', restrictions=8), person={})
        self.assertNotIn('leave', result['more'])
        self.assertIn('delete', result['more'])
        self.assertTrue(result['mute'])

    def test_channel_report_only_for_nonowner_without_bit256(self):
        raw = chat(type='CHANNEL', owner=202, joinTime=1)
        result = capabilities(raw, person={})
        self.assertIn('report', result['more'])
        self.assertNotIn('delete', result['more'])
        self.assertNotIn('clear', result['more'])
        self.assertTrue(result['clear_for_all'])
        self.assertNotIn('report', capabilities({**raw, 'restrictions': 256}, person={})['more'])
        owned = capabilities({**raw, 'owner': 101}, person={})
        self.assertNotIn('report', owned['more'])
        self.assertIn('clear', owned['more'])

    def test_bot_mute_and_complaint_follow_their_distinct_config_and_restriction_bits(self):
        raw = chat(hasBots=True)
        result = capabilities(raw)
        self.assertEqual(result['kind'], 'bot')
        self.assertTrue(result['mute'])
        self.assertIn('stop_bot', result['more'])
        self.assertIn('delete', result['more'])
        self.assertNotIn('report', result['more'])
        self.assertIn('report', capabilities(raw, config={'bot-complaint-enabled': True})['more'])
        self.assertNotIn('report', capabilities({**raw, 'restrictions': 256},
            config={'bot-complaint-enabled': True})['more'])
        muted_bit = {**raw, 'restrictions': 8192}
        self.assertTrue(capabilities(muted_bit)['mute'])  # Default bots-mute-restriction is false.
        self.assertFalse(capabilities(muted_bit, config={'bots-mute-restriction': True})['mute'])

    def test_bot_stop_and_delete_both_require_absent_bit128(self):
        result = capabilities(chat(hasBots=True, restrictions=128))
        self.assertNotIn('stop_bot', result['more'])
        self.assertNotIn('delete', result['more'])
        self.assertTrue(result['mute'])

    def test_last_event_zero_suspends_bot_even_when_raw_is_suspended_false(self):
        result = capabilities(chat(hasBots=True, lastEventTime=0, isSuspended=False))
        self.assertTrue(result['suspended'])
        self.assertFalse(result['mute'])
        self.assertNotIn('stop_bot', result['more'])

    def test_service_bot_uses_chat_option_not_contact_service_account_flag(self):
        raw = chat(hasBots=True, options={'SERVICE_CHAT': True})
        result = capabilities(raw)
        self.assertTrue(result['service'])
        self.assertTrue(result['mute'])
        self.assertFalse({'clear', 'delete', 'stop_bot'} & set(result['more']))
        ordinary = capabilities(chat(hasBots=True), person=contact(flags=8))
        self.assertFalse(ordinary['service'])
        self.assertIn('stop_bot', ordinary['more'])

    def test_suspended_bot_without_actual_last_message_hides_the_entire_more_trigger(self):
        raw = chat(hasBots=True, lastEventTime=0)
        raw.pop('lastMessage')
        result = capabilities(raw, config={'bot-complaint-enabled': True})
        self.assertTrue(result['suspended'])
        self.assertEqual(result['more'], [])

    def test_video_chat_info_excludes_mute_and_general_menu_but_owner_can_clear_for_all(self):
        video = {'conversationId': 'synthetic-conversation', 'joinLink': 'synthetic-link',
                 'callType': 'AUDIO', 'type': 0}
        result = capabilities(chat(type='CHAT', videoConversation=video), person={})
        self.assertEqual(result['kind'], 'video')
        self.assertFalse(result['mute'])
        self.assertEqual(result['more'], ['search', 'clear'])
        self.assertTrue(result['clear_for_all_optional'])  # node0 Xd includes both hM and gM owners.

    def test_video_type1_stays_ordinary_group_and_absent_type_selects_special_video(self):
        video = {'conversationId': 'synthetic-conversation', 'joinLink': 'synthetic-link', 'callType': 'AUDIO'}
        self.assertEqual(capabilities(chat(type='CHAT', videoConversation=video), person={})['kind'], 'video')
        result = capabilities(chat(type='CHAT', videoConversation={**video, 'type': 1}), person={})
        self.assertEqual(result['kind'], 'group')
        self.assertTrue(result['mute'])
        self.assertIn('delete', result['more'])

    def test_contact_story_state_is_flags_bit1024(self):
        result = capabilities(person=contact(flags=1024))
        self.assertTrue(result['hide_stories'])
        self.assertIn('show_stories', result['more'])
        self.assertNotIn('hide_stories', result['more'])
        result = capabilities(person=contact(hideStories=True))
        self.assertFalse(result['hide_stories'])

    def test_has_bots_and_service_option_are_booleans_without_truthy_coercion(self):
        for value in (1, 0, None, 'true'):
            with self.subTest(value=value):
                self.assertFalse(capabilities(chat(hasBots=value))['supported'])
                self.assertFalse(capabilities(chat(hasBots=True, options={'SERVICE_CHAT': value}))['supported'])

    def test_zero_last_message_id_is_not_a_truthy_source_menu_anchor(self):
        result = capabilities(chat(lastMessage={'id': 0, 'type': 'USER', 'time': STAMP}))
        self.assertFalse(result['last_message'])
        self.assertNotIn('clear', result['more'])
        self.assertNotIn('search', result['more'])

    def test_omitted_contact_status_is_unblocked_but_explicit_null_is_invalid_DTO(self):
        self.assertTrue(capabilities(person=contact())['mute'])
        self.assertFalse(capabilities(person=contact(status=None))['supported'])
        for status in ('REMOVED', 'HIDDEN'):
            self.assertTrue(capabilities(person=contact(status=status))['mute'])

    def test_bad_restriction_flags_config_or_peer_binding_cannot_authorize_actions(self):
        for value in (True, None, 0.25, 2**31, -(2**31)-1):
            with self.subTest(value=value):
                self.assertFalse(capabilities(chat(restrictions=value))['supported'])
                self.assertFalse(capabilities(person=contact(flags=value))['supported'])
        for key in ('bots-mute-restriction', 'bot-complaint-enabled', 'delete-for-all'):
            self.assertFalse(capabilities(config={key: 'true'})['supported'])
        self.assertFalse(capabilities(person=contact(id=303))['supported'])

    def test_nullable_or_boolean_last_event_time_does_not_fake_bot_suspension(self):
        for value in (False, True, None, float('nan'), float('inf')):
            self.assertFalse(capabilities(chat(hasBots=True, lastEventTime=value))['supported'])

    def test_video_literal_type_does_not_accept_python_boolean_as_one(self):
        video = {'conversationId': 'synthetic-conversation', 'joinLink': 'synthetic-link', 'callType': 'AUDIO'}
        for value in (True, False, 2, None, '0'):
            with self.subTest(value=value):
                self.assertFalse(capabilities(chat(type='CHAT',
                    videoConversation={**video, 'type': value}), person={})['supported'])
        self.assertFalse(capabilities(chat(type='CHAT', videoConversation=None), person={})['supported'])

    def test_unknown_status_strings_are_rejected_by_local_DTO_policy(self):
        self.assertFalse(capabilities(chat(status='unknown'))['supported'])
        self.assertFalse(capabilities(person=contact(status='ACTIVE'))['supported'])

    def test_participant_identifiers_are_bounded_before_integer_conversion(self):
        for key in ('9' * 10000, '9' * 21, 'not-an-id', True):
            with self.subTest(kind=type(key).__name__):
                self.assertFalse(capabilities(chat(type='CHAT', participants={key: 0}), person={})['supported'])

    def test_evaluating_capabilities_never_mutates_raw_metadata(self):
        raw, person, config = chat(), contact(flags=1024), {'delete-for-all': True}
        before = copy.deepcopy((raw, person, config))
        capabilities(raw, person, config)
        self.assertEqual((raw, person, config), before)


if __name__ == '__main__':
    unittest.main()
