"""Read-only invitation contracts; isolated fake sessions, no clipboard/network."""
from types import SimpleNamespace
import threading
import unittest
from unittest.mock import Mock

from maxclient.core.models import Profile
from maxclient.core.service import MaxService
from maxclient.core.store import Store
from maxclient.protocol.client import AuthenticatedSession, ConnectionState, MaxClient
from maxclient.protocol.invite_link_contracts import invite_link_config, invite_link_text, INVITE_TEXT_LIMIT

DEVICE = '00112233-4455-6677-8899-aabbccddeeff'
TEXT = '  Synthetic invitation\nhttps://max.ru/\n'


class InviteLinkContractTests(unittest.TestCase):
    def test_exact_server_text_is_copied_without_rewriting(self):
        self.assertEqual(invite_link_text({'invite-long': TEXT}), TEXT)

    def test_missing_and_explicit_empty_are_distinct_for_config_merge(self):
        self.assertEqual(invite_link_config({}), {})
        self.assertEqual(invite_link_config({'invite-long': ''}), {'invite-long': ''})
        for source in ({}, {'invite-long': ''}, {'invite-long': ' \n\t'}):
            with self.assertRaises(ValueError):
                invite_link_text(source)

    def test_typed_bounded_text_rejects_untrusted_config_shapes(self):
        for value in (None, True, 123, {}, [], 'x' * (INVITE_TEXT_LIMIT + 1), '\x00bad', '\ud800'):
            with self.subTest(value_type=type(value).__name__), self.assertRaises(ValueError):
                invite_link_config({'invite-long': value})

    def test_utf16_bound_and_multiline_are_supported(self):
        accepted = '😀' * (INVITE_TEXT_LIMIT // 2)
        self.assertEqual(invite_link_text({'invite-long': accepted}), accepted)
        with self.assertRaises(ValueError):
            invite_link_text({'invite-long': accepted + 'a'})

    def test_no_profile_phone_token_or_unrelated_link_fallback(self):
        for source in ({'link': 'https://max.ru/u/synthetic'}, {'invite-short': TEXT},
                       {'phone': '+70000000001'}, {'token': 'synthetic-invalid-token'}):
            with self.assertRaises(ValueError):
                invite_link_text(source)


class InviteLinkServiceTests(unittest.TestCase):
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
        self.client.request = Mock(side_effect=AssertionError('Invitation must not call network'))
        self.service.client = self.client

    def read(self, context=None):
        return self.service.invite_link_text(context=self.context if context is None else context)

    def test_remember_config_retains_supplied_invitation_for_readonly_action(self):
        self.service._remember_config({'server': {'invite-long': TEXT}}, owner_id=101)
        self.assertEqual(self.read(), TEXT)
        self.client.request.assert_not_called()

    def test_config_merge_keeps_absent_invite_but_explicit_empty_revokes(self):
        self.service._remember_config({'server': {'invite-long': TEXT}}, owner_id=101)
        self.service._remember_config({'server': {'sessions-end-disabled': True}}, owner_id=101)
        self.assertEqual(self.read(), TEXT)
        self.service._remember_config({'server': {'invite-long': ''}}, owner_id=101)
        with self.assertRaises(ValueError):
            self.read()

    def test_missing_config_is_explicit_not_generic_url_fallback(self):
        self.store.save_profile(Profile(id=101, phone='+70000000001', link='https://max.ru/u/synthetic'))
        with self.assertRaises(ValueError):
            self.read()
        self.client.request.assert_not_called()

    def test_context_captured_before_queue_is_required(self):
        self.service._remember_config({'server': {'invite-long': TEXT}}, owner_id=101)
        self.client._auth_session = AuthenticatedSession(101, DEVICE, 'WEB', self.client._sock)
        with self.assertRaises(ValueError):
            self.read()
        with self.assertRaises(ValueError):
            self.service.invite_link_text(context=None)

    def test_owner_swap_does_not_reuse_other_accounts_invitation(self):
        self.service._remember_config({'server': {'invite-long': TEXT}}, owner_id=101)
        self.store.bind_owner(202)
        self.service.session.my_user_id = 202
        self.service._server_metadata_owner = 202
        new_context = AuthenticatedSession(202, DEVICE, 'WEB', self.client._sock)
        self.client._auth_session = new_context
        with self.assertRaises(ValueError):
            self.read()
        with self.assertRaises(ValueError):
            self.read(new_context)

    def test_owner_change_during_cache_load_is_rejected(self):
        self.service._remember_config({'server': {'invite-long': TEXT}}, owner_id=101)
        original = self.store.load_config
        def changed(owner):
            result = original(owner)
            self.client._auth_session = AuthenticatedSession(101, DEVICE, 'WEB', self.client._sock)
            return result
        self.store.load_config = changed
        with self.assertRaises(ValueError):
            self.read()

    def test_invalid_invite_config_is_not_installed(self):
        for value in (123, None, True, '\x00'):
            with self.assertRaises(ValueError):
                self.service._remember_config({'server': {'invite-long': value}}, owner_id=101)
        self.assertNotIn('invite-long', self.store.load_config(101).get('server', {}))

    def test_live_config_creation_limits_are_retained_without_default_invention(self):
        self.service._remember_config({'server': {'max-cname-length': 9, 'max-added-participants': 2}}, owner_id=101)
        self.assertEqual(self.store.load_config(101)['server'], {'max-cname-length': 9, 'max-added-participants': 2})
        actual = self.service.conversation_creation_limits(context=self.context)
        self.assertEqual(actual['max-cname-length'], 9)
        self.assertEqual(actual['max-added-participants'], 2)
        self.assertEqual(actual['max-description-length'], 400)

    def test_invalid_creation_limits_not_recorded(self):
        for value in (True, '200', 0, -1):
            with self.assertRaises(ValueError):
                self.service._remember_config({'server': {'max-cname-length': value}}, owner_id=101)
        self.assertEqual(self.store.load_config(101), {})


if __name__ == '__main__':
    unittest.main()
