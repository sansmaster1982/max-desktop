"""Synthetic config/wire/notification checks: never delivers live OS events."""
import copy
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from maxclient.core.models import Chat, Message
from maxclient.core.notification_settings import NotificationSettings, DEFAULTS
from maxclient.core.notifications import NotificationPolicy, Notice
from maxclient.protocol.settings_contracts import notification_values, user_setting_values, privacy_values
from maxclient.protocol.client import MaxClient, MaxFrame, MaxWriteOutcomeUnknown, MaxError, MaxTimeout
from maxclient.ui.notification_settings_panel import NotificationSettingsPanel
from maxclient.ui.desktop_notifications import MessageToast


class FakeService:
    def __init__(self):
        self.owner = 123
        self.config = {'hash': 'synthetic', 'user': {'PUSH_DETAILS': True}}
        self.store = SimpleNamespace(load_config=lambda owner: copy.deepcopy(self.config))
        self.client = SimpleNamespace(update_user_settings=Mock(return_value=None))

    def _confirmed_profile_id(self):
        return self.owner

    def _remember_config(self, config, owner_id=None):
        if owner_id != self.owner:
            raise ValueError('owner changed')
        self.config['user'].update(config.get('user', {}))


class NotificationSettingsTests(unittest.TestCase):
    def test_exact_wire_and_optional_ack(self):
        client = MaxClient(auto_reconnect=False)
        client.request = Mock(return_value=MaxFrame(1, 1, 22, b'', None))
        patch = {'DONT_DISTURB_UNTIL': -1, 'CHATS_PUSH_NOTIFICATION': 'ON'}
        self.assertIsNone(client.update_user_settings(patch))
        client.request.assert_called_once_with(22, {'settings': {'user': patch}})

    def test_unknown_sound_read_preserved_but_not_writable(self):
        self.assertEqual(notification_values({'PUSH_SOUND': 'custom'}), {'PUSH_SOUND': 'custom'})
        for value in ('custom', '_DEFAULT_', 1):
            with self.assertRaises(ValueError):
                notification_values({'PUSH_SOUND': value}, writable=True)

    def test_invalid_types_readonly_values_reject_before_request(self):
        client = MaxClient(auto_reconnect=False)
        client.request = Mock()
        for value in ({'PUSH_DETAILS': 1}, {'DONT_DISTURB_UNTIL': True},
                      {'DONT_DISTURB_UNTIL': 50}, {'CHATS_PUSH_NOTIFICATION': 'REPLY'},
                      {'unknown': True}, {'SAFE_MODE': False}):
            with self.assertRaises(ValueError):
                client.update_user_settings(value)
        client.request.assert_not_called()

    def test_privacy_and_notification_validators_stay_scoped(self):
        self.assertEqual(user_setting_values({'HIDDEN': False, 'PUSH_DETAILS': True}),
                         {'HIDDEN': False, 'PUSH_DETAILS': True})
        with self.assertRaises(ValueError):
            privacy_values({'PUSH_DETAILS': True}, writable=True)

    def test_defaults_only_after_confirmed_config(self):
        service = FakeService()
        adapter = NotificationSettings(service)
        self.assertEqual(adapter.read()['settings'], DEFAULTS)
        service.config = {}
        self.assertFalse(adapter.read()['available'])
        self.assertEqual(adapter.read()['settings'], {})
        with self.assertRaises(ValueError):
            adapter.update({'PUSH_DETAILS': False})
        service.client.update_user_settings.assert_not_called()

    def test_optional_ack_keeps_old_values_and_blocks_duplicate_write(self):
        service = FakeService()
        adapter = NotificationSettings(service)
        result = adapter.update({'PUSH_DETAILS': False})
        self.assertTrue(result['pending'])
        self.assertTrue(result['settings']['PUSH_DETAILS'])
        with self.assertRaises(ValueError):
            adapter.update({'PUSH_DETAILS': False})
        self.assertEqual(service.client.update_user_settings.call_count, 1)
        service.config['user']['PUSH_DETAILS'] = False
        self.assertFalse(adapter.read()['pending'])

    def test_full_confirmed_ack_clears_pending(self):
        service = FakeService()
        service.client.update_user_settings.return_value = {'hash': 'new', 'user': {'PUSH_DETAILS': False}}
        result = NotificationSettings(service).update({'PUSH_DETAILS': False})
        self.assertFalse(result['pending'])
        self.assertFalse(result['settings']['PUSH_DETAILS'])

    def test_partial_ack_does_not_confirm_two_field_write(self):
        service = FakeService()
        service.client.update_user_settings.return_value = {'hash': 'new', 'user': {'DONT_DISTURB_UNTIL': -1}}
        result = NotificationSettings(service).update({'DONT_DISTURB_UNTIL': -1, 'CHATS_PUSH_NOTIFICATION': 'ON'})
        self.assertTrue(result['pending'])

    def test_unknown_write_not_retried_after_panel_reopen(self):
        service = FakeService()
        service.client.update_user_settings.side_effect = MaxWriteOutcomeUnknown('synthetic')
        adapter = NotificationSettings(service)
        with self.assertRaises(MaxWriteOutcomeUnknown):
            adapter.update({'PUSH_DETAILS': False})
        self.assertTrue(adapter.read()['pending'])
        with self.assertRaises(ValueError):
            adapter.update({'PUSH_DETAILS': False})
        self.assertEqual(service.client.update_user_settings.call_count, 1)

    def test_explicit_nack_can_be_corrected(self):
        service = FakeService()
        service.client.update_user_settings.side_effect = MaxError('synthetic.denied')
        adapter = NotificationSettings(service)
        with self.assertRaises(MaxError):
            adapter.update({'PUSH_DETAILS': False})
        self.assertFalse(adapter.read()['pending'])

    def test_account_change_does_not_apply_reply_to_next_account(self):
        service = FakeService()
        def change(_patch):
            service.owner = 456
            return {'hash': 'new', 'user': {'PUSH_DETAILS': False}}
        service.client.update_user_settings.side_effect = change
        with self.assertRaises(MaxWriteOutcomeUnknown):
            NotificationSettings(service).update({'PUSH_DETAILS': False})
        self.assertTrue(service.config['user']['PUSH_DETAILS'])

    def test_account_change_during_read_never_exposes_previous_preferences(self):
        service = FakeService()
        def swapped(_owner):
            service.owner = 456
            return {'user': {'PUSH_DETAILS': True}}
        service.store.load_config = swapped
        with self.assertRaises(ValueError):
            NotificationSettings(service).read()

    def test_network_failure_does_not_resend_or_reveal_error(self):
        client = MaxClient(auto_reconnect=False)
        client.request = Mock(side_effect=MaxTimeout('synthetic-private-message'))
        with self.assertRaises(MaxWriteOutcomeUnknown) as error:
            client.update_user_settings({'PUSH_SOUND': '_NONE_'})
        self.assertNotIn('synthetic-private-message', str(error.exception))
        self.assertEqual(client.request.call_count, 1)


