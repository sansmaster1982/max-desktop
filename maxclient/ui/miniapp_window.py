"""A scoped, off-record OPEN_APP iframe host; no LOGIN token or general JS RPC."""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from threading import Event, Thread

from PySide6.QtCore import QFile, QIODevice, QObject, QTimer, QUrl, Qt, Signal, Slot
from PySide6.QtWebChannel import QWebChannel
from PySide6.QtWebEngineCore import (QWebEnginePage, QWebEngineProfile, QWebEngineScript,
                                   QWebEngineSettings, QWebEngineUrlRequestInterceptor)
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget
from shiboken6 import isValid

from .. import __app_name__
from ..protocol.miniapp_contracts import miniapp_origin


_HTML = b'''<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="referrer" content="strict-origin"><title>MAX Mini App</title>
<style>html,body,iframe{width:100%;height:100%;margin:0;border:0;display:block}
html{color-scheme:light dark}body{overflow:hidden}</style></head><body>
<iframe id="miniapp" credentialless referrerpolicy="strict-origin"
sandbox="allow-scripts allow-same-origin" title="MAX Mini App"></iframe>
</body></html>'''


class _HostAssets:
    """Only static, credential-free HTML is served. Signed URLs stay in memory."""
    def __init__(self):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                if self.path != '/index.html':
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.send_header('Content-Length', str(len(_HTML)))
                self.send_header('Cache-Control', 'no-store')
                self.send_header('X-Content-Type-Options', 'nosniff')
                self.end_headers()
                self.wfile.write(_HTML)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True
        self.url = f'http://127.0.0.1:{self.server.server_port}/index.html'
        self._thread = Thread(target=self.server.serve_forever, kwargs={'poll_interval': 0.05},
                              name='max-miniapp-assets', daemon=True)
        self._thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def _origin(value):
    try:
        return miniapp_origin(value)
    except (ValueError, TypeError):
        return None


class _MiniAppGate(QWebEngineUrlRequestInterceptor):
    def __init__(self, host_url, origin, active, parent=None):
        super().__init__(parent)
        self.host_url, self.origin, self.active = host_url, origin, active

    def interceptRequest(self, info):  # noqa: N802
        value = info.requestUrl().toString(QUrl.ComponentFormattingOption.FullyEncoded)
        # Qt invokes this on its IO thread. Never call back into GUI/SQLite or
        # acquire service locks while the UI can be waiting for this request.
        allowed = (self.active.is_set() and (value == self.host_url
                   or _origin(value) == self.origin or value.startswith(('data:', 'blob:'))))
        info.block(not allowed)


class _MiniAppPage(QWebEnginePage):
    def __init__(self, profile, host_url, origin, current, parent=None):
        super().__init__(profile, parent)
        self.host_url, self.origin, self.current = host_url, origin, current

    def acceptNavigationRequest(self, url, navigation_type, is_main_frame):  # noqa: N802
        if not self.current():
            return False
        value = url.toString(QUrl.ComponentFormattingOption.FullyEncoded)
        if is_main_frame:
            return value == self.host_url
        return value == 'about:blank' or _origin(value) == self.origin

    def createWindow(self, kind):  # noqa: N802
        return None

    def javaScriptConsoleMessage(self, level, message, line, source):  # noqa: N802
        pass  # Renderer logs may include signed launch URLs or private data.

    def javaScriptAlert(self, origin, message):  # noqa: N802
        pass

    def javaScriptConfirm(self, origin, message):  # noqa: N802
        return False

    def javaScriptPrompt(self, origin, message, default):  # noqa: N802
        return False, ''

    def chooseFiles(self, mode, old_files, mime_types):  # noqa: N802
        return []


class _LifecycleBridge(QObject):
    ready_received = Signal()
    close_received = Signal()

    def __init__(self, current, parent=None):
        super().__init__(parent)
        self.current = current

    @Slot()
    def ready(self):
        if self.current():
            self.ready_received.emit()

    @Slot()
    def closeApp(self):  # noqa: N802
        if self.current():
            self.close_received.emit()


