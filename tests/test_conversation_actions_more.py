"""Synthetic ordinary stories/bot actions, with optional/VOID ACK readback."""
import copy
from types import MethodType
import unittest
from unittest.mock import Mock

import test_conversation_actions_service as fixtures
from maxclient.core.service import MaxService
from maxclient.protocol.client import (
    AuthenticatedSession, MaxClient, MaxError, MaxFrame, MaxTimeout, MaxWriteOutcomeUnknown,
)
from maxclient.protocol.conversation_actions_contracts import (
    build_story_visibility, parse_story_visibility_ack, parse_action_contact_readback,
    story_hidden_from_contact, build_stop_bot, parse_stop_bot_ack,
    parse_action_chat_readback, bot_suspended_from_chat,
)


def contact(flags=None, **extra):
    value = {'id': 202, 'updateTime': 100.25, 'names': [{'type': 'ONEME', 'name': 'Synthetic peer'}], **extra}
    if flags is not None:
        value['flags'] = flags
    return value


def bot_chat(**extra):
    return {'id': 303, 'type': 'DIALOG', 'status': 'ACTIVE', 'owner': 0,
            'participants': {'101': 0, '202': 0}, 'created': 0,
            'lastEventTime': 100.25, 'hasBots': True, **extra}


class ConversationMoreContractsTests(unittest.TestCase):
    def test_exact34_119_payloads_and_no_sibling_destructive_actions(self):
        self.assertEqual(build_story_visibility(202, True), {'contactId': 202, 'action': 'HIDE_STORIES'})
        self.assertEqual(build_story_visibility(202, False), {'contactId': 202, 'action': 'SHOW_STORIES'})
        self.assertEqual(build_stop_bot(202), {'botId': 202, 'value': True})
        for identity in (True, '202', 2**63):
            with self.assertRaises(ValueError): build_story_visibility(identity, True)
            with self.assertRaises(ValueError): build_stop_bot(identity)
        for hidden in (0, 1, None, 'true'):
            with self.assertRaises(ValueError): build_story_visibility(202, hidden)

    def test_optional_contact_flags_and_unique_matching32(self):
        self.assertEqual(parse_story_visibility_ack({}, 202), {})
        parsed = parse_story_visibility_ack({'contact': contact(1024)}, 202)
        self.assertIs(story_hidden_from_contact(parsed['contact']), True)
        self.assertIs(story_hidden_from_contact(contact(0)), False)
        self.assertIsNone(story_hidden_from_contact(contact()))
        self.assertIsNone(story_hidden_from_contact(contact(True)))
        self.assertEqual(parse_action_contact_readback({'contacts': [contact(0)]}, 202), contact(0))
        for data in ({}, {'contact': contact(0)}, {'contacts': []},
                     {'contacts': [contact(0), contact(0)]}, {'contacts': [contact(0, id=999)]}):
            with self.assertRaises(ValueError): parse_action_contact_readback(data, 202)
        with self.assertRaises(ValueError): parse_story_visibility_ack({'contact': contact(0, id=999)}, 202)
        with self.assertRaises(ValueError): parse_story_visibility_ack({'contact': contact(True)}, 202)

    def test119_void_has_no_authoritative_state_and48_model_is_exact(self):
        self.assertIsNone(parse_stop_bot_ack(None))
        for data in ({}, True, {'isSuspended': True}):
            with self.assertRaises(ValueError): parse_stop_bot_ack(data)
        for fields, value in (({}, False), ({'lastEventTime': 0}, True),
                              ({'isSuspended': True}, True), ({'isSuspended': False}, False)):
            raw = bot_chat(**fields)
            self.assertIs(bot_suspended_from_chat(raw), value)
            self.assertEqual(parse_action_chat_readback({'chats': [raw]}, 303), raw)
        self.assertIsNone(bot_suspended_from_chat({'hasBots': True}))
        self.assertIsNone(bot_suspended_from_chat(bot_chat(isSuspended=1)))
        with self.assertRaises(ValueError): parse_action_chat_readback({'chats': [bot_chat(id=999)]}, 303)
        with self.assertRaises(ValueError): parse_action_chat_readback({'chats': [bot_chat(isSuspended=1)]}, 303)

    def test_real_wrappers_single_write_optional_and_void(self):
        client = MaxClient(auto_reconnect=False)
        client.request = Mock(return_value=MaxFrame(1, 1, 34, b'', {}))
        self.assertEqual(client.set_contact_stories_hidden(202, True), {})
        client.request.assert_called_once_with(34, {'contactId': 202, 'action': 'HIDE_STORIES'})
        client.request.reset_mock()
        client.request.return_value = MaxFrame(1, 2, 119, b'', None)
        self.assertIsNone(client.stop_bot(202))
        client.request.assert_called_once_with(119, {'botId': 202, 'value': True})
        client.request.return_value = MaxFrame(1, 2, 119, b'', {})
        with self.assertRaises(MaxWriteOutcomeUnknown): client.stop_bot(202)
        client.request.side_effect = MaxTimeout('synthetic')
        client.request.reset_mock()
        with self.assertRaises(MaxWriteOutcomeUnknown): client.stop_bot(202)
        client.request.assert_called_once()


