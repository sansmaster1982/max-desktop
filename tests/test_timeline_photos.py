"""Synthetic PHOTO bytes and callbacks only: no session, account, RPC or network."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from types import SimpleNamespace
import unittest
from unittest.mock import patch

from PySide6.QtCore import QBuffer, QCoreApplication, QEvent, QIODevice, Qt
from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QApplication, QScrollArea, QVBoxLayout, QWidget
from shiboken6 import isValid

from maxclient.core.models import Attach, Message
from maxclient.ui.timeline_photos import TimelinePhotos, PhotoPreview, photo_source
from maxclient.ui.widgets.message_bubble import MessageBubble
from maxclient.ui.theme import palette_for
from test_desktop_shell import _load_offscreen_font


def png_bytes():
    image = QImage(240, 160, QImage.Format.Format_RGB32)
    image.fill(QColor('#69AABB'))
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    assert image.save(buffer, 'PNG')
    return bytes(buffer.data())


def photo(**extra):
    values = dict(type='PHOTO', base_url='https://photos.example.test/image?credential=synthetic',
                  width=600, height=400)
    values.update(extra)
    return Attach(**values)


class _View(QWidget):
    def __init__(self):
        super().__init__()
        self._file_context = SimpleNamespace(chat_id=42)
        self.valid = True
        self._rows = []
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        root = QVBoxLayout(self)
        root.addWidget(self.scroll)
        body = QWidget()
        self.layout = QVBoxLayout(body)
        self.layout.addStretch()
        self.scroll.setWidget(body)

    def file_context_current(self, value):
        return self.valid and value is not None and value is self._file_context

    def append(self, attach, *, message_id=91, theme='light', text=''):
        msg = Message(chat_id=42, id=message_id, attaches=[attach], text=text)
        bubble = MessageBubble(msg, '#0085FF', palette_for(theme))
        self.layout.insertWidget(self.layout.count() - 1, bubble)
        self._rows.append({'msg': msg, 'bubble': bubble})
        return msg, bubble


class TimelinePhotosTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        _load_offscreen_font(cls.app)

    def setUp(self):
        self.requests = []
        def capture(function, *args, **kwargs):
            self.requests.append((function, args, kwargs))
        self.network = patch('maxclient.ui.conversation_media_loader.run_async', side_effect=capture)
        self.network.start()
        self.view = _View()
        self.controller = TimelinePhotos(self.view)
        self.controller.timer.stop()
        self.view.resize(600, 500)

    def tearDown(self):
        self.controller.reset()
        self.view.close()
        self.view.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()
        self.network.stop()

    def show(self):
        self.view.show()
        self.app.processEvents()
        self.controller.refresh()

    def done(self, index=0):
        self.requests[index][2]['on_done'](png_bytes())

    def test_base_url_only_photo_renders_supplied_url_and_stable_dimensions(self):
        attach = photo()
        msg, bubble = self.view.append(attach)
        preview = bubble._photo_bindings[0][1]
        before = preview.size()
        self.show()
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.requests[0][1], (attach.base_url,))
        self.done()
        self.assertFalse(preview._pixmap.isNull())
        self.assertEqual(preview.size(), before)
        self.controller.refresh()
        self.assertEqual(len(self.requests), 1)

    def test_base_url_precedence_and_direct_url_fallback(self):
        self.assertEqual(photo_source(photo(url='https://other.example.test/image')), photo().base_url)
        self.assertEqual(photo_source(photo(base_url=None, url='https://other.example.test/image')),
                         'https://other.example.test/image')
        self.assertIsNone(photo_source(photo(base_url='file:///private/image',
                                           url='https://other.example.test/image')))

    def test_deleted_malformed_local_and_unsafe_sources_are_never_loaded(self):
        for extra in ({'deleted': True}, {'deleted': 'false'},
                      {'base_url': 'http://photos.example.test/image'}, {'base_url': 'https://127.0.0.1/image'},
                      {'base_url': 'https://user:pass@photos.example.test/image'}, {'base_url': None}):
            self.assertIsNone(photo_source(photo(**extra)), extra)
        for flag in (True, 'false', 0, None):
            attach = Attach.from_server({'_type': 'PHOTO', 'baseUrl': photo().base_url, 'deleted': flag})
            self.assertIsNone(photo_source(attach))
        self.assertEqual(photo_source(Attach.from_server({'_type': 'PHOTO', 'baseUrl': photo().base_url})),
                         photo().base_url)

    def test_missing_local_copy_uses_actual_remote_source_and_bound_viewer(self):
        attach = photo(local_path='C:/synthetic-missing-photo.png')
        with patch('maxclient.ui.timeline_photos.os.path.isfile', return_value=False):
            msg, bubble = self.view.append(attach)
            self.show()
            self.assertEqual(len(self.requests), 1)
            self.assertEqual(self.requests[0][1], (attach.base_url,))
            self.done()
            self.assertFalse(bubble._photo_bindings[0][1]._pixmap.isNull())
            from maxclient.ui.chat_view import ChatView
            with patch.object(self.view, '_photos', self.controller, create=True):
                with patch.object(self.controller, 'open_photo') as opened:
                    ChatView._open_media(self.view, msg, attach)
                    opened.assert_called_once_with(msg, attach)
        with patch('maxclient.ui.timeline_photos.os.path.isfile', return_value=True):
            self.assertIsNone(photo_source(attach))

    def test_hidden_view_and_offscreen_rows_are_not_fetched(self):
        self.view.append(photo())
        self.controller.refresh()
        self.assertEqual(self.requests, [])
        spacer = QWidget()
        spacer.setFixedHeight(1200)
        self.view.layout.insertWidget(0, spacer)
        self.show()
        self.assertEqual(self.requests, [])
        bar = self.view.scroll.verticalScrollBar()
        bar.setValue(bar.maximum())
        self.app.processEvents()
        self.controller.refresh()
        self.assertEqual(len(self.requests), 1)

    def test_late_bytes_after_session_change_are_discarded(self):
        msg, bubble = self.view.append(photo())
        self.show()
        self.view._file_context = SimpleNamespace(chat_id=42)
        self.done()
        self.assertTrue(bubble._photo_bindings[0][1]._pixmap.isNull())
        self.controller.refresh()
        self.assertTrue(self.requests[0][2]['cancelled'].is_set())
        self.assertEqual(len(self.requests), 2)
        self.done(1)
        self.assertFalse(bubble._photo_bindings[0][1]._pixmap.isNull())

    def test_replaced_row_and_mutated_source_reject_late_pixels(self):
        msg, bubble = self.view.append(photo())
        self.show()
        msg.attaches[0].base_url = 'https://photos.example.test/replaced'
        self.done()
        self.assertTrue(bubble._photo_bindings[0][1]._pixmap.isNull())
        self.view._rows.clear()
        self.done()
        self.assertTrue(bubble._photo_bindings[0][1]._pixmap.isNull())

    def test_failure_is_visible_without_unbounded_automatic_retry(self):
        msg, bubble = self.view.append(photo())
        self.show()
        self.requests[0][2]['on_error'](RuntimeError('never expose URL or credential'))
        preview = bubble._photo_bindings[0][1]
        self.assertIn('Не удалось', preview._text)
        self.assertNotIn('credential', preview._text)
        for _ in range(4):
            self.controller.refresh()
        self.assertEqual(len(self.requests), 1)

    def test_malformed_image_bytes_are_not_rendered(self):
        msg, bubble = self.view.append(photo())
        self.show()
        self.requests[0][2]['on_done'](b'<html>private error body</html>')
        preview = bubble._photo_bindings[0][1]
        self.assertTrue(preview._pixmap.isNull())
        self.assertIn('Не удалось', preview._text)

    def test_pending_boolean_message_id_cannot_start_http(self):
        self.view.append(photo(), message_id=True)
        self.show()
        self.assertEqual(self.requests, [])

    def test_click_uses_bound_viewer_without_legacy_resolve_rpc(self):
        msg, bubble = self.view.append(photo())
        self.show()
        with patch('maxclient.ui.conversation_photo_viewer.ConversationPhotoViewer') as factory:
            viewer = factory.return_value
            self.controller.open_photo(msg, msg.attaches[0])
            factory.assert_called_once()
            args = factory.call_args.args
            self.assertEqual(args[0][0]['open_url'], msg.attaches[0].base_url)
            self.assertTrue(args[3](self.view._file_context))
            viewer.show.assert_called_once()
            self.view.valid = False
            self.assertFalse(args[3](self.view._file_context))
            self.controller._viewers.clear()  # Mock is not a Qt object.

    def test_closed_scope_cancels_pending_work(self):
        self.view.append(photo())
        self.show()
        cancel = self.requests[0][2]['cancelled']
        self.controller.reset()
        self.assertTrue(cancel.is_set())
        self.assertEqual(self.controller._bindings, {})

    def test_removed_attachment_never_starts_fetch_and_changed_loaded_source_clears_pixels(self):
        msg, bubble = self.view.append(photo())
        selected = msg.attaches.pop()
        self.show()
        self.assertEqual(self.requests, [])
        msg.attaches.append(selected)
        self.controller.refresh()
        self.done()
        preview = bubble._photo_bindings[0][1]
        self.assertFalse(preview._pixmap.isNull())
        selected.deleted = True
        self.controller.refresh()
        self.assertTrue(preview._pixmap.isNull())
        self.assertNotIn('Загрузка', preview._text)


if __name__ == '__main__':
    unittest.main()
