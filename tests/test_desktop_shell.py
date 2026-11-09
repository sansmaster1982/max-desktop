"""Synthetic, offscreen shell checks: no accounts, tokens, or network access."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPixmap, QFontDatabase, QFont
from PySide6.QtWidgets import QApplication

from maxclient.core.config import Settings
from maxclient.core.models import Chat
from maxclient.protocol.client import ConnectionState
from maxclient.ui.main_window import MainWindow
from maxclient.ui.theme import LIGHT, build_stylesheet, _darken
from maxclient.ui.widgets.avatar import Avatar
from maxclient.ui.widgets.chat_list_item import ChatListItem
from maxclient.ui.widgets.chat_wallpaper import ChatWallpaper
from maxclient.ui.widgets.line_icon import line_icon


class _Service:
    def __init__(self):
        self.chats = [
            Chat(id=1, title='Обычный диалог', unread=0, last_preview='Первое сообщение'),
            Chat(id=2, title='Рабочий канал', unread=152, last_preview='Обновление канала'),
            Chat(id=3, title='Общая группа', is_group=True, unread=2, last_preview='Встреча завтра'),
        ]
        self.chats[1].is_channel = True
        self.chats[1].chat_type = 'CHANNEL'
        self.client = SimpleNamespace(state=ConnectionState.CONNECTED)
        self.session = SimpleNamespace(my_user_id=123, my_name='Тестовый профиль')
        self.store = SimpleNamespace(get_chat=self.get_chat, get_contact=lambda _id: None)

    def get_chat(self, chat_id):
        return next((chat for chat in self.chats if chat.id == chat_id), None)

    def list_chats(self):
        return list(self.chats)

    def sync_chats(self):
        raise AssertionError('An offscreen shell test must not sync over the network')

    def local_history(self, _chat_id):
        return []

    def open_chat(self, _chat_id):
        raise AssertionError('A shell test must not issue history RPCs')


class DesktopShellTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        _load_offscreen_font(cls.app)
        cls.app.setStyle('Fusion')
        cls.app.setStyleSheet(build_stylesheet('light', '#0085FF'))

    def setUp(self):
        self.service = _Service()
        self.async_patch = patch('maxclient.ui.main_window.run_async')
        self.async_patch.start()
        self.history_patch = patch('maxclient.ui.chat_view.run_async')
        self.history_patch.start()
        self.window = MainWindow(self.service, Settings(theme='light', accent='#0085FF'))
        self.window.show()
        self.app.processEvents()

    def tearDown(self):
        self.window.close()
        self.history_patch.stop()
        self.async_patch.stop()
        self.app.processEvents()

    def ids(self):
        return [self.window.chat_list.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.window.chat_list.count())]

    def test_layout_has_reference_columns_and_accessible_code_entry(self):
        self.assertEqual(self.window.nav_buttons['all'].width(), 64)
        self.assertTrue(320 <= self.window.sidebar.width() <= 400)
        self.assertEqual(self.window.codes_button.accessibleName(), 'Коды подтверждения')
        self.assertEqual(self.window.new_chat_button.accessibleName(), 'Новое сообщение')
        self.assertFalse(self.window.codes_button.icon().isNull())

    def test_filters_use_actual_unread_and_channel_metadata(self):
        self.assertEqual(self.ids(), [1, 2, 3])
        self.window.nav_buttons['new'].click()
        self.assertEqual(self.ids(), [2, 3])
        self.window.nav_buttons['channels'].click()
        self.assertEqual(self.ids(), [2])
        self.assertNotIn(3, self.ids())  # an ordinary group is not a channel
        self.window.nav_buttons['all'].click()
        self.assertEqual(self.ids(), [1, 2, 3])

    def test_search_combines_with_current_section_and_has_real_empty_state(self):
        self.window._select_section('new')
        self.window.search.setText('ГРУППА')
        self.assertEqual(self.ids(), [3])
        self.window.search.setText('такого чата нет')
        self.assertEqual(self.ids(), [])
        self.assertEqual(self.window._chat_pages.currentWidget(), self.window.empty_list_hint)
        self.assertEqual(self.window.empty_list_hint.text(), 'Ничего не найдено')

    def test_incremental_rows_keep_selection_and_real_badge_count(self):
        item = self.window.chat_list.item(1)
        self.window.chat_list.setCurrentItem(item)
        row = self.window.chat_list.itemWidget(item)
        self.assertIsInstance(row, ChatListItem)
        self.assertEqual(row.badge.text(), '152')
        self.assertTrue(row.pin.isHidden())
        self.service.chats[1].unread = 4
        self.window._reload_chats()
        self.assertIs(self.window.chat_list.itemWidget(item), row)
        self.assertEqual(row.badge.text(), '4')
        self.assertEqual(self.window.chat_list.currentItem(), item)

    def test_calls_entry_opens_history_without_claiming_audio_video_support(self):
        self.assertTrue(self.window.calls_button.isEnabled())
        self.assertFalse(self.window.chat_view.call_button.isEnabled())
        self.assertFalse(self.window.chat_view.video_button.isEnabled())
        with patch('maxclient.ui.main_window.CallHistoryDialog') as dialog:
            self.window.calls_button.click()
            dialog.assert_called_once_with(self.service, self.window)
            dialog.return_value.show.assert_called_once()

    def test_current_chat_selection_survives_filtering(self):
        item = self.window.chat_list.item(0)
        self.window._on_chat_clicked(item)
        self.assertEqual(self.window._current_chat_id, 1)
        self.window._select_section('new')
        self.window._select_section('all')
        self.assertEqual(self.window.chat_list.currentItem().data(Qt.ItemDataRole.UserRole), 1)

    def test_preview_is_plain_text_and_metadata_badges_are_not_invented(self):
        self.service.chats[0].last_preview = '<b>Текст из чата</b>'
        self.window._reload_chats()
        row = self.window.chat_list.itemWidget(self.window.chat_list.item(0))
        self.assertEqual(row.preview.textFormat(), Qt.TextFormat.PlainText)
        self.assertIn('<b>', row.preview._full_text)
        self.assertTrue(row.verified.isHidden())
        self.assertTrue(row.pin.isHidden())
        self.assertTrue(row.muted.isHidden())


class SurfaceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        _load_offscreen_font(cls.app)

    def test_supplied_photo_is_rendered_without_fetching_a_url(self):
        avatar = Avatar(54)
        avatar.set_identity('Тестовый контакт', 99)
        photo = QPixmap(54, 54)
        photo.fill(QColor('#D83B48'))
        avatar.set_photo(photo)
        avatar.show()
        self.app.processEvents()
        image = avatar.grab().toImage()
        self.assertEqual(image.pixelColor(27, 27).name(), '#d83b48')
        avatar.close()

    def test_chat_wallpaper_and_light_palette_have_reference_surfaces(self):
        surface = ChatWallpaper('light')
        surface.resize(360, 360)
        surface.show()
        self.app.processEvents()
        image = surface.grab().toImage()
        self.assertNotEqual(image.pixelColor(5, 5).name(), '#ffffff')
        self.assertEqual(surface._tile.size().width(), 280)
        self.assertGreater(sum(surface._tile.toImage().pixelColor(x, y).alpha() > 0
                               for x in range(280) for y in range(280)), 1000)
        output = Path('.test-tmp/wallpaper_light.png')
        output.parent.mkdir(parents=True, exist_ok=True)
        self.assertTrue(image.save(str(output)))
        self.assertEqual(LIGHT.bubble_in, '#FFFFFF')
        self.assertEqual(LIGHT.bubble_out, '#DDF7FF')
        surface.close()

    def test_blue_accent_darkening_does_not_generate_negative_rgb(self):
        self.assertEqual(_darken('#0085FF', 18), '#0073ED')

    def test_composer_icons_render_real_vector_pixels(self):
        for name in ['attach', 'microphone', 'send', 'stop']:
            image = line_icon(name, '#0085FF', 24).pixmap(24, 24).toImage()
            opaque = sum(image.pixelColor(x, y).alpha() > 20 for x in range(image.width()) for y in range(image.height()))
            self.assertGreater(opaque, 20, name)


def _load_offscreen_font(app):
    # The offscreen Windows plugin has no system-font discovery. Register a
    # local font in this test process only; the native Windows app needs no fix.
    font = Path(os.environ.get('WINDIR', r'C:\Windows')) / 'Fonts' / 'segoeui.ttf'
    if not QFontDatabase.families() and font.is_file():
        QFontDatabase.addApplicationFont(str(font))
        app.setFont(QFont('Segoe UI', 10))


if __name__ == '__main__':
    unittest.main()
