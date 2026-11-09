"""Server contact-save orchestration with an in-memory owner and fake requests."""
import copy
from types import SimpleNamespace
import threading
import unittest
from unittest.mock import Mock

from maxclient.core.models import Chat, Profile
from maxclient.core.service import MaxService, _person_name, _profile_from_data
from maxclient.core.store import Store
from maxclient.protocol.client import (
    AuthenticatedSession, ConnectionState, MaxClient, MaxFrame, MaxError,
    MaxTimeout, MaxWriteNotStarted, MaxWriteOutcomeUnknown,
)
from test_contact_create_contracts import PHONE, record


DEVICE = '00112233-4455-6677-8899-aabbccddeeff'


class ContactCreateCoreTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(':memory:')
        self.addCleanup(self.store.close)
        self.store.bind_owner(101)
        self.service = MaxService.__new__(MaxService)
        self.service.store = self.store
        self.service.session = SimpleNamespace(my_user_id=101, device_id=DEVICE)
        self.service._metadata_lock = threading.RLock()
        self.service._server_metadata_owner = 101
        self.service._server_chats = {}
        self.service._server_contacts = {}
        self.service._contacts_cache = None
        self.client = MaxClient(device_id=DEVICE, auto_reconnect=False)
        self.client._device_type = 'WEB'
        self.client._state = ConnectionState.CONNECTED
        self.client._sock = object()
        self.context = AuthenticatedSession(101, DEVICE, 'WEB', self.client._sock)
        self.client._auth_session = self.context
        self.client.request = Mock(side_effect=self.reply)
        self.service.client = self.client

    def reply(self, opcode, payload, **kwargs):
        self.assertIs(kwargs['expected_session'], self.context)
        if opcode == 41:
            return MaxFrame(1, 1, 41, b'', {'contact': record(), 'new': True})
        if opcode == 32:
            return MaxFrame(1, 2, 32, b'', {'contacts': [record()]})
        if opcode == 46:
            return MaxFrame(1, 3, 46, b'', {'contact': record()})
        raise AssertionError('Unexpected synthetic RPC')

    def create(self, **kwargs):
        return self.service.create_contact_by_phone(PHONE, 'Synthetic', 'Person', context=self.context, **kwargs)

    def switch(self, owner=101):
        if owner != 101:
            self.store.bind_owner(owner)
            self.service.session.my_user_id = owner
            self.service._server_metadata_owner = owner
            self.service._server_contacts.clear()
        self.client._auth_session = AuthenticatedSession(owner, DEVICE, 'WEB', self.client._sock)

    def test_exact_write_then_actual_id_readback_cache_without_fake_chat(self):
        self.store.upsert_chat(Chat(id=202, title='Unrelated group', is_group=True, chat_type='CHAT'))
        result = self.create()
        self.assertTrue(result.is_new)
        self.assertEqual((result.contact.id, result.contact.name), (202, 'Synthetic Person'))
        self.assertEqual(self.client.request.call_args_list[0].args,
                         (41, {'phone': PHONE, 'firstName': 'Synthetic', 'lastName': 'Person'}))
        self.assertEqual(self.client.request.call_args_list[1].args, (32, {'contactIds': [202]}))
        self.assertEqual(self.store.get_chat(202).title, 'Unrelated group')
        self.assertEqual(len(self.store.list_chats()), 1)
        self.assertEqual(self.service._contact_create_pending, {})

    def test_existing_contact_false_new_is_valid_and_rows_are_deduplicated(self):
        def reply(opcode, payload, **kwargs):
            frame = self.reply(opcode, payload, **kwargs)
            if opcode == 41:
                frame.decoded['new'] = False
            return frame
        self.client.request.side_effect = reply
        self.assertFalse(self.create().is_new)
        self.assertFalse(self.create().is_new)
        self.assertEqual(len(self.store.list_contacts()), 1)

    def test_mixed_public_and_custom_names_keep_saved_alias_after_refresh(self):
        raw = record()
        raw['names'].insert(0, {'type': 'ONEME', 'firstName': 'Public', 'lastName': 'Identity'})
        def reply(opcode, payload, **kwargs):
            return MaxFrame(1, 1, opcode, b'', {'contact': copy.deepcopy(raw), 'new': False}
                            if opcode == 41 else {'contacts': [copy.deepcopy(raw)]})
        self.client.request.side_effect = reply
        result = self.create()
        self.assertEqual(result.contact.name, 'Synthetic Person')
        self.assertEqual(self.service.list_contacts('Synthetic')[0].name, 'Synthetic Person')
        self.assertEqual(self.service.list_contacts('Public'), [])
        self.service._remember_contact_info({'contacts': [copy.deepcopy(raw)]})
        self.assertEqual(self.store.list_contacts()[0].name, 'Synthetic Person')
        self.assertEqual(self.service._server_contacts[202], raw)
        self.assertEqual(_profile_from_data(raw, own=True).name, 'Public Identity')

    def test_saved_alias_updates_only_actual_dialog_projection_and_keeps_locked_titles(self):
        raw = record()
        raw['names'].insert(0, {'type': 'ONEME', 'firstName': 'Public'})
        self.service._remember_chat_info({'chats': [
            {'id': 303, 'type': 'DIALOG', 'participants': {101: 1, 202: 1}},
            {'id': 304, 'type': 'DIALOG', 'participants': {101: 1, 202: 1}}]}, my_id=101)
        self.store.rename_chat(304, 'Explicit local title')
        self.store.upsert_chat(Chat(id=202, title='Unrelated group', is_group=True, chat_type='CHAT'))
        def reply(opcode, payload, **kwargs):
            return MaxFrame(1, 1, opcode, b'', {'contact': copy.deepcopy(raw), 'new': True}
                            if opcode == 41 else {'contacts': [copy.deepcopy(raw)]})
        self.client.request.side_effect = reply
        self.create()
        chats = {chat.id: chat for chat in self.service.list_chats()}
        self.assertEqual(chats[303].title, 'Synthetic Person')
        self.assertEqual(chats[304].title, 'Explicit local title')
        self.assertTrue(chats[304].title_locked)
        self.assertEqual(chats[202].title, 'Unrelated group')

    def test_only_typed_added_flag_changes_mixed_record_display_precedence(self):
        raw = record()
        raw['names'].insert(0, {'type': 'ONEME', 'firstName': 'Public'})
        for flags in (None, 0, 1, True, '512', 512.0, 2**40 + 512):
            with self.subTest(flags=flags):
                raw['flags'] = flags
                self.assertEqual(_person_name(raw), 'Public')
        raw['flags'] = 512
        self.assertEqual(_person_name(raw), 'Synthetic Person')

    def test_absent_readback_phone_preserves_confirmed_input_without_fake_server_field(self):
        raw = record()
        raw.pop('phone')
        def reply(opcode, payload, **kwargs):
            return MaxFrame(1, 1, opcode, b'', {'contact': copy.deepcopy(raw), 'new': True}
                            if opcode == 41 else {'contacts': [copy.deepcopy(raw)]})
        self.client.request.side_effect = reply
        result = self.create()
        self.assertEqual(result.contact.phone, PHONE)
        self.assertEqual(self.service.list_contacts(PHONE)[0].phone, PHONE)
        self.assertNotIn('phone', self.service._server_contacts[202])
        self.service._remember_contact_info({'contacts': [copy.deepcopy(raw)]})
        self.assertEqual(self.store.list_contacts()[0].phone, PHONE)

    def test_contradictory_custom_names_leave_write_unconfirmed(self):
        def reply(opcode, payload, **kwargs):
            frame = self.reply(opcode, payload, **kwargs)
            if opcode == 32:
                frame.decoded['contacts'][0]['names'].append({'type': 'CUSTOM', 'firstName': 'Different'})
            return frame
        self.client.request.side_effect = reply
        with self.assertRaises(MaxWriteOutcomeUnknown):
            self.create()
        self.assertEqual(self.store.list_contacts(), [])
        self.assertTrue(self.service._contact_create_pending)

    def test_invalid_or_uncaptured_context_and_invalid_input_have_no_requests(self):
        for context in (None, object()):
            with self.assertRaises(ValueError):
                self.service.create_contact_by_phone(PHONE, 'Name', context=context)
        for first in ('', True, 'x' * 61):
            with self.assertRaises(ValueError):
                self.service.create_contact_by_phone(PHONE, first, context=self.context)
        self.client.request.assert_not_called()

    def test_context_captured_before_queue_and_own_phone_have_no_write(self):
        self.switch()
        with self.assertRaises(ValueError):
            self.create()
        self.client._auth_session = self.context
        self.store.save_profile(Profile(id=101, name='Owner', phone=PHONE))
        with self.assertRaises(ValueError):
            self.create()
        self.client.request.assert_not_called()

    def test_atomic_not_started_clears_only_own_pending_without_retry(self):
        self.client.request.side_effect = MaxWriteNotStarted('Synthetic stale transport')
        with self.assertRaises(MaxWriteNotStarted):
            self.create()
        self.assertEqual(self.client.request.call_count, 1)
        self.assertEqual(self.service._contact_create_pending, {})
        self.assertEqual(self.store.list_contacts(), [])

    def test_real_request_guard_catches_session_switch_under_send_lock_with_zero_send(self):
        socket = Mock()
        self.client._sock = socket
        self.context = AuthenticatedSession(101, DEVICE, 'WEB', socket)
        self.client._auth_session = self.context
        fixture = self
        class SwapOnEnter:
            def __enter__(self):
                fixture.switch()
            def __exit__(self, *_args):
                return False
        self.client._send_lock = SwapOnEnter()
        self.client.request = MaxClient.request.__get__(self.client, MaxClient)
        with self.assertRaises(MaxWriteNotStarted):
            self.create()
        socket.sendall.assert_not_called()
        self.assertEqual(self.service._contact_create_pending, {})
        self.assertEqual(self.store.list_contacts(), [])

    def test_explicit_server_rejection_allows_a_corrected_later_attempt(self):
        self.client.request.return_value = MaxFrame(3, 1, 41, b'', {'error': 'user.not.found'})
        self.client.request.side_effect = None
        with self.assertRaises(MaxError) as error:
            self.create()
        self.assertNotIsInstance(error.exception, MaxWriteOutcomeUnknown)
        self.assertEqual(self.service._contact_create_pending, {})
        self.client.request.side_effect = self.reply
        self.assertTrue(self.create().is_new)

    def test_timeout_blocks_repeated_write_then_verify_only_reads_and_confirms(self):
        self.client.request.side_effect = MaxTimeout('Synthetic timeout')
        with self.assertRaises(MaxWriteOutcomeUnknown):
            self.create()
        with self.assertRaises(MaxWriteOutcomeUnknown):
            self.create()
        self.assertEqual(self.client.request.call_count, 1)
        self.client.request.reset_mock(side_effect=True)
        self.client.request.side_effect = self.reply
        result = self.service.verify_contact_creation(PHONE, context=self.context)
        self.assertIsNone(result.is_new)
        self.assertEqual([call.args[0] for call in self.client.request.call_args_list], [46, 32])
        self.assertEqual(self.service._contact_create_pending, {})

    def test_ack_without_added_or_name_readback_does_not_claim_or_cache_success(self):
        for patch in ({'flags': 0}, {'names': [{'type': 'CUSTOM', 'firstName': 'Old'}]}):
            with self.subTest(patch=patch):
                self.service._contact_create_pending = {}
                def reply(opcode, payload, **kwargs):
                    frame = self.reply(opcode, payload, **kwargs)
                    if opcode == 32:
                        frame.decoded['contacts'][0].update(patch)
                    return frame
                self.client.request.side_effect = reply
                with self.assertRaises(MaxWriteOutcomeUnknown):
                    self.create()
                self.assertEqual(self.store.list_contacts(), [])
                self.assertEqual(self.service._server_contacts, {})
                self.client.request.reset_mock()
                with self.assertRaises(MaxWriteOutcomeUnknown):
                    self.create()
                self.client.request.assert_not_called()

    def test_known_ack_identity_verify_uses_only32(self):
        def reply(opcode, payload, **kwargs):
            if opcode == 32:
                raise MaxTimeout('Synthetic read timeout')
            return self.reply(opcode, payload, **kwargs)
        self.client.request.side_effect = reply
        with self.assertRaises(MaxWriteOutcomeUnknown):
            self.create()
        self.client.request.reset_mock()
        self.client.request.side_effect = self.reply
        result = self.service.verify_contact_creation(PHONE, context=self.context)
        self.assertTrue(result.is_new)
        self.assertEqual([call.args[0] for call in self.client.request.call_args_list], [32])

    def test_malformed_ack_and_wrong_opcode_remain_unknown_without_readback(self):
        for frame in (MaxFrame(1, 1, 46, b'', {'contact': record(), 'new': True}),
                      MaxFrame(2, 1, 41, b'', {'contact': record(), 'new': True}),
                      MaxFrame(1, 1, 41, b'', {'contact': record(), 'new': 1}),
                      MaxFrame(1, 1, 41, b'', {'contact': record(True), 'new': True})):
            self.service._contact_create_pending = {}
            self.client.request = Mock(return_value=frame)
            with self.assertRaises(MaxWriteOutcomeUnknown):
                self.create()
            self.assertEqual(self.client.request.call_count, 1)
            self.assertEqual(self.store.list_contacts(), [])

    def test_switch_after_write_or_after_readback_cannot_touch_new_owner(self):
        for switch_at, owner in ((41, 101), (41, 404), (32, 101), (32, 404)):
            with self.subTest(switch_at=switch_at, owner=owner):
                self.store.bind_owner(101)
                self.service.session.my_user_id = self.service._server_metadata_owner = 101
                self.client._auth_session = self.context
                self.service._contact_create_pending = {}
                self.client.request.reset_mock()
                def reply(opcode, payload, **kwargs):
                    frame = self.reply(opcode, payload, **kwargs)
                    if opcode == switch_at:
                        self.switch(owner)
                    return frame
                self.client.request.side_effect = reply
                with self.assertRaises(MaxWriteOutcomeUnknown):
                    self.create()
                self.assertEqual(self.store.list_contacts(), [])
                self.assertEqual(self.service._server_contacts, {})
                self.assertEqual(self.client.request.call_count, 1 if switch_at == 41 else 2)

    def test_same_owner_reconnect_still_cannot_resubmit_unknown_write(self):
        self.client.request.side_effect = MaxTimeout('Synthetic timeout')
        with self.assertRaises(MaxWriteOutcomeUnknown):
            self.create()
        self.switch()
        with self.assertRaises(MaxWriteOutcomeUnknown):
            self.service.create_contact_by_phone(PHONE, 'Synthetic', 'Person', context=self.client.authenticated_session)
        self.assertEqual(self.client.request.call_count, 1)

    def test_reentrant_submission_is_blocked_while_first_write_in_flight(self):
        def reply(opcode, payload, **kwargs):
            if opcode == 41:
                with self.assertRaises(ValueError):
                    self.create()
            return self.reply(opcode, payload, **kwargs)
        self.client.request.side_effect = reply
        self.create()
        self.assertEqual([call.args[0] for call in self.client.request.call_args_list], [41, 32])

    def test_int64_identity_not_coerced_or_narrowed(self):
        for identity in (-(2**63), 0, 2**63-1):
            def reply(opcode, payload, **kwargs):
                return MaxFrame(1, 1, opcode, b'', {'contact': record(identity), 'new': True}
                                if opcode == 41 else {'contacts': [record(identity)]})
            self.client.request.side_effect = reply
            self.assertEqual(self.create().contact.id, identity)

    def test_no_verify_without_pending(self):
        with self.assertRaises(ValueError):
            self.service.verify_contact_creation(PHONE, context=self.context)
        self.client.request.assert_not_called()

    def test_pending_different_owner_does_not_block_new_owner_or_leak_cached_data(self):
        self.client.request.side_effect = MaxTimeout('Synthetic timeout')
        with self.assertRaises(MaxWriteOutcomeUnknown):
            self.create()
        self.switch(404)
        new_context = self.client.authenticated_session
        def reply(opcode, payload, **kwargs):
            self.assertIs(kwargs['expected_session'], new_context)
            return MaxFrame(1, 1, opcode, b'', {'contact': record(505), 'new': False}
                            if opcode == 41 else {'contacts': [record(505)]})
        self.client.request.side_effect = reply
        result = self.service.create_contact_by_phone(PHONE, 'Synthetic', 'Person', context=new_context)
        self.assertEqual(result.contact.id, 505)
        self.assertEqual([contact.id for contact in self.store.list_contacts()], [505])
        self.assertIn((101, PHONE), self.service._contact_create_pending)

    def test_changed_input_cannot_overwrite_unresolved_intent(self):
        self.client.request.side_effect = MaxTimeout('Synthetic timeout')
        with self.assertRaises(MaxWriteOutcomeUnknown):
            self.create()
        with self.assertRaises(MaxWriteOutcomeUnknown):
            self.service.create_contact_by_phone(PHONE, 'Changed', context=self.context)
        self.assertEqual(self.client.request.call_count, 1)
        self.assertEqual(self.service._contact_create_pending[(101, PHONE)]['payload']['firstName'], 'Synthetic')

    def test_verify_switch_after46_cannot_read32_or_cache(self):
        self.client.request.side_effect = MaxTimeout('Synthetic timeout')
        with self.assertRaises(MaxWriteOutcomeUnknown):
            self.create()
        self.client.request.reset_mock()
        def reply(opcode, payload, **kwargs):
            frame = self.reply(opcode, payload, **kwargs)
            self.switch(404)
            return frame
        self.client.request.side_effect = reply
        with self.assertRaises(MaxWriteOutcomeUnknown):
            self.service.verify_contact_creation(PHONE, context=self.context)
        self.assertEqual([call.args[0] for call in self.client.request.call_args_list], [46])
        self.assertEqual(self.store.list_contacts(), [])

    def test_self_ack_is_unknown_without_readback_and_never_creates_saved_chat(self):
        self.client.request.side_effect = None
        self.client.request.return_value = MaxFrame(1, 1, 41, b'', {'contact': record(101), 'new': True})
        with self.assertRaises(MaxWriteOutcomeUnknown):
            self.create()
        self.assertEqual(self.client.request.call_count, 1)
        self.assertEqual(self.store.list_chats(), [])
        self.assertEqual(self.store.list_contacts(), [])
