"""Disabled row-menu reasons translate by provenance without changing actions."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import copy
import re
import unittest

from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication

from maxclient.core._chat_row_reason_translations import CHAT_ROW_REASON_TRANSLATIONS
from maxclient.core.i18n import tr
from maxclient.ui.chat_row_menu import chat_row_entries, flatten_entries
from maxclient.ui.conversation_actions_menu import ConversationActionsMenu


class ChatRowReasonLocalizationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def snapshot(self):
        return {'action_capabilities': {'supported': True, 'kind': 'dialog', 'last_message': True, 'mute': True},
                'availability': {'mute': False}, 'row_menu': {'chat_status': 'ACTIVE'},
                'more_actions': [{'action': 'delete', 'available': False},
                                 {'action': 'block', 'available': False},
                                 {'action': 'folder', 'available': False}]}

    def rows(self, snapshot=None, **kwargs):
        return {r['action']: r for r in flatten_entries(chat_row_entries(snapshot or self.snapshot(), **kwargs))}

    def test_all_six_catalogue_columns_are_integrated(self):
        self.assertEqual(len(CHAT_ROW_REASON_TRANSLATIONS), 6)
        for source, values in CHAT_ROW_REASON_TRANSLATIONS.items():
            self.assertEqual(len(values), 6)
            for lang, value in zip(('en', 'es', 'pt', 'fr', 'uz', 'be'), values):
                self.assertEqual(tr(source, lang), value)
                if lang != 'be':
                    self.assertIsNone(re.search('[А-Яа-яЁё]', value))
            self.assertEqual(tr(source, 'pt-BR'), values[2])

    def test_default_unknown_and_unimplemented_reasons_are_translated_and_marked(self):
        rows = self.rows(language='en')
        expected = {'preview': 'Preview availability could not be confirmed.',
                    'toggle_read': 'The current read status could not be confirmed.',
                    'toggle_pin': 'The pin status in the selected folder could not be confirmed.',
                    'delete': 'Deleting this type of chat is not available in this build yet.',
                    'block': 'This action’s status has not been confirmed yet.'}
        for action, text in expected.items():
            self.assertFalse(rows[action]['available'])
            self.assertEqual(rows[action]['unavailable_reason'], text)
            self.assertEqual(tr(rows[action]['unavailable_reason_source'], 'en'), text)

    def test_pending_reason_preserves_uncertainty_and_does_not_enable_actions(self):
        snapshot = self.snapshot()
        snapshot['action_pending'] = True
        for lang in ('ru', 'en', 'es', 'pt', 'fr', 'uz', 'be'):
            rows = self.rows(snapshot, language=lang)
            for action in ('preview', 'toggle_read', 'toggle_pin', 'delete', 'block'):
                self.assertFalse(rows[action]['available'])
                self.assertEqual(rows[action]['unavailable_reason_source'], 'Сначала дождитесь подтверждения текущего действия.')
                self.assertEqual(rows[action]['unavailable_reason'], tr(rows[action]['unavailable_reason_source'], lang))

    def test_unmarked_external_reason_equal_to_a_key_is_not_translated_or_marked(self):
        snapshot = self.snapshot()
        external = 'Состояние этого действия ещё не подтверждено.'
        snapshot['more_actions'][0]['unavailable_reason'] = external
        before = copy.deepcopy(snapshot)
        row = self.rows(snapshot, language='en')['delete']
        self.assertEqual(row['unavailable_reason'], external)
        self.assertNotIn('unavailable_reason_source', row)
        self.assertEqual(snapshot, before)

    def test_marked_upstream_reason_keeps_source_when_snapshot_language_differs(self):
        snapshot = self.snapshot()
        source = 'Жалоба на этот чат уже отправлена.'
        snapshot['more_actions'][0].update(unavailable_reason=tr(source, 'fr'), unavailable_reason_source=source)
        before = copy.deepcopy(snapshot)
        row = self.rows(snapshot, language='en')['delete']
        self.assertEqual(row['unavailable_reason_source'], source)
        self.assertEqual(row['unavailable_reason'], tr(source, 'en'))
        self.assertEqual(snapshot, before)

    def test_language_changes_no_action_identity_scope_values_or_availability(self):
        snapshot = self.snapshot()
        baseline = list(flatten_entries(chat_row_entries(snapshot, language='ru')))
        before = copy.deepcopy(snapshot)
        for lang in ('en', 'es', 'pt', 'fr', 'uz', 'be'):
            rows = list(flatten_entries(chat_row_entries(snapshot, language=lang)))
            self.assertEqual(len(rows), len(baseline))
            for a, b in zip(baseline, rows):
                for key in ('action', 'value', 'available', 'write', 'danger', 'icon', 'separator_before'):
                    self.assertEqual(a.get(key), b.get(key))
                if 'unavailable_reason_source' in b:
                    self.assertEqual(b['unavailable_reason'], tr(b['unavailable_reason_source'], lang))
        self.assertEqual(snapshot, before)

    def test_folder_names_and_unmarked_reasons_remain_raw(self):
        folders = [{'action': 'folder_include', 'label': 'Состояние этого действия ещё не подтверждено.',
                    'unavailable_reason': 'Не удалось подтвердить текущее состояние прочтения.',
                    'available': False, 'write': True, 'value': {'folder_id': 'synthetic-folder'}}]
        row = self.rows(language='en', folders=folders)['folder_include']
        self.assertEqual(row['label'], folders[0]['label'])
        self.assertEqual(row['unavailable_reason'], folders[0]['unavailable_reason'])
        self.assertNotIn('unavailable_reason_source', row)

    def test_shared_menu_retranslates_owned_tooltip_but_not_external_reason(self):
        snapshot = self.snapshot()
        external = 'Не удалось подтвердить текущее состояние прочтения.'
        snapshot['more_actions'][0]['unavailable_reason'] = external
        rows = chat_row_entries(snapshot, language='en')
        menu = ConversationActionsMenu(object(), rows, language='en')
        try:
            actions = {r['action']: a for a, r, submenu in menu._rows if submenu is None}
            self.assertEqual(actions['preview'].toolTip(), 'Preview availability could not be confirmed.')
            self.assertEqual(actions['delete'].toolTip(), external)
            menu.set_language('fr')
            self.assertEqual(actions['preview'].toolTip(), 'Impossible de confirmer la disponibilité de l’aperçu.')
            self.assertEqual(actions['delete'].toolTip(), external)
            self.assertFalse(actions['delete'].isEnabled())
        finally:
            menu.deleteLater()
            QCoreApplication.sendPostedEvents(menu, QEvent.Type.DeferredDelete)


if __name__ == '__main__':
    unittest.main()
