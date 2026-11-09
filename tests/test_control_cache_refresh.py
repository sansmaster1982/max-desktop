"""Fresh source CONTROL metadata upgrades legacy cache without replacing rows."""
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from maxclient.core.models import Attach, Chat, Contact, Message
from maxclient.core.service import MaxService
from maxclient.core.store import Store
from maxclient.protocol.client import MaxFrame


KEY = 'welcome.saved.dialog.message'
FRIENDLY = 'Здесь можно сохранять сообщения и файлы.'


def raw_control(**changes):
    raw = {'id': 7, 'type': 'USER', 'time': 100, 'sender': 101, 'text': KEY,
           'attaches': [{'_type': 'CONTROL', 'event': 'system', 'message': KEY,
                        'shortMessage': 'Actual short metadata'}]}
    raw.update(changes)
    return raw


class ControlCacheRefreshTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(':memory:')
        self.addCleanup(self.store.close)
        self.store.bind_owner(101)
        self.service = MaxService.__new__(MaxService)
        self.service.store = self.store
        self.service.session = SimpleNamespace(my_user_id=101)
        self.service._metadata_lock = threading.RLock()
        self.service._server_metadata_owner = 101
        self.service._server_chats = {}
        self.service._server_contacts = {}
        self.service._contacts_cache = None
        self.service.client = SimpleNamespace(contact_by_phone=Mock(
            side_effect=AssertionError('LOGIN cache repair must not look up contacts')))
        self.store.upsert_chat(Chat(id=0, chat_type='DIALOG', title='Избранное'))

    def legacy(self, *, identity=7, attaches=None, **changes):
        values = dict(chat_id=0, id=identity, sender=101, text=KEY, time_ms=100,
                      outgoing=True, attaches=attaches or [Attach(type='CONTROL')])
        values.update(changes)
        model = Message(**values)
        self.store.insert_message(model)
        return model

    def test_history_refresh_upgrades_legacy_control_in_place_despite_duplicate_insert(self):
        original = self.legacy()
        self.assertIsNone(self.store.message(0, 7).control_display_text)
        self.service._cache_history_messages(0, [raw_control()])
        actual = self.store.message(0, 7)
        self.assertEqual(len(self.store.messages(0)), 1)
        self.assertEqual((actual.id, actual.sender, actual.text, actual.time_ms, actual.outgoing),
                         (original.id, original.sender, original.text, original.time_ms, original.outgoing))
        self.assertTrue(actual.attaches[0].control_metadata_confirmed)
        self.assertEqual(actual.attaches[0].control_event, 'system')
        self.assertEqual(actual.attaches[0].control_short_message, 'Actual short metadata')
        self.assertEqual(actual.control_display_text, FRIENDLY)
        self.assertEqual(self.store.get_chat(0).last_preview, FRIENDLY)

    def test_outer_key_alone_or_type_alias_never_promotes_legacy_control(self):
        self.legacy()
        for attaches in ([], [{'type': 'CONTROL', 'event': 'system', 'message': KEY}],
                         [{'_type': 'CONTROL', 'message': KEY}],
                         [{'_type': 'CONTROL', 'event': 'system', 'message': KEY, 'shortMessage': []}]):
            self.store.refresh_message_control_metadata(0, raw_control(attaches=attaches))
        actual = self.store.message(0, 7)
        self.assertFalse(actual.attaches[0].control_metadata_confirmed)
        self.assertIsNone(actual.attaches[0].control_event)

    def test_actual_non_system_metadata_is_retained_without_translation(self):
        self.legacy()
        raw = raw_control()
        raw['attaches'][0]['event'] = 'hello'
        self.store.refresh_message_control_metadata(0, raw)
        actual = self.store.message(0, 7)
        self.assertEqual(actual.attaches[0].control_event, 'hello')
        self.assertEqual(actual.attaches[0].control_message, KEY)
        self.assertFalse(actual.attaches[0].control_metadata_confirmed)
        self.assertIsNone(actual.control_display_text)
        self.assertEqual(actual.text, KEY)

    def test_different_outer_identity_text_time_sender_or_shape_does_not_upgrade(self):
        self.legacy()
        for changes in ({'id': 8}, {'text': 'Different edit'}, {'time': 101}, {'sender': 202},
                        {'attaches': [{'_type': 'PHOTO'}, raw_control()['attaches'][0]]}):
            self.store.refresh_message_control_metadata(0, raw_control(**changes))
        self.store.refresh_message_control_metadata(303, raw_control())
        actual = self.store.message(0, 7)
        self.assertFalse(actual.attaches[0].control_metadata_confirmed)
        self.assertEqual(actual.text, KEY)

    def test_removed_pending_or_unknown_rows_are_not_revived(self):
        for index, status in enumerate(('removed', 'pending', 'unknown'), 7):
            self.legacy(identity=index, status=status)
            self.store.refresh_message_control_metadata(0, raw_control(id=index))
            actual = self.store.message(0, index)
            self.assertEqual(actual.status, status)
            self.assertFalse(actual.attaches[0].control_metadata_confirmed)

    def test_malformed_full_row_and_removed_response_cannot_provide_provenance(self):
        self.legacy()
        rows = [raw_control(id=True), raw_control(time=True), raw_control(time=float('nan')),
                raw_control(time=2**63), raw_control(sender=True), raw_control(type='CONTROL'),
                raw_control(updateTime=True), raw_control(status='REMOVED')]
        for key in ('type', 'id', 'time'):
            value = raw_control(); del value[key]; rows.append(value)
        for raw in rows:
            self.store.refresh_message_control_metadata(0, raw)
        self.assertFalse(self.store.message(0, 7).attaches[0].control_metadata_confirmed)

    def test_existing_conflicting_or_proven_control_is_never_overwritten(self):
        self.legacy(attaches=[Attach(type='CONTROL', control_event='hello')])
        self.store.refresh_message_control_metadata(0, raw_control())
        self.assertEqual(self.store.message(0, 7).attaches[0].control_event, 'hello')
        proven = Message.from_server(0, raw_control(id=8), 101)
        self.store.insert_message(proven)
        newer = raw_control(id=8, updateTime=300)
        newer['attaches'][0]['message'] = 'Different actual system message'
        self.store.refresh_message_control_metadata(0, newer)
        actual = self.store.message(0, 8)
        self.assertEqual(actual.attaches[0].control_message, KEY)
        self.assertEqual(actual.control_display_text, FRIENDLY)

    def test_known_watermark_rejects_older_or_unversioned_upgrade(self):
        self.legacy(attaches=[Attach(type='CONTROL', control_update_time=200)])
        for raw in (raw_control(updateTime=100), raw_control()):
            self.store.refresh_message_control_metadata(0, raw)
            self.assertFalse(self.store.message(0, 7).attaches[0].control_metadata_confirmed)
        self.store.refresh_message_control_metadata(0, raw_control(updateTime=200))
        actual = self.store.message(0, 7)
        self.assertTrue(actual.attaches[0].control_metadata_confirmed)
        self.assertEqual(actual.attaches[0].control_update_time, 200)
        self.store.refresh_message_control_metadata(0, raw_control(updateTime=100))
        self.assertEqual(self.store.message(0, 7).attaches[0].control_update_time, 200)

    def test_first_control_order_and_unrelated_attachment_metadata_stay_intact(self):
        self.legacy(attaches=[Attach(type='CONTROL'), Attach(type='CONTROL')])
        raw = raw_control(attaches=[{'_type': 'CONTROL', 'event': 'hello', 'message': 'Other'},
                                   {'_type': 'CONTROL', 'event': 'system', 'message': KEY}])
        self.store.refresh_message_control_metadata(0, raw)
        actual = self.store.message(0, 7)
        self.assertEqual(actual.attaches[0].control_event, 'hello')
        self.assertTrue(actual.attaches[1].control_metadata_confirmed)
        self.assertIsNone(actual.control_display_text)

    def test_mixed_file_metadata_is_not_replaced_by_control_refresh(self):
        file = Attach(type='FILE', file_id=0.25, file_name='Actual.bin', size=10,
                      deleted=True, file_update_time=200, file_metadata_confirmed=False)
        self.legacy(attaches=[Attach(type='CONTROL'), file])
        raw = raw_control(attaches=[raw_control()['attaches'][0],
            {'_type': 'FILE', 'fileId': 0.25, 'name': 'Actual.bin', 'size': 10,
             'deleted': True}], updateTime=200)
        self.store.refresh_message_control_metadata(0, raw)
        actual = self.store.message(0, 7)
        self.assertTrue(actual.attaches[0].control_metadata_confirmed)
        self.assertEqual(actual.attaches[1], file)
        raw['attaches'][1]['fileId'] = 99
        self.store.refresh_message_control_metadata(0, raw)
        self.assertEqual(self.store.message(0, 7).attaches[1], file)

    def test_older_file_watermark_also_blocks_control_promotion(self):
        file = Attach(type='FILE', file_id=0.25, file_name='Actual.bin', size=10,
                      file_update_time=200, file_metadata_confirmed=True)
        self.legacy(attaches=[Attach(type='CONTROL'), file])
        raw = raw_control(attaches=[raw_control()['attaches'][0],
            {'_type': 'FILE', 'fileId': 0.25, 'name': 'Actual.bin', 'size': 10}], updateTime=100)
        self.store.refresh_message_control_metadata(0, raw)
        self.assertFalse(self.store.message(0, 7).attaches[0].control_metadata_confirmed)

    def test_older_history_does_not_move_preview_and_rejected_proof_is_not_previewed(self):
        self.legacy(attaches=[Attach(type='CONTROL', control_event='hello')])
        self.store.update_chat_preview(0, 100, 'Current preview')
        self.service._cache_history_messages(0, [raw_control()])
        self.assertEqual(self.store.get_chat(0).last_preview, KEY)
        self.store.update_chat_preview(0, 200, 'Newer actual preview')
        self.service._cache_history_messages(0, [raw_control()])
        self.assertEqual(self.store.get_chat(0).last_preview, 'Newer actual preview')
        self.assertEqual(self.store.get_chat(0).last_time_ms, 200)

    def test_new_control_watermark_roundtrips_only_actual_full_source(self):
        full = Message.from_server(0, raw_control(updateTime=200), 101)
        self.store.insert_message(full)
        self.assertEqual(self.store.message(0, 7).attaches[0].control_update_time, 200)
        partial = Message.from_server(0, {'id': 8, 'updateTime': 200,
            'attaches': [{'_type': 'CONTROL', 'event': 'system', 'message': KEY}]}, 101)
        self.assertIsNone(partial.attaches[0].control_update_time)

    def test_composed_history_file_refresh_never_overwrites_proven_control(self):
        original_raw = raw_control(updateTime=200, attaches=[raw_control()['attaches'][0],
            {'_type': 'FILE', 'fileId': 25, 'name': 'Synthetic.bin', 'size': 1}])
        original = Message.from_server(0, original_raw, 101)
        self.store.insert_message(original)
        incoming = raw_control(updateTime=300, attaches=[
            {'_type': 'CONTROL', 'event': 'system', 'message': 'Different synthetic control'},
            {'_type': 'FILE', 'fileId': 25, 'name': 'Updated synthetic.bin', 'size': 2}])
        self.service._cache_history_messages(0, [incoming])
        actual = self.store.message(0, 7)
        self.assertEqual(actual.attaches[0], original.attaches[0])
        self.assertEqual(actual.control_display_text, FRIENDLY)
        self.assertEqual(actual.attaches[1].file_name, 'Updated synthetic.bin')
        self.assertEqual(actual.attaches[1].file_update_time, 300)
        self.assertEqual(self.store.get_chat(0).last_preview, FRIENDLY)

    def test_composed_history_refresh_cannot_revive_removed_mixed_row(self):
        original_raw = raw_control(updateTime=200, attaches=[raw_control()['attaches'][0],
            {'_type': 'FILE', 'fileId': 25, 'name': 'Synthetic.bin', 'size': 1}])
        original = Message.from_server(0, original_raw, 101)
        original.status = 'removed'
        self.store.insert_message(original)
        self.store.update_chat_preview(0, 100, 'Current removed preview')
        incoming = raw_control(updateTime=300, attaches=[
            {'_type': 'CONTROL', 'event': 'system', 'message': 'Different synthetic control'},
            {'_type': 'FILE', 'fileId': 25, 'name': 'New.bin', 'size': 2}])
        self.service._cache_history_messages(0, [incoming])
        actual = self.store.message(0, 7)
        self.assertEqual(actual.status, 'removed')
        self.assertEqual(actual.attaches, original.attaches)
        self.assertEqual(self.store.get_chat(0).last_preview, 'Current removed preview')

    def test_newer_changed_file_layout_disables_old_proof_without_guessing_deleted(self):
        original_raw = raw_control(updateTime=200, attaches=[
            {'_type': 'FILE', 'fileId': 25, 'name': 'Synthetic.bin', 'size': 1}])
        self.store.insert_message(Message.from_server(0, original_raw, 101))
        # An older empty layout cannot invalidate a known newer resource.
        self.service._cache_history_messages(0, [raw_control(updateTime=100, attaches=[])])
        self.assertTrue(self.store.message(0, 7).attaches[0].file_metadata_confirmed)
        self.service._cache_history_messages(0, [raw_control(updateTime=300, attaches=[])])
        actual = self.store.message(0, 7).attaches[0]
        self.assertFalse(actual.file_metadata_confirmed)
        self.assertFalse(actual.deleted)
        self.assertEqual((actual.file_id, actual.file_name, actual.file_update_time),
                         (25, 'Synthetic.bin', 300))
        # The invalidation watermark prevents a delayed full row reviving it.
        self.service._cache_history_messages(0, [original_raw])
        self.assertFalse(self.store.message(0, 7).attaches[0].file_metadata_confirmed)


