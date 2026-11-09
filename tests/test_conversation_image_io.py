"""Bounded image I/O and stale callbacks, with all network/workers replaced."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from threading import Event
import unittest
from unittest.mock import Mock, patch

from PySide6.QtCore import QBuffer, QIODevice, QCoreApplication, QEvent
from PySide6.QtGui import QPixmap, QColor
from PySide6.QtWidgets import QApplication
from shiboken6 import isValid

from maxclient.core.media_fetch import fetch_image, image_url, MediaFetchError
from maxclient.ui.conversation_media_loader import ConversationMediaLoader
from maxclient.ui.conversation_photo_viewer import ConversationPhotoViewer


def row(key='photo:1', **values):
    return {'key': key, 'kind': 'PHOTO', 'available': True,
            'open_url': 'https://images.example.test/photo.png',
            'thumbnail_url': 'https://images.example.test/thumb.png',
            'chat_id': 303, 'message_id': 7, 'time_ms': 1793500000000, 'action': 'message', **values}


def image_bytes():
    pixmap = QPixmap(64, 32)
    pixmap.fill(QColor('#16B3CA'))
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    pixmap.save(buffer, 'PNG')
    return bytes(buffer.data())


class ImageFetchTests(unittest.TestCase):
    def setUp(self):
        self.cancel = Event()
        self.response = Mock(status_code=200, headers={'Content-Type': 'image/png'})
        self.response.__enter__ = Mock(return_value=self.response)
        self.response.__exit__ = Mock(return_value=False)
        self.response.iter_content.return_value = [b'abc', b'def']
        self.session = Mock()
        self.session.__enter__ = Mock(return_value=self.session)
        self.session.__exit__ = Mock(return_value=False)
        self.session.get.return_value = self.response
        self.network = patch('requests.Session', return_value=self.session)
        self.network.start()
        self.dns = patch('maxclient.core.media_fetch.socket.getaddrinfo',
                         return_value=[(2, 1, 6, '', ('93.184.215.14', 443))])
        self.dns_mock = self.dns.start()
        self.addCleanup(self.network.stop)
        self.addCleanup(self.dns.stop)

    def fetch(self, **kw):
        return fetch_image('https://images.example.test/image.png', cancelled=self.cancel, **kw)

    def test_no_credentials_and_bounded_stream_parameters(self):
        self.assertEqual(self.fetch(), b'abcdef')
        call = self.session.get.call_args
        self.assertEqual(call.kwargs['timeout'], (5, 15))
        self.assertFalse(call.kwargs['allow_redirects'])
        self.assertEqual(call.args[0], 'https://93.184.215.14/image.png')
        self.assertEqual(call.kwargs['headers']['Host'], 'images.example.test')
        adapter = self.session.mount.call_args.args[1]
        self.assertEqual(adapter.poolmanager.connection_pool_kw['server_hostname'], 'images.example.test')
        self.assertEqual(adapter.poolmanager.connection_pool_kw['assert_hostname'], 'images.example.test')
        request = Mock(headers={'Authorization': 'private', 'Cookie': 'private'})
        self.session.auth(request)
        self.assertEqual(request.headers, {})
        self.session.cookies.clear.assert_called_once()

    def test_local_credential_and_invalid_schemes_are_rejected(self):
        for url in (None, {}, 'file:///c:/secret', 'http://example.com/a',
                    'https://a:b@example.com/a', 'https://127.0.0.1/a',
                    'https://host.local/a', 'https://example.com:8443/a',
                    'https://example.com/\nsecret'):
            self.assertIsNone(image_url(url))
        self.session.get.assert_not_called()

    def test_private_dns_is_rejected_before_request(self):
        self.dns_mock.return_value = [(2, 1, 6, '', ('127.0.0.1', 443))]
        with self.assertRaises(MediaFetchError): self.fetch()
        self.session.get.assert_not_called()

    def test_size_is_enforced_without_trusting_content_length(self):
        with self.assertRaises(MediaFetchError): self.fetch(max_bytes=5)
        self.response.headers['Content-Length'] = '100'
        self.response.iter_content.reset_mock()
        with self.assertRaises(MediaFetchError): self.fetch(max_bytes=10)
        self.response.iter_content.assert_not_called()

    def test_mime_and_bad_content_length_rejected(self):
        for headers in ({'Content-Type': 'text/html'}, {'Content-Type': 'image/png', 'Content-Length': 'bad'}):
            self.response.headers = headers
            with self.assertRaises(MediaFetchError): self.fetch()

    def test_cancel_before_and_during_stream(self):
        self.cancel.set()
        with self.assertRaises(MediaFetchError): self.fetch()
        self.session.get.assert_not_called()
        self.cancel.clear()
        def chunks(**_):
            yield b'first'
            self.cancel.set()
            yield b'next'
        self.response.iter_content.side_effect = chunks
        with self.assertRaises(MediaFetchError): self.fetch()

    def test_redirect_is_validated_without_following_local_location(self):
        self.response.status_code = 302
        self.response.headers = {'Location': 'file:///c:/secret'}
        with self.assertRaises(MediaFetchError): self.fetch()
        self.assertEqual(self.session.get.call_count, 1)

    def test_safe_relative_redirect_has_finite_limit(self):
        self.response.status_code = 302
        self.response.headers = {'Location': '/other.png'}
        with self.assertRaises(MediaFetchError): self.fetch()
        self.assertEqual(self.session.get.call_count, 2)
        self.assertEqual(self.session.cookies.clear.call_count, 2)

    def test_transport_errors_do_not_expose_signed_url(self):
        import requests
        self.session.get.side_effect = requests.ConnectionError('https://private?token=secret')
        with self.assertRaises(MediaFetchError) as caught: self.fetch()
        self.assertNotIn('secret', str(caught.exception))


class ImageLoaderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.context = object()
        self.current = self.context
        self.loader = ConversationMediaLoader(lambda context: context is self.current)
        self.loader.bind(self.context)
        self.patch = patch('maxclient.ui.conversation_media_loader.run_async')
        self.jobs = self.patch.start()
        self.ready = Mock()
        self.loader.ready.connect(self.ready)

    def tearDown(self):
        if isValid(self.loader):
            self.loader.bind(None)
            self.loader.deleteLater()
        self.app.processEvents()
        self.patch.stop()

    def test_queue_has_four_workers_and_duplicate_requests_are_ignored(self):
        for index in range(6): self.loader.request(row(str(index)), self.context)
        self.assertEqual(self.jobs.call_count, 4)
        self.loader.request(row('0'), self.context)
        self.assertEqual(self.jobs.call_count, 4)
        self.jobs.call_args_list[0].kwargs['on_done'](image_bytes())
        self.assertEqual(self.jobs.call_count, 5)
        self.assertEqual(self.ready.call_args.args[:3], (self.context, '0', row()['thumbnail_url']))

    def test_changed_url_can_reload_same_key_with_url_provenance(self):
        self.loader.request(row(), self.context)
        old = self.jobs.call_args.kwargs['on_done']
        self.loader.request(row(thumbnail_url='https://images.example.test/new.png'), self.context)
        self.assertEqual(self.jobs.call_count, 2)
        old(image_bytes())
        self.assertEqual(self.ready.call_args.args[2], row()['thumbnail_url'])

    def test_finished_thumbnail_can_be_requested_again_after_presenter_eviction(self):
        self.loader.request(row(), self.context)
        self.jobs.call_args.kwargs['on_done'](image_bytes())
        self.assertFalse(self.loader._seen)
        self.loader.request(row(), self.context)
        self.assertEqual(self.jobs.call_count, 2)

    def test_context_change_cancels_old_request_and_ignores_pixels(self):
        self.loader.request(row(), self.context)
        call = self.jobs.call_args
        self.loader.bind(object())
        self.assertTrue(call.kwargs['cancelled'].is_set())
        call.kwargs['on_done'](image_bytes())
        self.ready.assert_not_called()

    def test_invalid_bytes_signal_failure_without_private_error(self):
        failed = Mock()
        self.loader.failed.connect(failed)
        self.loader.request(row(), self.context)
        self.jobs.call_args.kwargs['on_done'](b'not an image')
        failed.assert_called_once_with(self.context, 'photo:1')
        self.ready.assert_not_called()

    def test_destroyed_loader_cancels_and_ignores_callback(self):
        self.loader.request(row(), self.context)
        call = self.jobs.call_args
        self.loader.deleteLater()
        QCoreApplication.sendPostedEvents(self.loader, QEvent.Type.DeferredDelete)
        self.assertTrue(call.kwargs['cancelled'].is_set())
        call.kwargs['on_done'](image_bytes())
        self.ready.assert_not_called()


class PhotoViewerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.context = object()
        self.current = self.context
        self.patch = patch('maxclient.ui.conversation_photo_viewer.run_async')
        self.jobs = self.patch.start()
        self.viewer = ConversationPhotoViewer([row('a'), row('b')], 'a', self.context,
                                              lambda context: context is self.current)

    def tearDown(self):
        if isValid(self.viewer): self.viewer.close()
        self.app.processEvents()
        self.patch.stop()

    def test_only_selected_photo_fetches_and_late_previous_image_is_ignored(self):
        self.assertEqual(self.jobs.call_count, 1)
        old = self.jobs.call_args
        self.viewer.select(1)
        self.assertTrue(old.kwargs['cancelled'].is_set())
        old.kwargs['on_done'](image_bytes())
        self.assertTrue(self.viewer._pixmap.isNull())
        self.jobs.call_args.kwargs['on_done'](image_bytes())
        self.assertEqual(self.viewer._pixmap.width(), 64)
        self.assertEqual(self.viewer.counter.text(), '2 / 2')

    def test_closed_or_changed_session_does_not_receive_image(self):
        call = self.jobs.call_args
        self.current = object()
        call.kwargs['on_done'](image_bytes())
        self.assertTrue(self.viewer._pixmap.isNull())
        self.viewer._check_session()
        self.assertTrue(call.kwargs['cancelled'].is_set())

    def test_errors_are_generic_and_retry_is_explicit(self):
        self.jobs.call_args.kwargs['on_error']('private URL signed secret')
        self.assertNotIn('secret', self.viewer.status.text())
        self.assertEqual(self.jobs.call_count, 1)
        self.viewer.retry.click()
        self.assertEqual(self.jobs.call_count, 2)

    def test_message_navigation_keeps_original_outer_identity(self):
        navigate = Mock()
        self.viewer.message_requested.connect(navigate)
        self.viewer._jump()
        item, context = navigate.call_args.args
        self.assertEqual((item['chat_id'], item['message_id']), (303, 7))
        self.assertIs(context, self.context)

    def test_deleted_viewer_cancels_and_ignores_old_result(self):
        call = self.jobs.call_args
        self.viewer.deleteLater()
        QCoreApplication.sendPostedEvents(self.viewer, QEvent.Type.DeferredDelete)
        self.assertTrue(call.kwargs['cancelled'].is_set())
        call.kwargs['on_done'](image_bytes())

    def test_zoom_keeps_original_pixels_and_scales_only_canvas_geometry(self):
        self.jobs.call_args.kwargs['on_done'](image_bytes())
        identity = self.viewer._pixmap.cacheKey()
        for _ in range(30): self.viewer.zoom_by(0.2)
        self.assertEqual(self.viewer._zoom, 4.0)
        self.assertEqual(self.viewer.picture._image.cacheKey(), identity)
        self.assertEqual(self.viewer.picture._image.size(), self.viewer._pixmap.size())
        self.viewer.fit_image()
        self.assertEqual(self.viewer._zoom, 1.0)

    def test_save_dialog_rechecks_session_before_any_file_write(self):
        self.jobs.call_args.kwargs['on_done'](image_bytes())
        def changed(*args):
            self.current = object()
            return ('must-not-write.png', 'PNG')
        with patch('maxclient.ui.conversation_photo_viewer.QFileDialog.getSaveFileName', side_effect=changed), \
                patch('maxclient.ui.conversation_photo_viewer.QSaveFile') as writer:
            self.viewer.save_image()
            writer.assert_not_called()

    def test_save_writes_decoded_photo_only_to_explicit_destination(self):
        import tempfile
        from pathlib import Path
        self.jobs.call_args.kwargs['on_done'](image_bytes())
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / 'photo.png'
            with patch('maxclient.ui.conversation_photo_viewer.QFileDialog.getSaveFileName',
                       return_value=(str(target), 'PNG')):
                self.viewer.save_image()
            self.assertTrue(target.read_bytes().startswith(b'\x89PNG\r\n\x1a\n'))
            self.assertEqual(self.viewer.status.text(), 'Фото сохранено.')


if __name__ == '__main__': unittest.main()
