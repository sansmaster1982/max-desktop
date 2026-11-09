"""Offline explicit choices only; dialogs cannot mutate a service."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import unittest
from unittest.mock import patch
from PySide6.QtWidgets import QApplication, QCheckBox, QDialog, QLabel
from maxclient.core.models import Chat
from maxclient.ui.conversation_management_dialogs import ShareContactDialog, confirm_conversation_removal


class ConversationManagementDialogsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_share_starts_without_recipient_and_preserves_duplicate_title_identity(self):
        dialog = ShareContactDialog('Synthetic contact', [Chat(11, 'Same'), Chat(22, 'Same')])
        self.assertIsNone(dialog.selected_chat_id)
        self.assertFalse(dialog.send_button.isEnabled())
        dialog.chats.setCurrentRow(1)
        self.assertTrue(dialog.send_button.isEnabled())
        dialog._choose()
        self.assertEqual(dialog.selected_chat_id, 22)
        dialog.deleteLater()

    def test_hidden_recipient_cannot_be_sent(self):
        dialog = ShareContactDialog('Synthetic', [Chat(11, 'Alice')])
        dialog.chats.setCurrentRow(0)
        dialog.search.setText('Bob')
        self.assertFalse(dialog.send_button.isEnabled())
        dialog._choose()
        self.assertIsNone(dialog.selected_chat_id)
        dialog.deleteLater()

    def test_cancel_removal_is_distinct_from_confirming_self_only(self):
        with patch.object(QDialog, 'exec', return_value=QDialog.DialogCode.Rejected):
            self.assertIsNone(confirm_conversation_removal(None, 'clear', 'Synthetic', {'kind': 'dialog'}))
        with patch.object(QDialog, 'exec', return_value=QDialog.DialogCode.Accepted):
            self.assertIs(confirm_conversation_removal(None, 'clear', 'Synthetic', {'kind': 'dialog'}), False)

    def test_optional_all_is_not_preselected_but_honors_explicit_choice(self):
        observed = []
        def choose(dialog):
            checkbox = dialog.findChild(QCheckBox)
            observed.append((checkbox.isChecked(), checkbox.isHidden()))
            checkbox.setChecked(True)
            return QDialog.DialogCode.Accepted
        with patch.object(QDialog, 'exec', new=choose):
            result = confirm_conversation_removal(None, 'delete', 'Synthetic',
                                                 {'kind': 'dialog', 'delete_for_all_optional': True})
        self.assertEqual(observed, [(False, False)])
        self.assertIs(result, True)

    def test_group_delete_explicitly_warns_about_all_members(self):
        observed = []
        def choose(dialog):
            observed.extend(label.text() for label in dialog.findChildren(QLabel))
            return QDialog.DialogCode.Accepted
        with patch.object(QDialog, 'exec', new=choose):
            result = confirm_conversation_removal(None, 'delete', 'Synthetic', {'kind': 'group'})
        self.assertIs(result, True)
        self.assertTrue(any('для всех участников' in text for text in observed))


if __name__ == '__main__':
    unittest.main()
