"""Offline text-edit contracts, fresh permissions and durable no-replay state."""
import copy
import dataclasses
import unittest
from unittest.mock import Mock

import test_owner_transfer_core as owner_fixture
import test_conversation_leave_core as leave_fixture
from maxclient.protocol.client import MaxFrame, MaxError, MaxWriteOutcomeUnknown, MaxWriteNotStarted
from maxclient.protocol.conversation_edit_contracts import (
    normalize_conversation_edit_text, utf16_length, conversation_edit_limits,
    conversation_edit_values, build_conversation_edit, parse_conversation_edit_ack,
)


class ConversationEditWireTests(unittest.TestCase):
    def test_theme_not_title_changed_only_exact55(self):
        client = leave_fixture.ConversationLeaveWireTests().client()
        raw = owner_fixture.base.chat(type='CHAT', title='New')
        client.request.return_value = MaxFrame(1, 1, 55, b'', {'chat': raw})
        self.assertEqual(client.edit_conversation_fields(303, 'CHAT', {'theme': 'New'},
            expected_session=client.authenticated_session)['title'], 'New')
        client.request.assert_called_once_with(55, {'chatId': 303, 'theme': 'New'},
            expected_session=client.authenticated_session)
        for patch in ({}, {'title': 'New'}, {'photoToken': 'x'}, {'theme': ''}, {'description': None}):
            with self.assertRaises(ValueError):
                build_conversation_edit(303, patch)
        self.assertEqual(build_conversation_edit(303, {'description': ''}), {'chatId': 303, 'description': ''})

    def test_strict_actual_chat_ack_and_unknown_without_retry(self):
        raw = owner_fixture.base.chat(type='CHAT', title='New')
        for bad in ({}, None, {'chat': {}}, {'chat': {**raw, 'title': 'Other'}}, {'chat': {**raw, 'id': 999}}):
            with self.assertRaises(ValueError):
                parse_conversation_edit_ack(bad, 303, 'CHAT', {'theme': 'New'})
        client = leave_fixture.ConversationLeaveWireTests().client()
        client.request.return_value = MaxFrame(1, 1, 55, b'', {})
        with self.assertRaises(MaxWriteOutcomeUnknown):
            client.edit_conversation_fields(303, 'CHAT', {'theme': 'New'}, expected_session=client.authenticated_session)
        with self.assertRaises(MaxWriteNotStarted):
            client.edit_conversation_fields(303, 'CHAT', {'theme': 'New'}, expected_session=None)
        client.request.assert_called_once()

    def test_utf16_js_trim_and_config_limits(self):
        self.assertEqual(normalize_conversation_edit_text('\ufeff A \ufeff'), 'A')
        self.assertEqual(normalize_conversation_edit_text('\x85A\x85'), '\x85A\x85')
        self.assertEqual(utf16_length('😀'), 2)
        self.assertEqual(conversation_edit_limits({'max-description-length': 900}), (200, 400))
        self.assertEqual(conversation_edit_values(' 😀 ', '', (2, 1)), ('😀', ''))
        for title in ('\ufeff', '😀a'):
            with self.assertRaises(ValueError):
                conversation_edit_values(title, '', (2, 1))
        self.assertEqual(conversation_edit_limits({'max-cname-length': None}), (200, 400))
        for invalid in (True, 0, -1, 1.5, '200'):
            with self.assertRaises(ValueError):
                conversation_edit_limits({'max-cname-length': invalid})