def _host_script(url, origin, channel_source):
    """Only the host's ApplicationWorld gets Qt transport; no subframe injection."""
    return channel_source + '\n' + r'''
(() => {
  'use strict';
  const {launchUrl, origin} = __CONFIG__;
  const frame = document.getElementById('miniapp');
  if (!frame || !globalThis.qt || !qt.webChannelTransport) return;
  new QWebChannel(qt.webChannelTransport, channel => {
    const host = channel.objects.lifecycle;
    window.addEventListener('message', event => {
      if (event.origin !== origin || event.source !== frame.contentWindow ||
          typeof event.data !== 'string' || event.data.length > 65536) return;
      let packet;
      try { packet = JSON.parse(event.data); } catch (_) { return; }
      if (!packet || Array.isArray(packet) || typeof packet.type !== 'string' ||
          packet.type.length > 128) return;
      if (packet.type === 'WebAppReady') { host.ready(); return; }
      if (packet.type === 'WebAppClose') { host.closeApp(); return; }
      const reply = {type: packet.type,
        error: {code: 'client.unsupported_method.unsupported_method'}};
      if ((typeof packet.requestId === 'string' && packet.requestId.length <= 256) ||
          (typeof packet.requestId === 'number' && Number.isFinite(packet.requestId)))
        reply.requestId = packet.requestId;
      frame.contentWindow.postMessage(JSON.stringify(reply), origin);
    });
    frame.addEventListener('load', () => {
      if (frame.getAttribute('src') === launchUrl) host.ready();
    });
    frame.setAttribute('src', launchUrl);
  });
})();
'''.replace('__CONFIG__', json.dumps({'launchUrl': url, 'origin': origin}, ensure_ascii=True))


