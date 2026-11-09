"""Timeline FILE capability/descriptor binding; fake clients and memory cache."""
import copy
from dataclasses import replace
import math
import unittest
from unittest.mock import Mock

import test_conversation_info_service as fixture
from maxclient.core.conversation_file_service import ConversationFileServiceMixin
from maxclient.core.timeline_file_service import TimelineFileContext, TimelineFileServiceMixin
from maxclient.core.models import Attach, Message
from maxclient.core.service import MaxService
from maxclient.protocol.client import AuthenticatedSession, ConnectionState, MaxFrame
from maxclient.protocol.conversation_contracts import parse_info_media, ConversationContractError
from maxclient.protocol.file_download_contracts import FileDownloadReply


def raw_message(**fields):
    return fixture.message_dto(attaches=[{'_type': 'FILE', 'fileId': 1.25,
        'name': 'actual-name.bin', 'size': 12.5}], **fields)


class _Service(TimelineFileServiceMixin, ConversationFileServiceMixin, fixture._Service):
    _cache_history_messages = MaxService._cache_history_messages


class TimelineFileServiceTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture.ConversationInfoServiceTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.service = _Service()
        self.service.__dict__.update(self.fixture.service.__dict__)
        self.store, self.client = self.service.store, self.service.client
        self.client.conversation_file_download = Mock(return_value=FileDownloadReply(
            'https://files.example.test/actual?signature=synthetic', False))
        self.context = self.service.timeline_file_context(303)

    def tearDown(self):
        self.client.request.assert_not_called()

    def load(self, raw=None):
        message = Message.from_server(303, raw or raw_message(), 101)
        self.store.insert_message(message)
        return message

    def descriptor(self, message, index=0):
        return self.service.get_timeline_file_descriptor(self.context, message, index)

    def test_actual_file_number_zero_fraction_and_cache_roundtrip_are_not_coerced(self):
        for index, identity in enumerate((0, 1.25)):
            raw = raw_message(identity=7+index)
            raw['attaches'][0]['fileId'] = identity
            message = self.load(raw)
            stored = self.store.message(303, message.id)
            self.assertEqual(stored.attaches[0].file_id, identity)
            self.assertEqual(stored.attaches[0].size, 12.5)
            self.assertTrue(stored.attaches[0].file_metadata_confirmed)
            self.assertTrue(self.service.timeline_file_actions(self.context, message)[0]['download_available'])
            descriptor = self.descriptor(message)
            self.assertEqual((descriptor.file_id, descriptor.file_name), (identity, 'actual-name.bin'))

    def test_default_or_guessed_type_alias_and_missing_actual_id_cannot_authorize(self):
        for index, attach in enumerate(({'fileId': 3}, {'type': 'FILE', 'fileId': 3},
                {'_type': 'file', 'fileId': 3}, {'_type': 'FILE', 'id': 3},
                {'_type': 'FILE', 'fileId': '3'}, {'_type': 'FILE', 'fileId': True})):
            raw = raw_message(identity=10+index); raw['attaches'] = [attach]
            message = self.load(raw)
            self.assertFalse(self.service.timeline_file_actions(self.context, message).get(0, {}).get('download_available', False))
            with self.assertRaises(ValueError): self.descriptor(message)
        self.client.conversation_file_download.assert_not_called()

    def test_normalized_index_after_filtered_raw_entry_selects_actual_file(self):
        raw = raw_message(); raw['attaches'].insert(0, None)
        message = self.load(raw)
        self.assertEqual(len(message.attaches), 1)
        self.assertEqual(self.descriptor(message, 0).file_id, 1.25)
        with self.assertRaises(ValueError): self.descriptor(message, 1)
        with self.assertRaises(ValueError): self.descriptor(message, True)

    def test_exact_ui_model_must_match_current_stored_source(self):
        message = self.load()
        for changed in (replace(message, text='stale edit'), replace(message, chat_id=808),
                        replace(message, status='pending')):
            self.assertFalse(self.service.timeline_file_actions(self.context, changed)[0]['download_available'])
            with self.assertRaises(ValueError): self.descriptor(changed)
        changed = copy.deepcopy(message); changed.attaches[0].file_id = 99
        with self.assertRaises(ValueError): self.descriptor(changed)
        self.client.conversation_file_download.assert_not_called()

    def test_shared_controller_resolves_bound_timeline_without_info_activation(self):
        message = self.load(); descriptor = self.descriptor(message)
        self.assertFalse(hasattr(self.service, '_conversation_files_state'))
        self.assertTrue(self.service.is_current_conversation_file(self.context, descriptor))
        result = self.service.resolve_conversation_file_download(self.context, descriptor)
        self.assertIs(result.descriptor, descriptor)
        self.client.conversation_file_download.assert_called_once_with(1.25, 303, 7,
            item_type='REGULAR', expected_session=self.context.session_context)
        self.assertFalse(hasattr(self.service, '_conversation_files_state'))

    def test_info_and_timeline_scopes_do_not_revoke_each_other(self):
        info_context = self.service.conversation_context(303)
        self.client.conversation_media.return_value = parse_info_media({'total': 1, 'messages': [raw_message()]})
        row = self.service.load_conversation_tab(info_context, 'files')['items'][0]
        info_descriptor = self.service.get_conversation_file_descriptor(info_context, row['key'])
        message = self.load(); timeline_descriptor = self.descriptor(message)
        self.assertTrue(self.service.is_current_conversation_file(info_context, info_descriptor))
        self.service.clear_conversation_files(info_context)
        self.assertTrue(self.service.is_current_conversation_file(self.context, timeline_descriptor))

    def test_chat_switch_same_chat_reopen_clear_and_new_session_expire_old_intent(self):
        message = self.load(); old_context = self.context; descriptor = self.descriptor(message)
        self.context = self.service.timeline_file_context(303)
        self.assertFalse(self.service.is_current_timeline_file_context(old_context))
        self.assertFalse(self.service.is_current_conversation_file(old_context, descriptor))
        descriptor = self.descriptor(message)
        self.service.clear_timeline_files(self.context)
        self.assertFalse(self.service.is_current_timeline_file_context(self.context))
        with self.assertRaises(ValueError): self.service.resolve_conversation_file_download(self.context, descriptor)
        self.context = self.service.timeline_file_context(303); descriptor = self.descriptor(message)
        self.client._auth_session = AuthenticatedSession(101, self.client.device_id, 'WEB', self.client._sock)
        self.assertFalse(self.service.is_current_timeline_file_context(self.context))
        with self.assertRaises(ValueError): self.service.resolve_conversation_file_download(self.context, descriptor)
        self.client.conversation_file_download.assert_not_called()

    def test_disconnected_and_reconnecting_contexts_cannot_resolve(self):
        message = self.load(); descriptor = self.descriptor(message)
        for state in (ConnectionState.DISCONNECTED, ConnectionState.RECONNECTING):
            self.client._state = state
            self.assertFalse(self.service.is_current_timeline_file_context(self.context))
            with self.assertRaises(ValueError): self.service.resolve_conversation_file_download(self.context, descriptor)
        self.client.conversation_file_download.assert_not_called()

    def test_store_edit_delete_and_late88_change_reject_old_descriptor(self):
        message = self.load(); descriptor = self.descriptor(message)
        self.store.apply_message_ack(303, {'id': 7, 'text': 'actual edit'})
        self.assertFalse(self.service.is_current_conversation_file(self.context, descriptor))
        self.assertFalse(self.service.timeline_file_actions(self.context, message)[0]['download_available'])
        message = self.store.message(303, 7); descriptor = self.descriptor(message)
        def remove(*args, **kwargs):
            self.store.delete_messages(303, [7])
            return FileDownloadReply('https://files.example.test/actual', False)
        self.client.conversation_file_download.side_effect = remove
        with self.assertRaises(ValueError): self.service.resolve_conversation_file_download(self.context, descriptor)
        self.assertEqual(self.service._timeline_files_adapter()._inflight, set())

    def test_legacy_cache_remains_unproven_until_full_server_file_refresh(self):
        legacy = Message(chat_id=303, id=7, time_ms=fixture.STAMP,
                         attaches=[Attach(type='FILE', file_id=1, file_name='legacy')])
        self.store.insert_message(legacy)
        self.assertFalse(self.service.timeline_file_actions(self.context, legacy)[0]['download_available'])
        self.store.apply_message_ack(303, {'id': 7, 'attaches': [{'_type': 'FILE', 'fileId': 1}]})
        unproven = self.store.message(303, 7)
        self.assertFalse(unproven.attaches[0].file_metadata_confirmed)
        self.service._cache_history_messages(303, [raw_message()])
        current = self.store.message(303, 7)
        self.assertTrue(self.service.timeline_file_actions(self.context, current)[0]['download_available'])
        self.assertEqual(self.descriptor(current).file_id, 1.25)

    def test_partial_ack_keeps_proven_unchanged_fields_and_schedule(self):
        raw = raw_message(delayedAttributes={'timeToFire': 100}, updateTime=200)
        message = self.load(raw)
        self.store.apply_message_ack(303, {'id': 7, 'attaches': [{'_type': 'FILE', 'fileId': 1.25}]})
        current = self.store.message(303, 7); attach = current.attaches[0]
        self.assertTrue(attach.file_metadata_confirmed)
        self.assertEqual((attach.file_item_type, attach.file_name, attach.size), ('DELAYED', 'actual-name.bin', 12.5))
        self.assertEqual(self.descriptor(current).item_type, 'DELAYED')
        self.store.apply_message_ack(303, {'id': 7, 'attaches': [{'_type': 'FILE', 'fileId': 1.25}],
                                           'delayedAttributes': {'timeToFire': 0}})
        self.assertEqual(self.descriptor(self.store.message(303, 7)).item_type, 'REGULAR')
        self.store.apply_message_ack(303, {'id': 7, 'delayedAttributes': {'timeToFire': 100}})
        self.store.apply_message_ack(303, {'id': 7, 'delayedAttributes': {'timeToFire': 0}})
        self.assertEqual(self.descriptor(self.store.message(303, 7)).item_type, 'REGULAR')

    def test_partial_ack_cannot_revive_deleted_removed_or_invalid_context(self):
        raw = raw_message(); raw['attaches'][0]['deleted'] = True
        self.load(raw)
        self.store.apply_message_ack(303, {'id': 7, 'attaches': [{'_type': 'FILE', 'fileId': 1.25}]})
        current = self.store.message(303, 7)
        self.assertTrue(current.attaches[0].deleted)
        self.assertFalse(self.service.timeline_file_actions(self.context, current)[0]['download_available'])
        for fields in ({'status': 'REMOVED'}, {'type': 'INVALID'}, {'delayedAttributes': {}},
                       {'delayedAttributes': {'timeToFire': True}}):
            self.store.apply_message_ack(303, {'id': 7, 'attaches': [{'_type': 'FILE', 'fileId': 1.25}], **fields})
            current = self.store.message(303, 7)
            self.assertFalse(current.attaches[0].file_metadata_confirmed)
            self.assertFalse(self.service.timeline_file_actions(self.context, current)[0]['download_available'])

    def test_nested_schedule_source_is_preserved_in_push_history_and_info(self):
        raw = raw_message(delayedAttributes={'timeToFire': 100.25})
        message = self.load(raw)
        self.assertEqual(self.descriptor(message).item_type, 'DELAYED')
        push = MaxService._parse_push(self.service, MaxFrame(0, 1, 128, b'', {'chatId': 303, 'message': raw}))
        self.assertEqual(push.attaches[0].file_item_type, 'DELAYED')
        page = parse_info_media({'total': 1, 'messages': [raw]})
        self.assertEqual(page['messages'][0]['delayedAttributes'], {'timeToFire': 100.25})
        info_context = self.service.conversation_context(303)
        self.client.conversation_media.return_value = page
        row = self.service.load_conversation_tab(info_context, 'files')['items'][0]
        self.assertEqual(row['item_type'], 'DELAYED')
        raw = raw_message(timeToFire=100.25)  # Unproven top-level alias is ignored.
        self.assertEqual(Message.from_server(303, raw, 101).attaches[0].file_item_type, 'REGULAR')

    def test_invalid_present_schedule_and_nonfinite_file_metadata_fail_closed(self):
        for index, attributes in enumerate(({}, None, {'timeToFire': True}, {'timeToFire': math.nan})):
            raw = raw_message(identity=20+index, delayedAttributes=attributes)
            message = self.load(raw)
            self.assertFalse(message.attaches[0].file_metadata_confirmed)
            self.assertFalse(self.service.timeline_file_actions(self.context, message)[0]['download_available'])
            with self.assertRaises(ConversationContractError): parse_info_media({'messages': [raw], 'total': 1})

    def test_reference_only_forward_without_incoming_snapshot_is_unavailable(self):
        raw = fixture.message_dto(link={'type': 'FORWARD', 'chatId': 808, 'messageId': 7})
        message = self.load(raw)
        self.assertEqual(self.service.timeline_file_actions(self.context, message), {})
        with self.assertRaises(ValueError): self.descriptor(message)
        self.client.conversation_file_download.assert_not_called()

    def test_older_or_unversioned_history_cannot_revive_newer_deleted_file(self):
        newest = raw_message(updateTime=200)
        newest['attaches'][0].update(name='newer', deleted=True)
        self.load(newest)
        older = raw_message(updateTime=100)
        older['attaches'][0].update(name='older', deleted=False)
        self.service._cache_history_messages(303, [older])
        current = self.store.message(303, 7)
        self.assertTrue(current.attaches[0].deleted)
        self.assertEqual(current.attaches[0].file_name, 'newer')
        self.assertEqual(current.attaches[0].file_update_time, 200)
        older.pop('updateTime')
        self.service._cache_history_messages(303, [older])
        current = self.store.message(303, 7)
        self.assertTrue(current.attaches[0].deleted)
        self.assertFalse(self.service.timeline_file_actions(self.context, current)[0]['download_available'])

    def test_status_only_removed_and_late_ack_cannot_restore_timeline_download(self):
        self.load(raw_message(updateTime=200))
        self.store.apply_message_ack(303, {'id': 7, 'status': 'REMOVED'})
        current = self.store.message(303, 7)
        self.assertEqual(current.status, 'removed')
        self.assertTrue(current.attaches[0].deleted)
        self.assertFalse(current.attaches[0].file_metadata_confirmed)
        self.store.apply_message_ack(303, raw_message(updateTime=100))
        self.service._cache_history_messages(303, [raw_message(updateTime=100)])
        current = self.store.message(303, 7)
        self.assertEqual(current.status, 'removed')
        self.assertFalse(self.service.timeline_file_actions(self.context, current)[0]['download_available'])

    def test_unknown_history_order_does_not_replace_proven_current_file(self):
        newest = raw_message()
        newest['attaches'][0].update(name='proven current', deleted=True)
        self.load(newest)
        older = raw_message(updateTime=100)
        older['attaches'][0].update(name='unordered older', deleted=False)
        self.service._cache_history_messages(303, [older])
        current = self.store.message(303, 7)
        self.assertEqual(current.attaches[0].file_name, 'proven current')
        self.assertTrue(current.attaches[0].deleted)

    def test_schedule_only_ack_obeys_known_version_and_updates_newer_version(self):
        self.load(raw_message(updateTime=200, delayedAttributes={'timeToFire': 100}))
        self.store.apply_message_ack(303, {'id': 7, 'updateTime': 100, 'delayedAttributes': {'timeToFire': 0}})
        attach = self.store.message(303, 7).attaches[0]
        self.assertEqual((attach.file_item_type, attach.file_update_time), ('DELAYED', 200))
        self.store.apply_message_ack(303, {'id': 7, 'updateTime': 300, 'delayedAttributes': {'timeToFire': 0}})
        attach = self.store.message(303, 7).attaches[0]
        self.assertEqual((attach.file_item_type, attach.file_update_time), ('REGULAR', 300))

    def test_older_restrictive_ack_does_not_lower_version_and_enable_later_history_revival(self):
        newest = raw_message(updateTime=200); newest['attaches'][0]['deleted'] = True
        self.load(newest)
        self.store.apply_message_ack(303, {'id': 7, 'updateTime': 100,
            'attaches': [{'_type': 'FILE', 'fileId': 1.25, 'deleted': True}]})
        self.assertEqual(self.store.message(303, 7).attaches[0].file_update_time, 200)
        self.service._cache_history_messages(303, [raw_message(updateTime=150)])
        current = self.store.message(303, 7)
        self.assertTrue(current.attaches[0].deleted)
        self.assertEqual(current.attaches[0].file_update_time, 200)
        self.assertFalse(self.service.timeline_file_actions(self.context, current)[0]['download_available'])


if __name__ == '__main__':
    unittest.main()