class ConversationEditCoreTests(unittest.TestCase):
    def setUp(self):
        self.fixture = owner_fixture.OwnerTransferCoreTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.s = self.fixture.s
        self.context = self.fixture.context
        self.fixture.fixture.remote['description'] = 'Old description'
        self.s._server_chats[303] = copy.deepcopy(self.fixture.fixture.remote)
        self.s.client.edit_conversation_fields = Mock(side_effect=self.accept)

    @property
    def remote(self):
        return self.fixture.fixture.remote

    def tearDown(self):
        self.s.client.request.assert_not_called()
        self.s.client.transfer_conversation_owner.assert_not_called()
        self.s.client.leave_conversation.assert_not_called()

    def accept(self, chat_id, kind, patch, **kwargs):
        for field, value in patch.items():
            self.remote['title' if field == 'theme' else field] = value
        self.remote['modified'] = self.remote.get('modified', 100) + 100
        return copy.deepcopy(self.remote)

    def prepare(self):
        return self.s.prepare_conversation_edit(self.context)

    def submit(self, *, title='New title', description='New description', ticket=None):
        return self.s.submit_conversation_edit(self.context, ticket=ticket or self.prepare()['ticket'],
            title=title, description=description)

    def test_prepare_uses_server_fields_readonly_and_cancel_has_no_write(self):
        self.s.store.rename_chat(303, 'Local alias')
        data = self.prepare()
        self.assertEqual(data['title'], 'Synthetic group')
        self.assertEqual(data['description'], 'Old description')
        self.assertTrue(data['can_submit'])
        self.s.client.edit_conversation_fields.assert_not_called()
        self.assertIsNone(self.s.store.conversation_action_journal(101, 303))

    def test_explicit_edit_ack_plus48_confirms_and_unlocks_title_only(self):
        self.s.store.rename_chat(303, 'Local alias')
        result = self.submit()
        self.assertTrue(result['confirmed'])
        self.assertEqual(result['confirmed_fields'], ['title', 'description'])
        self.s.client.edit_conversation_fields.assert_called_once_with(303, 'CHAT',
            {'theme': 'New title', 'description': 'New description'}, expected_session=self.context.session_context)
        self.assertEqual(self.s.store.get_chat(303).title, 'New title')
        self.assertFalse(self.s.store.get_chat(303).title_locked)
        self.assertIsNotNone(self.s.store.message(303, 9))

    def test_description_clear_only_preserves_local_title_alias(self):
        self.s.store.rename_chat(303, 'Local alias')
        self.assertTrue(self.submit(title='Synthetic group', description='')['confirmed'])
        self.assertEqual(self.s.client.edit_conversation_fields.call_args.args[2], {'description': ''})
        self.assertTrue(self.s.store.get_chat(303).title_locked)
        self.assertEqual(self.s.store.get_chat(303).title, 'Local alias')

    def test_unchanged_input_no55_or_reservation(self):
        result = self.submit(title='Synthetic group', description='Old description')
        self.assertTrue(result['unchanged'])
        self.assertFalse(result['accepted'])
        self.s.client.edit_conversation_fields.assert_not_called()
        self.assertIsNone(self.s.store.conversation_action_journal(101, 303))

    def test_admin8_and_member_explicit_option_only(self):
        self.remote.update(owner=202, adminParticipants={'101': {'id': 101, 'permissions': 8}})
        self.s._server_chats[303] = copy.deepcopy(self.remote)
        self.assertTrue(self.prepare()['can_submit'])
        self.remote['adminParticipants']['101'].pop('permissions')
        self.s._server_chats[303] = copy.deepcopy(self.remote)
        with self.assertRaises(ValueError):
            self.prepare()
        self.remote['options'] = {'ONLY_OWNER_CAN_CHANGE_ICON_TITLE': False}
        self.s._server_chats[303] = copy.deepcopy(self.remote)
        self.assertTrue(self.prepare()['can_submit'])

    def test_blocked_and_restriction4_service_unknown_title_deny(self):
        saved = copy.deepcopy(self.remote)
        for change in ({'status': 'BLOCKED'}, {'restrictions': 4}, {'options': {'SERVICE_CHAT': True}},
                       {'title': ''}, {'videoConversation': {}}, {'postId': 9}):
            self.remote.update(change)
            self.s._server_chats[303] = copy.deepcopy(self.remote)
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.prepare()
            self.fixture.fixture.remote = copy.deepcopy(saved)
        self.s.client.edit_conversation_fields.assert_not_called()

    def test_hidden_status_allowed_with_confirmed_owner(self):
        self.remote['status'] = 'HIDDEN'
        self.s._server_chats[303] = copy.deepcopy(self.remote)
        self.assertTrue(self.submit()['confirmed'])

    def test_forged_ticket_wrong_context_and_changed_revision_do_not_send(self):
        ticket = self.prepare()['ticket']
        for invalid in (None, dataclasses.replace(ticket)):
            with self.assertRaises(ValueError):
                self.s.submit_conversation_edit(self.context, ticket=invalid, title='New', description='')
        other = self.s.conversation_context(303, activate_files=False)
        with self.assertRaises(ValueError):
            self.s.submit_conversation_edit(other, ticket=ticket, title='New', description='')
        self.remote.update(description='External edit', modified=150)
        with self.assertRaises(ValueError):
            self.submit(ticket=ticket)
        self.s.client.edit_conversation_fields.assert_not_called()

    def test_config_change_and_role_revocation_after_prepare_block55(self):
        ticket = self.prepare()['ticket']
        self.s.store.merge_config(101, {'server': {'max-cname-length': 20}})
        with self.assertRaises(ValueError):
            self.submit(ticket=ticket)
        ticket = self.prepare()['ticket']
        self.remote['restrictions'] = 4
        with self.assertRaises(ValueError):
            self.submit(ticket=ticket)
        self.s.client.edit_conversation_fields.assert_not_called()

    def test_ack_alone_pending_partial48_honest_and_read_only(self):
        self.s.client.edit_conversation_fields.side_effect = lambda *a, **k: {
            **copy.deepcopy(self.remote), 'title': 'New title', 'description': 'New description'}
        result = self.submit()
        self.assertTrue(result['acknowledged'])
        self.assertTrue(result['pending'])
        self.assertFalse(result['confirmed'])
        self.remote.update(title='New title', modified=200)
        result = self.s.refresh_conversation_controls(self.context)['lifecycle_result']
        self.assertEqual(result['confirmed_fields'], ['title'])
        self.assertEqual(result['pending_fields'], ['description'])
        self.assertTrue(result['pending'])
        self.remote.update(description='New description', modified=210)
        self.assertTrue(self.s.refresh_conversation_lifecycle(self.context)['confirmed'])
        self.s.client.edit_conversation_fields.assert_called_once()

    def test_unknown55_reopen_readonly_and_no_retry(self):
        self.s.client.edit_conversation_fields.side_effect = MaxWriteOutcomeUnknown('synthetic')
        ticket = self.prepare()['ticket']
        with self.assertRaises(MaxWriteOutcomeUnknown):
            self.submit(ticket=ticket)
        data = self.prepare()
        self.assertTrue(data['pending'])
        self.assertFalse(data['can_submit'])
        self.assertIsNone(data['ticket'])
        with self.assertRaises(ValueError):
            self.submit(ticket=ticket)
        self.remote.update(title='New title', description='New description', modified=200)
        result = self.s.refresh_conversation_lifecycle(self.context)
        self.assertTrue(result['confirmed'])
        self.assertFalse(result['acknowledged'])
        self.s.client.edit_conversation_fields.assert_called_once()

    def test_malformed_ack_keeps_unknown_and_title_alias(self):
        self.s.store.rename_chat(303, 'Local alias')
        self.s.client.edit_conversation_fields.side_effect = None
        self.s.client.edit_conversation_fields.return_value = {}
        with self.assertRaises(MaxWriteOutcomeUnknown):
            self.submit()
        self.assertTrue(self.s.store.get_chat(303).title_locked)
        self.assertTrue(self.s.conversation_lifecycle_status(self.context)['pending'])

    def test_rejected_write_releases_journal_and_preserves_metadata(self):
        self.s.client.edit_conversation_fields.side_effect = MaxError('synthetic')
        with self.assertRaises(MaxError):
            self.submit()
        self.assertIsNone(self.s.store.conversation_action_journal(101, 303))
        self.assertEqual(self.s._server_chats[303]['title'], 'Synthetic group')

    def test_newer_push_during_read_cannot_be_overwritten(self):
        ticket = self.prepare()['ticket']
        reader = self.s.client.chat_info.side_effect
        def read(*a, **k):
            result = reader(*a, **k)
            self.s._server_chats[303].update(title='Pushed title', modified=999)
            return result
        self.s.client.chat_info.side_effect = read
        with self.assertRaises(ValueError):
            self.submit(ticket=ticket)
        self.assertEqual(self.s._server_chats[303]['title'], 'Pushed title')
        self.s.client.edit_conversation_fields.assert_not_called()

    def test_pending_edit_blocks_conflicting_lifecycle_and_transfer(self):
        successor = self.fixture.ticket()
        self.s.client.edit_conversation_fields.side_effect = MaxWriteOutcomeUnknown('synthetic')
        with self.assertRaises(MaxWriteOutcomeUnknown):
            self.submit()
        for action in (lambda: self.s.clear_conversation_history(self.context),
                       lambda: self.s.transfer_conversation_owner_and_leave(self.context, successor)):
            with self.assertRaises(ValueError):
                action()
        self.s.client.change_conversation_lifecycle.assert_not_called()

    def test_inflight_refresh_never_completes_or_sends_again(self):
        def accept(*a, **k):
            raw = self.accept(*a, **k)
            self.assertTrue(self.s.refresh_conversation_lifecycle(self.context)['pending'])
            self.assertIsNotNone(self.s.store.conversation_action_journal(101, 303))
            return raw
        self.s.client.edit_conversation_fields.side_effect = accept
        self.assertTrue(self.submit()['confirmed'])
        self.s.client.edit_conversation_fields.assert_called_once()

    def test_unrelated_incoming_message_does_not_invalidate_ticket(self):
        ticket = self.prepare()['ticket']
        self.remote.update(lastEventTime=200, modified=160,
            lastMessage={'id': 10, 'time': 200, 'type': 'USER', 'text': 'Synthetic incoming'})
        self.remote['participants']['202'] = 180
        self.s._server_chats[303] = copy.deepcopy(self.remote)
        self.assertTrue(self.submit(ticket=ticket)['confirmed'])

    def test_experiment_limits_override_replace_and_nullish_fallback(self):
        self.s._remember_config({'server': {'max-cname-length': 100},
            'experiments': {'max-cname-length': 3, 'max-description-length': 5, 'unrelated': 9}}, owner_id=101)
        self.assertEqual(self.prepare()['title_limit'], 3)
        with self.assertRaises(ValueError):
            self.submit(title='Four')
        self.s._remember_config({'experiments': {'max-cname-length': None}}, owner_id=101)
        prepared = self.prepare()
        self.assertEqual((prepared['title_limit'], prepared['description_limit']), (100, 400))
        self.assertNotIn('unrelated', self.s.store.load_config(101)['experiments'])
        self.s._remember_config({'experiments': {'max-cname-length': False}}, owner_id=101)
        with self.assertRaises(ValueError):
            self.prepare()
        self.s._remember_config({'experiments': {}}, owner_id=101)
        with self.assertRaises(ValueError):
            self.s._remember_config({'server': {'max-cname-length': 'bad'}}, owner_id=101)
        self.assertEqual(self.prepare()['title_limit'], 100)
        self.s._remember_config({'server': {'max-cname-length': None}}, owner_id=101)
        self.assertEqual(self.prepare()['title_limit'], 200)
        self.s.client.edit_conversation_fields.assert_not_called()

    def test_pending_readonly_reopen_uses_original_limits_when_config_revoked(self):
        self.s.client.edit_conversation_fields.side_effect = MaxWriteOutcomeUnknown('synthetic')
        with self.assertRaises(MaxWriteOutcomeUnknown):
            self.submit()
        self.s._remember_config({'experiments': {'max-cname-length': False}}, owner_id=101)
        prepared = self.prepare()
        self.assertTrue(prepared['pending'])
        self.assertFalse(prepared['can_submit'])
        self.assertIsNone(prepared['ticket'])

    def test_malformed_server_limit_rejects_entire_config_without_partial_merge(self):
        self.s._remember_config({'hash': 'valid', 'server': {'max-cname-length': 100},
            'experiments': {'max-description-length': 20}}, owner_id=101)
        before = self.s.store.load_config(101)
        for key in ('max-cname-length', 'max-description-length'):
            for invalid in (True, '200', 0, -1, 1.5, 100001):
                with self.subTest(key=key, invalid=invalid), self.assertRaises(ValueError):
                    self.s._remember_config({'hash': 'invalid', 'server': {key: invalid,
                        'max-added-participants': 1}, 'experiments': {}}, owner_id=101)
                self.assertEqual(self.s.store.load_config(101), before)
        self.s.client.edit_conversation_fields.assert_not_called()
