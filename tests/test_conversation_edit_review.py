"""Independent edit regressions using only synthetic state and mocked RPC."""
import copy
from pathlib import Path
import tempfile
import unittest

import test_conversation_edit_core as edit_fixture
from maxclient.core.models import Chat, Contact
from maxclient.core.store import Store
from maxclient.protocol.client import AuthenticatedSession, MaxWriteOutcomeUnknown
from maxclient.protocol.conversation_edit_contracts import build_conversation_edit


class ConversationEditReviewTests(unittest.TestCase):
    def new_case(self):
        case = edit_fixture.ConversationEditCoreTests()
        case.setUp()
        self.addCleanup(case.doCleanups)
        self.addCleanup(case.s.client.request.assert_not_called)
        self.addCleanup(case.s.client.change_conversation_lifecycle.assert_not_called)
        return case

    def setUp(self):
        self.case = self.new_case()
        self.s, self.context = self.case.s, self.case.context

    def publish(self):
        self.s._server_chats[303] = copy.deepcopy(self.case.remote)

    def test_new_message_does_not_invalidate_unchanged_editor_fields(self):
        ticket = self.case.prepare()['ticket']
        self.case.remote.update(lastEventTime=180,
            lastMessage={'id': 10, 'time': 180, 'type': 'USER', 'text': 'Synthetic new message'})
        self.case.remote['participants']['202'] = 180
        self.publish()
        self.assertTrue(self.case.submit(ticket=ticket)['confirmed'])
        self.s.client.edit_conversation_fields.assert_called_once()

    def test_channel_member_cannot_edit_even_when_group_owner_only_option_false(self):
        self.case.remote.update(type='CHANNEL', owner=202, options={'ONLY_OWNER_CAN_CHANGE_ICON_TITLE': False},
                              adminParticipants={})
        self.s.store.upsert_chat(Chat(id=303, title='Channel', chat_type='CHANNEL', is_channel=True))
        self.publish()
        with self.assertRaises(ValueError):
            self.case.prepare()
        self.case.remote['adminParticipants'] = {'101': {'id': 101, 'permissions': 8}}
        self.publish()
        self.assertTrue(self.case.submit()['confirmed'])

    def test_icon_restriction_does_not_disable_text_and_other_admin_bits_do_not_grant_it(self):
        self.case.remote['restrictions'] = 2
        self.publish()
        self.assertTrue(self.case.prepare()['can_submit'])
        self.case.remote.update(owner=202, adminParticipants={'101': {'id': 101, 'permissions': 4}})
        self.publish()
        with self.assertRaises(ValueError):
            self.case.prepare()
        self.s.client.edit_conversation_fields.assert_not_called()

    def test_utf16_boundary_is_enforced_at_service_and_no_truncation(self):
        self.s.store.merge_config(101, {'server': {'max-cname-length': 4, 'max-description-length': 3}})
        ticket = self.case.prepare()['ticket']
        for title, description in [('😀😀x', ''), ('OK', '😀ab')]:
            with self.subTest(title=title, description=description), self.assertRaises(ValueError):
                self.case.submit(ticket=ticket, title=title, description=description)
        self.s.client.edit_conversation_fields.assert_not_called()
        self.assertTrue(self.case.submit(ticket=ticket, title='\ufeff😀😀\ufeff', description='😀a')['confirmed'])
        self.assertEqual(self.s.client.edit_conversation_fields.call_args.args[2],
                         {'theme': '😀😀', 'description': '😀a'})

    def test_experiment_limits_override_server_and_replacement_removes_old_experiment(self):
        self.s.store.merge_config(101, {'server': {'max-cname-length': 200},
                                      'experiments': {'max-cname-length': 5}})
        self.assertEqual(self.case.prepare()['title_limit'], 5)
        self.s.store.merge_config(101, {'experiments': {}})
        self.assertEqual(self.case.prepare()['title_limit'], 200)

    def test_experiment_change_after_prepare_blocks_submit(self):
        ticket = self.case.prepare()['ticket']
        self.s.store.merge_config(101, {'experiments': {'max-cname-length': 20}})
        with self.assertRaises(ValueError):
            self.case.submit(ticket=ticket)
        self.s.client.edit_conversation_fields.assert_not_called()

    def test_same_timestamp_conflicting_push_during_read_is_not_overwritten(self):
        ticket = self.case.prepare()['ticket']
        original = self.s.client.chat_info.side_effect

        def read(*args, **kwargs):
            result = original(*args, **kwargs)
            self.s._server_chats[303]['description'] = 'Concurrent pushed description'
            return result

        self.s.client.chat_info.side_effect = read
        with self.assertRaises(ValueError):
            self.case.submit(ticket=ticket)
        self.assertEqual(self.s._server_chats[303]['description'], 'Concurrent pushed description')
        self.s.client.edit_conversation_fields.assert_not_called()

    def test_two_contexts_for_same_chat_cannot_write_concurrently(self):
        first = self.case.prepare()['ticket']
        other = self.s.conversation_context(303, activate_files=False)
        second = self.s.prepare_conversation_edit(other)['ticket']

        def accept(*args, **kwargs):
            with self.assertRaises(ValueError):
                self.s.submit_conversation_edit(other, ticket=second, title='Concurrent', description='Other')
            return self.case.accept(*args, **kwargs)

        self.s.client.edit_conversation_fields.side_effect = accept
        self.assertTrue(self.case.submit(ticket=first)['confirmed'])
        self.s.client.edit_conversation_fields.assert_called_once()

    def test_pending_owner_transfer_blocks_editor_before_any_edit_write(self):
        ticket = self.case.prepare()['ticket']
        successor = self.case.fixture.ticket()
        self.s.client.transfer_conversation_owner.side_effect = MaxWriteOutcomeUnknown('Synthetic')
        with self.assertRaises(MaxWriteOutcomeUnknown):
            self.s.transfer_conversation_owner_and_leave(self.context, successor)
        with self.assertRaises(ValueError):
            self.case.submit(ticket=ticket)
        self.s.client.edit_conversation_fields.assert_not_called()
        self.s.client.transfer_conversation_owner.assert_called_once()
        self.s.client.leave_conversation.assert_not_called()

    def test_same_owner_session_replaced_after55_keeps_unknown_then_readonly_recovers(self):
        client = self.s.client

        def accept(*args, **kwargs):
            result = self.case.accept(*args, **kwargs)
            client._auth_session = AuthenticatedSession(101, client.device_id, 'WEB', client._sock)
            return result

        client.edit_conversation_fields.side_effect = accept
        with self.assertRaises((ValueError, MaxWriteOutcomeUnknown)):
            self.case.submit()
        fresh = self.s.conversation_context(303, activate_files=False)
        result = self.s.refresh_conversation_lifecycle(fresh)
        self.assertTrue(result['confirmed'])
        self.assertFalse(result['acknowledged'])
        client.edit_conversation_fields.assert_called_once()

    def test_account_switch_after55_cannot_unlock_old_alias_or_confirm_new_owner(self):
        self.s.store.rename_chat(303, 'Local alias')
        client = self.s.client

        def accept(*args, **kwargs):
            result = self.case.accept(*args, **kwargs)
            client._auth_session = AuthenticatedSession(999, client.device_id, 'WEB', client._sock)
            self.s.session.my_user_id = 999
            return result

        client.edit_conversation_fields.side_effect = accept
        with self.assertRaises((ValueError, MaxWriteOutcomeUnknown)):
            self.case.submit()
        self.assertTrue(self.s.store.get_chat(303).title_locked)
        self.assertIsNotNone(self.s.store.conversation_action_journal(101, 303))

    def test_cas_replacement_before55_survives_cleanup(self):
        original = self.s._reserve_lifecycle
        replacement = None

        def reserve(context, record):
            nonlocal replacement
            original(context, record)
            replacement = {**record, 'operation_id': 'Newer operation'}
            self.assertTrue(self.s.store.compare_conversation_action(101, 303, record, replacement))

        self.s._reserve_lifecycle = reserve
        with self.assertRaises(ValueError):
            self.case.submit()
        self.assertEqual(self.s.store.conversation_action_journal(101, 303), replacement)
        self.s.client.edit_conversation_fields.assert_not_called()

    def test_cas_replacement_after55_never_acknowledged_or_unlocks_alias(self):
        self.s.store.rename_chat(303, 'Local alias')
        replacement = None

        def accept(*args, **kwargs):
            nonlocal replacement
            result = self.case.accept(*args, **kwargs)
            old = self.s.store.conversation_action_journal(101, 303)
            replacement = {**old, 'operation_id': 'Replacement'}
            self.assertTrue(self.s.store.compare_conversation_action(101, 303, old, replacement))
            return result

        self.s.client.edit_conversation_fields.side_effect = accept
        with self.assertRaises(MaxWriteOutcomeUnknown):
            self.case.submit()
        self.assertEqual(self.s.store.conversation_action_journal(101, 303), replacement)
        self.assertTrue(self.s.store.get_chat(303).title_locked)

    def test_partial_matches_do_not_accumulate_after_field_reverts(self):
        self.s.client.edit_conversation_fields.side_effect = MaxWriteOutcomeUnknown('Synthetic')
        with self.assertRaises(MaxWriteOutcomeUnknown):
            self.case.submit()
        self.case.remote.update(title='New title', modified=200)
        first = self.s.refresh_conversation_lifecycle(self.context)
        self.assertEqual(first['confirmed_fields'], ['title'])
        self.case.remote.update(title='Someone else renamed', description='New description', modified=210)
        second = self.s.refresh_conversation_lifecycle(self.context)
        self.assertFalse(second['confirmed'])
        self.assertEqual(second['confirmed_fields'], ['description'])
        self.assertEqual(second['pending_fields'], ['title'])
        self.s.client.edit_conversation_fields.assert_called_once()

    def test_unknown_survives_sqlite_reopen_no_replay_and_clears_alias_only_when_confirmed(self):
        directory = self.enterContext(tempfile.TemporaryDirectory(prefix='edit-review-', dir=Path.cwd()))
        path = Path(directory) / 'synthetic.sqlite'
        first = Store(path)
        self.addCleanup(first.close)
        self.s.store._db.backup(first._db)
        self.s.store = first
        self.s.store.rename_chat(303, 'Local alias')
        self.s.client.edit_conversation_fields.side_effect = MaxWriteOutcomeUnknown('Synthetic')
        with self.assertRaises(MaxWriteOutcomeUnknown):
            self.case.submit()
        restored = self.new_case()
        reopened = Store(path)
        self.addCleanup(reopened.close)
        restored.s.store = reopened
        self.assertTrue(restored.prepare()['pending'])
        self.assertTrue(reopened.get_chat(303).title_locked)
        restored.remote.update(title='New title', description='New description', modified=200)
        self.assertTrue(restored.s.refresh_conversation_lifecycle(restored.context)['confirmed'])
        self.assertFalse(reopened.get_chat(303).title_locked)
        restored.s.client.edit_conversation_fields.assert_not_called()

    def test_atomic_final_commit_failure_preserves_alias_and_pending_record(self):
        self.s.store.rename_chat(303, 'Local alias')
        original = self.s.store.compare_conversation_action

        def fail_commit(*args, **kwargs):
            if kwargs.get('edit_title') is not None:
                raise RuntimeError('Synthetic SQLite failure')
            return original(*args, **kwargs)

        self.s.store.compare_conversation_action = fail_commit
        result = self.case.submit()
        self.assertFalse(result['confirmed'])
        self.assertTrue(self.s.store.get_chat(303).title_locked)
        self.assertIsNotNone(self.s.store.conversation_action_journal(101, 303))
        self.s.store.compare_conversation_action = original
        self.assertTrue(self.s.refresh_conversation_lifecycle(self.context)['confirmed'])
        self.s.client.edit_conversation_fields.assert_called_once()

    def test_edit_never_renames_contact_with_same_numeric_id_as_chat(self):
        self.s.store.upsert_contact(Contact(id=303, name='Unrelated contact', phone='+10000000000'))
        self.assertTrue(self.case.submit()['confirmed'])
        self.assertEqual(next(c.name for c in self.s.store.list_contacts() if c.id == 303), 'Unrelated contact')

    def test_clear_description_omitted_from_full_fresh_dto_uses_documented_empty_model(self):
        def accept(*args, **kwargs):
            result = self.case.accept(*args, **kwargs)
            self.case.remote.pop('description', None)
            result.pop('description', None)
            return result

        self.s.client.edit_conversation_fields.side_effect = accept
        self.assertTrue(self.case.submit(title='Synthetic group', description='')['confirmed'])
        self.assertEqual(self.s.client.edit_conversation_fields.call_args.args[2], {'description': ''})
        self.assertGreaterEqual(self.s.client.chat_info.call_count, 3)

    def test_malformed_persisted_fields_and_expanded_patch_never_authorize_rpc(self):
        self.s.client.edit_conversation_fields.side_effect = MaxWriteOutcomeUnknown('Synthetic')
        with self.assertRaises(MaxWriteOutcomeUnknown):
            self.case.submit()
        original = self.s.store.conversation_action_journal(101, 303)
        for changes in ({'patch': {'changeOwnerId': '202'}}, {'patch': {'theme': 123}},
                        {'observed_fields': [{}]}, {'observed_fields': ['photoToken']},
                        {'limits': [True, 400]}):
            damaged = {**original, **changes}
            self.assertTrue(self.s.store.compare_conversation_action(101, 303, original, damaged))
            with self.subTest(changes=changes), self.assertRaises((ValueError, TypeError)):
                self.s.prepare_conversation_edit(self.context)
            self.assertEqual(self.s.store.conversation_action_journal(101, 303), damaged)
            self.assertTrue(self.s.store.compare_conversation_action(101, 303, damaged, original))
        for patch in ({'description': b'bytes'}, {'options': {}}, {'photoToken': 'x'}, {'access': 'PUBLIC'}):
            with self.subTest(patch=patch), self.assertRaises(ValueError):
                build_conversation_edit(303, patch)
        self.s.client.edit_conversation_fields.assert_called_once()

    def test_owner_change_does_not_reuse_limit_experiments(self):
        self.s.store.merge_config(101, {'server': {'max-cname-length': 100},
                                      'experiments': {'max-cname-length': 5}})
        self.s.store.bind_owner(999)
        self.assertNotIn('experiments', self.s.store.load_config(999))
        self.s.store.merge_config(999, {'server': {'max-cname-length': 30}})
        self.assertEqual(self.s.store.load_config(999)['server']['max-cname-length'], 30)
        with self.assertRaises(ValueError):
            self.s.prepare_conversation_edit(self.context)
        self.s.client.edit_conversation_fields.assert_not_called()

    def test_stale_get_after55_never_rolls_back_newer_push(self):
        original = self.s.client.chat_info.side_effect

        def read(*args, **kwargs):
            result = original(*args, **kwargs)
            if self.s.client.edit_conversation_fields.called:
                self.s._server_chats[303].update(title='Newer pushed title', modified=900)
            return result

        self.s.client.chat_info.side_effect = read
        result = self.case.submit()
        self.assertFalse(result['confirmed'])
        self.assertEqual(self.s._server_chats[303]['title'], 'Newer pushed title')
        self.assertEqual(self.s._server_chats[303]['modified'], 900)
        self.s.client.edit_conversation_fields.assert_called_once()


if __name__ == '__main__':
    unittest.main()
