"""Incoming forward -> actual cache -> queued Qt -> file intent, no network."""
import copy
import unittest
from unittest.mock import Mock

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QPushButton

import test_message_change_ui as pipeline
from maxclient.protocol.file_download_contracts import FileDownloadReply


class ForwardedMessageIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        pipeline.MessageChangeUITests.setUpClass()

    def setUp(self):
        self.case = pipeline.MessageChangeUITests()
        self.case.setUp()
        self.service, self.store, self.client = self.case.service, self.case.store, self.case.client
        self.view, self.window = self.case.view, self.case.window
        # A real Save gesture is inspected before the filesystem picker.
        self.view.file_save_requested.disconnect()
        self.saved = []
        self.view.file_save_requested.connect(self.saved.append)
        self.client.conversation_file_download = Mock(return_value=FileDownloadReply(
            'https://files.example.test/synthetic-forward', False))

    def tearDown(self):
        self.case.tearDown()

    def raw(self, identity=8, *, file_id=None, text='<b>Forwarded literal body</b>', version=100):
        outer = dict(self.case.case.raw(identity, sender=202, text=''), updateTime=version)
        inner = self.case.case.raw(9001, timestamp=1000, sender=101, text=text)
        if file_id is not None:
            inner['attaches'] = [{'_type': 'FILE', 'fileId': file_id,
                                  'name': '<b>report.txt</b>', 'size': 12}]
        outer['link'] = {'type': 'FORWARD', 'chatId': 707, 'chatName': '<i>Source chat</i>',
                         'message': inner}
        return outer

    def push(self, raw, *, settle=True):
        self.case.push(128, {'chatId': 303, 'message': raw})
        if settle:
            self.case.settle()

    def row(self, identity=8):
        return next(row for row in self.view._rows if row['msg'].id == identity)

    def test_forward_only_push_reaches_literal_body_with_outer_direction_and_time(self):
        raw = self.raw()
        self.push(raw)
        row = self.row()
        label = row['bubble'].findChild(QLabel, 'MessageText')
        self.assertEqual(label.text(), raw['link']['message']['text'])
        self.assertEqual(label.textFormat(), Qt.TextFormat.PlainText)
        self.assertIsNotNone(row['bubble'].findChild(QLabel, 'MessageForwardHeader'))
        self.assertEqual((row['msg'].chat_id, row['msg'].id, row['msg'].sender), (303, 8, 202))
        self.assertFalse(row['msg'].outgoing)  # Inner author is owner; forwarder is not.
        self.assertEqual(row['msg'].time_ms, raw['time'])
        self.assertEqual(self.store.message(303, 8).display_text, label.text())
        self.assertEqual(self.store.get_chat(303).last_preview, label.text())
        self.client.conversation_file_download.assert_not_called()

    def test_selected_nested_file_preserves_zero_fraction_and_outer_download_context(self):
        for index, file_id in enumerate((0, 1.25)):
            with self.subTest(file_id=file_id):
                raw = self.raw(8 + index, file_id=file_id)
                raw['text'] = 'Outer caption is not the selected forward body'
                raw['attaches'] = [{'_type': 'FILE', 'fileId': 999, 'name': 'outer.txt', 'size': 20}]
                raw['delayedAttributes'] = {'timeToFire': 900}
                raw['link']['message']['delayedAttributes'] = {'timeToFire': 0}
                self.push(raw)
                row = self.row(8 + index)
                self.assertEqual(row['bubble'].findChild(QLabel, 'MessageText').text(),
                                 raw['link']['message']['text'])
                buttons = row['bubble'].findChildren(QPushButton, 'MessageFilePrimary')
                self.assertEqual(len(buttons), 1)
                self.assertTrue(buttons[0].isEnabled())
                buttons[0].click()
                self.assertEqual(len(self.saved), index + 1)
                descriptor = self.saved[-1]
                self.assertEqual((descriptor.file_id, descriptor.chat_id, descriptor.message_id,
                                  descriptor.item_type), (file_id, 303, 8 + index, 'DELAYED'))
                result = self.service.resolve_conversation_file_download(descriptor.context, descriptor)
                self.assertIs(result.descriptor, descriptor)
                self.client.conversation_file_download.assert_called_with(
                    file_id, 303, 8 + index, item_type='DELAYED',
                    expected_session=self.case.case.context)

    def test_nested_edit_invalidates_old_file_button_before_queued_repaint(self):
        original = self.raw(file_id=0)
        self.push(original)
        old_button = self.row()['bubble'].findChild(QPushButton, 'MessageFilePrimary')
        self.window._notifications.receive.reset_mock()
        self.view.input.setPlainText('Unsent draft')
        updated = copy.deepcopy(original)
        updated['updateTime'] = 200
        updated['link']['message']['text'] = 'Changed forwarded body'
        updated['link']['message']['attaches'][0]['fileId'] = 8.5
        self.push(updated, settle=False)
        old_button.click()
        self.assertEqual(self.saved, [])
        self.case.settle()
        self.assertEqual(self.row()['bubble'].findChild(QLabel, 'MessageText').text(), 'Changed forwarded body')
        self.assertEqual(self.view.input.toPlainText(), 'Unsent draft')
        self.row()['bubble'].findChild(QPushButton, 'MessageFilePrimary').click()
        self.assertEqual(self.saved[-1].file_id, 8.5)
        self.window._notifications.receive.assert_not_called()
        self.client.conversation_file_download.assert_not_called()

    def test_delete_denies_previously_rendered_forward_file_before_ui_removal(self):
        self.push(self.raw(file_id=0))
        button = self.row()['bubble'].findChild(QPushButton, 'MessageFilePrimary')
        self.case.push(142, {'chat': self.case.case.chat(), 'messageIds': [8], 'ttl': False})
        button.click()
        self.assertEqual(self.saved, [])
        self.case.settle()
        self.assertNotIn(8, self.case.ids())
        self.client.conversation_file_download.assert_not_called()

    def test_owner_switch_denies_already_rendered_forward_file(self):
        self.push(self.raw(file_id=0))
        button = self.row()['bubble'].findChild(QPushButton, 'MessageFilePrimary')
        self.case.case.change_account()
        button.click()
        self.assertEqual(self.saved, [])
        self.client.conversation_file_download.assert_not_called()

    def test_nested_forward_cannot_expose_outer_fallback_or_active_file(self):
        raw = self.raw(file_id=0)
        raw['text'] = 'Unrelated outer fallback must not authorize a file'
        raw['attaches'] = [{'_type': 'FILE', 'fileId': 99, 'name': 'outer.txt'}]
        raw['link']['message']['link'] = self.raw()['link']
        self.push(raw)
        for row in self.view._rows:
            if row['msg'].id == 8:
                for button in row['bubble'].findChildren(QPushButton, 'MessageFilePrimary'):
                    self.assertFalse(button.isEnabled())
                    button.click()
        self.assertEqual(self.saved, [])
        self.client.conversation_file_download.assert_not_called()


if __name__ == '__main__':
    unittest.main()
