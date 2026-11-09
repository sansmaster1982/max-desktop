"""Synthetic message links/inline actions; all external effects are mocked."""
import copy
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import threading
from types import SimpleNamespace
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from PySide6.QtCore import Qt
from PySide6.QtGui import QTextDocument
from PySide6.QtWidgets import QApplication, QLabel, QPushButton

from maxclient.core.message_presentation import incoming_button_rows, literal_link_html, safe_web_url
from maxclient.core.models import Attach, Chat, Message
from maxclient.core.service import MaxService
from maxclient.core.store import Store
from maxclient.ui.message_links import open_web_link
from maxclient.ui.theme import palette_for, build_stylesheet
from maxclient.ui.widgets.message_bubble import MessageBubble
from test_desktop_shell import _load_offscreen_font


def keyboard():
    return {'_type': 'INLINE_KEYBOARD', 'callbackId': 'synthetic-not-persisted', 'keyboard': {'buttons': [[
        {'type': 'CLIPBOARD', 'text': 'Copy & text <b>', 'payload': '000123'},
        {'type': 'LINK', 'text': 'Example link', 'url': 'https://example.test/path?q=1&b=2'}], [
        {'type': 'CALLBACK', 'text': 'Trust device', 'payload': 'synthetic-unsupported'}]]}}


def raw_message():
    return {'id': 10, 'type': 'USER', 'sender': 77, 'time': 1000, 'updateTime': 1001,
            'text': 'Literal <b> & text\nhttps://example.test/path?q=1&b=2',
            'attaches': [{'_type': 'SHARE', 'shareId': 50, 'title': 'Title <b>',
                         'description': 'Literal description & text', 'url': 'https://example.test/story'}, keyboard()]}


class MessagePresentationTests(unittest.TestCase):
    def test_incoming_shape_preserves_literal_copy_but_discards_action_payloads(self):
        rows = incoming_button_rows(keyboard())
        self.assertEqual(rows[0][0], {'type': 'CLIPBOARD', 'text': 'Copy & text <b>', 'payload': '000123'})
        self.assertEqual(rows[1][0], {'type': 'CALLBACK', 'text': 'Trust device'})
        attach = Attach.from_server(keyboard())
        self.assertTrue(attach.inline_metadata_confirmed)
        self.assertNotIn('synthetic-not-persisted', repr(attach.__dict__))
        self.assertNotIn('synthetic-unsupported', repr(attach.__dict__))

    def test_outgoing_alias_malformed_and_oversized_keyboard_are_not_guessed(self):
        candidates = [dict(type='INLINE_KEYBOARD', keyboard=keyboard()['keyboard'], callbackId='x'),
                      dict(_type='INLINE_KEYBOARD', buttons=keyboard()['keyboard']['buttons']),
                      dict(_type='INLINE_KEYBOARD', keyboard=keyboard()['keyboard']),
                      dict(_type='INLINE_KEYBOARD', callbackId='x', keyboard={'buttons': 'bad'})]
        for candidate in candidates:
            self.assertIsNone(incoming_button_rows(candidate))
        for value in (None, 123, [], 'x' * 16385):
            candidate = keyboard()
            candidate['keyboard']['buttons'][0][0]['payload'] = value
            self.assertIsNone(incoming_button_rows(candidate))

    def test_safe_urls_do_not_allow_local_or_app_commands_credentials_or_controls(self):
        for value in ('javascript:alert(1)', 'file:///C:/secret', 'max://auth/x', '//example.test',
                      'https://name:password@example.test/x', 'https://example.test/\nfoo',
                      'https://example.test\\@other.test', 'https://example.test:99999', None):
            self.assertIsNone(safe_web_url(value))
        self.assertEqual(safe_web_url('https://example.test/a?q=1&b=2'), 'https://example.test/a?q=1&b=2')

    def test_linkification_escapes_html_and_preserves_literal_whitespace(self):
        source = '<img src=x>  text\n\t https://example.test/a?q=1&b=2.\n  tail <b>'
        rich = literal_link_html(source)
        self.assertNotIn('<img', rich)
        self.assertIn('&lt;img', rich)
        doc = QTextDocument()
        doc.setHtml(rich)
        self.assertEqual(doc.toPlainText(), source)
        self.assertIsNone(literal_link_html('<b>000123</b>'))


class DisplayMetadataRefreshTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(':memory:')
        self.addCleanup(self.store.close)
        self.store.bind_owner(101)
        self.store.upsert_chat(Chat(id=42))
        self.service = MaxService.__new__(MaxService)
        self.service.store = self.store
        self.service.session = SimpleNamespace(my_user_id=101)
        self.service._metadata_lock = threading.RLock()
        self.service._server_metadata_owner = 101
        self.service._server_chats = {}
        self.service._server_contacts = {}
        self.service._contacts_cache = None

    def legacy(self):
        parsed = Message.from_server(42, raw_message(), 101)
        parsed.attaches = [Attach(type='SHARE', url='https://example.test/story'), Attach(type='INLINE_KEYBOARD')]
        self.store.insert_message(parsed)

    def test_history_same_version_enriches_in_place_and_roundtrips(self):
        self.legacy()
        self.service._cache_history_messages(42, [raw_message()])
        result = self.store.message(42, 10)
        self.assertEqual(len(self.store.messages(42)), 1)
        self.assertTrue(result.attaches[0].share_metadata_confirmed)
        self.assertEqual(result.attaches[0].share_title, 'Title <b>')
        self.assertTrue(result.attaches[1].inline_metadata_confirmed)
        self.assertEqual(result.attaches[1].inline_buttons[0][0]['payload'], '000123')
        self.assertEqual((result.text, result.sender, result.time_ms, result.update_time_ms),
                         (raw_message()['text'], 77, 1000, 1001))

    def test_changed_identity_version_text_or_order_cannot_enrich_legacy(self):
        self.legacy()
        for change in ({'id': 11}, {'time': 1002}, {'sender': 88}, {'updateTime': 1000},
                       {'text': 'Different'}, {'status': 'REMOVED'}, {'attaches': list(reversed(raw_message()['attaches']))}):
            raw = raw_message(); raw.update(change)
            self.store.refresh_message_display_metadata(42, raw)
            self.assertFalse(self.store.message(42, 10).attaches[1].inline_metadata_confirmed)

    def test_proven_button_is_not_overwritten_by_conflicting_same_version_history(self):
        self.store.insert_message(Message.from_server(42, raw_message(), 101))
        raw = raw_message(); raw['attaches'][1]['keyboard']['buttons'][0][0]['payload'] = '999999'
        self.store.refresh_message_display_metadata(42, raw)
        self.assertEqual(self.store.message(42, 10).attaches[1].inline_buttons[0][0]['payload'], '000123')

    def test_neighbour_photo_resource_is_not_replaced(self):
        self.legacy()
        row = self.store.message(42, 10)
        row.attaches.append(Attach(type='PHOTO', url='https://example.test/current'))
        self.store._db.execute('UPDATE messages SET attaches=? WHERE server_id=10',
                               (__import__('json').dumps([a.__dict__ for a in row.attaches]),))
        raw = raw_message(); raw['attaches'].append({'_type': 'PHOTO', 'url': 'https://example.test/old'})
        self.store.refresh_message_display_metadata(42, raw)
        self.assertEqual(self.store.message(42, 10).attaches[-1].url, 'https://example.test/current')


class MessageLinksButtonsUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        _load_offscreen_font(cls.app)

    def setUp(self):
        self.widgets = []

    def tearDown(self):
        for widget in self.widgets:
            widget.close(); widget.deleteLater()
        self.app.processEvents()

    def bubble(self, raw=None):
        widget = MessageBubble(Message.from_server(42, raw or raw_message(), 101), '#0085FF', palette_for('light'))
        self.widgets.append(widget)
        widget.resize(520, 500)
        widget.show()
        self.app.processEvents()
        return widget

    def test_keyboard_labels_and_gesture_copy_open_without_callbacks(self):
        with patch('maxclient.ui.message_links.QDesktopServices.openUrl', return_value=True) as opener:
            widget = self.bubble()
            buttons = widget.findChildren(QPushButton, 'MessageInlineButton')
            self.assertEqual([b.isEnabled() for b in buttons], [True, True, False])
            self.assertEqual(buttons[0].accessibleName(), 'Copy & text <b>')
            opener.assert_not_called()
            with patch('maxclient.ui.widgets.message_bubble.QApplication.clipboard') as clipboard:
                buttons[0].click()
                clipboard.return_value.setText.assert_called_once_with('000123')
            buttons[1].click()
            self.assertEqual(opener.call_args.args[0].toString(), 'https://example.test/path?q=1&b=2')
            buttons[2].click()
            self.assertEqual(opener.call_count, 1)

    def test_share_and_text_urls_are_real_links_but_title_stays_plain(self):
        widget = self.bubble()
        self.assertEqual(widget.findChild(QLabel, 'ShareTitle').text(), 'Title <b>')
        self.assertEqual(widget.findChild(QLabel, 'ShareTitle').textFormat(), Qt.TextFormat.PlainText)
        for name in ('ShareUrl', 'MessageText'):
            label = widget.findChild(QLabel, name)
            self.assertEqual(label.textFormat(), Qt.TextFormat.RichText)
            self.assertFalse(label.openExternalLinks())
            with patch('maxclient.ui.message_links.QDesktopServices.openUrl', return_value=True) as opener:
                label.linkActivated.emit('https://example.test/action')
                opener.assert_called_once()
                label.linkActivated.emit('file:///C:/secret')
                self.assertEqual(opener.call_count, 1)

    def test_stale_or_removed_message_buttons_cannot_act(self):
        for mutation in ('removed', 'replaced', 'changed_buttons', 'hidden'):
            widget = self.bubble()
            button = widget.findChildren(QPushButton, 'MessageInlineButton')[0]
            if mutation == 'removed': widget.msg.status = 'removed'
            elif mutation == 'changed_buttons': widget.msg.attaches[1].inline_buttons[0][0]['payload'] = 'changed'
            elif mutation == 'hidden': widget.hide()
            else: widget.msg.attaches = []
            with patch('maxclient.ui.widgets.message_bubble.QApplication.clipboard') as clipboard:
                button.click()
                clipboard.assert_not_called()

    def test_unsupported_device_trust_button_and_nonweb_link_are_disabled(self):
        raw = raw_message()
        raw['attaches'][1]['keyboard']['buttons'][0][1]['url'] = 'max://device/trust'
        widget = self.bubble(raw)
        self.assertEqual([b.isEnabled() for b in widget.findChildren(QPushButton, 'MessageInlineButton')],
                         [True, False, False])

    def test_synthetic_fixture_geometry_in_both_global_themes(self):
        folder = Path(__file__).resolve().parents[1] / '.test-tmp' / 'display_buttons_20261103'
        folder.mkdir(parents=True, exist_ok=True)
        original_style = self.app.styleSheet()
        try:
            for theme in ('light', 'dark'):
                self.app.setStyleSheet(build_stylesheet(theme, '#0085FF'))
                widget = MessageBubble(Message.from_server(42, raw_message(), 101), '#0085FF', palette_for(theme))
                self.widgets.append(widget)
                widget.resize(540, 420)
                widget.show(); self.app.processEvents()
                for button in widget.findChildren(QPushButton, 'MessageInlineButton'):
                    self.assertGreaterEqual(button.height(), 38)
                    caption = button.findChild(QLabel)
                    self.assertTrue(button.rect().contains(caption.geometry()))
                    self.assertGreaterEqual(caption.height(), caption.heightForWidth(caption.width()))
                self.assertTrue(widget.grab().save(str(folder / f'{theme}.png')))
        finally:
            self.app.setStyleSheet(original_style)


if __name__ == '__main__':
    unittest.main()