class ConversationMoreServiceTests(unittest.TestCase):
    setUp = fixtures.ConversationActionsServiceTests.setUp
    tearDown = fixtures.ConversationActionsServiceTests.tearDown

    def prepare(self, *, bot=False):
        self.service._remember_contact_info = MethodType(MaxService._remember_contact_info, self.service)
        self.service._remember_chat_info = MethodType(MaxService._remember_chat_info, self.service)
        self.service._reconcile_conversation_departure = MethodType(
            MaxService._reconcile_conversation_departure, self.service)
        self.service._invalidate_contacts = Mock()
        self.service._server_contacts[202] = contact(0)
        if bot:
            self.service._server_chats[303] = bot_chat()
        self.remote_contact, self.remote_chat = contact(0), bot_chat()
        self.client.set_contact_stories_hidden = Mock(return_value={})
        self.client.stop_bot = Mock(return_value=None)
        self.client.contact_info = Mock(return_value=None)
        self.client.chat_info = Mock(return_value=None)
        def stories(_peer, hidden, **kwargs):
            self.remote_contact['flags'] = 1024 if hidden else 0
            return self.client.set_contact_stories_hidden.return_value
        def stop(_peer, **kwargs):
            self.remote_chat['lastEventTime'] = 0
            self.remote_chat['isSuspended'] = True
            return self.client.stop_bot.return_value
        self.client.set_contact_stories_hidden.side_effect = stories
        self.client.stop_bot.side_effect = stop
        self.client.contact_info.side_effect = lambda *a, **k: (
            self.client.contact_info.return_value if self.client.contact_info.return_value is not None
            else {'contacts': [copy.deepcopy(self.remote_contact)]})
        self.client.chat_info.side_effect = lambda *a, **k: (
            self.client.chat_info.return_value if self.client.chat_info.return_value is not None
            else {'chats': [copy.deepcopy(self.remote_chat)]})
        fixtures.ConversationActionsServiceTests.use_actual_capabilities(self)

    def state(self):
        return self.service.get_conversation_action_state(self.context)

    def test34_optional_ack_uses_one32_and_preserves_known_phone_metadata(self):
        self.prepare()
        self.service._server_contacts[202]['phone'] = 70000000000
        result = self.service.set_conversation_stories_hidden(self.context, True)
        self.assertTrue(result['accepted'])
        self.assertTrue(result['confirmed'])
        self.assertFalse(result['pending'])
        self.assertTrue(result['story_hidden'])
        self.client.set_contact_stories_hidden.assert_called_once_with(202, True,
            expected_session=self.context.session_context)
        self.assertEqual(self.client.contact_info.call_count, 2)
        self.client.contact_info.assert_called_with([202], expected_session=self.context.session_context)
        self.assertEqual(self.service._server_contacts[202]['phone'], 70000000000)
        self.client.stop_bot.assert_not_called()
        self.client.update_chat_mute.assert_not_called()

    def test34_matching_actual_ack_needs_no_readback_or_second_write(self):
        self.prepare()
        self.client.set_contact_stories_hidden.return_value = {'contact': contact(1024)}
        self.assertTrue(self.service.set_conversation_stories_hidden(self.context, True)['confirmed'])
        self.client.contact_info.assert_called_once()
        result = self.service.set_conversation_stories_hidden(self.context, True)
        self.assertEqual((result['accepted'], result['confirmed'], result['unchanged']), (False, True, True))
        self.client.set_contact_stories_hidden.assert_called_once()

    def test34_missing_flags_stays_pending_and_manual_refresh_does_not_repeat_write(self):
        self.prepare()
        self.client.contact_info.side_effect = [{'contacts': [contact(0)]}, {'contacts': [contact()]}]
        result = self.service.set_conversation_stories_hidden(self.context, True)
        self.assertTrue(result['accepted'])
        self.assertFalse(result['confirmed'])
        self.assertTrue(result['pending_actions']['stories'])
        with self.assertRaises(ValueError): self.service.set_conversation_stories_hidden(self.context, False)
        self.client.contact_info.side_effect = None
        self.client.contact_info.return_value = {'contacts': [contact(1024)]}
        self.assertFalse(self.service.refresh_conversation_action_state(self.context)['pending'])
        self.client.set_contact_stories_hidden.assert_called_once()
        self.assertEqual(self.client.contact_info.call_count, 3)

    def test_readback_failure_reports_accepted_pending_without_fake_success(self):
        self.prepare()
        self.client.contact_info.side_effect = [{'contacts': [contact(0)]}, MaxError('synthetic read failure')]
        result = self.service.set_conversation_stories_hidden(self.context, True)
        self.assertEqual((result['accepted'], result['confirmed'], result['pending'], result['readback_failed']),
                         (True, False, True, True))
        self.assertFalse(result['story_hidden'])
        self.client.set_contact_stories_hidden.assert_called_once()

    def test119_zeroevent_stays_pending_until_explicit_suspended_readback(self):
        self.prepare(bot=True)
        def derived_only(_peer, **kwargs):
            self.remote_chat['lastEventTime'] = 0
            return None
        self.client.stop_bot.side_effect = derived_only
        result = self.service.stop_conversation_bot(self.context)
        self.assertEqual((result['accepted'], result['confirmed'], result['pending'], result['suspended']),
                         (True, False, True, True))
        self.client.stop_bot.assert_called_once_with(202, expected_session=self.context.session_context)
        self.assertEqual(self.client.chat_info.call_count, 2)
        self.client.chat_info.assert_called_with([303], expected_session=self.context.session_context)
        self.client.contact_info.assert_not_called()
        self.assertFalse(result['availability']['stop_bot'])
        self.remote_chat['isSuspended'] = True
        result = self.service.refresh_conversation_action_state(self.context)
        self.assertFalse(result['pending'])
        self.assertTrue(result['suspended'])
        self.client.stop_bot.assert_called_once()

    def test119_unchanged_active48_is_pending_until_explicit_refresh(self):
        self.prepare(bot=True)
        self.client.chat_info.return_value = {'chats': [bot_chat()]}
        result = self.service.stop_conversation_bot(self.context)
        self.assertTrue(result['accepted'])
        self.assertFalse(result['confirmed'])
        self.assertFalse(result['suspended'])
        with self.assertRaises(ValueError): self.service.stop_conversation_bot(self.context)
        self.client.chat_info.return_value = {'chats': [bot_chat(isSuspended=True)]}
        self.assertFalse(self.service.refresh_conversation_action_state(self.context)['pending'])
        self.client.stop_bot.assert_called_once()

    def test_unknown_write_no_post_read_no_retry_and_per_family_pending(self):
        self.prepare()
        self.client.set_contact_stories_hidden.side_effect = MaxWriteOutcomeUnknown('synthetic')
        with self.assertRaises(MaxWriteOutcomeUnknown): self.service.set_conversation_stories_hidden(self.context, True)
        self.client.contact_info.assert_called_once()
        with self.assertRaises(ValueError): self.service.set_conversation_stories_hidden(self.context, True)
        state = self.state()
        self.assertEqual(state['pending_actions'], {'mute': False, 'stories': True, 'stop_bot': False})
        self.service.set_conversation_mute(self.context, -1)
        self.client.update_chat_mute.assert_called_once()
        self.client.set_contact_stories_hidden.assert_called_once()

    def test119_unknown_and_negative_outcomes_are_distinct(self):
        self.prepare(bot=True)
        self.client.stop_bot.side_effect = MaxError('synthetic rejected')
        with self.assertRaises(MaxError): self.service.stop_conversation_bot(self.context)
        self.assertFalse(self.state()['pending'])
        self.client.stop_bot.side_effect = MaxWriteOutcomeUnknown('synthetic')
        with self.assertRaises(MaxWriteOutcomeUnknown): self.service.stop_conversation_bot(self.context)
        self.assertTrue(self.state()['pending_actions']['stop_bot'])
        self.assertEqual(self.client.chat_info.call_count, 2)

    def test_wrong_account_or_wrong_recipient_readback_cannot_mutate_cache(self):
        self.prepare()
        before = copy.deepcopy(self.service._server_contacts)
        self.client.contact_info.side_effect = [{'contacts': [contact(0)]}, {'contacts': [contact(1024, id=999)]}]
        result = self.service.set_conversation_stories_hidden(self.context, True)
        self.assertFalse(result['confirmed'])
        self.assertTrue(result['readback_failed'])
        self.assertEqual(self.service._server_contacts, before)
        def replacement(*args, **kwargs):
            self.client._auth_session = AuthenticatedSession(101, fixtures.DEVICE, 'WEB', self.client._sock)
            return {'contacts': [contact(1024)]}
        self.client.contact_info.side_effect = replacement
        with self.assertRaises(ValueError): self.service.refresh_conversation_action_state(self.context)
        self.assertEqual(self.service._server_contacts, before)

    def test_bot_service_or_normal_dialog_cannot_stop_and_blocked_cannot_hide(self):
        self.prepare()
        with self.assertRaises(ValueError): self.service.stop_conversation_bot(self.context)
        self.service._server_chats[303]['status'] = 'BLOCKED'
        with self.assertRaises(ValueError): self.service.set_conversation_stories_hidden(self.context, True)
        self.service._server_chats[303]['status'] = 'ACTIVE'
        self.service._server_chats[303] = bot_chat(options={'SERVICE_CHAT': True})
        self.remote_chat = bot_chat(options={'SERVICE_CHAT': True})
        with self.assertRaises(ValueError): self.service.stop_conversation_bot(self.context)
        self.client.stop_bot.assert_not_called()
        self.client.set_contact_stories_hidden.assert_not_called()

    def test_detached_refresh_menu_and_explicit_story_action_use_same_actual_state(self):
        self.prepare()
        self.client.chat_info.return_value = {'chats': [bot_chat(hasBots=False)]}
        self.remote_contact['flags'] = 1024
        changes = self.store._db.total_changes
        snapshot = self.service.refresh_conversation(self.context)
        self.assertTrue(any(row['action'] == 'show_stories' for row in snapshot['more_actions']))
        self.assertEqual(self.store._db.total_changes, changes)
        result = self.service.set_conversation_stories_hidden(self.context, False)
        self.assertTrue(result['accepted'])
        self.assertTrue(result['confirmed'])
        self.assertFalse(result['unchanged'])
        self.assertFalse(result['story_hidden'])
        self.client.set_contact_stories_hidden.assert_called_once_with(
            202, False, expected_session=self.context.session_context)

    def test_actual_suspended_bot_is_noop_after_fresh48_even_when_cached_active(self):
        self.prepare(bot=True)
        self.remote_chat['lastEventTime'] = 0
        result = self.service.stop_conversation_bot(self.context)
        self.assertEqual((result['accepted'], result['confirmed'], result['unchanged']), (False, True, True))
        self.assertTrue(result['suspended'])
        self.client.stop_bot.assert_not_called()
        self.client.chat_info.assert_called_once_with([303], expected_session=self.context.session_context)

    def test_failed_or_replaced_pre_read_removes_its_own_reservation_without_write(self):
        self.prepare()
        self.client.contact_info.side_effect = MaxError('synthetic pre-read failure')
        with self.assertRaises(MaxError): self.service.set_conversation_stories_hidden(self.context, True)
        self.assertFalse(self.state()['pending'])
        self.client.set_contact_stories_hidden.assert_not_called()
        def replace(*args, **kwargs):
            self.client._auth_session = AuthenticatedSession(101, fixtures.DEVICE, 'WEB', self.client._sock)
            return {'contacts': [contact(1024)]}
        self.client.contact_info.side_effect = replace
        with self.assertRaises(ValueError): self.service.set_conversation_stories_hidden(self.context, False)
        self.context = self.service.conversation_context(303)
        self.assertFalse(self.state()['pending'])
        self.client.set_contact_stories_hidden.assert_not_called()

    def test_stale_cached_suspension_does_not_skip_actual_active_bot_stop(self):
        self.prepare(bot=True)
        self.service._server_chats[303]['isSuspended'] = True
        result = self.service.stop_conversation_bot(self.context)
        self.assertTrue(result['accepted'])
        self.assertTrue(result['confirmed'])
        self.assertFalse(result['unchanged'])
        self.client.stop_bot.assert_called_once()

    def test_missing_fresh_flags_cannot_confirm_cached_matching_noop(self):
        self.prepare()
        self.client.contact_info.return_value = {'contacts': [contact()]}
        with self.assertRaises(ValueError): self.service.set_conversation_stories_hidden(self.context, False)
        self.assertFalse(self.state()['pending'])
        self.client.set_contact_stories_hidden.assert_not_called()

    def test_reservation_blocks_second_gesture_during_fresh_read(self):
        self.prepare()
        def read(*args, **kwargs):
            if self.client.contact_info.call_count == 1:
                self.assertTrue(self.state()['pending_actions']['stories'])
                with self.assertRaises(ValueError): self.service.set_conversation_stories_hidden(self.context, False)
                return {'contacts': [contact(0)]}
            return {'contacts': [contact(1024)]}
        self.client.contact_info.side_effect = read
        self.assertTrue(self.service.set_conversation_stories_hidden(self.context, True)['confirmed'])
        self.assertEqual(self.client.contact_info.call_count, 2)
        self.client.set_contact_stories_hidden.assert_called_once()


if __name__ == '__main__':
    unittest.main()
