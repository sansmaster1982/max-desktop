"""Synthetic miniapp policy and JS event harness; no renderer/network/account."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import json
import shutil
import subprocess
from threading import Event
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from PySide6.QtCore import QCoreApplication, QEvent, QObject, QUrl
from PySide6.QtWebEngineCore import QWebEngineProfile, QWebEngineScript
from PySide6.QtWidgets import QApplication, QWidget
from shiboken6 import isValid

from maxclient.ui.miniapp_window import (_HTML, _host_script, _LifecycleBridge,
                                       _MiniAppGate, _MiniAppPage, MiniAppWindow)


URL = ('https://miniapp.example.test/view#WebAppData=query_id%3Dsynthetic%26sig%3Dnot-a-secret'
       '&literal=__ORIGIN____LAUNCH____CONFIG__')
ORIGIN = 'https://miniapp.example.test'
HOST = 'http://127.0.0.1:43123/index.html'


class Request:
    def __init__(self, value):
        self.value, self.blocked = QUrl(value), None
    def requestUrl(self):
        return self.value
    def block(self, value):
        self.blocked = value


class MiniAppPolicyTests(unittest.TestCase):
    def test_gate_exact_origin_and_ephemeral_static_host_only(self):
        active = Event(); active.set()
        gate = _MiniAppGate(HOST, ORIGIN, active)
        for url in (HOST, URL, ORIGIN + ':443/api?value=synthetic%20value',
                    'data:image/png;base64,AA==', 'blob:' + ORIGIN + '/synthetic'):
            request = Request(url); gate.interceptRequest(request)
            self.assertFalse(request.blocked, url)
        for url in ('http://miniapp.example.test/view', 'https://other.example.test/',
                    'https://miniapp.example.test.evil.test/', 'file:///private',
                    'http://127.0.0.1:43123/secret', 'https://miniapp.example.test:444/',
                    'https://user:password@miniapp.example.test/'):
            request = Request(url); gate.interceptRequest(request)
            self.assertTrue(request.blocked, url)

    def test_revoked_context_blocks_even_existing_host_and_queued_request(self):
        active = Event(); active.set()
        gate = _MiniAppGate(HOST, ORIGIN, active)
        active.clear()  # The UI's context clock revokes this thread-safe event.
        for url in (HOST, URL, 'data:image/png;base64,AA=='):
            request = Request(url); gate.interceptRequest(request)
            self.assertTrue(request.blocked)
        request = Request(URL); gate.interceptRequest(request)
        self.assertTrue(request.blocked)

    def test_main_frame_cannot_be_replaced_by_remote_or_file_navigation(self):
        page = SimpleNamespace(host_url=HOST, origin=ORIGIN, current=lambda: True)
        check = lambda url, main: _MiniAppPage.acceptNavigationRequest(page, QUrl(url), None, main)
        self.assertTrue(check(HOST, True))
        self.assertTrue(check(URL, False))
        self.assertTrue(check('about:blank', False))
        for url in (URL, 'https://other.example.test/', 'file:///private', 'data:text/html,hi'):
            self.assertFalse(check(url, True))
        self.assertFalse(check('https://other.example.test/', False))
        page.current = lambda: False
        self.assertFalse(check(HOST, True)); self.assertFalse(check(URL, False))

    def test_no_popup_files_js_dialog_or_console_output(self):
        self.assertIsNone(_MiniAppPage.createWindow(None, None))
        self.assertEqual(_MiniAppPage.chooseFiles(None, None, [], []), [])
        self.assertFalse(_MiniAppPage.javaScriptConfirm(None, QUrl(ORIGIN), 'secret'))
        self.assertEqual(_MiniAppPage.javaScriptPrompt(None, QUrl(ORIGIN), 'secret', ''), (False, ''))
        with patch('builtins.print') as output:
            _MiniAppPage.javaScriptConsoleMessage(None, None, URL, 3, URL)
            _MiniAppPage.javaScriptAlert(None, QUrl(ORIGIN), URL)
        output.assert_not_called()

    def test_bridge_has_only_ready_close_and_rejects_late_context(self):
        current = [True]
        bridge = _LifecycleBridge(lambda: current[0])
        seen = []
        bridge.ready_received.connect(lambda: seen.append('ready'))
        bridge.close_received.connect(lambda: seen.append('close'))
        bridge.ready(); bridge.closeApp()
        current[0] = False
        bridge.ready(); bridge.closeApp()
        self.assertEqual(seen, ['ready', 'close'])
        self.assertFalse(hasattr(bridge, 'rpc'))

    def test_static_wrapper_contains_no_credentials_or_remote_url(self):
        self.assertNotIn(URL.encode(), _HTML)
        self.assertIn(b'credentialless', _HTML)
        self.assertIn(b'sandbox="allow-scripts allow-same-origin"', _HTML)
        for permission in (b'allow-popups', b'allow-downloads', b'allow-forms', b'allow-modals'):
            self.assertNotIn(permission, _HTML)

    @unittest.skipUnless(shutil.which('node'), 'Node is needed for isolated JS event harness')
    def test_exact_origin_and_frame_json_transport_rejects_foreign_frames_and_unsupported_actions(self):
        harness = r'''
const assert = require('node:assert/strict');
const counts = {ready:0,close:0}, sent=[];
const child = {postMessage:(packet, origin)=>sent.push([JSON.parse(packet),origin])};
const attrs = {}, listeners = {};
const frame = {contentWindow:child,addEventListener:(name,fn)=>listeners[name]=fn,
 setAttribute:(name,value)=>attrs[name]=value,getAttribute:name=>attrs[name]};
global.document={getElementById:id=>id==='miniapp'?frame:null};
global.window={addEventListener:(name,fn)=>listeners[name]=fn};
global.qt={webChannelTransport:{}};
global.QWebChannel=function(transport, callback){ callback({objects:{lifecycle:{
 ready:()=>counts.ready++,closeApp:()=>counts.close++}}}); };
'''
        harness += _host_script(URL, ORIGIN, '')
        harness += '\nconst origin=' + json.dumps(ORIGIN) + ';const launch=' + json.dumps(URL) + ';\n'
        harness += r'''
assert.equal(attrs.src,launch);
const event=(data,source=child,at=origin)=>listeners.message({origin:at,source,data});
event(JSON.stringify({type:'WebAppReady'}),{},origin);
event(JSON.stringify({type:'WebAppReady'}),child,'https://other.example.test');
event({type:'WebAppReady'});event('not json');event(JSON.stringify([]));
assert.equal(counts.ready,0);
event(JSON.stringify({type:'WebAppReady'}));assert.equal(counts.ready,1);
listeners.load();assert.equal(counts.ready,2);
event(JSON.stringify({type:'WebAppClose'}));assert.equal(counts.close,1);
for(const type of ['WebAppRequestPhone','WebAppDownloadFile','WebAppOpenLink','WebAppMaxShare',
                  'WebAppPermissionsRequestAccess','WebAppRequestTheme','WebAppRequestViewport']){
 event(JSON.stringify({type,requestId:'request-1',url:'file:///never-open'}));
 const [answer,target] = sent.pop();assert.equal(target,origin);assert.equal(answer.type,type);
 assert.equal(answer.requestId,'request-1');
 assert.equal(answer.error.code,'client.unsupported_method.unsupported_method');
}
assert.equal(counts.close,1);assert.equal(sent.length,0);
console.log('PASS synthetic miniapp event transport');
'''
        result = subprocess.run([shutil.which('node'), '-'], input=harness, text=True,
                                capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), 'PASS synthetic miniapp event transport')


class MiniAppWindowLifecycleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.current = [True]
        self.engine = patch('maxclient.ui.miniapp_window.QWebEngineView', QWidget)
        self.engine.start()
        self.window = MiniAppWindow(URL, object(), lambda context: self.current[0])
        self.assets = patch('maxclient.ui.miniapp_window._HostAssets', side_effect=AssertionError('No server/network'))
        self.assets.start()

    def tearDown(self):
        if isValid(self.window):
            self.window.close(); self.window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()
        self.assets.stop(); self.engine.stop()

    def test_constructor_never_launches_url_and_stale_context_closes(self):
        self.assertFalse(self.window._started)
        self.assertIsNone(self.window._assets)
        self.current[0] = False
        self.window._check_context()
        self.assertTrue(self.window._closed)
        self.assertEqual(self.window._url, '')

    def test_show_dispatch_is_single_and_close_releases_assets_once(self):
        with patch.object(self.window, '_start') as start:
            self.window.show(); self.app.processEvents()
            self.window.hide(); self.window.show(); self.app.processEvents()
            start.assert_called_once()
        fake = Mock()
        self.window._assets = fake
        self.window._active.set()
        self.window.close()
        self.window._release_assets()
        fake.close.assert_called_once()
        self.assertFalse(self.window._active.is_set())

    def test_destroyed_parentless_window_releases_static_server_without_close_event(self):
        fake = Mock(); self.window._assets = fake
        self.window.deleteLater()
        QCoreApplication.sendPostedEvents(self.window, QEvent.Type.DeferredDelete)
        fake.close.assert_called_once()

    def test_start_configures_isolated_profile_and_main_frame_application_world_only(self):
        profile = QObject()
        for name in ('setHttpCacheType', 'setPersistentCookiesPolicy', 'setPersistentPermissionsPolicy',
                     'setUrlRequestInterceptor'):
            setattr(profile, name, Mock())
        profile.downloadRequested = Mock()
        page = QObject()
        for name in ('permissionRequested', 'fileSystemAccessRequested', 'desktopMediaRequested',
                     'selectClientCertificate', 'registerProtocolHandlerRequested', 'quotaRequested',
                     'fullScreenRequested', 'certificateError'):
            setattr(page, name, Mock())
        page.settings, page.scripts, page.setWebChannel, page.triggerAction = Mock(), Mock(), Mock(), Mock()
        self.window.view.setPage, self.window.view.setUrl = Mock(), Mock()
        assets = Mock(url=HOST)
        self.assets.stop()
        with patch('maxclient.ui.miniapp_window._HostAssets', return_value=assets):
            with patch('maxclient.ui.miniapp_window.QWebEngineProfile', return_value=profile) as factory:
                factory.HttpCacheType = QWebEngineProfile.HttpCacheType
                factory.PersistentCookiesPolicy = QWebEngineProfile.PersistentCookiesPolicy
                factory.PersistentPermissionsPolicy = QWebEngineProfile.PersistentPermissionsPolicy
                with patch('maxclient.ui.miniapp_window._MiniAppPage', return_value=page):
                    self.window._start()
        self.assets.start()
        self.window.view.setUrl.assert_called_once_with(QUrl(HOST))
        profile.setHttpCacheType.assert_called_once_with(QWebEngineProfile.HttpCacheType.MemoryHttpCache)
        profile.setPersistentCookiesPolicy.assert_called_once_with(
            QWebEngineProfile.PersistentCookiesPolicy.NoPersistentCookies)
        page.setWebChannel.assert_called_once_with(self.window.channel,
                                                   QWebEngineScript.ScriptWorldId.ApplicationWorld)
        script = page.scripts.return_value.insert.call_args.args[0]
        self.assertFalse(script.runsOnSubFrames())
        self.assertEqual(script.worldId(), QWebEngineScript.ScriptWorldId.ApplicationWorld)
        self.assertIn('event.source !== frame.contentWindow', script.sourceCode())
        self.assertIn('WebAppReady', script.sourceCode())
        self.assertFalse(self.window.view.acceptDrops())
        for name, method in (('permissionRequested', 'deny'), ('fileSystemAccessRequested', 'reject'),
                             ('desktopMediaRequested', 'cancel'), ('selectClientCertificate', 'selectNone'),
                             ('registerProtocolHandlerRequested', 'reject'), ('quotaRequested', 'reject'),
                             ('fullScreenRequested', 'reject'), ('certificateError', 'rejectCertificate')):
            request = Mock()
            getattr(page, name).connect.call_args.args[0](request)
            getattr(request, method).assert_called_once()
        download = Mock()
        profile.downloadRequested.connect.call_args.args[0](download)
        download.cancel.assert_called_once()


if __name__ == '__main__':
    unittest.main()
