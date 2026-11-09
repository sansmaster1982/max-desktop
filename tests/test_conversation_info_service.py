"""Detached Info reads with real in-memory cache and synthetic clients only."""
import copy
from dataclasses import FrozenInstanceError
import json
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from maxclient.core.conversation_info_service import (
    ConversationContext, ConversationInfoServiceMixin, ConversationUnavailable,
)
from maxclient.core.models import Chat, Contact
from maxclient.core.service import MaxService
from maxclient.core.store import Store
from maxclient.protocol.client import AuthenticatedSession, ConnectionState, MaxClient


STAMP = 1_790_000_000_000.25
DEVICE = '00112233-4455-6677-8899-aabbccddeeff'


def chat_dto(identity=303, **fields):
    return {'id': identity, 'type': 'DIALOG', 'status': 'ACTIVE', 'owner': 101,
            'participants': {'101': 0, '202': 0}, 'created': 0,
            'lastEventTime': STAMP, 'title': 'Synthetic server chat',
            'lastMessage': message_dto(), **fields}


def message_dto(identity=7, timestamp=STAMP, **fields):
    return {'id': identity, 'type': 'USER', 'time': timestamp, 'text': 'Synthetic text', **fields}


class _Socket:
    def sendall(self, _frame):
        raise AssertionError('No actual requests allowed')


class _Service(ConversationInfoServiceMixin):
    _confirmed_profile_id = MaxService._confirmed_profile_id
    _history_read_context = MaxService._history_read_context
    _require_history_context = MaxService._require_history_context

    @property
    def my_id(self):
        return self.session.my_user_id


