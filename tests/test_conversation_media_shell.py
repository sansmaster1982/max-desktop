"""Photo/gallery integration: only loaded rows may initiate image previews."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import unittest
from unittest.mock import Mock, patch
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QApplication
from shiboken6 import isValid

from maxclient.core.config import Settings
from maxclient.ui.main_window import MainWindow
from test_conversation_info_shell import FakeService
from test_conversation_image_io import row


class ConversationMediaShellTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.service = FakeService()
        self.patches = [patch('maxclient.ui.main_window.run_async'),
                        patch('maxclient.ui.main_window.DesktopNotifications'),
                        patch('maxclient.ui.conversation_media_loader.run_async'),
                        patch('maxclient.ui.conversation_photo_viewer.run_async')]
        self.jobs, _, self.thumbnail_jobs, self.photo_jobs = [item.start() for item in self.patches]
        settings = Settings(theme='light')
        settings.save = Mock()
        self.window = MainWindow(self.service, settings)
        self.window.chat_view.chat = self.service.chats[303]
        self.window.chat_view.open_chat = Mock()
        self.window._open_conversation_info(self.service.chats[303])
        self.context = self.window._info_context
        self.item = row()
        self.window._info_pages['media'] = {'items': [self.item], 'marker': None,
                                          'seen': set(), 'request': None}

    def tearDown(self):
        if isValid(self.window):
            self.window.close()
            self.window.deleteLater()
        self.app.processEvents()
        for item in reversed(self.patches): item.stop()

    def test_loaded_photo_uses_actual_url_and_preserves_card(self):
        self.window._open_info_item({**self.item, 'open_url': 'https://forged.example.test/secret'}, self.context)
        self.assertEqual(self.photo_jobs.call_args.args[1], self.item['open_url'])
        self.assertIsNotNone(self.window._info_photo_viewer)
        self.assertIs(self.window._info_context, self.context)
        self.window.chat_view.open_chat.assert_not_called()

    def test_unloaded_row_cannot_fetch_image(self):
        self.window._open_info_item(row('unloaded'), self.context)
        self.photo_jobs.assert_not_called()
        self.thumbnail_jobs.assert_not_called()

    def test_thumbnail_requests_use_only_loaded_metadata(self):
        self.window._request_info_thumbnail({**self.item, 'thumbnail_url': 'https://forged.example.test/a'}, self.context)
        self.assertEqual(self.thumbnail_jobs.call_args.args[1], self.item['thumbnail_url'])
        self.window._request_info_thumbnail(row('unknown'), self.context)
        self.assertEqual(self.thumbnail_jobs.call_count, 1)

    def test_thumbnail_result_must_match_current_source_url(self):
        with patch.object(self.window._info_panel, 'set_thumbnail') as apply:
            self.window._info_thumbnail_ready(self.context, self.item['key'], 'https://old.example.test/a', QPixmap())
            apply.assert_not_called()
            self.window._info_thumbnail_ready(self.context, self.item['key'], self.item['thumbnail_url'], QPixmap())
            apply.assert_called_once()

    def test_closing_card_cancels_photo_and_thumbnail_requests(self):
        self.window._request_info_thumbnail(self.item, self.context)
        self.window._open_info_item(self.item, self.context)
        image = self.photo_jobs.call_args
        thumbnail = self.thumbnail_jobs.call_args
        self.window._close_conversation_info()
        self.assertTrue(image.kwargs['cancelled'].is_set())
        self.assertTrue(thumbnail.kwargs['cancelled'].is_set())
        self.assertIsNone(self.window._info_photo_viewer)

    def test_stale_context_cannot_fetch_or_apply_image(self):
        self.service.context = object()
        self.window._open_info_item(self.item, self.context)
        self.window._request_info_thumbnail(self.item, self.context)
        self.photo_jobs.assert_not_called()
        self.thumbnail_jobs.assert_not_called()

    def test_video_remains_message_navigation_without_new_playback(self):
        video = row(kind='VIDEO', open_url=None)
        self.window._info_pages['media']['items'] = [video]
        self.window._open_info_item(video, self.context)
        self.photo_jobs.assert_not_called()
        self.window.chat_view.open_chat.assert_called_once_with(self.service.chats[303],
                                                               message_id=7, time_ms=1793500000000)


if __name__ == '__main__': unittest.main()
