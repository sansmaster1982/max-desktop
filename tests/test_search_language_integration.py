"""Real shell locale wiring with fake read callbacks; no server search."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import unittest
from unittest.mock import Mock, patch

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from maxclient.core.config import Settings
from maxclient.core.i18n import SUPPORTED_LANGUAGES, tr
import test_search_shell as fixture


class SearchLanguageIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.f = fixture.SearchShellTests()
        self.f.app = self.app
        with patch.object(fixture, 'Settings', side_effect=lambda **kw: Settings(ui_language='fr', **kw)):
            self.f.setUp()
        self.addCleanup(self.f.tearDown)
        self.w = self.f.window
        self.panel = self.w.search_panel

    def test_initial_language_comes_from_settings_before_any_query(self):
        self.assertEqual(self.panel.title.text(), tr('Результаты поиска', 'fr'))
        self.assertEqual(self.panel.close_button.accessibleName(), tr('Закрыть поиск', 'fr'))
        self.assertEqual(self.panel.status.text(), tr('Введите текст для поиска', 'fr'))
        self.assertEqual(self.panel._query, '')
        self.assertEqual(self.panel._items, [])
        self.f.service.search_all.assert_not_called()
        self.f.jobs.assert_not_called()

    def test_language_hook_keeps_pending_query_timer_and_sidebar_state(self):
        self.panel._timer.setInterval(100000)
        self.w.search.setText('  Результаты поиска <literal>  ')
        self.w.search.setSelection(3, 7)
        self.w.chat_view.input.setPlainText('untouched draft')
        timer_id = self.panel._timer.timerId()
        original = (self.panel._context, self.panel._generation, self.panel._query,
                    self.w.search.text(), self.w.search.selectionStart(), self.w.search.selectedText())
        emitted = Mock()
        self.panel.search_requested.connect(emitted)
        with (patch.object(self.panel, 'set_query', side_effect=AssertionError('No query restart')),
              patch.object(self.panel, '_render', side_effect=AssertionError('No result rebuild')),
              patch.object(self.panel, '_invalidate', side_effect=AssertionError('No invalidation'))):
            for language in SUPPORTED_LANGUAGES:
                self.w.settings.ui_language = language
                self.w._apply_local_language()
                self.assertEqual(self.panel.status.text(), tr('Ищем…', language))
                self.assertEqual(self.panel.title.text(), tr('Результаты поиска', language))
                self.assertTrue(self.panel._timer.isActive())
                self.assertEqual(self.panel._timer.timerId(), timer_id)
                self.assertEqual((self.panel._context, self.panel._generation, self.panel._query,
                                  self.w.search.text(), self.w.search.selectionStart(),
                                  self.w.search.selectedText()), original)
                self.assertEqual(self.w.chat_view.input.toPlainText(), 'untouched draft')
                self.assertIs(self.w._chat_pages.currentWidget(), self.panel)
        emitted.assert_not_called()
        self.f.jobs.assert_not_called()
        self.f.service.search_all.assert_not_called()

    def test_current_callback_after_locale_change_preserves_raw_result_and_old_callback_is_rejected(self):
        request, callbacks = self.f.start('literal query')
        pending = self.panel._request
        self.w.settings.ui_language = 'en'
        self.w._apply_local_language()
        self.assertIs(self.panel._request, pending)
        self.assertEqual(self.f.jobs.call_count, 1)
        raw = '<b>Результаты поиска & raw</b>'
        page = self.f.page(request)
        page['items'] = [self.f.row(title=raw, preview='Введите текст для поиска')]
        callbacks['on_done'](page)
        item = self.panel.results.item(1)
        row_widget = self.panel.results.itemWidget(item)
        self.assertEqual(self.panel.results.item(0).text(), tr('Сообщения', 'en'))
        self.assertEqual(row_widget.title._full_text, raw)
        self.assertEqual(row_widget.preview._full_text, 'Введите текст для поиска')
        self.panel.results.setCurrentItem(item)
        data = item.data(Qt.ItemDataRole.UserRole)
        with patch.object(self.panel, '_render', side_effect=AssertionError('No result rebuild')):
            for language in SUPPORTED_LANGUAGES:
                self.w.settings.ui_language = language
                self.w._apply_local_language()
                self.assertIs(self.panel.results.item(1), item)
                self.assertIs(self.panel.results.currentItem(), item)
                self.assertIs(self.panel.results.itemWidget(item), row_widget)
                self.assertEqual(item.data(Qt.ItemDataRole.UserRole), data)
                self.assertEqual(row_widget.title._full_text, raw)
                self.assertEqual(row_widget.preview._full_text, 'Введите текст для поиска')
        self.assertEqual(self.f.jobs.call_count, 1)
        self.w.search.setText('new query')
        callbacks['on_done'](page)
        self.assertEqual(self.panel._items, [])
        self.assertEqual(self.panel._query, 'new query')
        self.f.service.search_all.assert_not_called()


if __name__ == '__main__':
    unittest.main()
