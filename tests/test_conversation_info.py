"""Info card presentation only: synthetic snapshots, no service/network/media."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from pathlib import Path
import unittest
from unittest.mock import patch

from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QColor, QPixmap
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLabel

from maxclient.ui.conversation_info import ConversationInfo, TABS
from maxclient.ui.theme import build_stylesheet
from test_desktop_shell import _load_offscreen_font


def snapshot(context, **extra):
    value = {'context': context, 'chat_id': 42, 'peer_id': 7,
             'title': '<b>Синтетический контакт</b>', 'presence': 'Синтетический статус',
             'phone': '+7 000 000-00-00', 'description': '', 'muted': False,
             'availability': {name: True for name in ('chat', 'call', 'video', 'mute', 'find', 'more', 'edit')},
             'tabs': {key: True for key, _title in TABS}}
    value.update(extra)
    return value


def entry(key, **extra):
    value = {'key': key, 'title': f'<b>Синтетический элемент {key}</b>',
             'subtitle': '<img src="never-load"> Буквальный текст', 'available': True,
             'message_id': 91, 'time_ms': 1793459500091}
    value.update(extra)
    return value


class ConversationInfoTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        _load_offscreen_font(cls.app)
        cls.app.setStyle('Fusion')

    def setUp(self):
        self.old_style = self.app.styleSheet()
        self.app.setStyleSheet(build_stylesheet('light', '#0085FF'))
        self.network = patch('maxclient.ui.widgets.avatar.QNetworkAccessManager',
                             side_effect=AssertionError('No network in Info presenter'))
        self.network.start()
        self.panel = ConversationInfo()
        self.panel.resize(815, 900)
        self.panel.show()
        self.context = object()
        self.actions, self.requests, self.more, self.opened, self.menu_actions = [], [], [], [], []
        self.panel.action_requested.connect(lambda action, context: self.actions.append((action, context)))
        self.panel.tab_requested.connect(lambda tab, context: self.requests.append((tab, context)))
        self.panel.more_requested.connect(lambda tab, context: self.more.append((tab, context)))
        self.panel.item_requested.connect(lambda item, context: self.opened.append((item, context)))
        self.panel.menu_action_requested.connect(lambda action, value, context: self.menu_actions.append((action, value, context)))
        self.app.processEvents()

    def tearDown(self):
        self.panel.close()
        self.panel.deleteLater()
        self.app.processEvents()
        self.network.stop()
        self.app.setStyleSheet(self.old_style)

    def test_unknown_availability_is_visible_disabled_and_has_no_action_callbacks(self):
        self.assertTrue(self.panel.set_conversation(snapshot(self.context, availability={}, tabs={})))
        self.assertEqual(set(self.panel.buttons), {'chat', 'call', 'video', 'mute', 'find', 'more'})
        for button in self.panel.buttons.values():
            self.assertFalse(button.isEnabled())
            button.click()
        self.panel.edit_button.click()
        self.assertEqual(self.actions, [])
        self.assertEqual(self.requests, [])
        self.assertIn('недоступен', self.panel.tab_status.text())

    def test_explicit_actions_emit_context_without_changing_confirmed_mute_state(self):
        self.panel.set_conversation(snapshot(self.context))
        self.panel.buttons['mute'].click()
        self.assertEqual(self.menu_actions, [('mute', -1, self.context)])
        self.assertEqual(self.actions, [])
        self.assertFalse(self.panel._snapshot['muted'])
        self.panel.set_conversation(snapshot(self.context, muted=True))
        self.assertEqual(self.panel.buttons['mute'].toolTip(), 'Включить звук')
        self.panel.copy_button.click()
        self.panel.edit_button.click()
        self.assertEqual([name for name, _context in self.actions], ['copy_phone', 'edit'])
        self.assertTrue(all(context is self.context for _name, context in self.actions))

    def test_unknown_mute_value_requires_explicit_duration_and_missing_phone_disables_copy(self):
        self.panel.set_conversation(snapshot(self.context, muted=None, phone=None))
        self.assertTrue(self.panel.buttons['mute'].isEnabled())
        self.assertFalse(self.panel.copy_button.isEnabled())
        self.assertEqual(self.panel.phone.text(), 'Номер не передан')
        self.assertEqual(self.panel.phone_label.text(), 'Телефон')
        self.panel.copy_button.click()
        self.panel.buttons['mute'].click()
        self.assertEqual(self.actions, [])
        self.assertEqual(self.menu_actions, [])
        self.assertIsNotNone(self.panel._menu)

    def test_read_only_blocks_effectful_actions_but_back_copy_chat_and_find_work(self):
        self.panel.set_conversation(snapshot(self.context, read_only=True))
        for action in ('call', 'video', 'mute', 'more'):
            self.assertFalse(self.panel.buttons[action].isEnabled())
        self.assertFalse(self.panel.edit_button.isEnabled())
        for action in ('chat', 'find'):
            self.panel.buttons[action].click()
        self.panel.copy_button.click()
        back = []
        self.panel.back_requested.connect(lambda: back.append(True))
        self.panel.back_button.click()
        self.assertEqual(back, [True])
        self.assertEqual([name for name, _context in self.actions], ['chat', 'find', 'copy_phone'])
        self.assertFalse(self.panel.set_read_only(object(), False))
        self.assertTrue(self.panel.set_read_only(self.context, False))
        self.assertTrue(self.panel.buttons['call'].isEnabled())

    def test_busy_initial_state_defers_first_read_and_never_duplicates_it(self):
        self.panel.set_conversation(snapshot(self.context, busy=True))
        self.assertEqual(self.requests, [])
        self.assertFalse(self.panel.buttons['chat'].isEnabled())
        self.assertTrue(self.panel.set_busy(self.context, False))
        self.assertEqual(self.requests, [('common_chats', self.context)])
        self.panel.set_busy(self.context, False)
        self.assertEqual(len(self.requests), 1)
        self.panel.set_error(self.context, '<b>private exception must not display</b>')
        self.assertEqual(self.panel.status.text(), 'Не удалось обновить сведения')
        self.assertNotIn('private', self.panel.status.text())

    def test_tabs_request_only_supported_pages_cache_them_and_render_literal_items(self):
        self.panel.set_conversation(snapshot(self.context, tabs={'common_chats': True, 'files': True}))
        self.assertEqual(self.requests, [('common_chats', self.context)])
        self.assertEqual([self.panel.tabs.tabText(i) for i in range(5)], [title for _key, title in TABS])
        self.assertTrue(self.panel.set_tab_items(self.context, 'common_chats', [entry('a')]))
        self.panel.tabs.setCurrentIndex(2)
        self.assertEqual(self.requests[-1], ('files', self.context))
        self.panel.set_tab_items(self.context, 'files', [entry('file')])
        self.panel.tabs.setCurrentIndex(0)
        self.assertEqual(len(self.requests), 2)
        item = self.panel.items.item(0)
        row = self.panel.items.itemWidget(item)
        labels = row.findChildren(QLabel)
        self.assertTrue(all(label.textFormat() == Qt.TextFormat.PlainText for label in labels))
        self.assertEqual(labels[0]._full_text, entry('a')['title'])
        self.assertEqual(labels[1]._full_text, entry('a')['subtitle'])
        self.panel.items.setCurrentRow(0)
        QTest.keyClick(self.panel.items, Qt.Key.Key_Return)
        self.assertEqual(self.opened, [(entry('a'), self.context)])

    def test_item_availability_unknown_or_false_never_emits_activation(self):
        self.panel.set_conversation(snapshot(self.context))
        values = [entry('disabled', available=False), entry('unknown')]
        values[1].pop('available')
        self.panel.set_tab_items(self.context, 'common_chats', values)
        for index in range(2):
            item = self.panel.items.item(index)
            self.assertEqual(item.flags(), Qt.ItemFlag.NoItemFlags)
            self.panel._activate_item(item)
        self.assertEqual(self.opened, [])

    def test_tab_pagination_is_explicit_busy_guarded_and_cursor_owned_by_parent(self):
        self.panel.set_conversation(snapshot(self.context))
        self.panel.set_tab_items(self.context, 'common_chats', [entry('a')], complete=False,
                                 status='Доступна следующая страница')
        self.assertTrue(self.panel.more_button.isVisible())
        self.panel.more_button.click()
        self.panel.more_button.click()
        self.assertEqual(self.more, [('common_chats', self.context)])
        self.assertEqual(self.panel.items.count(), 1)
        self.panel.set_tab_error(self.context, 'common_chats', 'secret cursor/body')
        self.assertEqual(self.panel.items.count(), 1)
        self.panel.retry_button.click()
        self.assertEqual(len(self.more), 2)
        self.panel.set_tab_items(self.context, 'common_chats', [entry('a'), entry('b')], append=True)
        self.assertEqual(self.panel.items.count(), 2)
        self.assertFalse(self.panel.more_button.isVisible())
        self.assertEqual(len(self.requests), 1)

    def test_initial_error_is_distinct_from_confirmed_empty_and_manual_retry_is_a_read(self):
        self.panel.set_conversation(snapshot(self.context))
        self.panel.set_tab_error(self.context, 'common_chats', '<b>do not echo</b>')
        self.assertIn('Не удалось', self.panel.tab_status.text())
        self.assertTrue(self.panel.retry_button.isVisible())
        self.panel.retry_button.click()
        self.assertEqual(self.requests, [('common_chats', self.context)] * 2)
        self.panel.set_tab_items(self.context, 'common_chats', [])
        self.assertEqual(self.panel.tab_status.text(), 'Нет данных для отображения')
        self.assertFalse(self.panel.retry_button.isVisible())

    def test_context_swap_clears_items_phone_photo_and_rejects_old_callbacks(self):
        photo = QPixmap(10, 10)
        photo.fill(QColor('#11AA55'))
        self.panel.set_conversation(snapshot(self.context, avatar_pixmap=photo))
        self.panel.set_tab_items(self.context, 'common_chats', [entry('old')])
        other = object()
        self.panel.set_conversation(snapshot(other, chat_id=43, peer_id=8, title='Новый синтетический контакт', phone=None))
        self.assertTrue(self.panel.avatar._photo.isNull())
        self.assertEqual(self.panel.items.count(), 0)
        self.assertEqual(self.panel.phone.text(), 'Номер не передан')
        self.assertFalse(self.panel.set_tab_items(self.context, 'common_chats', [entry('late')]))
        self.assertFalse(self.panel.set_tab_error(self.context, 'common_chats'))
        self.assertFalse(self.panel.set_busy(self.context, True))
        self.assertEqual(self.panel.items.count(), 0)

    def test_clear_rejects_callbacks_and_same_context_metadata_refresh_keeps_loaded_tab(self):
        self.panel.set_conversation(snapshot(self.context))
        self.panel.set_tab_items(self.context, 'common_chats', [entry('old')])
        self.panel.set_conversation(snapshot(self.context, presence='Обновлённый синтетический статус'))
        self.assertEqual(self.panel.items.count(), 1)
        self.assertEqual(len(self.requests), 1)
        self.panel.clear()
        self.assertIsNone(self.panel._context)
        self.assertEqual(self.panel.items.count(), 0)
        self.assertFalse(self.panel.set_tab_items(self.context, 'common_chats', [entry('late')]))
        self.assertFalse(self.panel.set_tab_busy(self.context, 'common_chats', True))
        self.assertFalse(self.panel.set_tab_error(self.context, 'common_chats'))

    def test_per_tab_busy_is_context_guarded_and_not_a_fabricated_empty_completion(self):
        self.panel.set_conversation(snapshot(self.context))
        self.assertFalse(self.panel.set_tab_busy(object(), 'common_chats', True))
        self.panel.set_tab_busy(self.context, 'common_chats', True)
        self.assertEqual(self.panel.tab_status.text(), 'Загрузка…')
        self.panel.set_tab_busy(self.context, 'common_chats', False)
        self.assertEqual(self.panel.tab_status.text(), 'Данные ещё не получены')
        self.assertTrue(self.panel.retry_button.isVisible())
        self.panel.set_tab_items(self.context, 'common_chats', [entry('a')], complete=False)
        self.panel.set_tab_busy(self.context, 'common_chats', True)
        self.assertFalse(self.panel.more_button.isEnabled())
        self.panel._activate_item(self.panel.items.item(0))
        self.assertEqual(self.opened, [])
        self.panel.set_tab_busy(self.context, 'common_chats', False)
        self.assertEqual(self.panel.items.count(), 1)
        self.assertTrue(self.panel.more_button.isEnabled())

    def test_avatar_accepts_only_owned_pixmap_and_ignores_url_path_and_country_guesses(self):
        self.panel.set_conversation(snapshot(self.context, avatar_url='https://invalid.example/never',
                                              avatar_pixmap='C:/never/open.png'))
        self.assertTrue(self.panel.avatar._photo.isNull())
        self.assertEqual(self.panel.phone_label.text(), 'Телефон')
        self.panel.set_conversation(snapshot(self.context, phone_label='Синтетическая метка источника'))
        self.assertEqual(self.panel.phone_label.text(), 'Синтетическая метка источника')

    def test_local_filter_is_not_a_server_request_and_preserves_exact_item_payload(self):
        self.panel.set_conversation(snapshot(self.context))
        self.panel.set_tab_items(self.context, 'common_chats', [entry('a', title='Первый'), entry('b', title='Искомый')])
        request_count = len(self.requests)
        self.panel.search.setText('искомый')
        self.assertEqual(self.panel.items.count(), 1)
        self.assertEqual(len(self.requests), request_count)
        self.panel._activate_item(self.panel.items.item(0))
        self.assertEqual(self.opened[0][0]['message_id'], 91)
        self.assertEqual(self.opened[0][0]['time_ms'], 1793459500091)
        self.panel.search.setText('нет совпадения')
        self.assertIn('среди загруженных', self.panel.tab_status.text())

    def test_invalid_data_and_reused_context_with_other_ids_fail_closed(self):
        self.assertFalse(self.panel.set_conversation(snapshot(self.context, chat_id=True)))
        self.assertEqual(self.actions, [])
        self.assertTrue(self.panel.set_conversation(snapshot(self.context)))
        self.assertFalse(self.panel.set_conversation(snapshot(self.context, chat_id=43)))
        self.assertIsNone(self.panel._context)
        self.panel.set_conversation(snapshot(object()))
        current = self.panel._context
        self.assertTrue(self.panel.set_tab_items(current, 'common_chats', [{'key': 'invalid', 'title': {'html': 1}}]))
        self.assertEqual(self.panel.items.item(0).flags(), Qt.ItemFlag.NoItemFlags)
        self.assertIn('недоступны', self.panel.items.itemWidget(self.panel.items.item(0)).findChildren(QLabel)[0]._full_text)

    def test_official_geometry_and_responsive_card_scrolling_synthetic_captures(self):
        output = Path('.test-tmp')
        output.mkdir(exist_ok=True)
        self.panel.set_conversation(snapshot(self.context, description='Синтетическое описание без данных аккаунта.'))
        self.panel.set_tab_items(self.context, 'common_chats', [entry(str(index)) for index in range(9)], complete=False)
        for theme in ('light', 'dark'):
            self.app.setStyleSheet(build_stylesheet(theme, '#0085FF'))
            self.panel.apply_theme(theme)
            for width, height in ((815, 900), (360, 560)):
                self.panel.resize(width, height)
                self.app.processEvents()
                QTest.qWait(25)
                self.assertEqual(self.panel.header.height(), 62)
                self.assertEqual(self.panel.scroll.horizontalScrollBar().maximum(), 0)
                self.assertEqual(self.panel.items.horizontalScrollBar().maximum(), 0)
                self.assertLessEqual(self.panel.content.width(), 624)
                self.assertLessEqual(self.panel.tabs.width(), self.panel.tabs_card.width())
                for index in range(self.panel.tabs.count()):
                    self.assertLessEqual(self.panel.tabs.tabRect(index).right(), self.panel.tabs.width())
                for index in range(self.panel.items.count()):
                    row_widget = self.panel.items.itemWidget(self.panel.items.item(index))
                    self.assertLessEqual(row_widget.width(), self.panel.items.viewport().width())
                    for label in row_widget.findChildren(QLabel):
                        self.assertLessEqual(label.fontMetrics().horizontalAdvance(label.text()), label.width() + 1)
                if width == 815:
                    self.assertEqual(self.panel.content.width(), 624)
                    self.assertEqual(self.panel._columns, 6)
                    self.assertEqual(self.panel.buttons['chat'].size().toTuple(), (94, 68))
                else:
                    self.assertEqual(self.panel._columns, 3)
                self.panel.scroll.verticalScrollBar().setValue(0)
                self.app.processEvents()
                self.assertTrue(self.panel.grab().save(str(output / f'conversation_info_{width}_{theme}.png')))
                self.panel.scroll.verticalScrollBar().setValue(self.panel.scroll.verticalScrollBar().maximum())
                self.app.processEvents()
                button = self.panel.more_button
                point = button.mapTo(self.panel.scroll.viewport(), QPoint(0, 0))
                self.assertGreaterEqual(point.y(), 0)
                self.assertLessEqual(point.y() + button.height(), self.panel.scroll.viewport().height())
                self.assertTrue(self.panel.grab().save(str(output / f'conversation_info_{width}_{theme}_bottom.png')))


if __name__ == '__main__':
    unittest.main()
