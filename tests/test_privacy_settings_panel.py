"""Synthetic Qt checks for actual privacy values and single explicit writes."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import unittest
from unittest.mock import Mock, patch

from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication
from maxclient.ui.privacy_settings_panel import PrivacySettingsPanel


class PrivacyPanelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.service = Mock()
        self.context = (101, object(), object())
        self.service._history_read_context.return_value = self.context
        def require_context(context):
            if context is not self.context:
                raise ValueError('Synthetic stale context')
            return context[1]
        self.service._require_history_context.side_effect = require_context
        self.patch = patch('maxclient.ui.privacy_settings_panel.run_async')
        self.run = self.patch.start()
        self.panel = PrivacySettingsPanel(self.service)
        self.panel.resize(700, 900)
        self.panel.show()

    def tearDown(self):
        self.panel.close()
        self.panel.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()
        self.patch.stop()
        self.service.update_privacy_settings.assert_not_called()

    def loaded(self, **extra):
        self.panel._read_done({'SEARCH_BY_PHONE': 'ALL', 'INCOMING_CALL': 'CONTACTS',
            'CHATS_INVITE': 'ALL', 'PHONE_NUMBER_PRIVACY': 'CONTACTS', 'HIDDEN': False,
            'CONTENT_LEVEL_ACCESS': False, **extra})

    def choose(self, key, value):
        menu = self.panel.open_menu(key)
        if menu is None:
            return
        self.app.processEvents()
        action = next(action for action in menu.actions()
                      if type(action.data()) is type(value) and action.data() == value)
        action.trigger()

    def assert_queued(self, method, *args):
        self.assertEqual(self.run.call_args.args, (method, *args))
        self.assertIs(self.run.call_args.kwargs['context'], self.context)
        self.assertTrue(callable(self.run.call_args.kwargs['on_done']))
        self.assertTrue(callable(self.run.call_args.kwargs['on_error']))

    def complete(self, result):
        self.run.call_args.kwargs['on_done'](result)

    def fail(self, error='unknown'):
        self.run.call_args.kwargs['on_error'](error)

    def test_population_never_writes_and_missing_fields_are_unknown_disabled(self):
        self.panel._read_done({'HIDDEN': False})
        self.run.assert_not_called()
        self.assertIs(self.panel.rows['HIDDEN'].current_value, False)
        self.assertFalse(self.panel.rows['INCOMING_CALL'].isEnabled())
        self.assertEqual(self.panel.rows['INCOMING_CALL'].value_label.text(), 'Неизвестно')
        self.assertIsNone(self.panel.open_menu('INCOMING_CALL'))

    def test_ui_choices_and_hidden_boolean_mapping_are_exact(self):
        self.loaded()
        for key in ('SEARCH_BY_PHONE', 'INCOMING_CALL', 'CHATS_INVITE'):
            menu = self.panel.open_menu(key)
            self.assertEqual([action.data() for action in menu.actions()], ['CONTACTS', 'ALL'])
            self.assertTrue(all(action.isCheckable() for action in menu.actions()))
            self.assertEqual(sum(action.isChecked() for action in menu.actions()), 1)
        hidden = self.panel.open_menu('HIDDEN')
        self.assertEqual([(action.text(), action.data()) for action in hidden.actions()],
                         [('никто', True), ('могут контакты', False)])
        self.assertEqual([action.data() for action in self.panel.open_menu('PHONE_NUMBER_PRIVACY').actions()],
                         ['ALL', 'CONTACTS', 'NOBODY'])
        self.assertEqual(list(self.panel.rows), ['INCOMING_CALL', 'SEARCH_BY_PHONE', 'CHATS_INVITE',
                                               'CONTENT_LEVEL_ACCESS', 'HIDDEN', 'PHONE_NUMBER_PRIVACY'])
        self.run.assert_not_called()

    def test_content_is_editable_with_exact_boolean_choices_and_bound_intent(self):
        self.loaded()
        row = self.panel.rows['CONTENT_LEVEL_ACCESS']
        self.assertTrue(row.isEnabled())
        menu = self.panel.open_menu('CONTENT_LEVEL_ACCESS')
        self.assertEqual([(a.text(), a.data()) for a in menu.actions()], [('весь', False), ('безопасный', True)])
        self.assertTrue(all(type(a.data()) is bool for a in menu.actions()))
        self.choose('CONTENT_LEVEL_ACCESS', True)
        self.assertIs(row.current_value, False)
        self.assertEqual(row.value_label.text(), 'весь')
        self.assert_queued(self.service.update_privacy_settings, {'CONTENT_LEVEL_ACCESS': True})
        self.assertEqual(self.run.call_count, 1)

    def test_current_server_nobody_is_displayed_without_becoming_an_offered_new_choice(self):
        self.loaded(INCOMING_CALL='NOBODY', CHATS_INVITE='NOBODY')
        for key in ('INCOMING_CALL', 'CHATS_INVITE'):
            row = self.panel.rows[key]
            self.assertEqual(row.current_value, 'NOBODY')
            self.assertEqual(row.value_label.text(), 'никто')
            menu = self.panel.open_menu(key)
            current = next(action for action in menu.actions() if action.data() == 'NOBODY')
            self.assertIn('текущее значение', current.text())
            self.assertTrue(current.isChecked())
            self.assertFalse(current.isEnabled())
            self.assertTrue(row.isEnabled())
            self.assertEqual([a.data() for a in menu.actions() if a.isEnabled()], ['CONTACTS', 'ALL'])
        self.run.assert_not_called()

    def test_one_user_write_keeps_confirmed_value_and_prevents_parallel_action(self):
        self.loaded()
        self.choose('HIDDEN', True)
        self.assertIs(self.panel.rows['HIDDEN'].current_value, False)
        self.assertTrue(self.panel._busy)
        self.assertFalse(self.panel.rows['PHONE_NUMBER_PRIVACY'].isEnabled())
        self.assertEqual(self.run.call_count, 1)
        self.assert_queued(self.service.update_privacy_settings, {'HIDDEN': True})
        self.choose('SEARCH_BY_PHONE', 'CONTACTS')
        self.assertEqual(self.run.call_count, 1)
        self.assertEqual(self.panel.rows['SEARCH_BY_PHONE'].current_value, 'ALL')

    def test_pending_ack_does_not_show_requested_value_until_refresh_supplies_it(self):
        self.loaded()
        self.choose('HIDDEN', True)
        self.complete({'accepted': True, 'pending': True, 'settings': {'HIDDEN': True}})
        self.assertIs(self.panel.rows['HIDDEN'].current_value, False)
        self.assertIn('не подтверждено', self.panel.status.text())
        self.assertFalse(self.panel.rows['HIDDEN'].isEnabled())
        self.choose('HIDDEN', True)
        self.assertEqual(self.run.call_count, 1)
        self.panel.refresh()
        self.assert_queued(self.service.get_privacy_settings)
        self.complete({'HIDDEN': True})
        self.assertIs(self.panel.rows['HIDDEN'].current_value, True)
        self.assertFalse(self.panel._unconfirmed)
        self.assertTrue(self.panel.rows['HIDDEN'].isEnabled())
        self.assertEqual(self.run.call_count, 2)

    def test_matching_confirmed_ack_applies_actual_value_with_no_signal_write(self):
        self.loaded()
        self.choose('SEARCH_BY_PHONE', 'CONTACTS')
        self.complete({'accepted': True, 'pending': False, 'settings': {'SEARCH_BY_PHONE': 'CONTACTS'}})
        self.assertEqual(self.panel.rows['SEARCH_BY_PHONE'].current_value, 'CONTACTS')
        self.assertEqual(self.run.call_count, 1)
        self.assertEqual(self.panel.status.text(), '')

    def test_unknown_write_disables_only_unresolved_key_until_matching_readback(self):
        self.loaded()
        self.choose('HIDDEN', True)
        self.fail()
        self.assertFalse(self.panel.rows['HIDDEN'].isEnabled())
        self.assertTrue(self.panel.rows['PHONE_NUMBER_PRIVACY'].isEnabled())
        self.panel._read_done({'HIDDEN': False})
        self.assertFalse(self.panel.rows['HIDDEN'].isEnabled())
        self.choose('HIDDEN', True)
        self.assertEqual(self.run.call_count, 1)
        self.panel._read_done({'HIDDEN': True})
        self.assertTrue(self.panel.rows['HIDDEN'].isEnabled())

    def test_noop_selection_and_manual_population_make_no_rpc(self):
        self.loaded()
        self.choose('SEARCH_BY_PHONE', 'ALL')
        self.loaded(PHONE_NUMBER_PRIVACY='NOBODY', HIDDEN=True)
        self.run.assert_not_called()

    def test_family_and_safe_mode_lock_exactly_the_four_guarded_rows(self):
        for flags in ({'FAMILY_PROTECTION': 'MANAGEABLE', 'SAFE_MODE': False},
                      {'FAMILY_PROTECTION': 'OFF', 'SAFE_MODE': True}):
            with self.subTest(flags=flags):
                self.loaded(**flags, CONTENT_LEVEL_ACCESS=True)
                for key in ('SEARCH_BY_PHONE', 'INCOMING_CALL', 'CHATS_INVITE', 'CONTENT_LEVEL_ACCESS'):
                    self.assertFalse(self.panel.rows[key].isEnabled())
                    self.assertIsNone(self.panel.open_menu(key))
                self.assertTrue(self.panel.rows['PHONE_NUMBER_PRIVACY'].isEnabled())
                self.assertTrue(self.panel.rows['HIDDEN'].isEnabled())
                self.assertNotIn('FAMILY_PROTECTION', self.panel.rows)
                self.assertNotIn('SAFE_MODE', self.panel.rows)
                self.assertFalse(self.panel.family_row.isEnabled())
                self.assertFalse(self.panel.safe_mode_row.isEnabled())
                self.assertEqual(self.panel.content_status.text(), 'безопасный')
        self.loaded(FAMILY_PROTECTION='MANAGEABLE')
        self.assertEqual(self.panel.family_status.text(), 'Профиль под защитой')
        self.run.assert_not_called()

    def test_failure_and_inconsistent_ack_never_retry_or_fake_value(self):
        self.loaded()
        self.choose('HIDDEN', True)
        self.complete({'accepted': True, 'pending': False, 'settings': {'HIDDEN': False}})
        self.assertIs(self.panel.rows['HIDDEN'].current_value, False)
        self.assertIn('Не удалось подтвердить', self.panel.status.text())
        self.assertEqual(self.run.call_count, 1)

    def test_config_refresh_during_write_is_one_sequential_getter(self):
        self.loaded()
        self.choose('HIDDEN', True)
        self.panel.refresh(); self.panel.refresh()
        self.assertEqual(self.run.call_count, 1)
        self.complete({'accepted': True, 'pending': True, 'settings': {'HIDDEN': False}})
        self.assertEqual(self.run.call_count, 2)
        self.assert_queued(self.service.get_privacy_settings)
        self.assertTrue(self.panel._busy)

    def test_read_failure_is_distinct_from_empty_and_disables_edits(self):
        self.loaded()
        self.panel._read_error('synthetic private detail')
        self.assertIn('Не удалось загрузить', self.panel.status.text())
        self.assertNotIn('synthetic private detail', self.panel.status.text())
        self.assertFalse(any(row.isEnabled() for row in self.panel.rows.values()))
        self.assertTrue(self.panel.refresh_button.isEnabled())


if __name__ == '__main__':
    unittest.main()
