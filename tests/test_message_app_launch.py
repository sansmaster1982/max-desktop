"""Ordinary mini-app launch with fake transport and synthetic inline metadata."""
import copy
from dataclasses import replace
import unittest
from unittest.mock import Mock

import test_conversation_info_service as fixture
from maxclient.core.message_app_service import MessageAppServiceMixin
from maxclient.core.timeline_file_service import TimelineFileServiceMixin
from maxclient.core.models import Attach, Message
from maxclient.core.service import MaxService
from maxclient.protocol.client import (AuthenticatedSession, ConnectionState, MaxClient, MaxFrame,
                                       MaxError, MaxTimeout, MaxWriteNotStarted)
from maxclient.protocol.miniapp_contracts import (MiniAppReply, build_miniapp_launch,
                                                miniapp_origin, parse_miniapp_launch)


URL = 'https://app.example.test/view#WebAppData=query_id%3Dsynthetic-query%26hash%3Dsynthetic'


def raw_message():
    return fixture.message_dto(sender=202, updateTime=fixture.STAMP, attaches=[{
        '_type': 'INLINE_KEYBOARD', 'callbackId': 'synthetic-callback', 'keyboard': {'buttons': [[
            {'type': 'OPEN_APP', 'text': 'View supplied content', 'contactId': 202, 'payload': 'literal-start'}]]}}])


class _Service(MessageAppServiceMixin, TimelineFileServiceMixin, fixture._Service):
    _cache_history_messages = MaxService._cache_history_messages


class MessageAppLaunchTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture.ConversationInfoServiceTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.service = _Service()
        self.service.__dict__.update(self.fixture.service.__dict__)
        self.store, self.client = self.service.store, self.service.client
        self.client.contact_info.return_value = {'contacts': [
            {'id': 202, 'flags': 18, 'webApp': 'https://app.example.test/'}]}
        self.client.open_bot_app = Mock(return_value=parse_miniapp_launch({'url': URL, 'query_id': 'synthetic-query'}))
        self.message = Message.from_server(303, raw_message(), 101)
        self.store.insert_message(self.message)
        self.context = self.service.timeline_file_context(303)

    def launch(self, message=None, **kwargs):
        return self.service.open_message_app(message or self.message, 0, 0, 0, context=kwargs.get('context', self.context))

    def test_exact_literal_launch_on_current_session_does_not_reauth_or_send(self):
        before = self.store._db.total_changes
        result = self.launch()
        self.assertEqual(result, {'url': URL, 'origin': 'https://app.example.test', 'query_id': 'synthetic-query'})
        self.client.contact_info.assert_called_once_with([202], expected_session=self.context.session_context)
        self.client.open_bot_app.assert_called_once_with(202, 303, 'literal-start', expected_session=self.context.session_context)
        self.client.chat_info.assert_not_called()
        self.client.request.assert_not_called()
        self.assertEqual(self.store._db.total_changes, before)

    def test_native_derived_dialog_is_only_candidate_until_server_confirms_actual_peer(self):
        self.service._server_chats = {}
        candidate = 101 ^ 202
        self.client.chat_info.return_value = {'chats': [fixture.chat_dto(identity=candidate)]}
        self.launch()
        self.client.chat_info.assert_called_once_with([candidate], expected_session=self.context.session_context)
        self.client.open_bot_app.assert_called_once_with(202, candidate, 'literal-start', expected_session=self.context.session_context)

    def test_unconfirmed_candidate_or_other_bot_cannot_be_used(self):
        self.service._server_chats = {}
        self.client.chat_info.return_value = {'chats': [fixture.chat_dto(identity=101 ^ 202,
            participants={'101': 0, '999': 0})]}
        with self.assertRaises(ValueError): self.launch()
        self.client.open_bot_app.assert_not_called()

    def test_missing_bot_flags_webapp_or_matching_contact_stops_before160(self):
        for contact in ({'id': 202, 'flags': 2, 'webApp': 'url'}, {'id': 202, 'flags': 18},
                        {'id': 999, 'flags': 18, 'webApp': 'url'}, {'id': 202, 'flags': True, 'webApp': 'url'},
                        {'id': 202, 'flags': 18, 'webApp': 'url', 'accountStatus': 2}):
            self.client.contact_info.return_value = {'contacts': [contact]}
            with self.assertRaises(ValueError): self.launch()
        self.client.open_bot_app.assert_not_called()

    def test_closed_context_changed_owner_or_mutated_button_stop_before_any_request(self):
        for mutation in ('closed', 'changed', 'legacy'):
            context = self.service.timeline_file_context(303)
            candidate = copy.deepcopy(self.message)
            if mutation == 'closed': self.service.clear_timeline_files(context)
            elif mutation == 'changed': candidate.attaches[0].inline_buttons[0][0]['payload'] = 'changed'
            else: candidate.attaches[0].inline_open_app_metadata_confirmed = False
            with self.assertRaises(ValueError): self.launch(candidate, context=context)
        self.client.contact_info.assert_not_called()
        self.client.open_bot_app.assert_not_called()

    def test_late_contact_response_or160_reply_is_rejected_after_context_switch(self):
        for stage in ('contact', 'launch'):
            self.context = self.service.timeline_file_context(303)
            original = self.client.contact_info if stage == 'contact' else self.client.open_bot_app
            result = original.return_value
            def late(*args, **kwargs):
                self.service.clear_timeline_files(self.context)
                return result
            original.side_effect = late
            with self.assertRaises(ValueError): self.launch()
            original.side_effect = None
            if stage == 'contact': self.client.open_bot_app.assert_not_called()

    def test_edited_removed_and_swapped_transport_late_replies_are_not_returned(self):
        for mutation in ('edit', 'remove', 'transport'):
            if mutation != 'edit':
                self.store._db.execute('UPDATE messages SET status="sent",text=? WHERE server_id=7', (self.message.text,))
            self.context = self.service.timeline_file_context(303)
            def late(*args, **kwargs):
                if mutation == 'edit':
                    self.store._db.execute('UPDATE messages SET text="changed" WHERE server_id=7')
                elif mutation == 'remove':
                    self.store._db.execute('UPDATE messages SET status="removed" WHERE server_id=7')
                else:
                    self.client._auth_session = AuthenticatedSession(101, self.client.device_id, 'WEB', self.client._sock)
                return parse_miniapp_launch({'url': URL, 'query_id': 'synthetic'})
            self.client.open_bot_app.side_effect = late
            with self.assertRaises(ValueError): self.launch()

    def test_no_parallel_or_automatic_repeat_after_timeout(self):
        def reentrant(*args, **kwargs):
            with self.assertRaises(ValueError): self.launch()
            raise MaxTimeout('synthetic-timeout')
        self.client.open_bot_app.side_effect = reentrant
        with self.assertRaises(MaxTimeout): self.launch()
        self.assertEqual(self.client.open_bot_app.call_count, 1)
        self.assertEqual(self.service._message_app_inflight, set())

    def test_bad_response_is_not_repaired_from_contact_webapp_or_start_parameter(self):
        self.client.open_bot_app.return_value = {'url': URL, 'query_id': 'synthetic'}
        with self.assertRaises(ValueError): self.launch()
        self.client.open_bot_app.assert_called_once()

    def test_proven_old_labels_upgrade_once_from_exact_same_version_source(self):
        legacy = self.store.message(303, 7)
        legacy.attaches[0].inline_open_app_metadata_confirmed = False
        legacy.attaches[0].inline_buttons = [[{'type': 'OPEN_APP', 'text': 'View supplied content'}]]
        import json
        self.store._db.execute('UPDATE messages SET attaches=? WHERE server_id=7',
                               (json.dumps([a.__dict__ for a in legacy.attaches]),))
        self.assertFalse(self.service.is_current_message_app(legacy, 0, 0, 0, context=self.context))
        self.store.refresh_message_display_metadata(303, raw_message())
        upgraded = self.store.message(303, 7)
        self.assertTrue(upgraded.attaches[0].inline_open_app_metadata_confirmed)
        self.assertEqual(upgraded.attaches[0].inline_buttons[0][0]['contactId'], 202)
        self.assertTrue(self.service.is_current_message_app(upgraded, 0, 0, 0, context=self.context))
        self.assertEqual(len(self.store.messages(303)), 1)

    def test_label_upgrade_rejects_changed_label_or_existing_conflicting_action_fields(self):
        import json
        for previous in ([[{'type': 'OPEN_APP', 'text': 'Different edit'}]],
                         [[{'type': 'OPEN_APP', 'text': 'View supplied content', 'contactId': 999}]]):
            legacy = copy.deepcopy(self.message)
            legacy.attaches[0].inline_open_app_metadata_confirmed = False
            legacy.attaches[0].inline_buttons = previous
            self.store._db.execute('UPDATE messages SET attaches=? WHERE server_id=7',
                                   (json.dumps([a.__dict__ for a in legacy.attaches]),))
            self.store.refresh_message_display_metadata(303, raw_message())
            self.assertFalse(self.store.message(303,7).attaches[0].inline_open_app_metadata_confirmed)

    def test_optional_open_app_fields_require_actual_typed_values(self):
        for identity in ('202', True, 2**63):
            raw = raw_message()
            raw['attaches'][0]['keyboard']['buttons'][0][0]['contactId'] = identity
            parsed = Message.from_server(303,raw,101)
            self.assertFalse(parsed.attaches[0].inline_metadata_confirmed)
        raw = raw_message()
        del raw['attaches'][0]['keyboard']['buttons'][0][0]['contactId']
        parsed = Message.from_server(303,raw,101)
        self.assertTrue(parsed.attaches[0].inline_metadata_confirmed)
        self.assertFalse(self.service.is_current_message_app(parsed,0,0,0,context=self.context))


