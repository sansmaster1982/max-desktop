"""All HTTP/DNS are fake; filesystem effects stay in an owned workspace tempdir."""
from pathlib import Path
import os
import tempfile
from threading import Event
import unittest
from unittest.mock import Mock, patch

import requests

from maxclient.core.file_download_transport import (
    FileDownloadCancelled, FileDownloadError, MAX_FILE_BYTES, StagedFile,
    capture_target, stream_file, suggested_filename,
)


URL = 'https://files.example.test/item?signature=synthetic-private'


def response(chunks=(b'abc', b'def'), *, headers=None, status=200):
    value = Mock(status_code=status, headers=headers or {})
    value.__enter__ = Mock(return_value=value)
    value.__exit__ = Mock(return_value=False)
    value.iter_content.return_value = iter(chunks)
    return value


class FileTransportTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parents[1] / '.test-tmp'
        root.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix='file-transport-', dir=root)
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name).resolve()
        self.assertTrue(self.folder.is_relative_to(root.resolve()))
        self.target = self.folder / 'selected.bin'
        self.target.write_bytes(b'previous contents')
        self.expected = capture_target(self.target)
        self.cancel = Event()
        self.current = True
        self.progress = []
        self.reply = response()
        self.session = Mock()
        self.session.__enter__ = Mock(return_value=self.session)
        self.session.__exit__ = Mock(return_value=False)
        self.session.get.return_value = self.reply
        self.network = patch('requests.Session', return_value=self.session)
        self.network.start()
        self.addCleanup(self.network.stop)
        self.dns = patch('maxclient.core.file_download_transport.socket.getaddrinfo',
            return_value=[(2, 1, 6, '', ('93.184.215.14', 443))])
        self.resolver = self.dns.start()
        self.addCleanup(self.dns.stop)

    def fetch(self, **options):
        defaults = {'expected_target': self.expected, 'cancelled': self.cancel,
            'is_current': lambda: self.current, 'on_progress': lambda n, total: self.progress.append((n, total))}
        defaults.update(options)
        return stream_file(URL, self.target, **defaults)

    def parts(self):
        return list(self.folder.glob('.max-download-*.part'))

    def test_stage_then_explicit_commit_preserves_target_until_final_action(self):
        self.reply.headers['Content-Length'] = '6'
        staged = self.fetch()
        self.assertIsInstance(staged, StagedFile)
        self.assertEqual(self.target.read_bytes(), b'previous contents')
        self.assertEqual(staged.received, 6)
        self.assertEqual(staged.total, 6)
        self.assertEqual(len(self.parts()), 1)
        self.assertEqual(self.progress, [(0, 6), (3, 6), (6, 6)])
        self.assertEqual(staged.commit(), self.target)
        self.assertEqual(self.target.read_bytes(), b'abcdef')
        self.assertFalse(self.parts())
        self.assertTrue(staged.discard())  # Never removes the final destination.
        self.assertEqual(self.target.read_bytes(), b'abcdef')

    def test_unknown_total_and_no_credentials_or_second_host_resolution(self):
        staged = self.fetch()
        self.addCleanup(staged.discard)
        call = self.session.get.call_args
        self.assertEqual(call.args[0], 'https://93.184.215.14/item?signature=synthetic-private')
        self.assertEqual(call.kwargs['headers']['Host'], 'files.example.test')
        self.assertEqual(call.kwargs['headers']['Accept-Encoding'], 'identity')
        self.assertFalse(call.kwargs['allow_redirects'])
        self.assertEqual(call.kwargs['timeout'], (5, 15))
        adapter = self.session.mount.call_args.args[1]
        self.assertEqual(adapter.poolmanager.connection_pool_kw['server_hostname'], 'files.example.test')
        self.assertEqual(adapter.poolmanager.connection_pool_kw['assert_hostname'], 'files.example.test')
        proxy = adapter.proxy_manager_for('http://proxy.example.test:8080')
        self.assertEqual(proxy.connection_pool_kw['server_hostname'], 'files.example.test')
        request = Mock(headers={'Authorization': 'private', 'Cookie': 'private'})
        self.session.auth(request)
        self.assertEqual(request.headers, {})
        self.session.cookies.clear.assert_called_once()
        self.assertEqual(self.resolver.call_count, 1)
        self.assertEqual(self.progress, [(0, None), (3, None), (6, None)])
        self.assertNotIn('signature', repr(staged))
        self.assertNotIn(str(self.folder), repr(staged))

    def test_destination_created_after_capture_is_not_overwritten(self):
        self.target.unlink()
        self.expected = capture_target(self.target)
        self.assertFalse(self.expected.exists)
        staged = self.fetch()
        self.target.write_bytes(b'another user action')
        with self.assertRaises(FileDownloadError): staged.commit()
        self.assertEqual(self.target.read_bytes(), b'another user action')
        self.assertFalse(self.parts())

    def test_existing_destination_changed_after_stream_is_preserved(self):
        staged = self.fetch()
        self.target.write_bytes(b'newer external contents')
        with self.assertRaises(FileDownloadError): staged.commit()
        self.assertEqual(self.target.read_bytes(), b'newer external contents')
        self.assertFalse(self.parts())

    def test_absent_destination_created_at_final_publish_handoff_is_not_overwritten(self):
        self.target.unlink()
        self.expected = capture_target(self.target)
        staged = self.fetch()
        function = 'rename' if os.name == 'nt' else 'link'
        publish = getattr(os, function)
        def raced(source, destination):
            self.target.write_bytes(b'created concurrently by user')
            return publish(source, destination)
        with patch('maxclient.core.file_download_transport.os.' + function, side_effect=raced):
            with self.assertRaises(FileDownloadError): staged.commit()
        self.assertEqual(self.target.read_bytes(), b'created concurrently by user')
        self.assertFalse(self.parts())

    def test_stale_fingerprint_stops_before_any_request(self):
        self.target.write_bytes(b'changed before starting')
        with self.assertRaises(FileDownloadError): self.fetch()
        self.session.get.assert_not_called()
        self.assertFalse(self.parts())

    def test_fingerprint_cannot_be_reused_for_another_destination(self):
        other = self.folder / 'another.bin'
        with self.assertRaises(FileDownloadError): self.fetch(expected_target=capture_target(other))
        self.session.get.assert_not_called()

    def test_cancel_or_stale_context_at_commit_discards_stage(self):
        for kind in ('cancel', 'context'):
            with self.subTest(kind=kind):
                self.cancel.clear()
                self.current = True
                self.reply = response()
                self.session.get.return_value = self.reply
                staged = self.fetch()
                if kind == 'cancel': self.cancel.set()
                else: self.current = False
                with self.assertRaises(FileDownloadCancelled): staged.commit()
                self.assertEqual(self.target.read_bytes(), b'previous contents')
                self.assertFalse(self.parts())

    def test_cancellation_before_start_does_not_request_or_create_part(self):
        self.cancel.set()
        with self.assertRaises(FileDownloadCancelled): self.fetch()
        self.session.get.assert_not_called()
        self.assertFalse(self.parts())

    def test_cancel_and_owner_loss_midstream_cleanup_partial_bytes(self):
        for kind in ('cancel', 'context'):
            with self.subTest(kind=kind):
                self.cancel.clear()
                self.current = True
                def chunks(**_):
                    yield b'initial'
                    if kind == 'cancel': self.cancel.set()
                    else: self.current = False
                    yield b'never written'
                self.reply.iter_content.side_effect = chunks
                with self.assertRaises(FileDownloadCancelled): self.fetch()
                self.assertFalse(self.parts())
                self.assertEqual(self.target.read_bytes(), b'previous contents')

    def test_cancel_after_last_chunk_is_checked_before_returning_stage(self):
        def chunks(**_):
            yield b'all bytes'
            self.cancel.set()
        self.reply.iter_content.side_effect = chunks
        with self.assertRaises(FileDownloadCancelled): self.fetch()
        self.assertFalse(self.parts())

    def test_actual_byte_cap_does_not_trust_missing_or_false_content_length(self):
        with self.assertRaises(FileDownloadError): self.fetch(max_bytes=5)
        self.assertFalse(self.parts())
        self.reply.iter_content.return_value = iter((b'123456',))
        self.reply.headers['Content-Length'] = '1'
        with self.assertRaises(FileDownloadError): self.fetch(max_bytes=5)
        self.assertFalse(self.parts())

    def test_declared_oversize_or_invalid_length_rejected_before_creating_part(self):
        for length in (str(MAX_FILE_BYTES + 1), 'bad', '-1', '+1', ' 1', '9' * 200):
            with self.subTest(length=length):
                self.reply.headers['Content-Length'] = length
                with self.assertRaises(FileDownloadError): self.fetch()
                self.assertFalse(self.parts())
        self.reply.iter_content.assert_not_called()

    def test_truncated_response_is_not_returned_or_committed(self):
        self.reply.headers['Content-Length'] = '20'
        with self.assertRaises(FileDownloadError): self.fetch()
        self.assertFalse(self.parts())
        self.assertEqual(self.target.read_bytes(), b'previous contents')

    def test_zero_length_file_is_valid_without_fabricated_progress(self):
        self.reply = response((), headers={'Content-Length': '0'})
        self.session.get.return_value = self.reply
        staged = self.fetch()
        self.assertEqual((staged.received, staged.total), (0, 0))
        staged.commit()
        self.assertEqual(self.target.read_bytes(), b'')
        self.assertEqual(self.progress, [(0, 0)])

    def test_encoded_content_has_unknown_decoded_total_but_still_actual_cap(self):
        self.reply.headers.update({'Content-Encoding': 'gzip', 'Content-Length': '2'})
        staged = self.fetch()
        self.addCleanup(staged.discard)
        self.assertIsNone(staged.total)
        self.assertEqual(staged.received, 6)
        self.assertEqual(self.progress[-1], (6, None))

    def test_local_multicast_or_mixed_dns_answers_never_reach_http(self):
        for addresses in [('127.0.0.1',), ('224.0.0.1',), ('93.184.215.14', '10.0.0.1')]:
            with self.subTest(addresses=addresses):
                self.resolver.return_value = [(2, 1, 6, '', (address, 443)) for address in addresses]
                with self.assertRaises(FileDownloadError): self.fetch()
                self.session.get.assert_not_called()
        self.assertFalse(self.parts())

    def test_every_redirect_is_revalidated_and_remains_pinned(self):
        first = response(status=302, headers={'Location': 'https://second.example.test/new'})
        self.session.get.side_effect = [first, response()]
        staged = self.fetch()
        self.addCleanup(staged.discard)
        self.assertEqual(self.resolver.call_count, 2)
        self.assertEqual(self.session.get.call_args.kwargs['headers']['Host'], 'second.example.test')
        self.assertEqual(self.session.get.call_args.args[0], 'https://93.184.215.14/new')

    def test_redirect_to_local_address_or_unsafe_scheme_is_not_requested(self):
        for location in ('file:///private', 'https://127.0.0.1/private', 'http://example.test/plain'):
            with self.subTest(location=location):
                self.reply.status_code = 302
                self.reply.headers = {'Location': location}
                self.session.get.reset_mock()
                with self.assertRaises(FileDownloadError): self.fetch()
                self.assertEqual(self.session.get.call_count, 1)
                self.assertFalse(self.parts())

    def test_redirect_limit_does_not_loop(self):
        self.session.get.side_effect = [response(status=302, headers={'Location': f'/{n}'}) for n in range(4)]
        with self.assertRaises(FileDownloadError): self.fetch()
        self.assertEqual(self.session.get.call_count, 4)

    def test_signed_transport_and_progress_errors_are_redacted_and_cleaned(self):
        def chunks(**_):
            yield b'written'
            raise requests.ConnectionError(URL)
        self.reply.iter_content.side_effect = chunks
        with self.assertRaises(FileDownloadError) as error: self.fetch()
        self.assertNotIn('synthetic-private', str(error.exception))
        self.assertFalse(self.parts())
        self.reply.iter_content.side_effect = None
        self.reply.iter_content.return_value = iter((b'abc',))
        with self.assertRaises(FileDownloadError) as error:
            self.fetch(on_progress=Mock(side_effect=RuntimeError(URL)))
        self.assertNotIn('synthetic-private', str(error.exception))
        self.assertFalse(self.parts())

    def test_tampered_stage_cannot_replace_existing_destination(self):
        staged = self.fetch()
        self.parts()[0].write_bytes(b'tampered different data')
        with self.assertRaises(FileDownloadError): staged.commit()
        self.assertEqual(self.target.read_bytes(), b'previous contents')
        self.assertFalse(self.parts())

    def test_replaced_part_at_closed_handle_handoff_is_never_adopted_or_deleted(self):
        ready, replaced = [False], [False]
        sync = os.fsync
        def finish_descriptor(descriptor):
            sync(descriptor)
            ready[0] = True
        def current():
            if ready[0] and not replaced[0]:
                foreign = self.folder / 'unrelated-user-file.bin'
                foreign.write_bytes(b'foreign contents must survive')
                os.replace(foreign, self.parts()[0])
                replaced[0] = True
            return True
        with patch('maxclient.core.file_download_transport.os.fsync', side_effect=finish_descriptor):
            with self.assertRaises(FileDownloadError): self.fetch(is_current=current)
        self.assertTrue(replaced[0])
        self.assertEqual(self.parts()[0].read_bytes(), b'foreign contents must survive')
        self.assertEqual(self.target.read_bytes(), b'previous contents')

    def test_commit_is_idempotent_and_never_replaces_again(self):
        staged = self.fetch()
        staged.commit()
        self.target.write_bytes(b'later user change')
        self.assertEqual(staged.commit(), self.target)
        self.assertEqual(self.target.read_bytes(), b'later user change')

    def test_failed_atomic_replace_cleans_only_part_and_redacts_os_error(self):
        staged = self.fetch()
        with patch('maxclient.core.file_download_transport.os.replace', side_effect=OSError(URL)):
            with self.assertRaises(FileDownloadError) as error: staged.commit()
        self.assertNotIn('synthetic-private', str(error.exception))
        self.assertEqual(self.target.read_bytes(), b'previous contents')
        self.assertFalse(self.parts())

    def test_invalid_url_admission_never_uses_network(self):
        for url in ('file:///private', 'http://example.test/file', 'https://u:p@example.test/a', URL+'#fragment'):
            with self.assertRaises(FileDownloadError):
                stream_file(url, self.target, expected_target=self.expected, cancelled=self.cancel,
                            is_current=lambda: True)
        self.session.get.assert_not_called()

    def test_cooperative_deadline_stops_without_creating_part(self):
        with patch('maxclient.core.file_download_transport.time.monotonic', side_effect=[0, 601]):
            with self.assertRaises(FileDownloadError): self.fetch()
        self.session.get.assert_not_called()
        self.assertFalse(self.parts())


class SuggestedFilenameTests(unittest.TestCase):
    def test_windows_basename_reserved_traversal_ads_and_controls(self):
        cases = {'../../safe.txt': 'safe.txt', r'C:\fake\file.txt': 'file.txt',
            'CON.txt': '_CON.txt', 'ＣＯＮ.txt': '_CON.txt', 'LPT¹.log': '_LPT1.log',
            'data.txt:stream': 'data.txt_stream', 'bad\nname?.txt': 'bad_name_.txt',
            '..': 'download.bin', ' . ': 'download.bin', 'file. ': 'file',
            '\ud800.txt': '_.txt'}
        for raw, expected in cases.items():
            with self.subTest(raw=ascii(raw)):
                self.assertEqual(suggested_filename(raw), expected)
        self.assertEqual(suggested_filename(None), 'download.bin')

    def test_suggestion_preserves_extension_with_finite_utf16_component(self):
        value = suggested_filename('😀' * 500 + '.zip')
        self.assertLessEqual(len(value.encode('utf-16-le')), 360)
        self.assertTrue(value.endswith('.zip'))


if __name__ == '__main__':
    unittest.main()
