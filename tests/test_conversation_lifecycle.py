"""Synthetic More flows: single write, exact scope, readback and late data."""
import copy
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from maxclient.core.service import MaxService
from maxclient.core.store import Store
from maxclient.core.models import Chat, Message
from maxclient.protocol.client import (MaxClient, MaxFrame, AuthenticatedSession,
    ConnectionState, MaxError, MaxWriteOutcomeUnknown, MaxTimeout)
from maxclient.protocol.conversation_lifecycle_contracts import (
    build_chat_lifecycle, chat_sync_time, parse_lifecycle_ack, build_contact_share)


def chat(**fields):
    return {'id': 303, 'type': 'DIALOG', 'status': 'ACTIVE', 'owner': 0,
        'participants': {'101': 1, '202': 1}, 'created': 10, 'modified': 80,
        'lastEventTime': 100, 'lastMessage': {'id': 9, 'time': 90, 'type': 'USER', 'text': 'old'}, **fields}


class ConversationLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.service = object.__new__(MaxService)
        s = self.service
        s.store = Store(':memory:')
        self.addCleanup(s.store.close)
        s.store.bind_owner(101)
        s.session = SimpleNamespace(my_user_id=101, device_id='synthetic-device')
        s._metadata_lock = threading.RLock()
        s._server_metadata_owner = 101
        s._server_chats = {303: chat()}
        self.contact = {'id': 202, 'names': [{'name': 'Synthetic', 'type': 'ONEME'}], 'updateTime': 1, 'flags': 512}
        s._server_contacts = {202: self.contact}
        s._chat_fetch_ts = {}; s._chat_fetch_anchor = {}
        s._invalidate_contacts = Mock()
        s._contacts_cache = None
        s.on_chat_changed = Mock(); s.on_config_changed = Mock(); s.log = Mock()
        s.store.upsert_chat(Chat(id=303, title='Synthetic', chat_type='DIALOG', peer_user_id=202))
        s.store.insert_message(Message(chat_id=303, id=9, time_ms=90, text='old'))
        s.store.update_chat_preview(303, 90, 'old')
        c = s.client = MaxClient(device_id='synthetic-device', auto_reconnect=False)
        c._sock = object(); c._state = ConnectionState.CONNECTED; c._device_type = 'WEB'
        c._auth_session = AuthenticatedSession(101, c.device_id, 'WEB', c._sock)
        c.request = Mock(side_effect=AssertionError('Network forbidden'))
        self.remote = chat()
        c.chat_info = Mock(side_effect=lambda *a, **k: {'chats': [copy.deepcopy(self.remote)]} if self.remote else {'chats': []})
        c.contact_info = Mock(side_effect=lambda *a, **k: {'contacts': [copy.deepcopy(self.contact)]})
        c.change_conversation_lifecycle = Mock(side_effect=self.accept)
        self.context = s.conversation_context(303, activate_files=False)

    def accept(self, action, chat_id, cutoff, for_all, **kwargs):
        self.remote.pop('lastMessage', None)
        if action == 'delete':
            self.remote['status'] = 'REMOVED'

    def tearDown(self):
        self.service.client.request.assert_not_called()

    def test_clear_one54_preserves_other_chat_and_pending_and_newer_messages(self):
        s = self.service
        s.store.insert_messages([Message(chat_id=303, id=10, time_ms=120, text='new'),
            Message(chat_id=303, time_ms=50, text='unsent', local_id='pending', status='pending'),
            Message(chat_id=404, id=9, time_ms=80, text='other')])
        result = s.clear_conversation_history(self.context)
        self.assertTrue(result['confirmed'])
        s.client.change_conversation_lifecycle.assert_called_once_with('clear', 303, 100, False,
            expected_session=self.context.session_context)
        self.assertEqual([m.text for m in s.store.messages(303)], ['unsent', 'new'])
        self.assertIsNotNone(s.store.message(404, 9))

    def test_unknown_persists_and_refresh_only_reads_then_unlocks(self):
        s = self.service
        s.client.change_conversation_lifecycle.side_effect = MaxWriteOutcomeUnknown('synthetic')
        with self.assertRaises(MaxWriteOutcomeUnknown): s.clear_conversation_history(self.context)
        self.assertTrue(s.conversation_lifecycle_status(self.context)['pending'])
        with self.assertRaises(ValueError): s.clear_conversation_history(self.context)
        self.assertFalse(s.refresh_conversation_lifecycle(self.context)['confirmed'])
        self.remote.pop('lastMessage')
        self.assertTrue(s.refresh_conversation_lifecycle(self.context)['confirmed'])
        s.client.change_conversation_lifecycle.assert_called_once()

    def test_structured_rejection_releases_without_erasing(self):
        s = self.service
        s.client.change_conversation_lifecycle.side_effect = MaxError('rejected')
        with self.assertRaises(MaxError): s.delete_conversation(self.context)
        self.assertFalse(s.conversation_lifecycle_status(self.context)['pending'])
        self.assertIsNotNone(s.store.message(303, 9))

    def test_delete_hides_conversation_but_not_contact_and_new_message_revives(self):
        s = self.service
        result = s.delete_conversation(self.context)
        self.assertTrue(result['confirmed'])
        self.assertTrue(s.store.history_boundary(303)['deleted'])
        self.assertEqual(s.list_chats(), [])
        self.assertIn(202, s._server_contacts)
        s.store.insert_message(Message(chat_id=303, id=11, time_ms=130, text='later'))
        s.store.update_chat_preview(303, 130, 'later')
        self.assertEqual([x.id for x in s.list_chats()], [303])

    def test_void_ack_plus_stale_readback_is_pending(self):
        s = self.service
        s.client.change_conversation_lifecycle.side_effect = None
        s.client.change_conversation_lifecycle.return_value = None
        result = s.clear_conversation_history(self.context)
        self.assertTrue(result['accepted']); self.assertFalse(result['confirmed'])
        self.assertIsNotNone(s.store.message(303, 9))

    def test_late_history_and_preview_cannot_restore_cleared_messages(self):
        s = self.service
        s.clear_conversation_history(self.context)
        s._cache_history_messages(303, [chat()['lastMessage']])
        s.store.insert_message(Message(chat_id=303, id=9, time_ms=90, text='old'))
        s.store.update_chat_preview(303, 90, 'old', 1)
        s.store.upsert_chat(Chat(id=303, last_time_ms=90, last_preview='old', unread=4))
        self.assertEqual(s.store.messages(303), [])
        self.assertIsNone(s.store.get_chat(303).last_preview)
        self.assertEqual(s.store.get_chat(303).unread, 0)

    def test_changed_session_before_write_no_mutation(self):
        s = self.service
        old = s.client.contact_info.side_effect
        def switch(*args, **kwargs):
            result = old(*args, **kwargs)
            s.client._auth_session = AuthenticatedSession(101, s.client.device_id, 'WEB', s.client._sock)
            return result
        s.client.contact_info.side_effect = switch
        with self.assertRaises(ValueError): s.clear_conversation_history(self.context)
        s.client.change_conversation_lifecycle.assert_not_called()

    def test_inflight_refresh_cannot_release_reservation(self):
        s = self.service
        def write(*args, **kwargs):
            self.assertFalse(s.refresh_conversation_lifecycle(self.context)['confirmed'])
            with self.assertRaises(ValueError): s.delete_conversation(self.context)
            self.accept(*args, **kwargs)
        s.client.change_conversation_lifecycle.side_effect = write
        self.assertTrue(s.clear_conversation_history(self.context)['confirmed'])
        s.client.change_conversation_lifecycle.assert_called_once()

    def test_scope_cannot_silently_expand(self):
        with self.assertRaises(ValueError): self.service.clear_conversation_history(self.context, for_all=True)
        self.service.client.change_conversation_lifecycle.assert_not_called()
        self.service.store.merge_config(101, {'server': {'delete-for-all': True}})
        self.assertTrue(self.service.clear_conversation_history(self.context, for_all=True)['confirmed'])

    def test_share_actual_contact_to_captured_destination_once(self):
        s = self.service
        s.store.upsert_chat(Chat(id=0, chat_type='DIALOG', peer_user_id=101))
        s._server_chats[0] = chat(id=0, participants={'101': 1})
        s._server_contacts[101] = {'id': 101}
        saved_raw = copy.deepcopy(s._server_chats[0])
        s.client.chat_info.side_effect = lambda ids, **_: {'chats': [copy.deepcopy(
            saved_raw if ids == [0] else self.remote)]}
        s.client.contact_info.side_effect = lambda ids, **_: {'contacts': [copy.deepcopy(
            {'id': 101, 'names': [{'name': 'Self', 'type': 'ONEME'}], 'updateTime': 1}
            if ids == [101] else self.contact)]}
        target = s.conversation_context(0, activate_files=False)
        def ack(chat_id, contact_id, cid, **kwargs):
            self.assertEqual((chat_id, contact_id), (0,202))
            return {'chatId': 0, 'message': {'id': 31,'time': 150,'type':'USER','cid':cid,
                'attaches':[{'_type':'CONTACT','contactId':202}]}}
        s.client.share_contact_message = Mock(side_effect=ack)
        self.assertTrue(s.share_conversation_contact(self.context, target)['confirmed'])
        s.client.share_contact_message.assert_called_once()
        self.assertIsNotNone(s.store.message(0,31))

    def test_share_unknown_is_durable_and_not_repeatable(self):
        s = self.service
        s.client.share_contact_message = Mock(side_effect=MaxWriteOutcomeUnknown('unknown'))
        with self.assertRaises(MaxWriteOutcomeUnknown): s.share_conversation_contact(self.context, self.context)
        with self.assertRaises(ValueError): s.share_conversation_contact(self.context, self.context)
        self.assertTrue(s.refresh_conversation_lifecycle(self.context)['pending'])
        s.client.share_contact_message.assert_called_once()