class MiniAppProtocolTests(unittest.TestCase):
    def client(self, opcode=160, data=None):
        client = MaxClient(auto_reconnect=False)
        client._sock = Mock()
        client._device_type = 'WEB'; client._state = ConnectionState.CONNECTED
        client._auth_session = AuthenticatedSession(101, client.device_id, 'WEB', client._sock)
        client.request = Mock(return_value=MaxFrame(1, 1, opcode, b'', data))
        return client

    def test_payload_exact_actual_integer_ids_optional_literal_start_and_no_token(self):
        self.assertEqual(build_miniapp_launch(202, 303, '000123'),
                         {'botId': 202, 'chatId': 303, 'startParam': '000123'})
        self.assertEqual(build_miniapp_launch(202, 303, ''), {'botId': 202, 'chatId': 303})
        for bot, chat in ((True,303), ('202',303), (0,303), (202,2**63)):
            with self.assertRaises(ValueError): build_miniapp_launch(bot,chat)
        for value in ([], 123):
            with self.assertRaises(ValueError): build_miniapp_launch(202,303,value)

    def test_returned_url_hash_is_preserved_without_initdata_reconstruction(self):
        reply = parse_miniapp_launch({'url': URL, 'query_id': 'actual-query'})
        self.assertEqual(reply.url, URL)
        self.assertEqual(reply.origin, 'https://app.example.test')
        self.assertNotIn('synthetic', repr(reply))
        self.assertNotIn('actual-query', repr(reply))
        for bad in ('http://example.test', 'file:///x', 'https://127.0.0.1/x',
                    'https://127.1/x', 'https://0177.0.0.1/x', 'https://0x7f.0.0.1/x',
                    'https://user:pass@example.test/x', 'https://example.test:8080/x',
                    'https://example.test/\nfoo', 'https://localhost/x'):
            with self.assertRaises(ValueError): miniapp_origin(bad)
        for query in (None, True, 10, []):
            with self.assertRaises(ValueError): parse_miniapp_launch({'url':URL,'query_id':query})

    def test_typed_wrapper_bound_one160_and_sanitized_errors(self):
        client = self.client(data={'url':URL,'query_id':'synthetic'})
        result = client.open_bot_app(202,303,'literal',expected_session=client.authenticated_session)
        self.assertIsInstance(result,MiniAppReply)
        client.request.assert_called_once_with(160,{'botId':202,'chatId':303,'startParam':'literal'},
                                               expected_session=client.authenticated_session)
        client.request.return_value = MaxFrame(3,1,160,b'',{'error':'synthetic-private-error'})
        with self.assertRaises(MaxError) as error:
            client.open_bot_app(202,303,expected_session=client.authenticated_session)
        self.assertNotIn('synthetic-private',str(error.exception))
        client.request.reset_mock()
        with self.assertRaises(MaxWriteNotStarted): client.open_bot_app(202,303,expected_session=None)
        client.request.assert_not_called()

    def test_transport_replaced_at_send_handoff_cannot_send_to_either_socket(self):
        client = self.client(); old, session = client._sock, client.authenticated_session
        new = Mock()
        def handoff(*args, **kwargs):
            client._sock = new
            client._auth_session = AuthenticatedSession(101,client.device_id,'WEB',new)
            return MaxClient.request(client,*args,**kwargs)
        client.request = handoff
        with self.assertRaises(MaxWriteNotStarted): client.open_bot_app(202,303,expected_session=session)
        old.sendall.assert_not_called(); new.sendall.assert_not_called()


if __name__ == '__main__': unittest.main()
