"""Grouped search: synthetic values, opaque contexts, no service or network."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from pathlib import Path
import unittest

from PySide6.QtCore import QCoreApplication, QElapsedTimer, QEvent, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLabel, QPushButton, QVBoxLayout, QWidget
from shiboken6 import isValid

from maxclient.ui.search_panel import SearchPanel
from maxclient.ui.theme import build_stylesheet
from test_desktop_shell import _load_offscreen_font


def row(key, section='CHATS', **extra):
    value = {'key': key, 'section': section, 'title': '<b>Тестовый заголовок</b>',
             'preview': '<img src="never-load"> Синтетический текст',
             'chat_id': 42, 'message_id': None, 'user_id': None}
    if section == 'MESSAGES':
        value['message_id'] = 701
    if section == 'CONTACTS':
        value['chat_id'] = None
        value['user_id'] = 91
    value.update(extra)
    return value


class SearchPanelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        _load_offscreen_font(cls.app)
        if cls.app.style().objectName().casefold() != 'fusion':
            cls.app.setStyle('Fusion')

    def setUp(self):
        self.host = QWidget()
        self.host.setStyleSheet(build_stylesheet('light', '#0085FF'))
        layout = QVBoxLayout(self.host)
        layout.setContentsMargins(0, 0, 0, 0)
        self.panel = SearchPanel(self.host, debounce_ms=20)
        layout.addWidget(self.panel)
        self.host.resize(360, 460)
        self.context = object()
        self.requests = []
        self.activations = []
        self.panel.search_requested.connect(self.requests.append)
        self.panel.result_activated.connect(self.activations.append)
        self.panel.bind_owner(self.context)
        self.host.show()
        self.app.processEvents()

    def tearDown(self):
        self.host.close()
        self.host.deleteLater()
        # processEvents alone does not deliver DeferredDelete without a running
        # outer Qt event loop. Delete this owned tree, including its timers.
        if isValid(self.host):
            QCoreApplication.sendPostedEvents(self.host, QEvent.Type.DeferredDelete)
        self.app.processEvents()

    def wait_until(self, condition, *, timeout_ms=2000):
        elapsed = QElapsedTimer()
        elapsed.start()
        while not condition() and elapsed.elapsed() < timeout_ms:
            QTest.qWait(5)
        self.assertTrue(condition(), f'Qt condition not reached within {timeout_ms}ms')

    def wait_for_request(self, previous_count):
        self.wait_until(lambda: len(self.requests) > previous_count)
        self.assertEqual(len(self.requests), previous_count + 1)
        request = self.requests[-1]
        self.assertEqual(request['query'], self.panel._query)
        self.assertEqual(request['generation'], self.panel._generation)
        self.assertIs(request['context'], self.panel._context)
        return request

    def start(self, query='пример'):
        before = len(self.requests)
        self.panel.set_query(query)
        return self.wait_for_request(before)

    def page(self, request, items, marker=None, **extra):
        value = {'query': request['query'], 'context': request['context'],
                 'items': items, 'marker': marker, 'total': len(items), 'counters': {}}
        value.update(extra)
        return value

    def shown_rows(self):
        return [item.data(Qt.ItemDataRole.UserRole) for item in
                (self.panel.results.item(i) for i in range(self.panel.results.count()))
                if isinstance(item.data(Qt.ItemDataRole.UserRole), dict)]

    def test_debounce_is_300_in_production_trimmed_coalesced_and_empty_has_no_request(self):
        default = SearchPanel(self.host)
        self.assertEqual(default._timer.interval(), 300)
        actual_requests = []
        elapsed = QElapsedTimer()
        default.search_requested.connect(lambda request: actual_requests.append((elapsed.elapsed(), request)))
        default.bind_owner(object())
        elapsed.start()
        default.set_query('production delay')
        self.assertEqual(actual_requests, [])
        QTest.qWait(80)
        # Under load 80ms of event processing may itself finish late. Measure the
        # real signal time; default Qt CoarseTimer permits up to5% early firing.
        self.assertTrue(all(time_ms >= 280 for time_ms, _request in actual_requests))
        self.wait_until(lambda: bool(actual_requests))
        self.assertEqual(len(actual_requests), 1)
        self.assertGreaterEqual(actual_requests[0][0], 280)
        default.deleteLater()
        self.panel.set_query(' п ')
        self.panel.set_query(' пример ')
        self.assertEqual(self.requests, [])
        self.wait_for_request(0)
        self.assertEqual(len(self.requests), 1)
        request = self.requests[0]
        self.assertEqual(set(request), {'query', 'marker', 'generation', 'context'})
        self.assertEqual(request['query'], 'пример')
        self.assertIs(request['context'], self.context)
        self.panel.set_query('   ')
        QTest.qWait(30)
        self.assertEqual(len(self.requests), 1)
        self.assertFalse(self.panel.complete(request, self.page(request, [row('late')])))
        self.assertEqual(self.shown_rows(), [])

    def test_late_query_success_and_error_cannot_replace_current_results(self):
        old = self.start('старый')
        current = self.start('новый')
        self.assertFalse(self.panel.complete(old, self.page(old, [row('old')])))
        self.assertFalse(self.panel.fail(old, 'secret error'))
        self.assertTrue(self.panel.complete(current, self.page(current, [row('new')])))
        self.assertEqual([value['key'] for value in self.shown_rows()], ['new'])
        self.assertFalse(self.panel.complete(old, self.page(old, [row('old')])))

    def test_owner_context_is_identity_bound_and_swap_clears_rows_and_callbacks(self):
        class EqualContext:
            def __eq__(self, other):
                return True
        a, b = EqualContext(), EqualContext()
        self.panel.bind_owner(a)
        old = self.start()
        self.panel.complete(old, self.page(old, [row('old')]))
        before = len(self.requests)
        self.panel.bind_owner(b)
        self.assertEqual(self.shown_rows(), [])
        current = self.wait_for_request(before)
        self.assertIs(current['context'], b)
        self.assertFalse(self.panel.complete(old, self.page(old, [row('leak')])))
        wrong_page = self.page(current, [row('wrong-owner')], context=a)
        self.assertFalse(self.panel.complete(current, wrong_page))
        self.assertEqual(self.shown_rows(), [])
        self.panel.bind_owner(None)
        self.panel.set_query('другое')
        count = len(self.requests)
        QTest.qWait(35)
        self.assertEqual(len(self.requests), count)

    def test_groups_public_first_five_local_expand_and_no_speculative_tabs(self):
        request = self.start()
        values = [row('message', 'MESSAGES'), row('owned')]
        values += [row(f'public-{i}', 'PUBLIC_CHATS', chat_id=100+i) for i in range(8)]
        self.panel.complete(request, self.page(request, values))
        self.assertEqual([value['key'] for value in self.shown_rows()],
                         ['owned', *[f'public-{i}' for i in range(5)], 'message'])
        buttons = [button for button in self.panel.results.findChildren(QPushButton)
                   if button.text() == 'Показать ещё']
        self.assertEqual(len(buttons), 1)
        before = len(self.requests)
        buttons[0].click()
        self.assertEqual(len(self.shown_rows()), 10)
        self.assertEqual(len(self.requests), before)
        self.assertFalse(self.panel.more_button.isVisible())

    def test_literal_result_text_keyboard_and_contact_activation_preserve_exact_ids(self):
        request = self.start()
        contact = row('contact', 'CONTACTS')
        self.panel.complete(request, self.page(request, [contact]))
        item = next(self.panel.results.item(i) for i in range(self.panel.results.count())
                    if isinstance(self.panel.results.item(i).data(Qt.ItemDataRole.UserRole), dict))
        widget = self.panel.results.itemWidget(item)
        self.assertEqual(widget.title._full_text, contact['title'])
        self.assertEqual(widget.preview._full_text, contact['preview'])
        self.assertEqual(widget.title.textFormat(), Qt.TextFormat.PlainText)
        self.assertEqual(widget.preview.textFormat(), Qt.TextFormat.PlainText)
        self.panel.results.setCurrentItem(item)
        QTest.keyClick(self.panel.results, Qt.Key.Key_Return)
        self.assertEqual(self.activations, [contact])
        self.assertIsNone(self.activations[0]['chat_id'])
        self.assertEqual(self.activations[0]['user_id'], 91)
        self.activations[0]['title'] = 'caller mutation'
        self.assertNotEqual(self.shown_rows()[0]['title'], 'caller mutation')
        header = self.panel.results.item(0)
        self.panel._activate(header)
        self.assertEqual(len(self.activations), 1)

    def test_malformed_rows_are_unavailable_and_no_id_is_derived(self):
        request = self.start()
        values = [row('valid', 'MESSAGES', chat_id=-12, message_id=2**62),
                  row('missing', 'MESSAGES', message_id=None),
                  row('bool', chat_id=True), row('oversize', chat_id=2**64),
                  row('bad-section', section=[]), row('missing-contact', 'CONTACTS', user_id=None),
                  row('missing-bot', 'BOTS', chat_id=None, user_id=None),
                  row('bad-text', title={'html': 'no'})]
        self.assertTrue(self.panel.complete(request, self.page(request, values)))
        self.assertEqual([value['key'] for value in self.shown_rows()], ['valid'])
        self.assertIn('Часть', self.panel.status.text())
        self.assertEqual(self.shown_rows()[0]['message_id'], 2**62)

    def test_finite_timestamp_is_preserved_without_rescale_for_message_activation(self):
        request = self.start()
        values = [row('integer', 'MESSAGES', time_ms=2**62),
                  row('fractional', 'MESSAGES', message_id=702, time_ms=1234.5),
                  row('bool-time', 'MESSAGES', time_ms=True),
                  row('nan-time', 'MESSAGES', time_ms=float('nan')),
                  row('infinite-time', 'MESSAGES', time_ms=float('inf'))]
        self.panel.complete(request, self.page(request, values))
        self.assertEqual([value['time_ms'] for value in self.shown_rows()], [2**62, 1234.5])
        item = next(self.panel.results.item(i) for i in range(self.panel.results.count())
                    if isinstance(self.panel.results.item(i).data(Qt.ItemDataRole.UserRole), dict))
        self.panel._activate(item)
        self.assertEqual(self.activations[0]['time_ms'], 2**62)
        self.assertIs(type(self.activations[0]['time_ms']), int)

    def test_contact_only_bot_activation_keeps_user_id_without_deriving_chat(self):
        request = self.start()
        bot = row('bot-contact', 'BOTS', chat_id=None, user_id=998)
        self.panel.complete(request, self.page(request, [bot]))
        self.assertEqual(self.shown_rows(), [bot])
        item = next(self.panel.results.item(i) for i in range(self.panel.results.count())
                    if isinstance(self.panel.results.item(i).data(Qt.ItemDataRole.UserRole), dict))
        self.panel._activate(item)
        self.assertEqual(self.activations, [bot])
        self.assertIsNone(self.activations[0]['chat_id'])
        self.assertEqual(self.activations[0]['user_id'], 998)

    def test_pagination_uses_actual_string_marker_once_deduplicates_and_rejects_cycles(self):
        first = self.start()
        self.panel.complete(first, self.page(first, [row('a')], marker='cursor-A'))
        self.panel.load_more()
        second = self.requests[-1]
        self.assertEqual(second['marker'], 'cursor-A')
        self.panel.load_more()
        self.assertEqual(len(self.requests), 2)
        self.panel.complete(second, self.page(second, [row('a'), row('b')], marker='cursor-B'))
        self.panel.load_more()
        third = self.requests[-1]
        self.assertEqual(third['marker'], 'cursor-B')
        self.panel.complete(third, self.page(third, [row('c')], marker='cursor-A'))
        self.assertEqual([value['key'] for value in self.shown_rows()], ['a', 'b', 'c'])
        self.assertFalse(self.panel.more_button.isVisible())
        self.panel.load_more()
        self.assertEqual(len(self.requests), 3)
        self.assertFalse(self.panel.complete(second, self.page(second, [row('late')])) )

    def test_empty_string_marker_is_not_confused_with_missing_initial_cursor(self):
        first = self.start()
        self.assertTrue(self.panel.complete(first, self.page(first, [row('a')], marker='')))
        self.assertTrue(self.panel.more_button.isVisible())
        self.panel.load_more()
        second = self.requests[-1]
        self.assertEqual(second['marker'], '')
        self.assertIsNotNone(second['marker'])
        self.panel.complete(second, self.page(second, [row('b')], marker=''))
        self.assertEqual([value['key'] for value in self.shown_rows()], ['a', 'b'])
        self.assertFalse(self.panel.more_button.isVisible())

    def test_page_error_keeps_prior_rows_manual_retry_and_does_not_echo_exception(self):
        first = self.start()
        self.panel.complete(first, self.page(first, [row('a')], marker='cursor-A'))
        self.panel.load_more()
        second = self.requests[-1]
        self.assertTrue(self.panel.fail(second, '<b>private body/token must not be displayed</b>'))
        self.assertEqual([value['key'] for value in self.shown_rows()], ['a'])
        self.assertTrue(self.panel.retry_button.isEnabled())
        self.assertNotIn('private', self.panel.status.text())
        QTest.qWait(35)
        self.assertEqual(len(self.requests), 2)
        self.panel.retry_button.click()
        retry = self.requests[-1]
        self.assertEqual(retry['marker'], 'cursor-A')
        self.assertGreater(retry['generation'], second['generation'])
        self.assertFalse(self.panel.complete(second, self.page(second, [row('old')])) )
        self.panel.complete(retry, self.page(retry, [row('b')]))
        self.assertEqual([value['key'] for value in self.shown_rows()], ['a', 'b'])

    def test_malformed_page_has_explicit_manual_retry_no_fabricated_empty_success(self):
        for invalid in ({'items': None}, {'marker': 5}, {'total': True}, {'counters': []}):
            request = self.start(f'запрос {len(self.requests)}')
            page = self.page(request, [])
            page.update(invalid)
            self.assertFalse(self.panel.complete(request, page))
            self.assertEqual(self.shown_rows(), [])
            self.assertTrue(self.panel.retry_button.isEnabled())
            self.assertIn('Не удалось', self.panel.status.text())

    def test_confirmed_empty_page_is_distinct_from_error_loading_and_idle(self):
        request = self.start()
        self.assertTrue(self.panel.complete(request, self.page(request, [])))
        self.assertEqual(self.panel.status.text(), 'Ничего не найдено')
        self.assertEqual(self.shown_rows(), [])
        self.assertFalse(self.panel.retry_button.isVisible())
        self.assertFalse(self.panel.more_button.isVisible())
        self.app.processEvents()
        output = Path('.test-tmp')
        output.mkdir(exist_ok=True)
        self.assertTrue(self.panel.grab().save(str(output / 'search_no_results.png')))

    def test_clear_cancels_pending_timer_and_inflight_response_without_rpc(self):
        cleared = []
        self.panel.clear_requested.connect(lambda: cleared.append(True))
        request = self.start()
        self.panel.clear()
        self.assertEqual(cleared, [True])
        self.assertFalse(self.panel.complete(request, self.page(request, [row('late')])) )
        self.panel.set_query('следующий')
        self.panel.clear()
        QTest.qWait(35)
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.shown_rows(), [])

    def test_grouped_light_dark_minimum_width_empty_loading_error_captures(self):
        output = Path('.test-tmp')
        output.mkdir(exist_ok=True)
        request = self.start()
        self.assertTrue(self.panel.grab().save(str(output / 'search_loading.png')))
        self.panel.fail(request)
        self.assertTrue(self.panel.grab().save(str(output / 'search_error.png')))
        self.panel.retry()
        request = self.requests[-1]
        values = [row('owned', title='<b>' + 'Длинное название ' * 10),
                  row('message', 'MESSAGES'), row('contact', 'CONTACTS')]
        values += [row(f'public-{i}', 'PUBLIC_CHATS', chat_id=i+100) for i in range(7)]
        self.panel.complete(request, self.page(request, values, marker='next'))
        for theme in ('light', 'dark'):
            self.host.setStyleSheet(build_stylesheet(theme, '#0085FF'))
            self.panel.apply_theme(theme)
            for width, height in ((360, 440), (624, 600)):
                self.host.resize(width, height)
                self.app.processEvents()
                QTest.qWait(25)
                self.assertEqual(self.panel.results.horizontalScrollBar().maximum(), 0)
                self.assertLessEqual(self.panel.actions.width(), self.panel.width())
                viewport_width = self.panel.results.viewport().width()
                for index in range(self.panel.results.count()):
                    item = self.panel.results.item(index)
                    widget = self.panel.results.itemWidget(item)
                    if widget is not None:
                        self.assertLessEqual(widget.width(), viewport_width)
                        if hasattr(widget, 'title'):
                            self.assertLessEqual(widget.title.fontMetrics().horizontalAdvance(widget.title.text()),
                                                 widget.title.width() + 1)
                self.assertTrue(self.panel.grab().save(str(output / f'search_{width}_{theme}.png')))
                if width == 360:
                    self.panel.results.verticalScrollBar().setValue(self.panel.results.verticalScrollBar().maximum())
                    self.app.processEvents()
                    self.assertTrue(self.panel.grab().save(str(output / f'search_{width}_{theme}_bottom.png')))
                    self.panel.results.verticalScrollBar().setValue(0)
        self.panel.clear()
        self.app.processEvents()
        self.assertTrue(self.panel.grab().save(str(output / 'search_empty.png')))
        for value in self.panel.findChildren(QLabel):
            self.assertEqual(value.textFormat(), Qt.TextFormat.PlainText)


if __name__ == '__main__':
    unittest.main()