class ConversationLifecycleContractsTests(unittest.TestCase):
    def test_schema_and_clock_from_actual_chat(self):
        self.assertEqual(chat_sync_time(chat(modified=120)), 120)
        self.assertEqual(build_chat_lifecycle(303, 100.5, False),
                         {'chatId':303,'lastEventTime':100.5,'forAll':False})
        for args in [(True,1,False),(303,float('nan'),False),(303,1,1)]:
            with self.assertRaises(ValueError): build_chat_lifecycle(*args)
        with self.assertRaises(ValueError): chat_sync_time({})
        for data in ({},[],True):
            with self.assertRaises(ValueError): parse_lifecycle_ack(data)

    def test_exact_contact_serializer_contains_no_phone_or_name(self):
        self.assertEqual(build_contact_share(303,202,1), {'chatId':303,'message':{
            'cid':1,'text':'','attaches':[{'_type':'CONTACT','contactId':202}]},'notify':True})

    def test_wrappers_exact_opcodes_and_unknown_never_retry(self):
        c = MaxClient(auto_reconnect=False)
        c._sock=object(); c._state=ConnectionState.CONNECTED; c._device_type='WEB'
        c._auth_session=AuthenticatedSession(101,c.device_id,'WEB',c._sock)
        ctx=c.authenticated_session
        c.request=Mock(return_value=MaxFrame(1,1,54,b'',None))
        for action,opcode in [('clear',54),('delete',52)]:
            c.request.reset_mock()
            c.change_conversation_lifecycle(action,303,100,False,expected_session=ctx)
            c.request.assert_called_once_with(opcode,{'chatId':303,'lastEventTime':100,'forAll':False},expected_session=ctx)
        c.request.side_effect=MaxTimeout('synthetic')
        c.request.reset_mock()
        with self.assertRaises(MaxWriteOutcomeUnknown): c.change_conversation_lifecycle('clear',303,100,False,expected_session=ctx)
        c.request.assert_called_once()


if __name__ == '__main__':
    unittest.main()