class MiniAppWindow(QWidget):
    """Constructor validates only; showing the window starts the scoped page."""
    closed = Signal()

    def __init__(self, url, context, is_current, parent=None):
        super().__init__(parent)
        self._origin = miniapp_origin(url)
        self._url, self._context, self._is_current = url, context, is_current
        self._closed = self._started = False
        self._assets = None
        self._active = Event()
        if not self._current():
            raise ValueError('Сессия мини-приложения уже недоступна')
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setWindowFlag(Qt.WindowType.Window, True)
        self.setWindowTitle(f'Мини-приложение — {__app_name__}')
        self.resize(480, 700)
        self.setMinimumSize(340, 420)
        root = QVBoxLayout(self)
        controls = QHBoxLayout()
        self.status = QLabel('Подготовка мини-приложения…')
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        controls.addWidget(self.status, 1)
        close = QPushButton('Закрыть')
        close.clicked.connect(self.close)
        controls.addWidget(close)
        root.addLayout(controls)
        self.view = QWebEngineView(self)
        self.view.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
        self.view.setAcceptDrops(False)
        root.addWidget(self.view, 1)
        self.clock = QTimer(self)
        self.clock.setInterval(250)
        self.clock.timeout.connect(self._check_context)
        self.clock.start()
        self.ready_timer = QTimer(self)
        self.ready_timer.setSingleShot(True)
        self.ready_timer.setInterval(15000)
        self.ready_timer.timeout.connect(lambda: self.status.setText('Мини-приложение ещё не загрузилось.'))
        self.destroyed.connect(lambda: self._release_assets())

    def _current(self):
        if self._closed or not isValid(self):
            return False
        try:
            return self._is_current(self._context) is True
        except Exception:
            return False

    def _check_context(self):
        if not self._current():
            self.close()

    def showEvent(self, event):  # noqa: N802
        super().showEvent(event)
        if not self._started and self._current():
            self._started = True
            QTimer.singleShot(0, self._start)

    def _start(self):
        if not self._current():
            return
        try:
            channel_file = QFile(':/qtwebchannel/qwebchannel.js')
            if not channel_file.open(QIODevice.OpenModeFlag.ReadOnly):
                raise ValueError('Channel unavailable')
            channel_source = bytes(channel_file.readAll()).decode('utf-8')
            channel_file.close()
            self._assets = _HostAssets()
            self.profile = QWebEngineProfile(self)
            self.profile.setHttpCacheType(QWebEngineProfile.HttpCacheType.MemoryHttpCache)
            self.profile.setPersistentCookiesPolicy(QWebEngineProfile.PersistentCookiesPolicy.NoPersistentCookies)
            self.profile.setPersistentPermissionsPolicy(QWebEngineProfile.PersistentPermissionsPolicy.AskEveryTime)
            self.profile.downloadRequested.connect(lambda download: download.cancel())
            self._active.set()
            self.gate = _MiniAppGate(self._assets.url, self._origin, self._active, self.profile)
            self.profile.setUrlRequestInterceptor(self.gate)
            self.page = _MiniAppPage(self.profile, self._assets.url, self._origin, self._current, self.view)
            settings = self.page.settings()
            for name in ('JavascriptCanOpenWindows', 'JavascriptCanAccessClipboard',
                         'LocalContentCanAccessFileUrls', 'LocalContentCanAccessRemoteUrls',
                         'AllowRunningInsecureContent', 'FullScreenSupportEnabled', 'ScreenCaptureEnabled'):
                attribute = getattr(QWebEngineSettings.WebAttribute, name, None)
                if attribute is not None:
                    settings.setAttribute(attribute, False)
            self.page.permissionRequested.connect(lambda permission: permission.deny())
            self.page.fileSystemAccessRequested.connect(lambda request: request.reject())
            self.page.desktopMediaRequested.connect(lambda request: request.cancel())
            self.page.selectClientCertificate.connect(lambda selection: selection.selectNone())
            self.page.registerProtocolHandlerRequested.connect(lambda request: request.reject())
            self.page.quotaRequested.connect(lambda request: request.reject())
            self.page.fullScreenRequested.connect(lambda request: request.reject())
            self.page.certificateError.connect(lambda error: error.rejectCertificate())
            self.channel = QWebChannel(self.page)
            self.lifecycle = _LifecycleBridge(self._current, self.channel)
            self.lifecycle.ready_received.connect(self._ready)
            self.lifecycle.close_received.connect(self.close)
            self.channel.registerObject('lifecycle', self.lifecycle)
            world = QWebEngineScript.ScriptWorldId.ApplicationWorld
            self.page.setWebChannel(self.channel, world)
            script = QWebEngineScript()
            script.setName('MAX scoped miniapp lifecycle')
            script.setWorldId(world)
            script.setRunsOnSubFrames(False)
            script.setInjectionPoint(QWebEngineScript.InjectionPoint.DocumentReady)
            script.setSourceCode(_host_script(self._url, self._origin, channel_source))
            self.page.scripts().insert(script)
            self.view.setPage(self.page)
            self.ready_timer.start()
            self.view.setUrl(QUrl(self._assets.url))
        except Exception:
            self._active.clear()
            self.status.setText('Не удалось открыть мини-приложение.')
            if self._assets is not None:
                self._assets.close()
                self._assets = None

    def _ready(self):
        if self._current():
            self.ready_timer.stop()
            self.status.setText('Мини-приложение MAX')

    def _release_assets(self):
        self._active.clear()
        if self._assets is not None:
            self._assets.close()
            self._assets = None

    def closeEvent(self, event):  # noqa: N802
        if self._closed:
            super().closeEvent(event)
            return
        self._closed = True
        self._active.clear()
        self.clock.stop()
        self.ready_timer.stop()
        self._url = ''
        if hasattr(self, 'page') and isValid(self.page):
            self.page.triggerAction(QWebEnginePage.WebAction.Stop)
            self.page.scripts().clear()
        self._release_assets()
        self.closed.emit()
        super().closeEvent(event)