class NotificationPolicyTests(unittest.TestCase):
    def setUp(self):
        self.chat = Chat(40, title='<b>Тест</b>', unread=1)
        self.message = Message(40, id=10, sender=3, text='<b>Текст</b>', time_ms=1000)
        self.preferences = {'available': True, 'settings': dict(DEFAULTS)}

    def decide(self, **options):
        return NotificationPolicy(900).decide(self.message, self.chat, self.preferences,
                                             now_ms=1100, viewer_id=123, **options)

    def test_preview_disabled_hides_sender_and_text(self):
        self.preferences['settings']['PUSH_DETAILS'] = False
        notice = self.decide()
        self.assertEqual((notice.title, notice.body), ('MAX', 'Новое сообщение'))

    def test_duplicates_suppressed_and_bounded(self):
        policy = NotificationPolicy(900)
        for index in range(2200):
            self.message.id = index
            self.assertIsNotNone(policy.decide(self.message, self.chat, self.preferences, now_ms=1100))
            self.assertIsNone(policy.decide(self.message, self.chat, self.preferences, now_ms=1100))
        self.assertLessEqual(len(policy._seen), 2048)

    def test_old_outgoing_read_muted_and_opened_are_silent(self):
        self.assertIsNone(self.decide(opened_chat=40, window_active=True))
        self.assertIsNotNone(self.decide(opened_chat=40, window_active=False))
        self.message.time_ms = 800
        self.assertIsNone(self.decide())
        self.message.time_ms = 1000
        self.message.outgoing = True
        self.assertIsNone(self.decide())
        self.message.outgoing = False
        self.chat.unread = 0
        self.assertIsNone(self.decide())
        self.chat.unread = 1
        self.chat.is_muted = True
        self.assertIsNone(self.decide())

    def test_dnd_future_and_forever_block_expired_allows(self):
        for value in (-1, 1200):
            self.preferences['settings']['DONT_DISTURB_UNTIL'] = value
            self.assertIsNone(self.decide())
        self.preferences['settings']['DONT_DISTURB_UNTIL'] = 1000
        self.assertIsNotNone(self.decide())

    def test_group_reply_without_metadata_and_disabled_groups_suppress(self):
        self.chat.is_group = True
        for value in ('OFF', 'REPLY'):
            self.preferences['settings']['CHATS_PUSH_NOTIFICATION'] = value
            self.assertIsNone(self.decide())

    def test_silent_preferences_never_allow_sound(self):
        self.preferences['settings']['PUSH_SOUND'] = '_NONE_'
        self.assertFalse(self.decide().sound)
        self.preferences['settings']['PUSH_SOUND'] = 'DEFAULT'
        self.chat.is_group = True
        self.preferences['settings']['CHATS_PUSH_SOUND'] = '_NONE_'
        self.assertFalse(self.decide().sound)

    def test_unavailable_config_and_local_off_suppress(self):
        self.assertIsNone(self.decide(local_enabled=False))
        self.preferences['available'] = False
        self.assertIsNone(self.decide())

    def test_pending_privacy_change_suppresses_previous_preview(self):
        self.preferences['pending'] = True
        self.assertIsNone(self.decide())


class NotificationUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_read_populates_without_write_and_preview_toggle_has_exact_bound_patch(self):
        service = FakeService()
        session = object()
        context = (service.owner, service.client, session)
        service._history_read_context = lambda: context
        def require_context(expected):
            if (expected[0] != service.owner or expected[1] is not service.client
                    or expected[2] is not session):
                raise ValueError('Synthetic stale notification context')
            return service.client
        service._require_history_context = require_context
        def cache_write(expected, callback):
            require_context(expected)
            result = callback()
            require_context(expected)
            return result
        service._history_cache_write = cache_write
        settings = SimpleNamespace(notifications=True, save=Mock())
        panel = NotificationSettingsPanel(service, settings)
        def synchronous(fn, *args, on_done=None, on_error=None, **kwargs):
            try:
                value = fn(*args, **kwargs)
            except Exception as error:
                on_error(str(error))
            else:
                on_done(value)
        with patch('maxclient.ui.notification_settings_panel.run_async', synchronous):
            panel.ensure_loaded()
            service.client.update_user_settings.assert_not_called()
            panel.controls['preview'].click()
            service.client.update_user_settings.assert_called_once_with(
                {'PUSH_DETAILS': False}, expected_session=session)
            self.assertTrue(panel.controls['preview'].isChecked())
            self.assertFalse(panel.controls['preview'].isEnabled())
            self.assertIn('не подтверждено', panel.status.text())
        settings.save.assert_not_called()
        panel.close()

    def test_toast_plaintext_and_dismiss_erases_content_without_audio(self):
        toast = MessageToast()
        toast.present(Notice(40, '<b>Sender</b>', '<img src="file:///synthetic">', False), show=False)
        self.assertEqual(toast.title.textFormat(), Qt.TextFormat.PlainText)
        self.assertEqual(toast.body.textFormat(), Qt.TextFormat.PlainText)
        self.assertFalse(toast.isVisible())
        toast.dismiss()
        self.assertEqual(toast.body.text(), '')
        self.assertIsNone(toast.chat_id)
        toast.close()


if __name__ == '__main__':
    unittest.main()