class ConversationInfoServiceTests(unittest.TestCase):
    def test_self_dialog_uses_confirmed_viewer_fallback_without_chat_id_derivation(self):
        from maxclient.core.conversation_info_service import _peer
        saved = Chat(id=909, chat_type='DIALOG', peer_user_id=101)
        for participants in ({'101': 0}, {}):
            self.assertEqual(_peer(saved, {'type': 'DIALOG', 'participants': participants}, 101), 101)
        self.assertEqual(_peer(saved, {'type': 'DIALOG'}, 101), 101)

    def setUp(self):
        self.store = Store(':memory:')
        self.addCleanup(self.store.close)
        self.store.bind_owner(101)
        self.store.upsert_chat(Chat(id=303, title='Cached name', chat_type='DIALOG', peer_user_id=202))
        self.store.upsert_contact(Contact(id=202, name='Cached peer', phone='+70000000000'))
        self.service = _Service()
        self.service.store = self.store
        self.service.session = SimpleNamespace(my_user_id=101, device_id=DEVICE)
        self.service._metadata_lock = threading.RLock()
        self.service._server_metadata_owner = 101
        self.service._server_chats = {303: chat_dto()}
        self.service._server_contacts = {202: {'id': 202, 'updateTime': STAMP,
            'names': [{'name': '<b>Literal peer</b>', 'type': 'ONEME'}], 'phone': 70000000001,
            'description': '<i>Literal description</i>'}}
        self.client = MaxClient(device_id=DEVICE, auto_reconnect=False)
        self.client._device_type = 'WEB'
        self.client._sock = _Socket()
        self.client._state = ConnectionState.CONNECTED
        self.client._auth_session = AuthenticatedSession(101, DEVICE, 'WEB', self.client._sock)
        self.client.request = Mock(side_effect=AssertionError('No account RPC transport'))
        self.client.chat_info = Mock(return_value={'chats': [chat_dto()]})
        self.client.contact_info = Mock(return_value={'contacts': [copy.deepcopy(self.service._server_contacts[202])]})
        self.client.common_chats = Mock(return_value={'commonChats': [], 'hasMore': False})
        self.client.conversation_media = Mock(return_value={'messages': [], 'total': 0})
        self.service.client = self.client
        self.service.calls_available = Mock(return_value=True)
        self.service.rename_conversation = Mock(side_effect=AssertionError('No renames during reads'))
        self.context = self.service.conversation_context(303)
        self.changes = self.store._db.total_changes
        self.metadata = copy.deepcopy(self.service._server_chats)

    def tearDown(self):
        self.client.request.assert_not_called()
        self.service.rename_conversation.assert_not_called()
        self.assertEqual(self.store._db.total_changes, self.changes)
        self.assertEqual(self.service._server_chats, self.metadata)

    def replace_context(self):
        self.client._auth_session = AuthenticatedSession(101, DEVICE, 'WEB', self.client._sock)

    def test_snapshot_uses_actual_peer_and_known_fields_without_presence_defaults(self):
        snapshot = self.service.conversation_snapshot(self.context)
        self.assertIs(snapshot['context'], self.context)
        self.assertEqual((snapshot['chat_id'], snapshot['peer_id']), (303, 202))
        self.assertEqual(snapshot['title'], '<b>Literal peer</b>')
        self.assertEqual(snapshot['description'], '<i>Literal description</i>')
        self.assertEqual(snapshot['phone'], '70000000001')
        self.assertNotIn('presence', snapshot)
        self.assertNotIn('muted', snapshot)
        self.assertEqual(snapshot['availability'], {'chat': True, 'find': True, 'call': True,
            'video': True, 'edit': True, 'mute': False, 'more': True})
        actions = {row['action']: row for row in snapshot['more_actions']}
        self.assertTrue(actions['search']['available'])
        self.assertFalse(actions['share_contact']['available'])
        self.assertFalse(actions['delete']['available'])
        self.assertTrue(all(snapshot['tabs'].values()))
        self.client.chat_info.assert_not_called()
        self.client.contact_info.assert_not_called()
        snapshot['tabs']['files'] = False
        self.assertTrue(self.service.conversation_snapshot(self.context)['tabs']['files'])
        with self.assertRaises(FrozenInstanceError): self.context.peer_id = 303

    def test_context_rejects_guessed_chat_identity_or_mismatching_peer_metadata(self):
        for identity in (True, '303', 101 ^ 202, 999, 2**63):
            with self.subTest(identity=identity), self.assertRaises(ConversationUnavailable):
                self.service.conversation_context(identity)
        self.store.upsert_chat(Chat(id=303, chat_type='DIALOG', peer_user_id=999))
        self.changes = self.store._db.total_changes
        self.assertFalse(self.service.is_current_conversation(self.context))

    def test_info_uses_saved_custom_alias_and_follows_later_server_change(self):
        contact = self.service._server_contacts[202]
        contact['flags'] = 512
        contact['names'] = [{'type': 'ONEME', 'firstName': 'Public', 'lastName': 'Identity'},
                            {'type': 'CUSTOM', 'firstName': 'Saved', 'lastName': 'Alias'}]
        self.assertEqual(self.service.conversation_snapshot(self.context)['title'], 'Saved Alias')
        contact['names'][1]['firstName'] = 'Later'
        self.assertEqual(self.service.conversation_snapshot(self.context)['title'], 'Later Alias')
        self.client.contact_info.return_value = {'contacts': [copy.deepcopy(contact)]}
        self.assertEqual(self.service.refresh_conversation(self.context)['title'], 'Later Alias')

    def test_peer_only_participant_map_keeps_actual_peer_without_requiring_owner_entry(self):
        self.service._server_chats[303]['participants'] = {'202': 0}
        self.metadata = copy.deepcopy(self.service._server_chats)
        context = self.service.conversation_context(303)
        self.assertEqual(context.peer_id, 202)
        self.assertTrue(self.service.is_current_conversation(context))
        self.client.chat_info.return_value = {'chats': [chat_dto(participants={'202': 0})]}
        self.assertEqual(self.service.refresh_conversation(context)['peer_id'], 202)

    def test_ambiguous_participants_invalid_ids_or_cached_mismatch_never_choose_arbitrary_peer(self):
        for participants in ({'202': 0, '999': 0}, {'101': 0}, {}, {'not-an-id': 0}, {'999': 0}):
            with self.subTest(participants=participants):
                self.service._server_chats[303]['participants'] = participants
                with self.assertRaises(ConversationUnavailable): self.service.conversation_context(303)
        self.metadata = copy.deepcopy(self.service._server_chats)

    def test_context_is_invalid_after_client_socket_device_or_same_owner_login_change(self):
        original_socket, original_context = self.client._sock, self.client._auth_session
        for kind in ('context', 'socket', 'device', 'client'):
            with self.subTest(kind=kind):
                self.client._sock, self.client._auth_session = original_socket, original_context
                self.client.device_id = DEVICE
                self.service.client = self.client
                if kind == 'context': self.replace_context()
                elif kind == 'socket': self.client._sock = _Socket()
                elif kind == 'device': self.client.device_id = 'different-device'
                else: self.service.client = SimpleNamespace(authenticated_session=original_context)
                self.assertFalse(self.service.is_current_conversation(self.context))
                with self.assertRaises(ConversationUnavailable): self.service.refresh_conversation(self.context)
        self.client.chat_info.assert_not_called()
        self.client.contact_info.assert_not_called()

    def test_refresh_reads_48_then_actual_peer_32_without_changing_cache(self):
        fresh = copy.deepcopy(self.service._server_contacts[202])
        fresh['names'] = [{'name': 'Fresh returned name'}]
        fresh['phone'] = '+70000000002'
        self.client.contact_info.return_value = {'contacts': [fresh]}
        result = self.service.refresh_conversation(self.context)
        self.assertEqual(result['title'], 'Fresh returned name')
        self.assertEqual(result['phone'], '+70000000002')
        self.client.chat_info.assert_called_once_with([303])
        self.client.contact_info.assert_called_once_with([202])
        fresh['names'][0]['name'] = 'Changed external DTO'
        self.assertEqual(result['title'], 'Fresh returned name')

    def test_local_alias_renamed_during_refresh_uses_latest_locked_cache_title(self):
        for changed_during in ('chat', 'contact'):
            with self.subTest(changed_during=changed_during):
                def chat_reply(_ids):
                    if changed_during == 'chat': self.store.rename_chat(303, 'Latest alias during48')
                    return {'chats': [chat_dto(title='Older server title')]}
                def contact_reply(_ids):
                    if changed_during == 'contact': self.store.rename_chat(303, 'Latest alias during32')
                    return {'contacts': [copy.deepcopy(self.service._server_contacts[202])]}
                self.client.chat_info.side_effect = chat_reply
                self.client.contact_info.side_effect = contact_reply
                result = self.service.refresh_conversation(self.context)
                self.assertEqual(result['title'], f'Latest alias during{48 if changed_during == "chat" else 32}')
                # The only write here was the explicit synthetic user rename.
                self.changes = self.store._db.total_changes

    def test_fresh_contact_omission_does_not_republish_old_phone(self):
        fresh = copy.deepcopy(self.service._server_contacts[202])
        fresh.pop('phone')
        self.client.contact_info.return_value = {'contacts': [fresh]}
        self.assertNotIn('phone', self.service.refresh_conversation(self.context))

    def test_replaced_session_during_48_stops_before_32(self):
        def reply(_ids):
            self.replace_context()
            return {'chats': [chat_dto()]}
        self.client.chat_info.side_effect = reply
        with self.assertRaises(ConversationUnavailable): self.service.refresh_conversation(self.context)
        self.client.contact_info.assert_not_called()

    def test_replaced_session_during_32_cannot_return_detached_old_contact(self):
        def reply(_ids):
            self.replace_context()
            return {'contacts': [self.service._server_contacts[202]]}
        self.client.contact_info.side_effect = reply
        with self.assertRaises(ConversationUnavailable): self.service.refresh_conversation(self.context)

    def test_final_snapshot_guard_runs_after_call_readiness_check(self):
        self.service.calls_available.side_effect = lambda _chat: self.replace_context() or False
        with self.assertRaises(ConversationUnavailable): self.service.conversation_snapshot(self.context)

    def test_wrong_chat_or_contact_response_is_not_accepted(self):
        self.client.chat_info.return_value = {'chats': [chat_dto(identity=909)]}
        with self.assertRaises(ConversationUnavailable): self.service.refresh_conversation(self.context)
        self.client.contact_info.assert_not_called()
        self.client.chat_info.return_value = {'chats': [chat_dto()]}
        self.client.contact_info.return_value = {'contacts': [{'id': 909, 'names': []}]}
        with self.assertRaises(ConversationUnavailable): self.service.refresh_conversation(self.context)

    def test_common_chats_uses_actual_peer_and_fractional_timestamp_marker(self):
        returned = chat_dto(identity=919, type='CHAT', title='<b>Literal common chat</b>')
        self.client.common_chats.return_value = {'commonChats': [returned], 'hasMore': True, 'marker': 0.25}
        page = self.service.load_conversation_tab(self.context, 'common_chats', marker=0)
        self.client.common_chats.assert_called_once_with(202, marker=0)
        self.assertEqual(page['marker'], 0.25)
        self.assertFalse(page['complete'])
        self.assertEqual(page['items'][0], {'key': 'chat:919', 'title': '<b>Literal common chat</b>',
            'subtitle': '', 'chat_id': 919, 'action': 'chat', 'available': True})
        self.assertIsNone(self.store.get_chat(919))

    def test_common_completion_or_missing_repeated_marker_cannot_page_forever(self):
        for data, marker, complete in (
                ({'commonChats': [], 'hasMore': False, 'marker': 0.25}, None, True),
                ({'commonChats': [], 'hasMore': True}, None, False),
                ({'commonChats': [], 'hasMore': True, 'marker': 0.25}, 0.25, False)):
            self.client.common_chats.return_value = data
            page = self.service.load_conversation_tab(self.context, 'common_chats', marker=marker)
            self.assertIsNone(page['marker'])
            self.assertEqual(page['complete'], complete)
            self.assertEqual(bool(page['status']), not complete)

    def test_invalid_common_marker_is_rejected_before_client_call(self):
        for marker in (True, '0', float('nan'), float('inf')):
            with self.assertRaises(ConversationUnavailable):
                self.service.load_conversation_tab(self.context, 'common_chats', marker=marker)
        self.client.common_chats.assert_not_called()

    def test_context_replacement_during_either_tab_read_discards_its_page(self):
        for tab in ('common_chats', 'files'):
            with self.subTest(tab=tab):
                self.context = self.service.conversation_context(303)
                def common(*_args, **_kwargs):
                    self.replace_context()
                    return {'commonChats': [], 'hasMore': False}
                def media(*_args, **_kwargs):
                    self.replace_context()
                    return {'messages': [], 'total': 0}
                self.client.common_chats.side_effect = common
                self.client.conversation_media.side_effect = media
                with self.assertRaises(ConversationUnavailable):
                    self.service.load_conversation_tab(self.context, tab)

    def test_media_marker_is_an_actual_int64_not_time_or_text(self):
        for marker in (True, STAMP, '7', 2**63):
            with self.subTest(marker=marker), self.assertRaises(ConversationUnavailable):
                self.service.load_conversation_tab(self.context, 'files', marker=marker)
        self.client.conversation_media.assert_not_called()

    def test_media_initial_uses_actual_anchor_and_oldest_row_not_minimum_id(self):
        self.client.conversation_media.return_value = {'total': 100,
            'messages': [message_dto(1000-index, STAMP-30+index) for index in range(25)]}
        page = self.service.load_conversation_tab(self.context, 'media')
        self.client.conversation_media.assert_called_once_with(303, 'PHOTO_VIDEO', marker=7, initial=True)
        self.assertEqual(page['marker'], 1000)
        self.assertFalse(page['complete'])
        self.assertEqual(page['items'][0]['message_id'], 1000)
        self.assertEqual(page['items'][0]['action'], 'message')
        self.assertTrue(page['items'][0]['available'])

    def test_media_continuation_preserves_message_marker_and_fractional_times(self):
        self.client.conversation_media.return_value = {'total': 100,
            'messages': [message_dto(900, 0.25), message_dto(3, 0.5)]}
        page = self.service.load_conversation_tab(self.context, 'files', marker=1000)
        self.client.conversation_media.assert_called_once_with(303, 'FILE', marker=1000, initial=False)
        self.assertTrue(page['complete'])
        self.assertIsNone(page['marker'])
        self.assertEqual([row['time_ms'] for row in page['items']], [0.25, 0.5])

    def test_unconfirmed_order_stays_partial_without_fabricated_cursor(self):
        self.client.conversation_media.return_value = {'total': 2,
            'messages': [message_dto(1, 2), message_dto(2, 1)]}
        page = self.service.load_conversation_tab(self.context, 'voice')
        self.assertFalse(page['complete'])
        self.assertIsNone(page['marker'])
        self.assertTrue(page['status'])
        self.assertEqual([row['message_id'] for row in page['items']], [1, 2])

    def test_no_anchor_uses_bounded_initial_read_and_reports_unknown_continuation(self):
        self.service._server_chats[303].pop('lastMessage')
        self.metadata = copy.deepcopy(self.service._server_chats)
        self.client.conversation_media.return_value = {'total': 1, 'messages': [message_dto()]}
        page = self.service.load_conversation_tab(self.context, 'links')
        self.client.conversation_media.assert_called_once_with(303, 'SHARE', marker=None, initial=True)
        self.assertFalse(page['complete'])
        self.assertIsNone(page['marker'])
        self.assertTrue(page['status'])

    def test_file_without_actual_id_exposes_no_url_token_or_download_action(self):
        self.client.conversation_media.return_value = {'total': 1,
            'messages': [message_dto(attaches=[{'_type': 'FILE', 'name': '<b>Literal file</b>',
                'url': 'https://private.invalid/file', 'token': 'synthetic-private-token'}])]}
        page = self.service.load_conversation_tab(self.context, 'files')
        self.assertEqual(page['items'][0]['title'], '<b>Literal file</b>')
        encoded = json.dumps(page['items'])
        self.assertNotIn('https://', encoded)
        self.assertNotIn('synthetic-private-token', encoded)
        self.assertEqual(page['items'][0]['action'], 'message')
        self.assertFalse(page['items'][0]['download_available'])

    def test_group_has_no_invented_peer_or_common_chat_request(self):
        self.store.upsert_chat(Chat(id=808, chat_type='CHAT', is_group=True))
        self.service._server_chats[808] = chat_dto(identity=808, type='CHAT')
        self.changes, self.metadata = self.store._db.total_changes, copy.deepcopy(self.service._server_chats)
        context = self.service.conversation_context(808)
        self.assertIsNone(context.peer_id)
        self.assertFalse(self.service.conversation_snapshot(context)['tabs']['common_chats'])
        with self.assertRaises(ConversationUnavailable): self.service.load_conversation_tab(context, 'common_chats')
        self.client.common_chats.assert_not_called()

    def test_invalid_media_identity_or_duplicate_rows_cannot_create_navigation(self):
        for rows in ([message_dto(identity=True)], [message_dto(timestamp=float('nan'))],
                     [message_dto(), message_dto()]):
            self.client.conversation_media.return_value = {'messages': rows, 'total': 2}
            with self.assertRaises(ConversationUnavailable): self.service.load_conversation_tab(self.context, 'files')


if __name__ == '__main__':
    unittest.main()
