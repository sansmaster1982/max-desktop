"""Synthetic gallery only: no accounts, asset IO, downloads or media playback."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from datetime import datetime
from pathlib import Path
import unittest
from unittest.mock import patch

from PySide6.QtCore import QCoreApplication, QElapsedTimer, QEvent, QPoint, Qt
from PySide6.QtGui import QColor, QPainter, QPixmap
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from shiboken6 import isValid

from maxclient.ui.conversation_info import ConversationInfo
from maxclient.ui.conversation_media_grid import ConversationMediaGrid, duration_label, month_label
from maxclient.ui.theme import build_stylesheet
from test_desktop_shell import _load_offscreen_font


def snapshot(context, **extra):
    value = {'context': context, 'chat_id': 42, 'peer_id': 7,
             'title': 'Синтетический профиль', 'availability': {'chat': True, 'find': True},
             'tabs': {'media': True, 'files': True, 'voice': True, 'links': True}}
    value.update(extra)
    return value


def entry(index, **extra):
    value = {'key': f'attachment:42:91:{index}', 'kind': 'PHOTO',
             'attachment_index': index, 'title': f'<b>Синтетический снимок {index}</b>',
             'subtitle': 'Загруженный элемент', 'available': True, 'action': 'message',
             'chat_id': 42, 'message_id': 91, 'time_ms': 1793459500091.5,
             'thumbnail_url': f'https://invalid.example/never-fetch/{index}',
             'open_url': f'https://invalid.example/never-open/{index}'}
    value.update(extra)
    return value


def owned_pixmap(color='#6F9D91'):
    value = QPixmap(280, 180)
    value.fill(QColor(color))
    painter = QPainter(value)
    painter.setPen(QColor('white'))
    painter.drawText(value.rect(), Qt.AlignmentFlag.AlignCenter, 'Синтетическое\nизображение')
    painter.end()
    return value


class ConversationMediaGridTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        _load_offscreen_font(cls.app)

    def setUp(self):
        self.network = patch('maxclient.ui.widgets.avatar.QNetworkAccessManager',
                             side_effect=AssertionError('No network in gallery'))
        self.network.start()
        self.panel = ConversationInfo()
        self.panel.resize(815, 900)
        self.panel.show()
        self.context = object()
        self.requests, self.opened, self.tabs, self.more = [], [], [], []
        self.panel.thumbnail_requested.connect(lambda row, context: self.requests.append((row, context)))
        self.panel.item_requested.connect(lambda row, context: self.opened.append((row, context)))
        self.panel.tab_requested.connect(lambda tab, context: self.tabs.append((tab, context)))
        self.panel.more_requested.connect(lambda tab, context: self.more.append((tab, context)))
        self.panel.set_conversation(snapshot(self.context))
        self.app.processEvents()

    def tearDown(self):
        if isValid(self.panel):
            self.panel.close()
            self.panel.deleteLater()
            QCoreApplication.sendPostedEvents(self.panel, QEvent.Type.DeferredDelete)
        self.app.processEvents()
        self.network.stop()

    def settle(self):
        # Deliver layout/visibility timers without an assumed machine-load delay.
        for _ in range(3):
            self.app.processEvents()

    def wait_for(self, predicate):
        timer = QElapsedTimer()
        timer.start()
        while not predicate() and timer.elapsed() < 1500:
            QTest.qWait(5)
        self.assertTrue(predicate())

    def test_responsive_square_cells_preserve_backend_order_and_month_boundaries(self):
        january = datetime(2026, 1, 12).timestamp() * 1000
        february = datetime(2026, 2, 12).timestamp() * 1000
        rows = [entry(3, time_ms=february), entry(2, time_ms=february), entry(1, time_ms=january)]
        self.panel.set_tab_items(self.context, 'media', rows)
        self.settle()
        grid = self.panel.media_grid
        self.assertEqual([row['key'] for row in grid._rows], [row['key'] for row in rows])
        self.assertEqual([label for _rect, label in grid._headers], ['Февраль 2026', 'Январь 2026'])
        for width, expected_columns in ((815, 3), (360, 2), (230, 1)):
            self.panel.resize(width, 700)
            self.settle()
            self.assertEqual(grid.columns, expected_columns)
            self.assertTrue(all(rect.width() == rect.height() for rect in grid._cells))
            self.assertTrue(all(rect.left() >= 0 and rect.right() < grid.width() for rect in grid._cells))
            self.assertEqual(self.panel.scroll.horizontalScrollBar().maximum(), 0)

    def test_duration_is_explicit_milliseconds_finite_not_bool(self):
        self.assertEqual(duration_label(125999), '2:05')
        self.assertEqual(duration_label(3600000), '1:00:00')
        self.assertEqual(duration_label(0), '0:00')
        for value in (None, True, '123', -1, float('nan'), float('inf')):
            self.assertEqual(duration_label(value), '')
        self.assertEqual(month_label(True), '')
        self.assertEqual(month_label(float('inf')), '')

    def test_only_visible_explicit_thumbnail_urls_request_once_per_context(self):
        rows = [entry(index) for index in range(90)]
        self.panel.set_tab_items(self.context, 'media', rows)
        self.wait_for(lambda: len(self.requests) > 0)
        self.assertLess(len(self.requests), len(rows) // 2)
        first_keys = {row['key'] for row, _context in self.requests}
        self.assertTrue(all(context is self.context for _row, context in self.requests))
        self.panel.media_grid.request_visible_thumbnails()
        self.assertEqual(len(self.requests), len(first_keys))
        self.panel.scroll.verticalScrollBar().setValue(self.panel.scroll.verticalScrollBar().maximum())
        self.wait_for(lambda: len(self.requests) > len(first_keys))
        self.assertEqual(len(self.requests), len({row['key'] for row, _context in self.requests}))
        self.assertNotIn(rows[45]['key'], {row['key'] for row, _context in self.requests})

    def test_paint_and_grab_emit_no_thumbnail_or_navigation_requests(self):
        self.panel.set_tab_items(self.context, 'media', [entry(0)])
        self.settle()
        before = len(self.requests)
        self.panel.media_grid.grab()
        self.panel.media_grid.grab()
        self.assertEqual(len(self.requests), before)
        self.assertEqual(self.opened, [])

    def test_owned_pixmap_only_late_owner_and_unknown_key_rejected(self):
        row = entry(0)
        self.panel.set_tab_items(self.context, 'media', [row])
        pixels = owned_pixmap()
        self.assertTrue(self.panel.set_thumbnail(self.context, row['key'], pixels))
        self.assertFalse(self.panel.set_thumbnail(object(), row['key'], pixels))
        self.assertFalse(self.panel.set_thumbnail(self.context, 'unknown', pixels))
        self.assertFalse(self.panel.set_thumbnail(self.context, row['key'], 'C:/never-read.jpg'))
        self.assertFalse(self.panel.set_thumbnail(self.context, row['key'], QPixmap()))
        self.assertIn(row['key'], self.panel.media_grid._pixmaps)
        other = object()
        self.panel.set_conversation(snapshot(other, chat_id=43))
        self.assertEqual(self.panel.media_grid._pixmaps, {})
        self.assertFalse(self.panel.set_thumbnail(self.context, row['key'], pixels))
        self.panel.clear()
        self.assertFalse(self.panel.set_thumbnail(other, row['key'], pixels))

    def test_changed_thumbnail_source_invalidates_pixels_and_requests_for_same_key(self):
        row = entry(0)
        self.panel.set_tab_items(self.context, 'media', [row])
        self.wait_for(lambda: len(self.requests) == 1)
        self.panel.set_thumbnail(self.context, row['key'], owned_pixmap())
        self.panel.set_tab_items(self.context, 'media', [dict(row, thumbnail_url='https://invalid.example/changed')])
        self.assertNotIn(row['key'], self.panel._media_pixmaps)
        self.assertNotIn(row['key'], self.panel.media_grid._pixmaps)
        self.wait_for(lambda: len(self.requests) == 2)
        self.assertEqual(self.requests[-1][0]['thumbnail_url'], 'https://invalid.example/changed')

    def test_cache_entry_cap_drops_helper_references_and_evicted_visible_keys_reload(self):
        rows = [entry(index) for index in range(200)]
        self.panel.set_tab_items(self.context, 'media', rows)
        self.panel.media_grid.hide()
        image = owned_pixmap()
        for row in rows:
            self.assertTrue(self.panel.set_thumbnail(self.context, row['key'], image))
        self.assertEqual(len(self.panel._media_pixmaps), 64)
        self.assertEqual(len(self.panel.media_grid._pixmaps), 64)
        self.assertNotIn(rows[0]['key'], self.panel._media_pixmaps)
        self.assertIn(rows[-1]['key'], self.panel._media_pixmaps)
        self.panel.media_grid.show()
        self.wait_for(lambda: any(row['key'] == rows[0]['key'] for row, _context in self.requests))
        self.assertLess(len(self.requests), 40)

    def test_cache_byte_cap_rejects_oversized_pixels_and_bounds_initial_owned_pixmaps(self):
        self.panel._thumbnail_cache_limit_bytes = 2 * 280 * 180 * 4
        rows = [entry(index, thumbnail_pixmap=owned_pixmap()) for index in range(4)]
        self.panel.media_grid.hide()
        self.panel.set_tab_items(self.context, 'media', rows)
        self.assertLessEqual(len(self.panel._media_pixmaps), 2)
        self.assertLessEqual(len(self.panel.media_grid._pixmaps), 2)
        self.assertTrue(all('thumbnail_pixmap' not in row for row in self.panel._tab_data['media']))
        large = QPixmap(512, 512)
        large.fill(QColor('red'))
        self.assertFalse(self.panel.set_thumbnail(self.context, rows[-1]['key'], large))
        self.assertLessEqual(sum(image.width() * image.height() * 4 for image in self.panel._media_pixmaps.values()),
                             self.panel._thumbnail_cache_limit_bytes)

    def test_visible_cache_entries_are_pinned_and_cap_never_causes_immediate_fetch_loop(self):
        self.panel._thumbnail_cache_limit_entries = 1
        rows = [entry(0), entry(1)]
        self.panel.set_tab_items(self.context, 'media', rows)
        self.wait_for(lambda: len(self.requests) == 2)
        self.assertTrue(self.panel.set_thumbnail(self.context, rows[0]['key'], owned_pixmap()))
        self.assertFalse(self.panel.set_thumbnail(self.context, rows[1]['key'], owned_pixmap()))
        for _ in range(3):
            self.panel.media_grid.request_visible_thumbnails()
        self.assertEqual(len(self.requests), 2)
        self.assertIn(rows[0]['key'], self.panel._media_pixmaps)
        self.panel.media_grid.hide()
        self.settle()
        self.panel.media_grid.show()
        self.wait_for(lambda: len(self.requests) == 3)
        self.assertEqual(self.requests[-1][0]['key'], rows[1]['key'])

    def test_filter_and_tab_change_do_not_refetch_or_discard_accepted_thumbnail(self):
        row = entry(0, title='Искомое фото')
        self.panel.set_tab_items(self.context, 'media', [row, entry(1)])
        self.panel.set_thumbnail(self.context, row['key'], owned_pixmap())
        self.settle()
        request_count, tab_count = len(self.requests), len(self.tabs)
        self.panel.search.setText('Искомое')
        self.assertEqual(len(self.panel.media_grid._rows), 1)
        self.assertIn(row['key'], self.panel.media_grid._pixmaps)
        self.panel.search.setText('нет совпадения')
        self.assertEqual(self.panel.media_grid._rows, [])
        self.assertIn('среди загруженных', self.panel.tab_status.text())
        self.panel.search.clear()
        self.assertIn(row['key'], self.panel.media_grid._pixmaps)
        self.assertEqual(len(self.requests), request_count)
        self.assertEqual(len(self.tabs), tab_count)
        self.panel.tabs.setCurrentIndex(2)
        self.assertFalse(self.panel.media_grid.isVisible())
        self.assertTrue(self.panel.items.isVisible())
        self.panel.tabs.setCurrentIndex(1)
        self.assertTrue(self.panel.media_grid.isVisible())
        self.assertIn(row['key'], self.panel.media_grid._pixmaps)

    def test_activation_keeps_exact_attachment_and_outer_message_identity_without_url_open(self):
        row = entry(7, kind='VIDEO', duration_ms=65000, media_id=987)
        self.panel.set_tab_items(self.context, 'media', [row])
        self.settle()
        grid = self.panel.media_grid
        QTest.mouseClick(grid, Qt.MouseButton.LeftButton, pos=grid._cells[0].center())
        self.assertEqual(self.opened, [(row, self.context)])
        self.assertEqual(self.opened[0][0]['time_ms'], 1793459500091.5)
        self.assertEqual(self.opened[0][0]['attachment_index'], 7)
        grid.setFocus()
        QTest.keyClick(grid, Qt.Key.Key_Return)
        self.assertEqual(len(self.opened), 2)

    def test_unavailable_busy_and_stale_press_never_activate_or_fetch(self):
        self.panel.set_tab_items(self.context, 'media', [entry(0, available=False)])
        self.settle()
        grid = self.panel.media_grid
        QTest.mouseClick(grid, Qt.MouseButton.LeftButton, pos=grid._cells[0].center())
        self.assertEqual(self.requests, [])
        self.assertEqual(self.opened, [])
        self.panel.set_tab_items(self.context, 'media', [entry(1)])
        self.panel.set_tab_busy(self.context, 'media', True)
        QTest.mouseClick(grid, Qt.MouseButton.LeftButton, pos=grid._cells[0].center())
        self.assertEqual(self.opened, [])
        self.panel.set_tab_busy(self.context, 'media', False)
        QTest.mousePress(grid, Qt.MouseButton.LeftButton, pos=grid._cells[0].center())
        other = object()
        self.panel.set_conversation(snapshot(other, chat_id=43))
        self.panel.set_tab_items(other, 'media', [entry(1)])
        QTest.mouseRelease(grid, Qt.MouseButton.LeftButton, pos=grid._cells[0].center())
        self.assertEqual(self.opened, [])

    def test_loading_empty_error_and_explicit_more_preserve_loaded_tiles(self):
        self.assertEqual(self.panel.tab_status.text(), 'Загрузка…')
        self.panel.set_tab_error(self.context, 'media', '<secret>')
        self.assertIn('Не удалось', self.panel.tab_status.text())
        self.assertNotIn('secret', self.panel.tab_status.text())
        self.panel.retry_button.click()
        self.assertEqual(self.tabs, [('media', self.context)] * 2)
        self.panel.set_tab_items(self.context, 'media', [])
        self.assertEqual(self.panel.tab_status.text(), 'Нет данных для отображения')
        self.panel.set_tab_items(self.context, 'media', [entry(0)], complete=False)
        self.panel.more_button.click()
        self.panel.more_button.click()
        self.assertEqual(self.more, [('media', self.context)])
        self.assertEqual(len(self.panel.media_grid._rows), 1)
        self.panel.set_tab_error(self.context, 'media')
        self.assertEqual(len(self.panel.media_grid._rows), 1)
        self.panel.set_tab_items(self.context, 'media', [entry(1)], append=True)
        self.assertEqual(len(self.panel.media_grid._rows), 2)

    def test_unknown_media_kind_is_wide_message_fallback_and_others_remain_rows(self):
        fallback = entry(0, kind=None)
        self.panel.set_tab_items(self.context, 'media', [fallback, entry(1)])
        self.settle()
        self.assertEqual(self.panel.media_grid._cells[0].height(), 70)
        self.assertEqual(self.panel.media_grid._cells[0].width(), self.panel.media_grid.width())
        self.panel.tabs.setCurrentIndex(2)
        self.panel.set_tab_items(self.context, 'files', [entry(0, kind='FILE')])
        self.assertEqual(self.panel.items.count(), 1)
        self.assertEqual(self.panel.items.item(0).sizeHint().height(), 70)
        self.assertFalse(self.panel.media_grid.isVisible())

    def test_keyboard_moves_reveal_item_and_tooltip_escapes_literal_untrusted_title(self):
        self.panel.set_tab_items(self.context, 'media', [entry(index) for index in range(24)])
        self.settle()
        grid = self.panel.media_grid
        grid.setFocus()
        for _ in range(8):
            QTest.keyClick(grid, Qt.Key.Key_Down)
        self.assertGreater(self.panel.scroll.verticalScrollBar().value(), 0)
        self.assertGreaterEqual(grid._current, 18)
        self.panel._reveal_media(grid._cells[0])
        self.settle()
        QTest.mouseMove(grid, grid._cells[0].center())
        self.assertIn('&lt;b&gt;', grid.toolTip())
        self.assertNotIn('<b>', grid.toolTip())

    def test_visibility_timer_and_late_thumbnail_after_destroy_are_safe(self):
        self.panel.set_tab_items(self.context, 'media', [entry(0)])
        self.panel.media_grid.schedule_visibility()
        self.panel.deleteLater()
        QCoreApplication.sendPostedEvents(self.panel, QEvent.Type.DeferredDelete)
        self.app.processEvents()
        self.assertFalse(self.panel.set_thumbnail(self.context, entry(0)['key'], owned_pixmap()))

    def test_light_dark_wide_and_narrow_synthetic_captures(self):
        output = Path('.test-tmp')
        output.mkdir(exist_ok=True)
        rows = [entry(index, kind='VIDEO' if index % 3 == 1 else 'PHOTO', duration_ms=65000)
                for index in range(8)]
        self.panel.set_tab_items(self.context, 'media', rows, complete=False,
                                 status='Синтетические данные · доступна следующая страница')
        colors = ('#779C92', '#667D9F', '#B68D72', '#9E9B7A', '#B98087', '#807A9A')
        for index, row in enumerate(rows[:6]):
            self.panel.set_thumbnail(self.context, row['key'], owned_pixmap(colors[index]))
        for theme in ('light', 'dark'):
            self.panel.setStyleSheet(build_stylesheet(theme, '#0085FF'))
            self.panel.apply_theme(theme)
            for width, height in ((815, 900), (360, 650)):
                self.panel.resize(width, height)
                self.settle()
                self.panel.scroll.ensureWidgetVisible(self.panel.tabs_card, 0, 0)
                # Show the tabs and first rows, keeping pagination reachable below.
                y = self.panel.tabs_card.mapTo(self.panel.scroll.widget(), QPoint()).y()
                self.panel.scroll.verticalScrollBar().setValue(y - 12)
                self.settle()
                self.assertEqual(self.panel.scroll.horizontalScrollBar().maximum(), 0)
                self.assertTrue(self.panel.grab().save(str(output / f'info_gallery_{width}_{theme}.png')))


if __name__ == '__main__':
    unittest.main()
