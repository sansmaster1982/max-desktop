"""One-level forward presentation: synthetic widgets, no account or media IO."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtWidgets import QApplication, QLabel, QPushButton, QVBoxLayout, QWidget
from shiboken6 import isValid

from maxclient.core.config import Settings
from maxclient.core.models import Chat, Message
from maxclient.ui.chat_view import ChatView
from maxclient.ui.theme import palette_for
from maxclient.ui.widgets.message_bubble import MessageBubble, _ClickableFrame
from maxclient.ui.widgets.common import fmt_time_hm
from test_desktop_shell import _load_offscreen_font


def raw_file(name='inner <b>literal</b>.pdf', identity=0):
    return {'_type': 'FILE', 'fileId': identity, 'name': name, 'size': 1024}


def forwarded(*attaches, text='<b>Nested & literal</b>', name=None, inner_extra=None, **outer):
    inner = {'id': 808, 'time': 150.5, 'type': 'USER', 'sender': 17,
             'text': text, 'attaches': list(attaches)}
    inner.update(inner_extra or {})
    link = {'type': 'FORWARD', 'chatId': 909, 'message': inner}
    if name is not None:
        link['chatName'] = name
    raw = {'id': 7, 'time': 1793459500007.5, 'type': 'USER', 'sender': 6,
           'text': 'OUTER TEXT MUST NOT REPLACE FORWARD', 'attaches': [raw_file('outer.txt', 92)],
           'link': link}
    raw.update(outer)
    return Message.from_server(303, raw, 6)


def caps(name='inner <b>literal</b>.pdf', **changes):
    return {'download_available': True, 'file_name': name, 'deleted': False, **changes}


class ForwardedMessageUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        _load_offscreen_font(cls.app)

    def setUp(self):
        self.widgets, self.files = [], []
        self.media, self.voice = Mock(), Mock()

    def tearDown(self):
        for widget in self.widgets:
            if isValid(widget):
                widget.close()
                widget.deleteLater()
                QCoreApplication.sendPostedEvents(widget, QEvent.Type.DeferredDelete)
        self.app.processEvents()

    def bubble(self, msg, capabilities=None, theme='light', **extra):
        widget = MessageBubble(msg, '#0085FF', palette_for(theme), file_actions=capabilities,
                               on_open_media=self.media, on_play_voice=self.voice, **extra)
        widget.file_action_requested.connect(lambda *args: self.files.append(args))
        self.widgets.append(widget)
        widget.resize(520, 200)
        widget.show()
        self.app.processEvents()
        return widget

    def test_nested_text_replaces_outer_and_remains_literal(self):
        msg = forwarded()
        widget = self.bubble(msg)
        label = widget.findChild(QLabel, 'MessageText')
        self.assertEqual(label.text(), '<b>Nested & literal</b>')
        self.assertEqual(label.textFormat(), Qt.TextFormat.PlainText)
        self.assertFalse(widget.findChildren(QPushButton, 'MessageFilePrimary'))
        self.assertIs(widget.msg, msg)
        self.assertEqual((msg.chat_id, msg.id, msg.time_ms, msg.outgoing), (303, 7, 1793459500007.5, True))
        self.assertEqual(widget._footer_text(), fmt_time_hm(msg.time_ms) + '  ✓')
        self.assertNotEqual(fmt_time_hm(msg.time_ms), fmt_time_hm(msg.forwarded.source_time_ms))

    def test_supplied_source_name_is_literal_not_an_invented_sender(self):
        widget = self.bubble(forwarded(name='<b>Source & channel</b>'))
        header = widget.findChild(QLabel, 'MessageForwardHeader')
        self.assertEqual(header._full_text, 'Переслано: <b>Source & channel</b>')
        self.assertEqual(header.textFormat(), Qt.TextFormat.PlainText)
        self.assertFalse(header.textInteractionFlags() & Qt.TextInteractionFlag.LinksAccessibleByMouse)

    def test_missing_source_name_has_neutral_noninteractive_header(self):
        widget = self.bubble(forwarded())
        self.assertEqual(widget.findChild(QLabel, 'MessageForwardHeader')._full_text,
                         'Пересланное сообщение')

    def test_nested_file_uses_selected_index_but_emits_exact_outer_message(self):
        msg = forwarded({'_type': 'PHOTO'}, raw_file())
        widget = self.bubble(msg, {1: caps()})
        button = widget.findChild(QPushButton, 'MessageFilePrimary')
        self.assertEqual(button.property('attachmentIndex'), 1)
        button.click()
        self.assertEqual(self.files, [('save', msg, 1)])
        self.assertEqual(widget.findChild(QLabel, 'MessageFileName')._full_text, 'inner <b>literal</b>.pdf')
        self.assertNotEqual(msg.display_attaches[1].file_name, msg.attaches[0].file_name)

    def test_default_nested_file_save_is_disabled_without_backend_capability(self):
        widget = self.bubble(forwarded(raw_file()))
        self.assertFalse(widget.findChild(QPushButton, 'MessageFilePrimary').isEnabled())
        self.assertEqual(self.files, [])

    def test_deleted_nested_file_does_not_save_but_literal_name_can_be_copied(self):
        msg = forwarded({**raw_file(), 'deleted': True})
        widget = self.bubble(msg, {0: caps(deleted=True)})
        menu = widget.open_file_menu(0)
        self.assertFalse(menu.actions()[0].isEnabled())
        menu.actions()[1].trigger()
        self.assertEqual(self.files, [('copy_name', msg, 0)])

    def test_changed_forward_identity_revokes_existing_file_popup(self):
        msg = forwarded(raw_file())
        widget = self.bubble(msg, {0: caps()})
        menu = widget.open_file_menu(0)
        msg.forwarded = forwarded(raw_file(), inner_extra={'id': 809}).forwarded
        menu.actions()[0].trigger()
        self.assertEqual(self.files, [])

    def test_changed_nested_file_proof_revokes_existing_file_popup(self):
        msg = forwarded(raw_file())
        widget = self.bubble(msg, {0: caps()})
        menu = widget.open_file_menu(0)
        msg.display_attaches[0].file_metadata_confirmed = False
        menu.actions()[0].trigger()
        self.assertEqual(self.files, [])

    def test_nested_nonfile_cards_never_use_legacy_media_or_filesystem(self):
        msg = forwarded({'_type': 'PHOTO', 'url': 'https://invalid.example/image'},
                        {'_type': 'VIDEO', 'videoId': 3}, {'_type': 'AUDIO', 'audioId': 2, 'duration': 3000})
        with patch('os.path.isfile', side_effect=AssertionError('No forwarded path lookup')):
            widget = self.bubble(msg)
            for attach in msg.display_attaches:
                widget._open_media(attach)
                widget._play_voice(attach)
        self.assertEqual(len(widget.findChildren(QWidget, 'MessageForwardAttachment')), 3)
        self.assertFalse(widget.findChildren(_ClickableFrame))
        self.media.assert_not_called()
        self.voice.assert_not_called()

    def test_unsupported_deep_forward_has_clear_state_and_no_outer_file(self):
        msg = forwarded(inner_extra={'link': {'type': 'FORWARD', 'chatId': 111,
                                             'message': {'id': 99, 'time': 20, 'type': 'USER'}}})
        widget = self.bubble(msg)
        self.assertIsNotNone(widget.findChild(QLabel, 'MessageForwardUnavailable'))
        self.assertFalse(widget.findChildren(QPushButton, 'MessageFilePrimary'))

    def test_plain_outer_message_keeps_existing_rendering(self):
        widget = self.bubble(Message(chat_id=303, id=9, text='Ordinary <b>literal</b>'))
        self.assertIsNone(widget.findChild(QLabel, 'MessageForwardHeader'))
        self.assertEqual(widget.findChild(QLabel, 'MessageText').text(), 'Ordinary <b>literal</b>')

    def test_copy_message_uses_visible_forward_text_without_real_clipboard(self):
        clipboard = Mock()
        with patch('PySide6.QtWidgets.QApplication.clipboard', return_value=clipboard):
            ChatView._copy_message(forwarded())
        clipboard.setText.assert_called_once_with('<b>Nested & literal</b>')

    def test_chatview_selected_file_dispatch_rejects_outer_file_and_stale_bubble(self):
        msg = forwarded({'_type': 'PHOTO'}, raw_file())
        service = Mock()
        service.local_history.return_value = [msg]
        context = SimpleNamespace(chat_id=303)
        service.timeline_file_context.return_value = context
        service.is_current_timeline_file_context.side_effect = lambda owner: owner is context
        service.timeline_file_actions.return_value = {1: caps()}
        descriptor = SimpleNamespace(context=context, message_id=7)
        service.get_timeline_file_descriptor.return_value = descriptor
        with patch('maxclient.ui.chat_view.run_async') as workers:
            view = ChatView(service, Settings())
            self.widgets.append(view)
            view.open_chat(Chat(id=303))
            view.history_search.setText('Nested & literal')
            self.assertEqual(view._search_matches, [0])
            saved = []
            view.file_save_requested.connect(saved.append)
            view._rows[0]['bubble'].findChild(QPushButton, 'MessageFilePrimary').click()
            service.get_timeline_file_descriptor.assert_called_once_with(context, msg, 1)
            self.assertEqual(saved, [descriptor])
            service.get_timeline_file_descriptor.reset_mock()
            view._open_media(msg, msg.attaches[0])
            view._open_media(msg, msg.display_attaches[0])
            service.get_timeline_file_descriptor.assert_not_called()
            service.resolve_attach_url.assert_not_called()
            old = view._rows[0]['bubble']
            view._replace_row(0, msg)
            old.file_action_requested.emit('save', msg, 1)
            self.assertEqual(saved, [descriptor])
            self.assertEqual(workers.call_count, 1)  # Only captured initial history, never an action RPC.

    def test_synthetic_forward_captures_light_dark_narrow_wide(self):
        output = Path('.test-tmp/forwarded_message_ui')
        output.mkdir(parents=True, exist_ok=True)
        for theme in ('light', 'dark'):
            host = QWidget()
            self.widgets.append(host)
            host.setStyleSheet(f'background:{palette_for(theme).window};')
            layout = QVBoxLayout(host)
            layout.setContentsMargins(0, 12, 0, 12)
            for outgoing in (False, True):
                msg = forwarded(raw_file(), name='<b>Синтетический длинный источник & проекта</b>',
                                text='Синтетический текст пересылки. <b>Теги видны буквально.</b> ' * 2,
                                sender=6 if outgoing else 17)
                layout.addWidget(MessageBubble(msg, '#0085FF', palette_for(theme), file_actions={0: caps()}))
            layout.addStretch(1)
            host.show()
            for width in (520, 320):
                host.resize(width, 480)
                self.app.processEvents()
                self.assertLessEqual(host.width(), width)
                for header in host.findChildren(QLabel, 'MessageForwardHeader'):
                    self.assertGreater(header.width(), 100)
                    self.assertTrue(header.text())
                    self.assertLessEqual(header.fontMetrics().horizontalAdvance(header.text()), header.width() + 1)
                self.assertTrue(host.grab().save(str(output / f'forwarded_messages_{width}_{theme}.png')))


if __name__ == '__main__':
    unittest.main()
