"""Request-policy test only: does not create a web page, fetch assets or access media."""
import hashlib
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
from PySide6.QtCore import QUrl
from maxclient.ui.call_window import ASSETS, VENDOR, _NetworkGate, CallWindow


class Request:
    def __init__(self, url):
        self.url, self.blocked = QUrl(url), None
    def requestUrl(self):
        return self.url
    def block(self, value):
        self.blocked = value


class CallsWindowPolicyTests(unittest.TestCase):
    def test_opening_window_policy_blocks_all_remote_and_insecure_requests(self):
        gate = _NetworkGate('http://127.0.0.1:12345')
        for url in ('https://synthetic.invalid', 'wss://synthetic.invalid', 'http://outside.invalid', 'file:///private'):
            request = Request(url);gate.interceptRequest(request);self.assertTrue(request.blocked)
        for url in ('http://127.0.0.1:12345/index.html', 'qrc:///qtwebchannel/qwebchannel.js'):
            request = Request(url);gate.interceptRequest(request);self.assertFalse(request.blocked)
        gate.armed = True
        request = Request('https://synthetic.invalid');gate.interceptRequest(request);self.assertTrue(request.blocked)
        gate.allow_origins({'https://synthetic.invalid', 'wss://signal.invalid/call?token=synthetic'})
        request = Request('https://synthetic.invalid');gate.interceptRequest(request);self.assertFalse(request.blocked)
        request = Request('wss://signal.invalid/call?token=synthetic');gate.interceptRequest(request);self.assertFalse(request.blocked)
        request = Request('wss://unverified.invalid');gate.interceptRequest(request);self.assertTrue(request.blocked)
        request = Request('http://outside.invalid');gate.interceptRequest(request);self.assertTrue(request.blocked)

    def test_public_variant_excludes_vendor_and_fails_before_server(self):
        from maxclient.core.call_runtime import CallRuntimeError
        from maxclient.ui.call_window import _AssetsServer, calls_assets_available
        self.assertFalse(calls_assets_available())
        self.assertFalse((ASSETS / 'vendor').exists())
        with patch('maxclient.ui.call_window.ThreadingHTTPServer') as server:
            with self.assertRaises(CallRuntimeError):
                _AssetsServer()
        server.assert_not_called()

    def test_teardown_timeout_disposes_local_renderer_without_retry(self):
        old_page, new_page = Mock(), Mock()
        fake = SimpleNamespace(_finished=False, _force_dispose=False, _closing=True,
            gate=SimpleNamespace(armed=True), _js=Mock(), runtime=Mock(), status=Mock(),
            mic=Mock(),camera=Mock(),hangup=Mock(),closure_unknown=Mock(),close=Mock(),
            page=old_page, profile=Mock(), view=Mock(), _server=SimpleNamespace(origin='http://127.0.0.1:12345'))
        with patch('maxclient.ui.call_window._Page',return_value=new_page):
            CallWindow._teardown_timeout(fake)
        self.assertTrue(fake._finished and fake._force_dispose)
        self.assertFalse(fake.gate.armed)
        fake._js.assert_called_once_with('stopTracks')
        fake.runtime.close.assert_called_once()
        fake.view.setPage.assert_called_once_with(new_page)
        old_page.deleteLater.assert_called_once()
        fake.close.assert_called_once()
        self.assertIn('неизвестен',fake.closure_unknown.emit.call_args.args[0])


if __name__ == '__main__':
    unittest.main()
