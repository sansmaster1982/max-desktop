"""Timeline FILE presentation only: no clipboard, network, file or OS opening."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLabel, QPushButton, QVBoxLayout, QWidget
from shiboken6 import isValid

from maxclient.core.models import Attach, Message
from maxclient.ui.theme import palette_for
from maxclient.ui.widgets.message_bubble import MessageBubble
from test_desktop_shell import _load_offscreen_font


def attachment(**extra):
    value = dict(type='FILE', file_id=17, file_name='<b>Синтетический файл & приложение.pdf</b>',
                 size=10485760, url='https://invalid.example/never-open', local_path='C:/never-read.exe')
    value.update(extra)
    return Attach(**value)


def message(*attaches, **extra):
    value = dict(chat_id=42, id=91, time_ms=1793459500091.5, attaches=list(attaches))
    value.update(extra)
    return Message(**value)


def capability(name='<b>Синтетический файл & приложение.pdf</b>', **extra):
    value = {'download_available': True, 'file_name': name, 'deleted': False}
    value.update(extra)
    return value


class MessageFileBubbleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        _load_offscreen_font(cls.app)

    def setUp(self):
        self.widgets, self.actions = [], []
        self.media, self.voice = Mock(), Mock()

    def tearDown(self):
        for widget in self.widgets:
            if isValid(widget):
                widget.close()
                widget.deleteLater()
                QCoreApplication.sendPostedEvents(widget, QEvent.Type.DeferredDelete)
        self.app.processEvents()

    def bubble(self, msg, caps=None, theme='light'):
        widget = MessageBubble(msg, '#0085FF', palette_for(theme), file_actions=caps,
                               on_open_media=self.media, on_play_voice=self.voice)
        widget.file_action_requested.connect(lambda action, msg, index: self.actions.append((action, msg, index)))
        self.widgets.append(widget)
        widget.resize(540, 150)
        widget.show()
        self.app.processEvents()
        return widget

    def primary(self, widget, index=0):
        return next(button for button in widget.findChildren(QPushButton, 'MessageFilePrimary')
                    if button.property('attachmentIndex') == index)

    def test_default_unknown_capability_never_uses_legacy_file_id_or_url(self):
        file = attachment()
        with patch('os.path.isfile', side_effect=AssertionError('No FILE path reads')):
            widget = self.bubble(message(file))
            self.assertFalse(self.primary(widget).isEnabled())
            self.primary(widget).click()
            widget._open_media(file)
        self.assertEqual(self.actions, [])
        self.media.assert_not_called()
        self.voice.assert_not_called()
        self.assertIn('недоступно', widget.findChild(QLabel, 'MessageFileMeta')._full_text)

    def test_explicit_save_emits_exact_message_and_normalized_index(self):
        photo = Attach(type='PHOTO')
        first, second = attachment(), attachment(file_name='Другой синтетический файл.txt')
        msg = message(photo, first, second)
        widget = self.bubble(msg, {1: capability(), 2: capability(second.file_name)})
        self.primary(widget, 1).click()
        self.primary(widget, 2).click()
        self.assertEqual([index for _action, _msg, index in self.actions], [1, 2])
        self.assertTrue(all(action == 'save' and item is msg for action, item, _index in self.actions))
        self.assertIs(msg.attaches[1], first)
        self.media.assert_not_called()

    def test_only_explicit_snapshot_is_permission_even_when_legacy_id_was_lost(self):
        widget = self.bubble(message(attachment(file_id=None)), {0: capability()})
        self.assertTrue(self.primary(widget).isEnabled())
        self.primary(widget).click()
        self.assertEqual(len(self.actions), 1)
        # Root's authoritative backend is responsible for validating this grant.

    def test_capability_snapshot_is_scalar_immutable_and_not_a_live_external_dict(self):
        caps = {0: capability()}
        widget = self.bubble(message(attachment()), caps)
        caps[0]['download_available'] = False
        caps[0]['file_name'] = 'Changed outside the widget'
        self.assertTrue(widget._file_allowed('save', 0))
        self.assertEqual(widget._file_actions[0]['file_name'], capability()['file_name'])
        with self.assertRaises(TypeError):
            widget._file_actions[0]['download_available'] = False

    def test_deleted_save_disabled_while_actual_name_copy_remains_explicit(self):
        widget = self.bubble(message(attachment()), {0: capability(deleted=True)})
        self.assertFalse(self.primary(widget).isEnabled())
        self.assertIn('удалён', widget.findChild(QLabel, 'MessageFileMeta')._full_text)
        menu = widget.open_file_menu(0)
        self.assertEqual([action.text() for action in menu.actions()], ['Сохранить файл', 'Копировать имя файла'])
        self.assertFalse(menu.actions()[0].isEnabled())
        self.assertTrue(menu.actions()[1].isEnabled())
        menu.actions()[0].trigger()
        self.assertEqual(self.actions, [])
        with patch.object(QApplication, 'clipboard', side_effect=AssertionError('Copy belongs to root')):
            menu.actions()[1].trigger()
        self.assertEqual(self.actions, [('copy_name', widget.msg, 0)])

    def test_missing_or_empty_authoritative_name_never_copies_a_fallback_label(self):
        for name in (None, '', 123):
            widget = self.bubble(message(attachment(file_name='Legacy label')), {0: capability(name)})
            menu = widget.open_file_menu(0)
            self.assertFalse(menu.actions()[1].isEnabled())
            menu.actions()[1].trigger()
        self.assertEqual(self.actions, [])

    def test_labels_and_tooltip_keep_html_filename_literal_and_zero_size_visible(self):
        widget = self.bubble(message(attachment(size=0)), {0: capability()})
        title = widget.findChild(QLabel, 'MessageFileName')
        self.assertEqual(title._full_text, capability()['file_name'])
        self.assertEqual(title.textFormat(), Qt.TextFormat.PlainText)
        self.assertIn('&lt;b&gt;', title.toolTip())
        self.assertNotIn('<b>', title.toolTip())
        self.assertIn('0 Б', widget.findChild(QLabel, 'MessageFileMeta')._full_text)

    def test_keyboard_enter_and_space_use_same_save_signal(self):
        widget = self.bubble(message(attachment()), {0: capability()})
        button = self.primary(widget)
        button.setFocus()
        QTest.keyClick(button, Qt.Key.Key_Return)
        QTest.keyClick(button, Qt.Key.Key_Space)
        self.assertEqual([value[0] for value in self.actions], ['save', 'save'])

    def test_replaced_equal_attach_or_reordered_index_cannot_trigger_old_button(self):
        original = attachment()
        msg = message(original, attachment())
        widget = self.bubble(msg, {0: capability()})
        msg.attaches[0] = attachment()
        self.primary(widget).click()
        self.assertEqual(self.actions, [])
        self.assertIsNone(widget.open_file_menu(0))
        msg.attaches[0] = original
        msg.attaches.reverse()
        self.primary(widget).click()
        self.assertEqual(self.actions, [])

    def test_mutated_message_or_attachment_revokes_old_popup_and_primary(self):
        for field, value in (('id', 92), ('chat_id', 43), ('time_ms', 1793459500092.5)):
            msg = message(attachment())
            widget = self.bubble(msg, {0: capability()})
            menu = widget.open_file_menu(0)
            setattr(msg, field, value)
            menu.actions()[0].trigger()
            self.primary(widget).click()
        msg = message(attachment())
        widget = self.bubble(msg, {0: capability()})
        menu = widget.open_file_menu(0)
        msg.attaches[0].file_name = 'Replaced metadata'
        menu.actions()[1].trigger()
        self.primary(widget).click()
        self.assertEqual(self.actions, [])

    def test_close_hide_and_replaced_popup_revoke_saved_actions(self):
        widget = self.bubble(message(attachment()), {0: capability()})
        first = widget.open_file_menu(0)
        old = first.actions()[0]
        newer = widget.open_file_menu(0)
        old.trigger()
        self.assertEqual(self.actions, [])
        widget.hide()
        newer.actions()[0].trigger()
        self.assertEqual(self.actions, [])
        widget.show()
        current = widget.open_file_menu(0)
        widget.close()
        current.actions()[1].trigger()
        self.assertEqual(self.actions, [])

    def test_changed_model_proof_deleted_or_item_type_invalidates_snapshot(self):
        for field, initial, changed in (('file_metadata_confirmed', True, False),
                                        ('deleted', False, True),
                                        ('file_item_type', 'REGULAR', 'DELAYED')):
            file = attachment()
            setattr(file, field, initial)
            widget = self.bubble(message(file), {0: capability()})
            menu = widget.open_file_menu(0)
            setattr(file, field, changed)
            menu.actions()[0].trigger()
            self.primary(widget).click()
        self.assertEqual(self.actions, [])

    def test_receiver_may_destroy_bubble_during_file_menu_dispatch(self):
        widget = self.bubble(message(attachment()), {0: capability()})
        menu = widget.open_file_menu(0)
        def remove(*_args):
            widget.deleteLater()
            QCoreApplication.sendPostedEvents(widget, QEvent.Type.DeferredDelete)
        widget.file_action_requested.connect(remove)
        menu.actions()[0].trigger()
        self.assertFalse(isValid(widget))
        self.assertEqual(len(self.actions), 1)

    def test_file_more_button_opens_only_file_actions_and_unknown_action_is_inert(self):
        widget = self.bubble(message(attachment()), {0: capability()})
        more = widget.findChild(QPushButton, 'MessageFileMore')
        more.click()
        self.assertIsNotNone(widget._file_menu)
        widget._request_file('open_url', 0)
        widget._request_file('save', True)
        self.assertEqual(self.actions, [])
        self.media.assert_not_called()

    def test_light_dark_narrow_and_wide_synthetic_captures(self):
        output = Path('.test-tmp')
        output.mkdir(exist_ok=True)
        for theme in ('light', 'dark'):
            palette = palette_for(theme)
            host = QWidget()
            self.widgets.append(host)
            host.setStyleSheet(f'background:{palette.window};')
            layout = QVBoxLayout(host)
            layout.setContentsMargins(0, 18, 0, 18)
            for outgoing, deleted in ((False, False), (True, False), (False, True)):
                name = '<b>Синтетический документ & длинное приложение 2026.pdf</b>'
                widget = MessageBubble(message(attachment(file_name=name), outgoing=outgoing),
                    '#0085FF', palette, file_actions={0: capability(name, deleted=deleted)})
                layout.addWidget(widget)
            layout.addStretch(1)
            host.show()
            for width in (520, 320):
                host.resize(width, 400)
                self.app.processEvents()
                for child in host.findChildren(QLabel, 'MessageFileName'):
                    self.assertEqual(child.textFormat(), Qt.TextFormat.PlainText)
                    self.assertGreaterEqual(child.width(), 70)
                    self.assertTrue(child.text())
                    self.assertLessEqual(child.fontMetrics().horizontalAdvance(child.text()), child.width() + 1)
                self.assertLessEqual(host.width(), width)
                self.assertTrue(host.grab().save(str(output / f'message_file_bubbles_{width}_{theme}.png')))

    def test_actual_file_popup_synthetic_light_dark_and_disabled_captures(self):
        output = Path('.test-tmp')
        output.mkdir(exist_ok=True)
        for theme in ('light', 'dark'):
            for deleted in (False, True):
                widget = self.bubble(message(attachment()), {0: capability(deleted=deleted)}, theme)
                widget.findChild(QPushButton, 'MessageFileMore').click()
                menu = widget._file_menu
                self.app.processEvents()
                self.assertTrue(menu.isVisible())
                self.assertEqual([action.text() for action in menu.actions()],
                                 ['Сохранить файл', 'Копировать имя файла'])
                self.assertEqual(menu.actions()[0].isEnabled(), not deleted)
                self.assertTrue(menu.actions()[1].isEnabled())
                state = 'deleted' if deleted else 'available'
                self.assertTrue(menu.grab().save(str(output / f'message_file_popup_{state}_{theme}.png')))


if __name__ == '__main__':
    unittest.main()
