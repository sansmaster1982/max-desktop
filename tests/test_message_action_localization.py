"""Synthetic message controls: translation never changes payloads or authorisation."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import re
import unittest
from unittest.mock import Mock, patch

from PySide6.QtCore import QPoint, QTimer, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QInputDialog, QLabel, QMessageBox

from maxclient.core._message_action_translations import MESSAGE_ACTION_TRANSLATIONS
from maxclient.core.i18n import tr
from maxclient.core.models import Chat, Message
from maxclient.protocol.client import MaxError, MaxWriteOutcomeUnknown
from maxclient.ui.chat_view import _ForwardChatDialog
import test_message_menu_context as fixtures


class MessageActionLocalizationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixtures.MessageMenuContextTests.setUpClass()

    def setUp(self):
        self.f = fixtures.MessageMenuContextTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.addCleanup(self.f.tearDown)

    def language(self, value):
        self.f.view.settings.ui_language = value
        self.f.view.retranslate_message_actions(value)

    def modal(self, callback):
        """Always close the nested loop, including when an assertion fails."""
        errors = []
        def invoke():
            dialog = QApplication.activeModalWidget()
            try:
                callback(dialog)
            except BaseException as error:
                errors.append(error)
                if dialog is not None:
                    dialog.reject()
        QTimer.singleShot(0, invoke)
        return errors

    def assert_modal_ok(self, errors):
        if errors:
            raise errors[0]

    def test_catalogue_public_integration_all_six_languages(self):
        for source, values in MESSAGE_ACTION_TRANSLATIONS.items():
            self.assertEqual(len(values), 6)
            for lang, value in zip(('en', 'es', 'pt', 'fr', 'uz', 'be'), values):
                with self.subTest(source=source, language=lang):
                    self.assertIsInstance(value, str)
                    self.assertTrue(value.strip())
                    self.assertEqual(tr(source, lang), value)
                    if lang != 'be':
                        self.assertIsNone(re.search('[А-Яа-яЁё]', value))
            self.assertEqual(tr(source, 'pt-BR'), values[2])

    def test_menu_uses_current_language_but_copies_exact_user_text(self):
        self.f.message.text = 'Удалить сообщение <b>😀</b>'
        expected = ['Copy text', 'Reply', 'Forward…', 'Edit…', 'Reaction',
                    'Mark as read', '', 'Delete…']
        self.language('en')
        menu = self.f.view._message_menu(self.f.message)
        self.f.menus.append(menu)
        actions = menu.actions()  # Retain the native submenu QAction wrappers.
        self.assertEqual([a.text() for a in actions], expected)
        self.assertEqual(actions[4].menu().actions()[-1].text(), 'Remove my reaction')
        clipboard = Mock()
        with patch('maxclient.ui.chat_view.QApplication.clipboard', return_value=clipboard):
            actions[0].trigger()
        clipboard.setText.assert_called_once_with(self.f.message.text)
        self.assertEqual(self.f.jobs, [])

    def test_old_menu_actions_are_inert_after_language_change_and_return(self):
        menu = self.f.view._message_menu(self.f.message)
        self.f.menus.append(menu)
        actions = list(menu.actions()) + list(menu.actions()[4].menu().actions())
        self.language('en')
        self.language('ru')
        with patch.object(self.f.view, '_copy_message') as copied, \
                patch.object(self.f.view, '_reply_to') as replied, \
                patch.object(self.f.view, '_edit_message_text') as edited, \
                patch.object(self.f.view, '_mark_read') as read:
            for action in actions:
                action.trigger()
            copied.assert_not_called()
            replied.assert_not_called()
            edited.assert_not_called()
            read.assert_not_called()
        self.assertEqual(self.f.jobs, [])
        self.f.assert_no_service_writes()

    def test_current_language_guard_works_even_before_retranslate_hook(self):
        action = self.f.menu_action('Реакция').menu().actions()[0]
        self.f.view.settings.ui_language = 'en'
        action.trigger()
        self.assertEqual(self.f.jobs, [])
        self.f.assert_no_service_writes()

    def test_text_selection_menu_is_translated_and_preserves_selection(self):
        self.language('en')
        label = QLabel('Сохранить 😀', self.f.view)
        label.setTextFormat(Qt.TextFormat.PlainText)
        label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        label.setSelection(0, 9)
        self.f.message.text = label.text()
        menu = self.f.view._message_menu(self.f.message)
        clipboard = Mock()
        def popup(_point):
            actions = {a.text(): a for a in menu.actions()}
            self.assertIn('Copy selection', actions)
            self.assertIn('Select all', actions)
            actions['Copy selection'].trigger()
            self.language('fr')
            actions['Select all'].trigger()
            self.assertEqual(label.selectedText(), 'Сохранить')
        menu.exec = popup
        with patch.object(self.f.view, '_message_menu', return_value=menu), \
                patch('maxclient.ui.chat_view.QApplication.clipboard', return_value=clipboard):
            self.f.view._message_text_menu(self.f.message, label, QPoint())
        clipboard.setText.assert_called_once_with('Сохранить')
        self.assertEqual(self.f.jobs, [])

    def test_edit_retranslation_preserves_draft_and_cancel_never_writes(self):
        self.language('en')
        draft = 'Удалить сообщение\n<b>raw user text 😀</b>'
        def interact(dialog):
            self.assertIsInstance(dialog, QInputDialog)
            self.assertEqual(dialog.windowTitle(), 'Edit message')
            self.assertEqual(dialog.labelText(), 'Message text:')
            self.assertEqual(dialog.okButtonText(), 'Save')
            self.assertEqual(dialog.cancelButtonText(), 'Cancel')
            dialog.setTextValue(draft)
            self.language('fr')
            self.assertEqual(dialog.textValue(), draft)
            self.assertEqual(dialog.windowTitle(), 'Modifier le message')
            self.assertEqual(dialog.okButtonText(), 'Enregistrer')
            self.assertEqual(dialog.cancelButtonText(), 'Annuler')
            self.assertEqual(self.f.jobs, [])
            dialog.reject()
        errors = self.modal(interact)
        self.f.view._edit_message(self.f.message)
        self.assert_modal_ok(errors)
        self.assertEqual(self.f.jobs, [])
        self.assertEqual(self.f.view._message_dialog_retranslations, {})

    def test_edit_accept_after_retranslation_uses_exact_text_and_original_session(self):
        draft = '  Сохранить\n😀  '
        def interact(dialog):
            dialog.setTextValue(draft)
            self.language('es')
            self.assertEqual(dialog.textValue(), draft)
            dialog.accept()
        errors = self.modal(interact)
        self.f.view._edit_message(self.f.message)
        self.assert_modal_ok(errors)
        self.assertEqual(len(self.f.jobs), 1)
        with patch.object(self.f.view, '_after_send'):
            self.f.finish()
        self.f.service.edit_message.assert_called_once_with(
            1, self.f.message.id, draft, context=self.f.context)

    def test_edit_accept_after_locale_and_session_change_is_ignored(self):
        def interact(dialog):
            dialog.setTextValue('Synthetic changed')
            self.language('en')
            self.f.switch()
            dialog.accept()
        errors = self.modal(interact)
        self.f.view._edit_message(self.f.message)
        self.assert_modal_ok(errors)
        self.assertEqual(self.f.jobs, [])
        self.f.assert_no_service_writes()

    def test_delete_retranslation_keeps_scope_button_identity_and_cancel_default(self):
        self.language('en')
        def interact(dialog):
            self.assertIsInstance(dialog, QMessageBox)
            before = {b.text(): b for b in dialog.buttons()}
            self.assertEqual(set(before), {'Delete for me', 'Delete for everyone', 'Cancel'})
            self.assertIs(dialog.defaultButton(), before['Cancel'])
            self.language('fr')
            self.assertEqual(dialog.windowTitle(), 'Supprimer le message')
            self.assertEqual(before['Delete for everyone'].text(), 'Supprimer pour tout le monde')
            self.assertIs(dialog.defaultButton(), before['Cancel'])
            QTest.mouseClick(before['Delete for everyone'], Qt.MouseButton.LeftButton)
        errors = self.modal(interact)
        self.f.view._delete_message(self.f.message)
        self.assert_modal_ok(errors)
        with patch.object(self.f.view, '_after_send'):
            self.f.finish()
        self.f.service.delete_messages.assert_called_once_with(
            1, [self.f.message.id], for_all=True, context=self.f.context)

    def test_saved_delete_stays_self_only_and_enter_cancels_in_english(self):
        self.f.view.open_chat(Chat(0, title='Synthetic saved'))
        self.f.jobs.clear()
        self.language('en')
        message = Message(0, id=1234567890123, outgoing=True, text='Synthetic')
        def interact(dialog):
            self.assertEqual({b.text() for b in dialog.buttons()}, {'Delete for me', 'Cancel'})
            self.assertEqual(dialog.defaultButton().text(), 'Cancel')
            QTest.keyClick(dialog, Qt.Key.Key_Return)
        errors = self.modal(interact)
        self.f.view._delete_message(message)
        self.assert_modal_ok(errors)
        self.assertEqual(self.f.jobs, [])
        self.f.assert_no_service_writes()

    def test_forward_retranslation_preserves_raw_names_selected_identity_and_scope(self):
        self.f.service.list_chats = lambda: [Chat(1, title='Удалить сообщение'), Chat(2, title='Удалить сообщение')]
        self.language('en')
        def interact(dialog):
            self.assertIsInstance(dialog, _ForwardChatDialog)
            self.assertEqual(dialog.windowTitle(), 'Forward message')
            self.assertEqual(dialog.chats.accessibleName(), 'Choose a chat:')
            self.assertEqual([dialog.chats.item(i).text() for i in range(2)], ['Удалить сообщение'] * 2)
            dialog.chats.setCurrentRow(1)
            item = dialog.chats.currentItem()
            self.language('fr')
            self.assertIs(dialog.chats.currentItem(), item)
            self.assertEqual(dialog.send_button.text(), 'Transférer')
            self.assertEqual(dialog.cancel_button.text(), 'Annuler')
            self.assertEqual(item.data(Qt.ItemDataRole.UserRole), 2)
            QTest.mouseClick(dialog.send_button, Qt.MouseButton.LeftButton)
        errors = self.modal(interact)
        self.f.view._forward_message(self.f.message)
        self.assert_modal_ok(errors)
        with patch.object(self.f.view, '_after_send'):
            self.f.finish()
        self.f.service.forward_messages.assert_called_once_with(
            1, [self.f.message.id], 2, context=self.f.context)

    def test_forward_language_switch_and_cancel_do_not_send(self):
        def interact(dialog):
            dialog.chats.setCurrentRow(1)
            self.language('uz')
            QTest.mouseClick(dialog.cancel_button, Qt.MouseButton.LeftButton)
        errors = self.modal(interact)
        self.f.view._forward_message(self.f.message)
        self.assert_modal_ok(errors)
        self.assertEqual(self.f.jobs, [])
        self.assertEqual(self.f.view._message_dialog_retranslations, {})

    def test_retranslation_keeps_composer_reply_and_pending_request(self):
        self.f.view.input.setPlainText('Сохранить user draft')
        self.f.view._reply_to(self.f.message)
        self.f.view._react_to(self.f.message, '👍')
        request = self.f.view._message_action_request
        drafts, replies = self.f.view._drafts, self.f.view._reply_targets
        self.language('pt-BR')
        self.assertIs(self.f.view._message_action_request, request)
        self.assertIs(self.f.view._drafts, drafts)
        self.assertIs(self.f.view._reply_targets, replies)
        self.assertIs(self.f.view._reply_target, self.f.message)
        self.assertEqual(self.f.view.input.toPlainText(), 'Сохранить user draft')
        self.assertEqual(len(self.f.jobs), 1)
        self.f.assert_no_service_writes()

    def test_unknown_and_rejected_result_are_distinct_in_current_language(self):
        for error, title, fragment in (
            (MaxWriteOutcomeUnknown('synthetic-private'), 'Result not confirmed', 'result is unknown'),
            (MaxError('synthetic-private'), 'Action rejected', 'MAX rejected')):
            self.f.service.delete_messages.side_effect = error
            self.f.view._run_message_action(self.f.service.delete_messages, self.f.message,
                                           self.f.view.chat_metadata_binding(), [self.f.message.id], for_all=False)
            self.language('en')
            with patch('maxclient.ui.chat_view.QMessageBox.warning') as warning, \
                    patch.object(self.f.view, '_after_send') as refresh:
                self.f.finish(len(self.f.jobs) - 1)
                self.assertEqual(warning.call_args.args[1], title)
                self.assertIn(fragment, warning.call_args.args[2])
                self.assertNotIn('synthetic-private', str(warning.call_args))
                refresh.assert_not_called()

    def test_read_error_body_is_raw_even_when_equal_to_translation_key(self):
        self.language('en')
        self.f.service.mark_chat_read = Mock()
        with patch('maxclient.ui.chat_view.run_async') as runner:
            self.f.view._mark_read(self.f.message, self.f.view.chat_metadata_binding())
            failed = runner.call_args.kwargs['on_error']
        with patch('maxclient.ui.chat_view.QMessageBox.warning') as warning:
            failed('Удалить сообщение')
            self.assertEqual(warning.call_args.args[1:], ('Read status not confirmed', 'Удалить сообщение'))
            warning.reset_mock()
            self.f.switch()
            failed('Удалить сообщение')
            warning.assert_not_called()


if __name__ == '__main__':
    unittest.main()