class LoginSelfContactRepairTests(unittest.TestCase):
    setUp = ControlCacheRefreshTests.setUp

    def ingest(self, profile_id=101):
        self.service._ingest_login(MaxFrame(1, 1, 19, b'', {'profile': {'contact': {
            'id': profile_id, 'names': [{'type': 'ONEME', 'firstName': 'Synthetic', 'lastName': 'Owner'}],
            'phone': 79990000000, 'description': 'Actual about'}}}), my_id=101)

    def test_existing_own_contact_is_repaired_from_confirmed_login_without_lookup(self):
        self.store.upsert_contact(Contact(101, name="[{'firstName': 'Legacy repr'}]"))
        self.ingest()
        contact = self.store.list_contacts()[0]
        self.assertEqual((contact.id, contact.name), (101, 'Synthetic Owner'))
        self.assertEqual(self.service._server_contacts[101]['description'], 'Actual about')
        self.assertEqual([chat.id for chat in self.store.list_chats()], [0])
        self.service.client.contact_by_phone.assert_not_called()

    def test_login_does_not_create_a_new_self_contact_or_chat(self):
        self.ingest()
        self.assertEqual(self.store.list_contacts(), [])
        self.assertEqual([chat.id for chat in self.store.list_chats()], [0])
        self.service.client.contact_by_phone.assert_not_called()

    def test_other_profile_id_cannot_repair_owner_contact(self):
        self.store.upsert_contact(Contact(101, name='Existing own label'))
        self.ingest(profile_id=202)
        self.assertEqual(self.store.list_contacts()[0].name, 'Existing own label')
        self.assertNotIn(202, self.service._server_contacts)
