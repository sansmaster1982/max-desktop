"""Synthetic message interactions: no real clipboard, API or accounts."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import unittest
from unittest.mock import Mock, patch
from PySide6.QtWidgets import QApplication, QPushButton
from maxclient.core.config import Settings
from maxclient.core.models import Chat, Message
from maxclient.ui.chat_view import ChatView, _ForwardChatDialog
from maxclient.ui.theme import palette_for
from maxclient.ui.widgets.message_bubble import MessageBubble


class MessageActionsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.service = Mock()
        self.service.local_history.return_value = []
        self.worker = patch('maxclient.ui.chat_view.run_async')
        self.run = self.worker.start()
        self.view = ChatView(self.service, Settings())

    def tearDown(self):
        self.view.close()
        self.worker.stop()

    def test_unsent_message_cannot_issue_id_actions(self):
        menu = self.view._message_menu(Message(chat_id=1, text='synthetic pending', outgoing=True))
        self.assertEqual([a.text() for a in menu.actions()], ['Копировать текст'])
        self.run.assert_not_called()

    def test_other_persons_message_has_no_edit_action(self):
        menu = self.view._message_menu(Message(chat_id=1, id=5, text='synthetic', outgoing=False))
        self.assertNotIn('Изменить…', [a.text() for a in menu.actions()])

    def test_draft_and_reply_do_not_leak_into_another_chat(self):
        self.view.open_chat(Chat(id=1))
        self.view.input.setPlainText('synthetic draft A')
        self.view._reply_to(Message(chat_id=1, id=50, text='quoted A'))
        self.view.open_chat(Chat(id=2))
        self.assertEqual(self.view.input.toPlainText(), '')
        self.assertIsNone(self.view._reply_target)
        self.view.input.setPlainText('synthetic text B')
        self.run.reset_mock()
        self.view._send_text()
        self.assertEqual(self.run.call_args.args, (self.service.send_text, 2, 'synthetic text B'))
        self.view.open_chat(Chat(id=1))
        self.assertEqual(self.view.input.toPlainText(), 'synthetic draft A')
        self.assertEqual(self.view._reply_target.id, 50)

    def test_reply_sends_explicit_source_message_and_clears_reply_mode(self):
        self.view.open_chat(Chat(id=1))
        self.view._reply_to(Message(chat_id=1, id=50, text='quoted A'))
        self.view.input.setPlainText('synthetic reply')
        self.run.reset_mock()
        self.view._send_text()
        self.assertEqual(self.run.call_args.args, (self.service.reply_to_message, 1, 50, 'synthetic reply'))
        self.assertIsNone(self.view._reply_target)

    def test_forward_destination_is_explicit_and_uses_identity_not_duplicate_title(self):
        dialog = _ForwardChatDialog([Chat(id=1, title='Same'), Chat(id=2, title='Same')])
        self.assertIsNone(dialog.selected_chat_id)
        self.assertFalse(dialog.send_button.isEnabled())
        dialog.chats.setCurrentRow(1)
        dialog._choose()
        self.assertEqual(dialog.selected_chat_id, 2)
        dialog.close()

    def test_reaction_chips_show_only_server_counts_and_toggle_own_reaction(self):
        callback = Mock()
        msg = Message(chat_id=1, id=5, reaction_info={'yourReaction':'👍', 'counters':[
            {'reaction':'👍','count':2}, {'reaction':'🔥','count':0}]})
        bubble = MessageBubble(msg, '#0085FF', palette_for('light'), on_reaction=callback)
        chips = bubble.findChildren(QPushButton, 'MessageReaction')
        self.assertEqual([c.text() for c in chips], ['👍 2'])
        self.assertTrue(chips[0].isChecked())
        chips[0].click()
        callback.assert_called_once_with(msg, None)
        bubble.close()


if __name__ == '__main__':
    unittest.main()
