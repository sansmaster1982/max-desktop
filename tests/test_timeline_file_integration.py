"""Real parse/cache/service/widget boundary with synthetic DTOs and a fake88."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import unittest
from unittest.mock import Mock, patch
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication, QPushButton
from shiboken6 import isValid

from maxclient.core.config import Settings
from maxclient.core.models import Message
from maxclient.ui.chat_view import ChatView
import test_timeline_file_service as fixture


class TimelineFileIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.fixture = fixture.TimelineFileServiceTests()
        self.fixture.setUp()
        self.service = self.fixture.service
        self.service.local_history = lambda chat_id: self.service.store.messages(chat_id)
        self.service.open_chat = Mock(side_effect=AssertionError('No real history worker'))
        self.service.calls_available = Mock(return_value=False)
        self.worker_patch = patch('maxclient.ui.chat_view.run_async')
        self.worker_patch.start()
        self.view = ChatView(self.service, Settings())
        self.saved = []
        self.view.file_save_requested.connect(self.saved.append)

    def tearDown(self):
        if isValid(self.view):
            self.view.close(); self.view.deleteLater()
            QCoreApplication.sendPostedEvents(self.view, QEvent.Type.DeferredDelete)
        self.app.processEvents()
        self.worker_patch.stop()
        self.fixture.client.request.assert_not_called()
        self.fixture.doCleanups()

    def open_message(self, raw):
        message = Message.from_server(303, raw, 101)
        self.service.store.insert_message(message)
        self.view.open_chat(self.service.store.get_chat(303))
        return self.view._rows[0]['bubble'].findChild(QPushButton, 'MessageFilePrimary')

    def test_real_zero_file_identity_and_wire_delayed_schedule_reach_typed88(self):
        raw = fixture.raw_message(delayedAttributes={'timeToFire': 900})
        raw['attaches'][0]['fileId'] = 0
        button = self.open_message(raw)
        self.assertTrue(button.isEnabled())
        button.click()
        self.assertEqual(len(self.saved), 1)
        descriptor = self.saved[0]
        self.assertEqual((descriptor.file_id, descriptor.item_type), (0, 'DELAYED'))
        reply = self.service.resolve_conversation_file_download(descriptor.context, descriptor)
        self.assertIs(reply.descriptor, descriptor)
        self.fixture.client.conversation_file_download.assert_called_once_with(
            0, 303, 7, item_type='DELAYED', expected_session=descriptor.context.session_context)

    def test_store_removal_between_render_and_click_blocks_old_enabled_button(self):
        button = self.open_message(fixture.raw_message())
        self.assertTrue(button.isEnabled())
        self.service.store.delete_messages(303, [7])
        button.click()
        self.assertEqual(self.saved, [])
        self.fixture.client.conversation_file_download.assert_not_called()

    def test_stored_deleted_file_has_disabled_actual_widget(self):
        raw = fixture.raw_message(); raw['attaches'][0]['deleted'] = True
        button = self.open_message(raw)
        self.assertFalse(button.isEnabled())
        button.click()
        self.assertEqual(self.saved, [])
        self.fixture.client.conversation_file_download.assert_not_called()


if __name__ == '__main__': unittest.main()
