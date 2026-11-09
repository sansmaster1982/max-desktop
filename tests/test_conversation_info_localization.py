"""Info language changes use synthetic snapshots, never service or media IO."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from datetime import datetime
import re
import unittest
from unittest.mock import patch

from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtGui import QPixmap
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMenu
from shiboken6 import delete, isValid

from maxclient.core.i18n import SUPPORTED_LANGUAGES, translation_available, tr
from maxclient.core._conversation_info_translations import CONVERSATION_INFO_TRANSLATIONS
from maxclient.ui.conversation_info import ConversationInfo, TABS
from maxclient.ui.conversation_media_grid import month_label
from maxclient.ui.theme import build_stylesheet
from test_conversation_info import snapshot, entry
from test_desktop_shell import _load_offscreen_font


class ConversationInfoLocalizationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.old_style = self.app.styleSheet()
        self.app.setStyleSheet(build_stylesheet('light', '#0085FF'))
        _load_offscreen_font(self.app)
        self.network = patch('maxclient.ui.widgets.avatar.QNetworkAccessManager', side_effect=AssertionError('No network'))
        self.network.start()
        self.panel = ConversationInfo(language='en')
        self.panel.resize(820, 900)
        self.panel.show()
        self.context = object()
        self.events = []
        for name in ('action_requested', 'menu_action_requested', 'tab_requested', 'item_requested',
                     'file_action_requested', 'thumbnail_requested', 'more_requested'):
            getattr(self.panel, name).connect(lambda *args, kind=name: self.events.append((kind, args)))
        self.panel.set_conversation(snapshot(self.context, title='Synthetic chat', presence='', phone='',
                                             availability={'chat': True, 'mute': True, 'find': True, 'more': True},
                                             more_actions=[]))
        self.settle()
        self.events.clear()

    def tearDown(self):
        if isValid(self.panel):
            self.panel.close()
            self.panel.deleteLater()
            QCoreApplication.sendPostedEvents(self.panel, QEvent.Type.DeferredDelete)
        self.app.processEvents()
        self.network.stop()
        self.app.setStyleSheet(self.old_style)

    def settle(self):
        for _ in range(3):
            self.app.processEvents()

    def language(self, language):
        self.panel.retranslate(language)
        self.settle()

    def captions(self):
        p = self.panel
        result = [p.header_title.text(), p.back_button.toolTip(), p.edit_button.toolTip(),
                  p.copy_button.toolTip(), p.phone_label.text(), p.phone.text(), p.status.text(),
                  p.search.placeholderText(), p.search.toolTip(), p.search.accessibleName(),
                  p.retry_button.text(), p.more_button.text(), p.tab_status.text(), p.media_grid.accessibleName()]
        result.extend(p.tabs.tabText(i) for i in range(p.tabs.count()))
        result.extend(p.tabs.tabToolTip(i) for i in range(p.tabs.count()))
        for button in p.buttons.values():
            result.extend((button.title.text(), button.accessibleName(), button.toolTip()))
        return result

    def test_all_owned_captions_in_seven_languages_without_new_requests(self):
        for language in SUPPORTED_LANGUAGES:
            self.language(language)
            if language in ('en', 'es', 'pt', 'fr', 'uz'):
                for text in self.captions():
                    self.assertIsNone(re.search('[\u0400-\u04ff]', text), (language, text))
        self.language('en')
        self.assertEqual(self.panel.header_title.text(), 'Info')
        self.assertEqual(self.panel.more_button.text(), 'Load more')
        self.assertEqual(self.panel.search.accessibleName(), 'Search loaded items')
        self.assertEqual(self.panel.phone.text(), 'Phone number not provided')
        self.assertEqual(self.events, [])

    def test_catalogue_columns_integration_and_alias(self):
        for source, row in CONVERSATION_INFO_TRANSLATIONS.items():
            self.assertEqual(len(row), 6)
            for language, text in zip(SUPPORTED_LANGUAGES[1:], row):
                self.assertTrue(text)
                self.assertTrue(translation_available(source, language), (source, language))
                self.assertEqual(tr(source, language), text)
            self.assertEqual(tr(source, 'pt-BR'), row[2])

    def test_raw_snapshot_fields_equal_to_translation_keys_stay_literal(self):
        self.panel.set_conversation(snapshot(self.context, title='Сведения недоступны', profile_name='Найти',
            presence='Недоступно', description='<b>Загрузка…</b>', phone='Телефон', phone_label='Телефон'))
        self.events.clear()
        for language in SUPPORTED_LANGUAGES:
            self.language(language)
            self.assertEqual(self.panel.name.text(), 'Сведения недоступны')
            self.assertEqual(self.panel.profile_name.text(), 'Найти')
            self.assertEqual(self.panel.presence.text(), 'Недоступно')
            self.assertEqual(self.panel.description.text(), '<b>Загрузка…</b>')
            self.assertEqual(self.panel.phone.text(), 'Телефон')
            self.assertEqual(self.panel.phone_label.text(), 'Телефон')
            self.assertEqual(self.panel.description.textFormat(), Qt.TextFormat.PlainText)
        self.assertEqual(self.events, [])

    def test_language_keeps_list_item_identity_selection_search_and_data(self):
        p = self.panel
        rows = [entry('one', title='Данные недоступны', subtitle='Недоступно'),
                entry('two', title='Данные недоступны two', subtitle='Найти')]
        p.set_tab_items(self.context, 'common_chats', rows, complete=False)
        p.search.setText('Данные недоступны')
        p.items.setCurrentRow(1)
        self.settle()
        item = p.items.currentItem()
        data, state, complete = p._tab_data, dict(p._tab_state), dict(p._tab_complete)
        role = item.data(Qt.ItemDataRole.UserRole)
        self.events.clear()
        with patch.object(p, '_render_tab', side_effect=AssertionError('No rebuilding')), \
                patch.object(p, 'set_conversation', side_effect=AssertionError('No rebind')):
            for language in SUPPORTED_LANGUAGES:
                self.language(language)
                self.assertIs(p.items.currentItem(), item)
                self.assertEqual(item.data(Qt.ItemDataRole.UserRole), role)
                self.assertIs(p._tab_data, data)
                self.assertEqual(p._tab_state, state)
                self.assertEqual(p._tab_complete, complete)
                self.assertEqual(p._selected_tab, 'common_chats')
                self.assertEqual(p.search.text(), 'Данные недоступны')
                self.assertEqual(p._list_labels[0][1].text(), 'Данные недоступны')
                self.assertEqual(p._list_labels[0][2].text(), 'Недоступно')
        self.assertEqual(self.events, [])

    def test_only_locally_generated_invalid_row_fallback_is_translated(self):
        p = self.panel
        p.set_tab_items(self.context, 'common_chats', [{'key': 'bad', 'title': {}},
            entry('literal', title='Данные недоступны', subtitle='', available=False, _info_local_title=True)])
        self.settle()
        self.assertEqual(p._list_labels[0][1].text(), 'Data unavailable')
        self.assertEqual(p._list_labels[1][1].text(), 'Данные недоступны')
        first, second = p.items.item(0), p.items.item(1)
        self.language('fr')
        self.assertIs(p.items.item(0), first)
        self.assertIs(p.items.item(1), second)
        self.assertEqual(p._list_labels[0][1].text(), 'Données indisponibles')
        self.assertEqual(p._list_labels[1][1].text(), 'Данные недоступны')
        self.assertEqual(p._list_labels[1][2].text(), 'Indisponible')

    def test_busy_error_and_pending_are_not_reset_by_translation(self):
        p = self.panel
        p.set_busy(self.context, True)
        self.events.clear()
        self.language('fr')
        self.assertTrue(p._busy)
        self.assertFalse(p.buttons['chat'].isEnabled())
        self.assertEqual(p._status_mode, 'refreshing')
        p.set_error(self.context)
        self.events.clear()
        self.language('en')
        self.assertEqual(p.status.text(), 'Could not update details')
        self.assertEqual(p._status_mode, 'error')
        p.set_action_pending(self.context, True)
        self.language('en')
        self.assertEqual(p.status.text(), 'Waiting for confirmation…')
        self.assertTrue(p._action_pending)
        self.assertFalse(p.buttons['mute'].isEnabled())
        self.assertEqual(self.events, [])

    def test_raw_status_does_not_get_translated_and_clears_previous_source(self):
        p = self.panel
        p.apply_action_state(self.context, pending=True, status='Synthetic',
                             status_source='Сохранение…')
        self.assertNotEqual(p.status.text(), 'Сохранение…')
        p.apply_action_state(self.context, pending=True, status='Сохранение…')
        self.language('fr')
        self.assertEqual(p.status.text(), 'Сохранение…')
        self.assertIsNone(p._snapshot['action_status_source'])

    def test_owner_status_template_keeps_name_literal_and_localizes_fallback(self):
        p = self.panel
        source = 'Права переданы: {name}. Выход не подтверждён.'
        p.set_action_pending(self.context, True, status_source=source,
                             status_values={'name': '<b>новому владельцу</b>'},
                             status_values_sources={'name': 'новому владельцу'})
        self.language('fr')
        self.assertIn('<b>новому владельцу</b>', p.status.text())
        self.assertEqual(p.status.textFormat(), Qt.TextFormat.PlainText)
        p.set_action_pending(self.context, True, status_source=source,
                             status_values_sources={'name': 'новому владельцу'})
        self.language('en')
        self.assertNotIn('новому владельцу', p.status.text())
        self.assertNotIn('{name}', p.status.text())

    def test_status_sources_are_replaced_not_accumulated(self):
        p = self.panel
        p.set_action_pending(self.context, True, status_source='Права переданы: {name}. Выход не подтверждён.',
                             status_values={'name': 'Synthetic'})
        p.set_action_pending(self.context, True, status='Не удалось обновить сведения')
        self.language('es')
        self.assertEqual(p.status.text(), 'Не удалось обновить сведения')
        self.assertEqual(p._snapshot['action_status_values'], {})
        self.assertEqual(p._snapshot['action_status_values_sources'], {})

    def test_page_source_and_raw_page_status_have_distinct_provenance(self):
        p = self.panel
        p.set_tab_items(self.context, 'common_chats', [entry('one')], status='Загрузка…', status_source='Загрузка…')
        self.language('en')
        self.assertEqual(p.tab_status.text(), 'Loading…')
        p.set_tab_items(self.context, 'common_chats', [entry('one')], status='Загрузка…')
        self.language('fr')
        self.assertEqual(p.tab_status.text(), 'Загрузка…')
        self.assertIsNone(p._tab_status_sources['common_chats'])

    def test_empty_filter_error_and_loading_more_statuses_are_localized_without_retries(self):
        p = self.panel
        for state in ('loading', 'loading_more', 'waiting', 'error', 'more_error', 'ready'):
            p._tab_state['common_chats'] = state
            self.events.clear()
            self.language('es')
            self.assertIsNone(re.search('[\u0400-\u04ff]', p.tab_status.text()))
            self.assertEqual(p._tab_state['common_chats'], state)
            self.assertEqual(self.events, [])
        p.set_tab_items(self.context, 'common_chats', [entry('one', title='Synthetic')])
        p.search.setText('not-found')
        self.events.clear()
        self.language('en')
        self.assertEqual(p.tab_status.text(), 'No matches among loaded items')
        self.assertEqual(self.events, [])

    def test_locale_change_revokes_old_more_menu_actions(self):
        p = self.panel
        p.set_conversation(snapshot(self.context, more_actions=[
            {'action': 'search', 'label': 'Найти в чате', 'label_source': 'Найти в чате', 'available': True, 'write': False}]))
        with patch.object(QMenu, 'popup'):
            menu = p.open_more_menu()
        menu.show()
        self.settle()
        self.assertTrue(menu.isVisible())
        action = menu.actions()[0]
        epoch = p._menu_epoch
        self.events.clear()
        # Deliver a stale QAction before deferred deletion, exactly as a queued click could arrive.
        p.retranslate('fr')
        p.retranslate('en')
        action.trigger()
        self.assertGreater(p._menu_epoch, epoch)
        self.assertEqual(self.events, [])
        self.assertIsNone(p._menu)

    def test_mute_duration_values_are_unchanged_after_language_switch(self):
        self.language('fr')
        with patch.object(QMenu, 'popup'):
            menu = self.panel.open_sound_menu()
        menu.show()
        self.settle()
        self.assertTrue(menu.isVisible())
        self.assertEqual([row['value'] for _, row, _ in menu._rows], [3600000, 14400000, 86400000, -1])
        self.events.clear()
        menu.actions()[0].trigger()
        self.assertEqual(self.events, [('menu_action_requested', ('mute', 3600000, self.context))])

    def test_file_menu_is_localized_and_stale_action_cannot_save(self):
        p = self.panel
        p.tabs.setCurrentIndex(2)
        row = entry('file', kind='FILE', file_id=12, file_name='Сохранить файл',
                    download_available=True, chat_id=42, deleted=False, item_type='USER')
        p.set_tab_items(self.context, 'files', [row])
        with patch.object(QMenu, 'popup'):
            menu = p.open_file_menu(row)
        menu.show()
        self.settle()
        self.assertTrue(menu.isVisible())
        self.assertEqual(menu.actions()[0].text(), 'Save file')
        action = menu.actions()[0]
        self.events.clear()
        p.retranslate('fr')
        action.trigger()
        self.assertEqual(self.events, [])
        self.assertIsNone(p._file_menu_row)

    def test_media_language_preserves_pixels_selection_request_sets_and_geometry(self):
        p = self.panel
        p.tabs.setCurrentIndex(1)
        jan = datetime(2026, 1, 12).timestamp() * 1000
        feb = datetime(2026, 2, 12).timestamp() * 1000
        rows = [entry('photo', kind='PHOTO', time_ms=feb, thumbnail_url='https://example.invalid/a'),
                entry('video', kind='VIDEO', time_ms=jan, thumbnail_url='https://example.invalid/b')]
        p.set_tab_items(self.context, 'media', rows)
        self.settle()
        grid = p.media_grid
        pixmap = QPixmap(20, 20)
        pixmap.fill(Qt.GlobalColor.red)
        p.set_thumbnail(self.context, 'photo', pixmap)
        grid.setFocus()
        QTest.keyClick(grid, Qt.Key.Key_Right)
        self.settle()
        generation, current = grid._generation, grid._current
        cells, requested = list(grid._cells), set(grid._requested)
        p_requested = set(p._media_thumbnail_requests)
        pixels = grid._pixmaps['photo'].cacheKey()
        data = p._tab_data
        self.events.clear()
        with patch.object(grid, 'set_rows', side_effect=AssertionError('No rows reset')):
            for language in SUPPORTED_LANGUAGES:
                self.language(language)
                self.assertEqual(grid._generation, generation)
                self.assertEqual(grid._current, current)
                self.assertEqual(grid._cells, cells)
                self.assertEqual(grid._requested, requested)
                self.assertEqual(p._media_thumbnail_requests, p_requested)
                self.assertEqual(grid._pixmaps['photo'].cacheKey(), pixels)
                self.assertIs(p._tab_data, data)
        self.language('en')
        self.assertEqual([text for _, text in grid._headers], ['February 2026', 'January 2026'])
        self.assertEqual(self.events, [])

    def test_month_labels_are_localized_without_changing_timestamp_rules(self):
        january = datetime(2026, 1, 12).timestamp() * 1000
        self.assertEqual(month_label(january), 'Январь 2026')
        self.assertEqual(month_label(january, 'fr'), 'Janvier 2026')
        self.assertEqual(month_label(january, 'be'), 'Студзень 2026')
        self.assertEqual(month_label(True, 'en'), '')
        self.assertEqual(month_label(float('nan'), 'en'), '')

    def test_pending_tab_reply_uses_new_language_but_stale_context_still_rejected(self):
        p = self.panel
        state = dict(p._tab_state)
        self.language('fr')
        self.assertEqual(p._tab_state, state)
        self.assertTrue(p.set_tab_items(self.context, 'common_chats', []))
        self.assertEqual(p.tab_status.text(), 'Aucune donnée à afficher')
        old = self.context
        new = object()
        p.set_conversation(snapshot(new))
        self.assertFalse(p.set_tab_items(old, 'common_chats', [entry('late')]))
        self.assertFalse(p.set_action_pending(old, True, status_source='Сохранение…'))

    def test_closed_or_deleted_presenter_retranslation_is_safe(self):
        self.panel.close()
        self.panel.retranslate('fr')
        delete(self.panel)
        self.panel.retranslate('en')


if __name__ == '__main__':
    unittest.main()
