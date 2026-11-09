"""Synthetic menu and confirmation localization; no service or RPC is created."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import copy
from string import Formatter
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QDialogButtonBox, QWidget

from maxclient.core._conversation_management_translations import CONVERSATION_MANAGEMENT_TRANSLATIONS
from maxclient.core.i18n import SUPPORTED_LANGUAGES, tr, translation_available
from maxclient.core.models import Chat
from maxclient.ui.conversation_actions_menu import ConversationActionsMenu, SOUND_CHOICES
from maxclient.ui.conversation_management_dialogs import (
    ShareContactDialog, confirm_conversation_removal,
    accepted_lifecycle_pending_notice, confirmed_lifecycle_action,
)
from qt_test_lifecycle import capture_qt_state, restore_qt_state
from test_desktop_shell import _load_offscreen_font


class ConversationManagementLocalizationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.addCleanup(restore_qt_state, capture_qt_state())
        _load_offscreen_font(self.app)
        self.host = QWidget()
        self.host.settings = SimpleNamespace(ui_language='en')
        self.context = object()

    def menu(self, entries, **kwargs):
        return ConversationActionsMenu(self.context, entries, self.host, **kwargs)

    def show(self, menu):
        menu.popup(QPoint(20, 20))
        self.app.processEvents()

    def test_catalog_columns_placeholders_and_six_actual_lookups(self):
        self.assertEqual(len(CONVERSATION_MANAGEMENT_TRANSLATIONS), 17)
        fields = lambda value: {name for _, name, _, _ in Formatter().parse(value) if name}
        for source, translations in CONVERSATION_MANAGEMENT_TRANSLATIONS.items():
            self.assertEqual(len(translations), 6)
            for language, translated in zip(SUPPORTED_LANGUAGES[1:], translations):
                self.assertTrue(translation_available(source, language), (source, language))
                self.assertEqual(fields(source), fields(translated))
                self.assertEqual(tr(source, language), translated)

    def test_search_stories_and_bot_actions_have_explicit_english_labels(self):
        from maxclient.core.conversation_info_service import _ACTION_LABELS
        keys = ('search', 'hide_stories', 'show_stories', 'stop_bot')
        entries = [{'action': key, 'label': _ACTION_LABELS[key],
                    'label_source': _ACTION_LABELS[key], 'available': True}
                   for key in keys]
        menu = self.menu(entries, language='en', native_rows=True)
        expected = ['Search in chat', 'Hide stories', 'Show stories', 'Stop bot']
        self.assertEqual([action.text() for action in menu.actions()], expected)
        self.assertEqual([menu._display_label(row) for row in entries], expected)

    def test_all_emitted_info_menu_entries_and_reasons_are_translated(self):
        # Exercise the production producer, including conditional labels. Merely
        # checking keys already present in the catalog missed these live gaps.
        import test_conversation_info_service as fixture
        from maxclient.core.conversation_info_service import _ACTION_LABELS
        f = fixture.ConversationInfoServiceTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        service = f.service
        service.get_conversation_action_state = Mock()
        service.conversation_action_status = Mock(return_value={})
        service.conversation_report_status = Mock(return_value={})
        service.owner_transfer_available = Mock(return_value=False)
        service.bot_delete_available = Mock(return_value=True)
        writes = []
        for name in ('get_conversation_folders', 'set_conversation_contact_blocked',
                     'set_conversation_stories_hidden', 'stop_conversation_bot',
                     'get_conversation_report_reasons', 'submit_conversation_report',
                     'clear_conversation_history', 'delete_conversation', 'leave_conversation',
                     'share_conversation_contact', 'refresh_conversation_controls'):
            method = Mock(side_effect=AssertionError('Menu rendering must not invoke actions'))
            setattr(service, name, method)
            writes.append(method)
        group = {'type': 'CHAT', 'owner': 202, 'joinTime': 1, 'participantsCount': 2}
        channel = dict(group, type='CHANNEL')
        scenarios = [
            ('dialog', {}, {}, {}, {}),
            ('hidden-stories', {}, {'flags': 1024}, {}, {}),
            ('blocked-peer', {}, {'status': 'BLOCKED'}, {}, {}),
            ('group', group, {}, {}, {}),
            ('owner-group', dict(group, owner=101), {}, {}, {}),
            ('channel', channel, {}, {}, {}),
            ('owner-channel', dict(channel, owner=101), {}, {}, {}),
            ('bot', {'hasBots': True}, {}, {}, {}),
            ('stopped-bot', {'hasBots': True, 'isSuspended': True}, {}, {}, {}),
            ('pending', {}, {}, {'pending': True}, {}),
            ('bot-continuation', {'hasBots': True, 'isSuspended': True}, {},
             {'pending': True, 'bot_delete_state': {'can_continue_delete': True}}, {}),
            ('owner-continuation', group, {},
             {'pending': True, 'owner_transfer_state': {'can_continue_leave': True}}, {}),
            ('report-pending', channel, {}, {}, {'pending': True}),
            ('already-reported', channel, {}, {}, {'already_reported': True}),
        ]
        emitted = set()
        sources = set()
        changes = f.store._db.total_changes
        for name, raw_fields, contact_fields, state, report in scenarios:
            service._server_chats[303] = fixture.chat_dto(**raw_fields)
            service._server_contacts[202] = {'id': 202, 'updateTime': fixture.STAMP,
                                           'names': [{'type': 'ONEME', 'firstName': 'Raw peer'}],
                                           **contact_fields}
            service.conversation_action_status.return_value = state
            service.conversation_report_status.return_value = report
            context = service.conversation_context(303)
            rows = service.conversation_snapshot(context)['more_actions']
            original = copy.deepcopy(rows)
            for language in SUPPORTED_LANGUAGES[1:]:
                with self.subTest(scenario=name, language=language):
                    menu = ConversationActionsMenu(context, rows, self.host,
                                                   language=language, native_rows=True)
                    actions = [action for action in menu.actions() if not action.isSeparator()]
                    self.assertEqual(len(actions), len(rows))
                    for row, action in zip(rows, actions):
                        emitted.add(row['action'])
                        source = row['label_source']
                        sources.add(source)
                        self.assertTrue(translation_available(source, language), (source, language))
                        self.assertEqual(action.text(), tr(source, language).replace('&', '&&'))
                        self.assertEqual(menu._display_label(row), tr(source, language))
                        reason = row.get('unavailable_reason_source')
                        if reason:
                            self.assertTrue(translation_available(reason, language), (reason, language))
                            self.assertEqual(action.toolTip(), tr(reason, language))
                        if language != 'be':
                            self.assertFalse(any('\u0400' <= char <= '\u04ff'
                                                 for char in action.text() + action.toolTip()))
                    menu.deleteLater()
            self.assertEqual(rows, original)
        self.assertTrue(set(_ACTION_LABELS) <= emitted, set(_ACTION_LABELS) - emitted)
        self.assertTrue({'transfer_owner_leave', 'continue_owner_leave',
                         'continue_bot_delete', 'refresh_actions'} <= emitted)
        self.assertTrue({'Удалить и остановить', 'Отписаться от канала'} <= sources)
        for method in writes:
            method.assert_not_called()
        f.client.request.assert_not_called()
        f.client.chat_info.assert_not_called()
        f.client.contact_info.assert_not_called()
        self.assertEqual(f.store._db.total_changes, changes)

    def test_share_inherits_or_overrides_language_without_translating_raw_values(self):
        title = 'Удалить чат <raw> & {title}'
        dialog = ShareContactDialog(title, [Chat(0, 'Найти чат'), Chat(2**40, 'Найти чат')], self.host)
        self.assertEqual(dialog.windowTitle(), 'Share contact')
        self.assertIn(title, dialog.heading.text())
        self.assertEqual(dialog.heading.textFormat(), Qt.TextFormat.PlainText)
        self.assertEqual(dialog.search.placeholderText(), 'Find a chat')
        self.assertEqual([dialog.chats.item(i).text() for i in range(2)], ['Найти чат'] * 2)
        explicit = ShareContactDialog(title, [], self.host, language='fr')
        self.assertEqual(explicit.search.placeholderText(), 'Rechercher une conversation')

    def test_share_language_roundtrip_preserves_search_selection_and_identity(self):
        dialog = ShareContactDialog('Raw <contact>', [Chat(11, 'Same'), Chat(2**40, 'Same')], self.host)
        dialog.chats.setCurrentRow(1)
        dialog.search.setText('SaMe')
        for language in SUPPORTED_LANGUAGES:
            dialog.set_language(language)
            self.assertEqual(dialog.search.text(), 'SaMe')
            self.assertEqual(dialog.chats.currentRow(), 1)
            self.assertTrue(dialog.send_button.isEnabled())
            self.assertIsNone(dialog.selected_chat_id)
            self.assertIn('Raw <contact>', dialog.heading.text())
            self.assertEqual(dialog.chats.item(1).data(Qt.ItemDataRole.UserRole), 2**40)
        dialog._choose()
        self.assertEqual(dialog.selected_chat_id, 2**40)

    def test_hidden_share_recipient_stays_unavailable_after_translation(self):
        dialog = ShareContactDialog('Raw', [Chat(11, 'Alpha')], self.host)
        dialog.chats.setCurrentRow(0)
        dialog.search.setText('No match')
        for language in SUPPORTED_LANGUAGES:
            dialog.set_language(language)
            self.assertFalse(dialog.send_button.isEnabled())
            dialog._choose()
            self.assertIsNone(dialog.selected_chat_id)

    def test_share_default_enter_and_escape_cancel_even_with_selected_recipient(self):
        for key in (Qt.Key.Key_Return, Qt.Key.Key_Escape):
            dialog = ShareContactDialog('Raw', [Chat(11, 'Alpha')], self.host)
            dialog.chats.setCurrentRow(0)
            dialog.retranslate('fr')
            cancel = dialog.buttons.button(QDialogButtonBox.StandardButton.Cancel)
            self.assertTrue(cancel.isDefault())
            self.assertFalse(dialog.send_button.autoDefault())
            dialog.show()
            dialog.search.setFocus()
            self.app.processEvents()
            QTest.keyClick(dialog.search, key)
            self.assertEqual(dialog.result(), QDialog.DialogCode.Rejected)
            self.assertIsNone(dialog.selected_chat_id)
            self.assertFalse(dialog.isVisible())

    def test_removal_full_templates_keep_raw_title_and_scope_in_every_language(self):
        raw = 'Raw <b>title</b> & {title}'
        for language in SUPPORTED_LANGUAGES:
            for action in ('clear', 'delete'):
                for forced in (False, True):
                    with self.subTest(language=language, action=action, forced=forced):
                        def inspect(dialog):
                            self.assertIn(raw, dialog.body.text())
                            self.assertEqual(dialog.body.textFormat(), Qt.TextFormat.PlainText)
                            self.assertFalse(dialog.choice.isChecked())
                            self.assertTrue(dialog.choice.isHidden())
                            self.assertTrue(dialog.buttons.button(QDialogButtonBox.StandardButton.Cancel).isDefault())
                            self.assertFalse(dialog.buttons.button(QDialogButtonBox.StandardButton.Ok).autoDefault())
                            if language == 'en':
                                self.assertIn('for all participants?' if forced else 'only for you?', dialog.body.text())
                            return QDialog.DialogCode.Accepted
                        caps = {'kind': 'group' if forced else 'dialog', 'clear_for_all': forced}
                        with patch.object(QDialog, 'exec', new=inspect):
                            result = confirm_conversation_removal(self.host, action, raw, caps, language=language)
                        self.assertIs(result, forced)

    def test_optional_choice_survives_language_change_and_still_requires_acceptance(self):
        for accepted in (False, True):
            def inspect(dialog):
                self.assertFalse(dialog.choice.isChecked())
                self.assertFalse(dialog.choice.isHidden())
                dialog.choice.setChecked(True)
                for language in SUPPORTED_LANGUAGES:
                    dialog.retranslate(language)
                    self.assertTrue(dialog.choice.isChecked())
                    self.assertFalse(dialog.choice.isHidden())
                return QDialog.DialogCode.Accepted if accepted else QDialog.DialogCode.Rejected
            with patch.object(QDialog, 'exec', new=inspect):
                result = confirm_conversation_removal(self.host, 'delete', 'Raw',
                    {'kind': 'dialog', 'delete_for_all_optional': True})
            self.assertIs(result, True if accepted else None)

    def test_removal_enter_and_escape_cancel_instead_of_deleting(self):
        for key in (Qt.Key.Key_Return, Qt.Key.Key_Escape):
            def inspect(dialog):
                dialog.retranslate('uz')
                dialog.show()
                dialog.choice.setFocus()
                self.app.processEvents()
                QTest.keyClick(dialog, key)
                self.assertFalse(dialog.isVisible())
                return dialog.result()
            with patch.object(QDialog, 'exec', new=inspect):
                self.assertIsNone(confirm_conversation_removal(self.host, 'delete', 'Raw',
                    {'kind': 'dialog', 'delete_for_all_optional': True}))

    def test_only_marked_labels_translate_and_custom_tooltips_stay_literal(self):
        entries = [
            {'action': 'delete', 'label': 'Удалить чат', 'label_source': 'Удалить чат', 'available': False,
             'unavailable_reason_source': 'Действие недоступно'},
            {'action': 'folder_include', 'label': 'Удалить чат', 'available': False,
             'unavailable_reason': 'Действие недоступно'},
            {'action': 'custom', 'label': '<Raw> & Name', 'available': True},
        ]
        original = copy.deepcopy(entries)
        menu = self.menu(entries)
        self.assertEqual(menu.actions()[0].text(), 'Delete chat')
        self.assertEqual(menu.actions()[0].toolTip(), 'Action unavailable')
        self.assertEqual(menu.actions()[1].text(), 'Удалить чат')
        self.assertEqual(menu.actions()[1].toolTip(), 'Действие недоступно')
        self.assertEqual(menu.actions()[2].text(), '<Raw> && Name')
        for language in SUPPORTED_LANGUAGES:
            menu.set_language(language)
            self.assertEqual(menu.actions()[1].text(), 'Удалить чат')
            self.assertEqual(menu.actions()[1].toolTip(), 'Действие недоступно')
            self.assertEqual(entries, original)

    def test_builtin_unavailable_reasons_translate_but_custom_fallback_does_not(self):
        menu = self.menu([{'action': action, 'label': 'Raw', 'available': False}
                          for action in ('report', 'leave', 'delete', 'mute', 'unknown')])
        for language in SUPPORTED_LANGUAGES:
            menu.set_language(language)
            self.assertTrue(all(action.toolTip() for action in menu.actions()))
            if language in ('en', 'es', 'pt', 'fr', 'uz'):
                self.assertFalse(any('\u0400' <= char <= '\u04ff'
                                     for action in menu.actions() for char in action.toolTip()))
        literal = self.menu([{'action': 'unknown', 'label': 'Raw', 'available': False}],
                            unavailable_label='Действие недоступно')
        self.assertEqual(literal.actions()[0].toolTip(), 'Действие недоступно')

    def test_submenu_language_keeps_exact_context_copied_value_and_server_check(self):
        value = {'folder_id': 'synthetic', 'included': False}
        menu = self.menu([{'label': 'Папки', 'label_source': 'Папки', 'available': True,
            'children': [{'action': 'folder_include', 'label': 'Удалить чат', 'available': True,
                          'checked': True, 'value': value}]}])
        child = menu._submenus[0]
        action = child.actions()[0]
        received = Mock()
        menu.requested.connect(received)
        value['folder_id'] = 'mutated-caller'
        menu.set_language('fr')
        self.assertEqual(action.text(), 'Удалить чат')
        self.assertTrue(action.isChecked())
        self.show(child)
        action.trigger()
        self.assertTrue(action.isChecked())
        received.assert_called_once_with('folder_include', {'folder_id': 'synthetic', 'included': False}, self.context)

    def test_sound_translation_preserves_all_duration_values(self):
        for language in SUPPORTED_LANGUAGES:
            for source, duration in SOUND_CHOICES:
                menu = self.menu([{'action': 'mute', 'label': source, 'label_source': source,
                                   'available': True, 'value': duration}], language=language)
                received = Mock()
                menu.requested.connect(received)
                self.show(menu)
                menu.actions()[0].trigger()
                received.assert_called_once_with('mute', duration, self.context)

    def test_retranslation_does_not_revive_revoked_or_pending_actions(self):
        gate = {'allowed': False}
        menu = self.menu([{'action': 'delete', 'label': 'Удалить чат', 'label_source': 'Удалить чат',
                           'available': True}], allowed=lambda _row: gate['allowed'])
        received = Mock()
        menu.requested.connect(received)
        self.show(menu)
        action = menu.actions()[0]
        for language in SUPPORTED_LANGUAGES:
            menu.set_language(language)
            self.assertFalse(action.isEnabled())
            action.trigger()
        menu.cancel()
        gate['allowed'] = True
        menu.set_language('en')
        self.assertFalse(action.isEnabled())
        action.trigger()
        received.assert_not_called()

    def test_native_painter_and_qaction_use_the_same_translated_label(self):
        menu = self.menu([{'action': 'delete', 'label': 'Удалить чат', 'label_source': 'Удалить чат',
                           'available': True}], native_rows=True)
        self.show(menu)
        menu.set_language('en')
        with patch('maxclient.ui.conversation_actions_menu.QPainter') as painter_type:
            painter = painter_type.return_value
            painter.fontMetrics.return_value.elidedText.side_effect = lambda text, *_: text
            menu.grab()
        self.assertTrue(painter.drawText.called)
        painted = [call.args[-1] for call in painter.drawText.call_args_list]
        self.assertIn('Delete chat', painted)
        self.assertEqual(menu.actions()[0].text(), 'Delete chat')

    def test_accepted_pending_notice_cannot_become_a_confirmed_success(self):
        context = SimpleNamespace(chat_id=303)
        result = dict(context=context, chat_id=303, action='delete',
                      accepted=True, pending=True, confirmed=False)
        source = accepted_lifecycle_pending_notice(result, context, 'delete')
        for language in SUPPORTED_LANGUAGES:
            self.assertTrue(tr(source, language))
            self.assertIsNone(confirmed_lifecycle_action(result, context, 'delete'))
        self.assertIsNone(accepted_lifecycle_pending_notice(result, SimpleNamespace(chat_id=303), 'delete'))


if __name__ == '__main__':
    unittest.main()
