"""Owned contact-card captions and menu provenance; no service or account access."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import copy
import re
import unittest
from unittest.mock import Mock

from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication

from maxclient.core.i18n import tr
from maxclient.ui.contact_profile import ContactProfilePanel, contact_menu_entries
from maxclient.ui.conversation_actions_menu import ConversationActionsMenu


class ContactProfileLocalizationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.panel = ContactProfilePanel(language='en')
        self.context = object()
        self.snapshot = {'context': self.context, 'peer_id': 303, 'chat_id': None,
                         'title': 'Добавить в контакты', 'phone': '<b>Телефон</b>',
                         'phone_label': tr('Телефон (сохранённый)', 'en'),
                         'phone_label_source': 'Телефон (сохранённый)',
                         'availability': {'chat': True, 'edit': False, 'more': True},
                         'tabs': {}, 'action_pending': False}
        self.membership = {'fresh': True, 'is_added': False, 'is_self': False,
                           'pending': False, 'availability': {'add': True}}
        self.panel.set_conversation(self.snapshot)
        self.panel.set_membership(self.membership)

    def tearDown(self):
        self.panel.close()
        self.panel.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()

    def test_early_base_constructor_retranslate_and_child_labels_are_safe(self):
        self.assertEqual(self.panel.add_button.text(), tr('Добавить в контакты', 'en'))
        self.assertEqual(self.panel.add_button.accessibleName(), tr('Добавить в контакты', 'en'))
        self.assertEqual(self.panel.membership_hint.text(), 'This person has not been added to your MAX contacts yet.')
        self.assertEqual(self.panel.phone_label.text(), 'Phone (saved)')
        self.assertTrue(self.panel.add_button.isEnabled())

    def test_locale_change_preserves_contact_identity_membership_pending_and_raw_data(self):
        p = self.panel
        self.membership['pending'] = True
        p.set_membership(self.membership, busy=True)
        snapshot, availability, reasons = p._snapshot, p._availability, p._membership_reasons
        enabled = {name: button.isEnabled() for name, button in p.buttons.items()}
        requested = Mock()
        p.add_requested.connect(requested)
        p.action_requested.connect(requested)
        p.menu_action_requested.connect(requested)
        for language in ('en', 'es', 'pt', 'fr', 'uz', 'be', 'ru'):
            p.retranslate(language)
            self.assertIs(p._context, self.context)
            self.assertIs(p._snapshot, snapshot)
            self.assertIs(p._availability, availability)
            self.assertIs(p._membership_reasons, reasons)
            self.assertEqual(p.name.text(), 'Добавить в контакты')
            self.assertEqual(p.phone.text(), '<b>Телефон</b>')
            self.assertEqual(p.phone_label.text(), tr('Телефон (сохранённый)', language))
            self.assertEqual({name: button.isEnabled() for name, button in p.buttons.items()}, enabled)
            self.assertFalse(p.add_button.isEnabled())
            self.assertFalse(p.membership_hint.isHidden())
        requested.assert_not_called()

    def test_unmarked_phone_label_equal_to_catalogue_key_stays_raw(self):
        snapshot = dict(self.snapshot)
        snapshot.pop('phone_label_source')
        snapshot['phone_label'] = 'Телефон (сохранённый)'
        self.panel.set_conversation(snapshot)
        self.panel.retranslate('fr')
        self.assertEqual(self.panel.phone_label.text(), 'Телефон (сохранённый)')

    def test_membership_and_busy_disabled_reasons_translate_without_enabling_actions(self):
        p = self.panel
        self.assertIn('Add to contacts', p.edit_button.toolTip())
        self.membership['is_added'] = True
        self.membership['pending'] = True
        p.set_membership(self.membership)
        p.retranslate('en')
        self.assertEqual(p.edit_button.toolTip(), 'Check the previous action’s status first.')
        p.set_membership(self.membership, busy=True)
        p.retranslate('fr')
        self.assertEqual(p.edit_button.toolTip(), 'Attendez la fin de l’action en cours.')
        self.assertFalse(p.edit_button.isEnabled())
        self.assertFalse(p.add_button.isEnabled())
        self.assertIn('appels', p.buttons['call'].toolTip())

    def test_clearing_profile_removes_old_membership_and_translation_does_not_restore_it(self):
        self.panel.clear()
        self.panel.retranslate('fr')
        self.assertIsNone(self.panel._context)
        self.assertTrue(self.panel.add_button.isHidden())
        self.assertTrue(self.panel.membership_hint.isHidden())
        self.assertEqual(self.panel._membership_reasons, {})

    def test_deleted_profile_ignores_late_retranslate(self):
        deleted = ContactProfilePanel(language='en')
        deleted.deleteLater()
        QCoreApplication.sendPostedEvents(deleted, QEvent.Type.DeferredDelete)
        deleted.retranslate('fr')

    def test_contact_menu_marks_all_owned_labels_and_reasons_without_changing_capabilities(self):
        snapshot = {'title': 'Заблокировать', 'fresh': True, 'is_added': False, 'pending': True,
                    'availability': {'chat': True, 'block': True, 'delete': True, 'hide_local': True}}
        original = copy.deepcopy(snapshot)
        for profile in (False, True):
            baseline = contact_menu_entries(snapshot, profile=profile)
            for language in ('en', 'es', 'pt', 'fr', 'uz', 'be'):
                rows = contact_menu_entries(snapshot, profile=profile, language=language)
                self.assertEqual(len(rows), len(baseline))
                for old, row in zip(baseline, rows):
                    for key in ('action', 'available', 'write', 'danger', 'separator_before'):
                        self.assertEqual(row.get(key), old.get(key))
                    self.assertEqual(row['label'], tr(row['label_source'], language))
                    if row.get('available') is not True:
                        self.assertEqual(row['unavailable_reason'], tr(row['unavailable_reason_source'], language))
                    if language != 'be':
                        self.assertIsNone(re.search('[А-Яа-яЁё]', row['label']))
                        self.assertIsNone(re.search('[А-Яа-яЁё]', row.get('unavailable_reason', '')))
        self.assertEqual(snapshot, original)

    def test_common_menu_uses_sources_on_retranslate_and_cancelled_actions_stay_revoked(self):
        snapshot = {'fresh': True, 'is_added': True, 'availability': {'chat': True}}
        menu = ConversationActionsMenu(self.context, contact_menu_entries(snapshot, language='en'),
                                       self.panel, language='en', native_rows=True)
        calls = Mock()
        menu.requested.connect(calls)
        actions = menu.actions()
        try:
            self.assertEqual(actions[0].text(), tr('Написать', 'en'))
            self.assertEqual(actions[1].toolTip(), 'Calls are not available in this build yet.')
            menu.cancel()  # MainWindow owns closing old popups at a locale change.
            menu.set_language('fr')
            self.assertEqual(actions[0].text(), tr('Написать', 'fr'))
            self.assertEqual(actions[1].toolTip(), 'Les appels ne sont pas encore disponibles dans cette version.')
            actions[0].trigger()
            calls.assert_not_called()
        finally:
            menu.deleteLater()


if __name__ == '__main__':
    unittest.main()
