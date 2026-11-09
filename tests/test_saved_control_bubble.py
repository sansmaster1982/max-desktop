"""CONTROL display only: synthetic typed DTOs, never accounts or external IO."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtWidgets import QApplication, QLabel, QPushButton, QVBoxLayout, QWidget
from shiboken6 import isValid

from maxclient.core.models import Attach, Message
from maxclient.ui.theme import palette_for
from maxclient.ui.widgets.message_bubble import MessageBubble, _ClickableFrame
from test_desktop_shell import _load_offscreen_font


KEY = 'welcome.saved.dialog.message'
LOCAL = 'Здесь можно сохранять сообщения и файлы.'


def parsed(text=KEY, control=None, **extra):
    payload = {'id': 77, 'time': 123000, 'type': 'USER', 'text': text,
               'attaches': [{'_type': 'CONTROL', 'event': 'system', 'message': KEY}
                            if control is None else control]}
    payload.update(extra)
    return Message.from_server(0, payload, 7)


class SavedControlBubbleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        _load_offscreen_font(cls.app)

    def setUp(self):
        self.widgets = []
        self.media, self.voice, self.files = Mock(), Mock(), []

    def tearDown(self):
        for widget in self.widgets:
            if isValid(widget):
                widget.close()
                widget.deleteLater()
                QCoreApplication.sendPostedEvents(widget, QEvent.Type.DeferredDelete)
        self.app.processEvents()

    def bubble(self, msg, theme='light', font_scale='large', **extra):
        widget = MessageBubble(msg, '#0085FF', palette_for(theme),
                               on_open_media=self.media, on_play_voice=self.voice,
                               font_scale=font_scale, **extra)
        widget.file_action_requested.connect(lambda *args: self.files.append(args))
        self.widgets.append(widget)
        widget.resize(520, 160)
        widget.show()
        self.app.processEvents()
        return widget

    def test_proven_control_system_known_attachment_message_has_friendly_static_display(self):
        msg = parsed()
        widget = self.bubble(msg)
        service = widget.findChild(QLabel, 'ServiceMessage')
        self.assertEqual(service.text(), LOCAL)
        self.assertEqual(service.textFormat(), Qt.TextFormat.PlainText)
        self.assertIsNone(widget.findChild(QLabel, 'MessageText'))
        self.assertEqual(widget.findChildren(_ClickableFrame), [])
        self.assertEqual(widget.findChildren(QPushButton), [])
        self.assertFalse(any(label.text() == 'Вложение' for label in widget.findChildren(QLabel)))
        self.assertEqual(msg.text, KEY)
        self.assertEqual(msg.preview, LOCAL)

    def test_empty_outer_text_uses_same_strict_attachment_content(self):
        widget = self.bubble(parsed(text=''))
        self.assertEqual(widget.findChild(QLabel, 'ServiceMessage').text(), LOCAL)

    def test_literal_saved_caption_hides_outer_key_and_escapes_markup(self):
        actual = '<b>Save something</b>'
        msg = parsed(control={'_type': 'CONTROL', 'event': 'system', 'message': actual,
                              'shortMessage': 'Different short text'})
        widget = self.bubble(msg)
        label = widget.findChild(QLabel, 'ServiceMessage')
        self.assertEqual(label.text(), actual)
        self.assertEqual(label.textFormat(), Qt.TextFormat.PlainText)
        self.assertIsNone(widget.findChild(QLabel, 'MessageText'))
        self.assertEqual(msg.preview, actual)

    def test_ordinary_user_text_equal_to_key_remains_literal(self):
        msg = Message(chat_id=0, id=78, time_ms=123001, text=KEY)
        widget = self.bubble(msg)
        self.assertEqual(widget.findChild(QLabel, 'MessageText').text(), KEY)
        self.assertIsNone(widget.findChild(QLabel, 'ServiceMessage'))
        self.assertEqual(msg.preview, KEY)

    def test_legacy_control_type_without_typed_provenance_does_not_localize_text(self):
        msg = Message(chat_id=0, id=79, time_ms=123002, text=KEY, attaches=[Attach(type='CONTROL')])
        widget = self.bubble(msg)
        self.assertEqual(widget.findChild(QLabel, 'ServiceMessage').text(), 'Системное сообщение')
        self.assertEqual(widget.findChild(QLabel, 'MessageText').text(), KEY)
        self.assertEqual(widget.findChildren(_ClickableFrame), [])
        self.assertNotEqual(msg.preview, LOCAL)

    def test_type_alias_alone_cannot_promote_service_provenance(self):
        msg = parsed(control={'type': 'CONTROL', 'event': 'system', 'message': KEY})
        widget = self.bubble(msg)
        self.assertEqual(widget.findChild(QLabel, 'MessageText').text(), KEY)
        self.assertNotEqual(widget.findChild(QLabel, 'ServiceMessage').text(), LOCAL)

    def test_hello_and_unknown_events_are_generic_not_a_fake_attachment_or_welcome(self):
        for event in ('hello', 'new', 'unknown'):
            msg = parsed(control={'_type': 'CONTROL', 'event': event, 'message': KEY})
            widget = self.bubble(msg)
            self.assertEqual(widget.findChild(QLabel, 'MessageText').text(), KEY)
            self.assertEqual(widget.findChild(QLabel, 'ServiceMessage').text(), 'Системное сообщение')
            self.assertEqual(widget.findChildren(_ClickableFrame), [])

    def test_short_message_only_and_malformed_system_content_never_localize(self):
        for control in ({'_type': 'CONTROL', 'event': 'system', 'shortMessage': KEY},
                        {'_type': 'CONTROL', 'event': 'system', 'message': 123},
                        {'_type': 'CONTROL', 'event': 'system', 'message': KEY, 'shortMessage': 123}):
            widget = self.bubble(parsed(control=control))
            self.assertEqual(widget.findChild(QLabel, 'MessageText').text(), KEY)
            self.assertNotEqual(widget.findChild(QLabel, 'ServiceMessage').text(), LOCAL)

    def test_unrelated_outer_user_text_is_preserved_even_with_known_service_code(self):
        outer = '<b>Синтетический пользовательский текст</b>'
        msg = parsed(text=outer)
        widget = self.bubble(msg)
        self.assertEqual(widget.findChild(QLabel, 'MessageText').text(), outer)
        self.assertEqual(widget.findChild(QLabel, 'MessageText').textFormat(), Qt.TextFormat.PlainText)
        self.assertNotEqual(widget.findChild(QLabel, 'ServiceMessage').text(), LOCAL)
        self.assertEqual(msg.preview, outer)

    def test_unknown_proven_system_text_is_plain_and_shown_once(self):
        actual = '<b>Синтетическое системное событие</b>'
        msg = parsed(text=actual, control={'_type': 'CONTROL', 'event': 'system', 'message': actual})
        widget = self.bubble(msg)
        service = widget.findChild(QLabel, 'ServiceMessage')
        self.assertEqual(service.text(), actual)
        self.assertEqual(service.textFormat(), Qt.TextFormat.PlainText)
        self.assertIsNone(widget.findChild(QLabel, 'MessageText'))
        self.assertEqual(widget.findChildren(_ClickableFrame), [])

    def test_first_control_governs_service_display_not_a_later_matching_attachment(self):
        msg = parsed(attaches=[{'_type': 'CONTROL', 'event': 'hello'},
                               {'_type': 'CONTROL', 'event': 'system', 'message': KEY}])
        widget = self.bubble(msg)
        self.assertEqual(widget.findChild(QLabel, 'MessageText').text(), KEY)
        self.assertNotEqual(widget.findChild(QLabel, 'ServiceMessage').text(), LOCAL)

    def test_control_never_reads_or_opens_media_and_regular_attachment_indices_do_not_shift(self):
        msg = parsed(attaches=[{'_type': 'CONTROL', 'event': 'system', 'message': KEY},
                               {'_type': 'FILE', 'fileId': 15, 'name': 'Синтетический.txt'}])
        with patch('os.path.isfile', side_effect=AssertionError('No media path reads')):
            widget = self.bubble(msg, file_actions={1: {'download_available': True, 'file_name': 'Синтетический.txt'}})
            widget._open_media(msg.attaches[0])
            buttons = widget.findChildren(QPushButton, 'MessageFilePrimary')
            self.assertEqual(len(buttons), 1)
            self.assertEqual(buttons[0].property('attachmentIndex'), 1)
            buttons[0].click()
        self.assertEqual(self.files, [('save', msg, 1)])
        self.media.assert_not_called()
        self.voice.assert_not_called()

    def test_friendly_service_wraps_at_large_font_and_narrow_width(self):
        widget = self.bubble(parsed(), font_scale='ax2')
        widget.resize(320, 360)
        self.app.processEvents()
        service = widget.findChild(QLabel, 'ServiceMessage')
        self.assertEqual(service.text(), LOCAL)
        self.assertGreater(service.height(), service.fontMetrics().height())
        self.assertGreaterEqual(service.height(), service.heightForWidth(service.width()))
        self.assertEqual(service.textFormat(), Qt.TextFormat.PlainText)

    def test_synthetic_service_user_and_generic_control_captures(self):
        output = Path('.test-tmp')
        output.mkdir(exist_ok=True)
        for theme in ('light', 'dark'):
            host = QWidget()
            self.widgets.append(host)
            host.setStyleSheet(f'background:{palette_for(theme).window};')
            layout = QVBoxLayout(host)
            layout.setContentsMargins(0, 18, 0, 18)
            for msg in (parsed(), Message(chat_id=0, id=78, time_ms=123001, text=KEY),
                        parsed(text='', control={'_type': 'CONTROL', 'event': 'hello'})):
                layout.addWidget(MessageBubble(msg, '#0085FF', palette_for(theme)))
            layout.addStretch(1)
            host.show()
            for width in (520, 320):
                host.resize(width, 360)
                self.app.processEvents()
                self.assertEqual(host.width(), width)
                service = host.findChild(QLabel, 'ServiceMessage')
                self.assertGreater(service.width(), 200)
                self.assertGreaterEqual(service.height(), service.heightForWidth(service.width()))
                self.assertTrue(host.grab().save(str(output / f'saved_control_bubble_{width}_{theme}.png')))


if __name__ == '__main__':
    unittest.main()
