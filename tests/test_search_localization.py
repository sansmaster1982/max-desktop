"""Search relabeling preserves synthetic queries, responses and Qt presenters."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import ast
from contextlib import ExitStack
from pathlib import Path
import re
import unittest
from unittest.mock import patch

from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication, QVBoxLayout, QWidget
from shiboken6 import isValid

from maxclient.core._search_translations import SEARCH_TRANSLATIONS
from maxclient.core.i18n import tr, translation_available
from maxclient.ui.search_panel import SearchPanel
from maxclient.ui.theme import build_stylesheet
from qt_test_lifecycle import capture_qt_state, restore_qt_state


LANGUAGES = ('ru', 'en', 'es', 'pt', 'fr', 'uz', 'be')


def row(key, section='CHATS', **extra):
    return {'key': key, 'section': section, 'title': 'Результаты поиска',
            'preview': '<b>Ищем…</b>', 'chat_id': 42,
            'message_id': 71 if section == 'MESSAGES' else None, 'user_id': None, **extra}


class SearchLocalizationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.addCleanup(restore_qt_state, capture_qt_state())
        self.app.setStyleSheet('')
        self.app.setStyle('Fusion')
        self.app.setFont(QFont('Segoe UI', 10))
        self.app.setStyleSheet(build_stylesheet('light', '#0085FF'))
        self.host = QWidget()
        layout = QVBoxLayout(self.host)
        layout.setContentsMargins(0, 0, 0, 0)
        self.panel = SearchPanel(self.host, debounce_ms=60000, language='en')
        layout.addWidget(self.panel)
        self.host.resize(360, 480)
        self.requests, self.activations, self.clears = [], [], []
        self.panel.search_requested.connect(self.requests.append)
        self.panel.result_activated.connect(self.activations.append)
        self.panel.clear_requested.connect(lambda: self.clears.append(True))
        self.context = object()
        self.panel.bind_owner(self.context)
        self.host.show()
        self.app.processEvents()

    def tearDown(self):
        if isValid(self.host):
            self.host.close()
            self.host.deleteLater()
            QCoreApplication.sendPostedEvents(self.host, QEvent.Type.DeferredDelete)
        self.app.processEvents()

    def start(self, query='literal query'):
        self.panel.set_query(query)
        self.panel._timer.stop()
        self.panel._start(None)
        return self.requests[-1]

    def page(self, request, rows, marker=None, **extra):
        return {'query': request['query'], 'context': request['context'],
                'items': rows, 'marker': marker, 'total': len(rows), 'counters': {}, **extra}

    def complete(self, rows, marker=None, **extra):
        request = self.start()
        self.assertTrue(self.panel.complete(request, self.page(request, rows, marker, **extra)))
        self.app.processEvents()

    def snapshot(self):
        panel = self.panel
        identities = {name: getattr(panel, name) for name in
                      ('_context', '_request', '_items', '_keys', '_used_markers')}
        values = {name: getattr(panel, name) for name in
                  ('_query', '_generation', '_marker', '_retry_marker', '_retry_available',
                   '_public_expanded', '_partial', '_paging_blocked')}
        values['items_copy'] = [dict(value) for value in panel._items]
        values['keys_copy'] = set(panel._keys)
        values['used_copy'] = set(panel._used_markers)
        return identities, values

    def assert_state(self, before):
        identities, values = before
        for name, value in identities.items():
            self.assertIs(getattr(self.panel, name), value, name)
        after = self.snapshot()[1]
        self.assertEqual(after, values)

    def assert_status_languages(self, source):
        before = self.snapshot()
        counts = (len(self.requests), len(self.activations), len(self.clears))
        for language in LANGUAGES:
            self.panel.retranslate(language)
            self.assertEqual(self.panel.status.text(), tr(source, language))
            self.assertEqual(self.panel._status_source, source)
            self.assertEqual(self.panel.status.isHidden(), not bool(source))
            self.assert_state(before)
        self.assertEqual((len(self.requests), len(self.activations), len(self.clears)), counts)

    def test_catalogue_covers_all_eighteen_owned_sources_with_eleven_new_rows(self):
        self.assertEqual(len(SEARCH_TRANSLATIONS), 11)
        anchors = ('Search results', 'Close search', 'Enter text to search', 'Searching…',
                   'Sign in to search', 'Loading more…',
                   'Some results are unavailable. The results received are shown.',
                   'Available results received. The next page is unavailable.',
                   'Could not load more results.', 'Search failed.',
                   'Show the remaining global search results')
        for (source, values), anchor in zip(SEARCH_TRANSLATIONS.items(), anchors):
            self.assertEqual(len(values), 6)
            self.assertEqual(values[0], anchor)
            for language, value in zip(LANGUAGES[1:], values):
                self.assertEqual(tr(source, language), value)
                if language != 'be':
                    self.assertIsNone(re.search('[А-Яа-яЁё]', value))
            self.assertEqual(tr(source, 'pt-BR'), values[2])
        source = Path(__file__).parents[1] / 'maxclient/ui/search_panel.py'
        strings = {node.value for node in ast.walk(ast.parse(source.read_text(encoding='utf-8-sig')))
                   if isinstance(node, ast.Constant) and isinstance(node.value, str)
                   and re.search('[А-Яа-яЁё]', node.value)}
        self.assertEqual(len(strings), 18)
        for text in strings:
            for language in LANGUAGES[1:]:
                self.assertTrue(translation_available(text, language), (text, language))

    def test_constructor_static_labels_accessibility_and_normalization_all_languages(self):
        for language in (*LANGUAGES, 'pt-BR', None):
            panel = SearchPanel(self.host, language=language)
            self.assertEqual(panel.title.text(), tr('Результаты поиска', language))
            self.assertEqual(panel.results.accessibleName(), tr('Результаты поиска', language))
            self.assertEqual(panel.close_button.accessibleName(), tr('Закрыть поиск', language))
            self.assertEqual(panel.close_button.toolTip(), tr('Закрыть поиск', language))
            self.assertEqual(panel.retry_button.text(), tr('Повторить', language))
            self.assertEqual(panel.more_button.text(), tr('Загрузить ещё', language))
            self.assertEqual(panel.status.text(), tr('Введите текст для поиска', language))
            self.assertFalse(panel._timer.isActive())
            self.assertIsNone(panel._request)

    def test_all_status_transitions_use_current_language_without_echoing_raw_error(self):
        self.assert_status_languages('Введите текст для поиска')
        self.panel.bind_owner(None)
        self.assert_status_languages('Поиск доступен после входа')
        self.panel.bind_owner(self.context)
        first = self.start()
        self.assert_status_languages('Ищем…')
        self.panel.fail(first, 'Ищем… private server text')
        self.assert_status_languages('Не удалось выполнить поиск.')
        self.panel.retry()
        request = self.requests[-1]
        self.panel.complete(request, self.page(request, []))
        self.assert_status_languages('Ничего не найдено')
        request = self.start('next')
        self.panel.complete(request, self.page(request, [row('a')], marker='next'))
        self.assert_status_languages('')
        self.panel.load_more()
        self.assert_status_languages('Загружаем ещё…')
        self.panel.fail(self.requests[-1], '<img src="never"> private server text')
        self.assert_status_languages('Не удалось загрузить ещё результаты.')
        self.panel.retry()
        request = self.requests[-1]
        self.panel.complete(request, self.page(request, [row('b')], marker='next'))
        self.assert_status_languages('Получены доступные результаты. Следующая страница недоступна.')
        request = self.start('partial')
        self.panel.complete(request, self.page(request, [row('a')], unavailable_count=1))
        self.assert_status_languages('Часть результатов недоступна. Показаны полученные данные.')

    def test_existing_rows_widgets_selection_scroll_focus_and_data_stay_identical(self):
        values = [row(f'chat-{i}') for i in range(12)]
        values += [row(f'public-{i}', 'PUBLIC_CHATS', chat_id=100+i) for i in range(8)]
        values += [row('message', 'MESSAGES')]
        self.complete(values, marker='cursor')
        results = self.panel.results
        item = results.item(7)
        results.setCurrentItem(item)
        results.setFocus()
        results.verticalScrollBar().setValue(4)
        self.app.processEvents()
        items = [results.item(i) for i in range(results.count())]
        widgets = [results.itemWidget(value) for value in items]
        row_data = [value.data(Qt.ItemDataRole.UserRole) for value in items]
        scroll = results.verticalScrollBar().value()
        focus = QApplication.focusWidget()
        before = self.snapshot()
        emitted = []
        results.itemChanged.connect(lambda *_: emitted.append('item'))
        results.currentItemChanged.connect(lambda *_: emitted.append('current'))
        results.itemSelectionChanged.connect(lambda: emitted.append('selection'))
        with ExitStack() as stack:
            for name in ('_render', '_reset_rows', '_invalidate', '_update_actions', 'set_query', 'bind_owner'):
                stack.enter_context(patch.object(self.panel, name, side_effect=AssertionError(name)))
            for language in LANGUAGES:
                self.panel.retranslate(language)
                self.app.processEvents()
                self.assert_state(before)
                self.assertIs(results.currentItem(), item)
                self.assertIs(QApplication.focusWidget(), focus)
                self.assertEqual(results.verticalScrollBar().value(), scroll)
                for index, old in enumerate(items):
                    self.assertIs(results.item(index), old)
                    self.assertIs(results.itemWidget(old), widgets[index])
                    self.assertEqual(old.data(Qt.ItemDataRole.UserRole), row_data[index])
                self.assertEqual([header.text() for header, _ in self.panel._headers],
                                 [tr(source, language) for _, source in self.panel._headers])
        self.assertEqual(emitted, [])
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.activations, [])
        self.assertEqual(self.clears, [])

    def test_raw_key_collisions_html_accessibility_avatar_and_activation_are_literal(self):
        value = row('raw', title='Результаты поиска', preview='<b>Показать ещё</b>')
        self.complete([value])
        item = self.panel.results.item(1)
        widget = self.panel.results.itemWidget(item)
        avatar = widget.avatar
        for language in LANGUAGES:
            self.panel.retranslate(language)
            self.assertEqual(widget.title._full_text, value['title'])
            self.assertEqual(widget.preview._full_text, value['preview'])
            self.assertEqual(widget.title.textFormat(), Qt.TextFormat.PlainText)
            self.assertEqual(widget.preview.textFormat(), Qt.TextFormat.PlainText)
            self.assertEqual(widget.accessibleName(), value['title']+'\n'+value['preview'])
            self.assertEqual(item.data(Qt.ItemDataRole.AccessibleTextRole), value['title'])
            self.assertIs(widget.avatar, avatar)
        self.panel._activate(item)
        self.assertEqual(self.activations, [value])

    def test_public_expand_is_local_and_stable_after_translated_heading(self):
        self.complete([row(f'p-{i}', 'PUBLIC_CHATS') for i in range(8)])
        button = self.panel._expand_button
        self.panel.retranslate('fr')
        self.assertIs(self.panel._expand_button, button)
        self.assertEqual(button.text(), tr('Показать ещё', 'fr'))
        self.assertEqual(button.accessibleName(), tr('Показать остальные результаты глобального поиска', 'fr'))
        button.click()
        self.assertTrue(self.panel._public_expanded)
        self.assertEqual(self.panel.results.count(), 9)
        self.assertIsNone(self.panel._expand_button)
        items = [self.panel.results.item(i) for i in range(9)]
        self.panel.retranslate('uz')
        self.assertEqual([self.panel.results.item(i) for i in range(9)], items)
        self.assertEqual(len(self.requests), 1)
        self.panel.clear()
        self.assertEqual(self.panel._headers, [])
        self.panel.retranslate('be')  # Old QListWidgetItem wrappers are invalid now.
        self.assertEqual(self.panel.results.count(), 0)

    def test_debounce_timer_id_interval_and_pending_query_are_not_restarted(self):
        self.panel.set_query('  literal query  ')
        timer = self.panel._timer
        timer_id, interval, remaining = timer.timerId(), timer.interval(), timer.remainingTime()
        before = self.snapshot()
        for language in LANGUAGES:
            self.panel.retranslate(language)
            self.assertTrue(timer.isActive())
            self.assertEqual(timer.timerId(), timer_id)
            self.assertEqual(timer.interval(), interval)
            self.assertLessEqual(timer.remainingTime(), remaining)
            self.assert_state(before)
        self.assertEqual(self.requests, [])
        timer.stop()
        timer.timeout.emit()
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.requests[0]['query'], 'literal query')

    def test_inflight_request_survives_locale_and_callback_uses_latest_language(self):
        request = self.start()
        before = self.snapshot()
        self.panel.retranslate('fr')
        self.assert_state(before)
        self.assertTrue(self.panel.complete(request, self.page(request, [row('valid')], unavailable_count=1)))
        self.assertEqual(self.panel.status.text(), tr('Часть результатов недоступна. Показаны полученные данные.', 'fr'))
        self.assertEqual(self.panel.results.item(0).text(), tr('Чаты', 'fr'))
        self.assertEqual(len(self.requests), 1)

    def test_old_query_and_equal_foreign_context_still_rejected_after_retranslate(self):
        class EqualContext:
            def __eq__(self, other):
                return True
        self.panel.bind_owner(EqualContext())
        old = self.start('old')
        current = self.start('current')
        self.panel.retranslate('es')
        self.assertFalse(self.panel.complete(old, self.page(old, [row('old')])))
        self.assertFalse(self.panel.fail(old))
        self.panel.bind_owner(EqualContext())
        self.panel._timer.stop()
        self.panel._start(None)
        live = self.requests[-1]
        self.panel.retranslate('fr')
        self.assertFalse(self.panel.complete(current, self.page(current, [row('foreign')])))
        self.assertTrue(self.panel.complete(live, self.page(live, [row('live')])))
        self.assertEqual([item['key'] for item in self.panel._items], ['live'])

    def test_empty_marker_retry_flags_and_prior_rows_survive_locale_without_auto_retry(self):
        self.complete([row('first')], marker='')
        self.panel.load_more()
        request = self.requests[-1]
        self.assertEqual(request['marker'], '')
        self.panel.fail(request, 'raw ignored')
        before = self.snapshot()
        retry_enabled = self.panel.retry_button.isEnabled()
        for language in LANGUAGES:
            self.panel.retranslate(language)
            self.assert_state(before)
            self.assertEqual(self.panel.retry_button.isEnabled(), retry_enabled)
            self.assertTrue(self.panel.retry_button.isVisible())
            self.assertFalse(self.panel.more_button.isVisible())
        self.assertEqual(len(self.requests), 2)
        self.panel.retry_button.click()
        self.assertEqual(len(self.requests), 3)
        current = self.requests[-1]
        self.assertEqual(current['marker'], '')
        self.assertFalse(self.panel.complete(request, self.page(request, [row('stale')])))
        self.assertTrue(self.panel.complete(current, self.page(current, [row('second')], marker='')))
        self.assertEqual([value['key'] for value in self.panel._items], ['first', 'second'])
        self.assertTrue(self.panel._paging_blocked)
        self.assertFalse(self.panel.more_button.isVisible())

    def test_late_retranslate_deleted_panel_is_inert(self):
        panel = self.panel
        panel.deleteLater()
        QCoreApplication.sendPostedEvents(panel, QEvent.Type.DeferredDelete)
        self.assertFalse(isValid(panel))
        panel.retranslate('fr')
        self.assertEqual(self.requests, [])
        self.assertEqual(self.activations, [])
        self.assertEqual(self.clears, [])

    def test_real_light_dark_narrow_layout_keeps_controls_and_rows_inside(self):
        self.complete([row(f'public-{i}', 'PUBLIC_CHATS', title='Raw long filename ' * 12)
                       for i in range(8)], marker='next', unavailable_count=1)
        for theme in ('light', 'dark'):
            self.app.setStyleSheet(build_stylesheet(theme, '#0085FF'))
            self.panel.apply_theme(theme)
            for language in LANGUAGES:
                self.panel.retranslate(language)
                self.app.processEvents()
                self.assertEqual(self.panel.results.horizontalScrollBar().maximum(), 0)
                self.assertLessEqual(self.panel.actions.width(), self.panel.width())
                self.assertLessEqual(self.panel.close_button.geometry().right(), self.panel.width())
                self.assertLessEqual(self.panel.more_button.geometry().right(), self.panel.actions.width())
                for i in range(self.panel.results.count()):
                    widget = self.panel.results.itemWidget(self.panel.results.item(i))
                    if widget is not None:
                        self.assertLessEqual(widget.width(), self.panel.results.viewport().width())
