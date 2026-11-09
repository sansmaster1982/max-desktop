"""Isolated public-SDK call window. Opening it performs no remote request/media capture."""
from __future__ import annotations

import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
from urllib.parse import urlsplit

from PySide6.QtCore import QFile, QIODevice, QTimer, QUrl, Qt, Signal, Slot
from PySide6.QtWebChannel import QWebChannel
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile, QWebEngineUrlRequestInterceptor
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import QHBoxLayout, QLabel, QMessageBox, QPushButton, QVBoxLayout, QWidget

from ..core.call_runtime import CallConfig, CallRuntime, CallRuntimeError, _origin


ASSETS = Path(__file__).with_name('calls_web')
VENDOR = {'vendor/C5-Rnmn1.js': '45bc3ec27aa249a77bd4e1ea234c85674d5421819d4a80ddbbf48f09a08a3586',
          'vendor/BgEk880G.js': '46bcb172ea802aa9f47ef5632740ff898a301746147848572cd158a41f9ccdb8'}


def calls_assets_available():
    return all((ASSETS / name).is_file() for name in VENDOR)


class _AssetsServer:
    def __init__(self):
        if not calls_assets_available():
            raise CallRuntimeError('Звонки не включены в публичную сборку MAXIM.')
        allowed = {'index.html', 'main.mjs', 'session.mjs', *VENDOR}
        contents = {name: (ASSETS / name).read_bytes() for name in allowed}
        channel_script = QFile(':/qtwebchannel/qwebchannel.js')
        if not channel_script.open(QIODevice.OpenModeFlag.ReadOnly):
            raise CallRuntimeError('Не удалось загрузить канал окна звонков.')
        contents['qtwebchannel.js'] = bytes(channel_script.readAll())
        channel_script.close()
        for name, expected in VENDOR.items():
            if hashlib.sha256(contents[name]).hexdigest() != expected:
                raise CallRuntimeError('Не удалось проверить модуль звонков.')

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                name = urlsplit(self.path).path.lstrip('/') or 'index.html'
                data = contents.get(name)
                if data is None:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8' if name.endswith('.html') else 'text/javascript; charset=utf-8')
                self.send_header('Content-Length', str(len(data)))
                self.send_header('Cache-Control', 'no-store')
                self.send_header('X-Content-Type-Options', 'nosniff')
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True
        self.url = f'http://127.0.0.1:{self.server.server_port}/index.html'
        self.origin = f'http://127.0.0.1:{self.server.server_port}'
        threading.Thread(target=self.server.serve_forever, name='max-call-assets', daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class _NetworkGate(QWebEngineUrlRequestInterceptor):
    def __init__(self, origin, api_origin=None, parent=None):
        super().__init__(parent)
        self.origin, self.armed = origin, False
        self._allowed = {_origin(api_origin)} if api_origin else set()

    @Slot(object)
    def allow_origins(self, origins):
        # Called only by Python from confirmed RPC ACKs/137, not exposed to JS.
        self._allowed = self._allowed | {_origin(value) for value in origins}

    def interceptRequest(self, info):
        url = info.requestUrl()
        local = url.toString().startswith(self.origin + '/')
        bootstrap = url.scheme() == 'qrc' and url.path() == '/qtwebchannel/qwebchannel.js'
        media = url.scheme() in {'blob', 'data'}
        try:
            remote_origin = _origin(url.toString())
        except CallRuntimeError:
            remote_origin = None
        remote = self.armed and remote_origin in self._allowed
        info.block(not (local or bootstrap or media or remote))


class _Page(QWebEnginePage):
    def __init__(self, profile, origin, parent=None):
        super().__init__(profile, parent)
        self.origin = origin

    def acceptNavigationRequest(self, url, navigation_type, is_main_frame):
        return url.toString().startswith(self.origin + '/')

    def javaScriptConsoleMessage(self, level, message, line, source):
        # SDK errors/debug packets may contain ephemeral credentials.
        pass

    def createWindow(self, kind):
        return None


class CallWindow(QWidget):
    """Parent supplies a real scoped backend + source-derived CallConfig.

    Optional incoming must be a verified137 DTO (conversationId/vcp); contact_id
    and join_link are supplied by host UI, never accepted from JavaScript.
    """
    closure_unknown = Signal(str)

    def __init__(self, runtime: CallRuntime, config: CallConfig, *, contact_id=None,
                 incoming=None, join_link=None, title='Звонок MAX', parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self.runtime, self.config = runtime, config
        self.contact_id, self.incoming, self.join_link = contact_id, incoming, join_link
        self._started = self._finished = self._closing = self._ready = False
        self._force_dispose = False
        self._audio, self._video = True, False
        self._failure_message = ''
        self._teardown_timer = QTimer(self)
        self._teardown_timer.setSingleShot(True)
        self._teardown_timer.setInterval(5000)
        self._teardown_timer.timeout.connect(self._teardown_timeout)
        self.setWindowTitle(title)
        self.resize(900, 650)
        layout = QVBoxLayout(self)
        self.status = QLabel('Подготовка окна звонка…')
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.status)
        self.view = QWebEngineView(self)
        layout.addWidget(self.view, 1)
        row = QHBoxLayout()
        self.audio_call = QPushButton('Ответить' if incoming else 'Позвонить')
        self.video_call = QPushButton('Видеозвонок')
        self.decline = QPushButton('Отклонить')
        self.mic = QPushButton('Выключить микрофон')
        self.camera = QPushButton('Включить камеру')
        self.hangup = QPushButton('Завершить')
        for button in (self.audio_call, self.video_call, self.decline, self.mic, self.camera, self.hangup):
            row.addWidget(button)
            button.setEnabled(False)
        layout.addLayout(row)
        self.video_call.setVisible(not incoming)
        self.decline.setVisible(bool(incoming))
        self.audio_call.clicked.connect(lambda: self._start(False))
        self.video_call.clicked.connect(lambda: self._start(True))
        self.decline.clicked.connect(lambda: self._start(False, accept=False))
        self.mic.clicked.connect(self._toggle_audio)
        self.camera.clicked.connect(self._toggle_video)
        self.hangup.clicked.connect(self._end)
        self.runtime.event.connect(self._event)
        self.runtime.failure.connect(self._failure)
        self.runtime.idle.connect(self._maybe_close)
        self._server = None
        if not calls_assets_available():
            self.status.setText('Звонки не включены в публичную сборку MAXIM.')
            return
        try:
            self._public_config = config.public_dict()
            self._server = _AssetsServer()
        except Exception:
            self._server = None
            self.status.setText('Конфигурация или модуль звонков ещё не готовы.')
            return
        self.profile = QWebEngineProfile(self)  # off-the-record; no shared browser credentials
        self.profile.setHttpCacheType(QWebEngineProfile.HttpCacheType.MemoryHttpCache)
        self.profile.setPersistentCookiesPolicy(QWebEngineProfile.PersistentCookiesPolicy.NoPersistentCookies)
        self.gate = _NetworkGate(self._server.origin, self.config.api_base_url, self.profile)
        self.runtime.origins.connect(self.gate.allow_origins)
        self.profile.setUrlRequestInterceptor(self.gate)
        self.page = _Page(self.profile, self._server.origin, self.view)
        self.channel = QWebChannel(self.page)
        self.channel.registerObject('calls', runtime)
        self.page.setWebChannel(self.channel)
        self.page.featurePermissionRequested.connect(self._permission)
        self.view.setPage(self.page)
        self.view.setUrl(QUrl(self._server.url))

    def _js(self, method, *args):
        encoded = ','.join(json.dumps(arg, ensure_ascii=True) for arg in args)
        self.page.runJavaScript(f'window.callsUI && window.callsUI.{method}({encoded})')

    def _start(self, is_video, *, accept=True):
        if not self._ready or self._started:
            return
        try:
            if self.incoming:
                intent = self.runtime.arm_incoming(self.incoming['conversationId'], self.incoming['vcp'], accept=accept)
            elif self.join_link:
                intent = self.runtime.arm_join(self.join_link, is_video=is_video)
            else:
                intent = self.runtime.arm_outgoing(self.contact_id, is_video=is_video)
        except Exception:
            self.status.setText('Не удалось начать звонок в текущей сессии.')
            return
        self._started = True
        self._video = bool(is_video)
        self.gate.armed = True
        self.audio_call.setEnabled(False)
        self.video_call.setEnabled(False)
        self.decline.setEnabled(False)
        self.hangup.setEnabled(True)
        self.status.setText('Подключение…')
        self._js('launch', intent, self._public_config)

    def _event(self, name, metadata):
        if name == 'ready':
            if self._finished:
                return
            self._ready = True
            self.status.setText('Входящий звонок. Выберите «Ответить» или «Отклонить».'
                                if self.incoming else 'Можно начать звонок.')
            for button in (self.audio_call, self.video_call, self.decline):
                button.setEnabled(True)
        elif name in {'ringing', 'active'}:
            if self._finished:
                return
            self.status.setText('Вызов…' if name == 'ringing' else 'Звонок подключён.')
            self.mic.setEnabled(True)
            self.camera.setEnabled(True)
            self.camera.setText('Выключить камеру' if self._video else 'Включить камеру')
        elif name in {'ended', 'error'}:
            self._finished = True
            self.gate.armed = False
            self.status.setText('Звонок завершён.' if name == 'ended' else self._failure_message or 'Операция звонка не подтверждена. Повтор отключён.')
            self._js('stopTracks')
            for button in (self.mic, self.camera, self.hangup):
                button.setEnabled(False)
            if not self.runtime.busy:
                self._teardown_timer.stop()
            if self._closing:
                self.close()

    def _permission(self, origin, feature):
        allowed = {QWebEnginePage.Feature.MediaAudioCapture, QWebEnginePage.Feature.MediaVideoCapture,
                   QWebEnginePage.Feature.MediaAudioVideoCapture}
        permit = False
        if (self._started and not self._finished and feature in allowed
                and origin.toString().rstrip('/') == self._server.origin):
            permit = QMessageBox.question(self, 'Звонок MAX', 'Разрешить микрофон или камеру для этого звонка?',
                      QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                      QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes
        policy = QWebEnginePage.PermissionPolicy.PermissionGrantedByUser if permit else QWebEnginePage.PermissionPolicy.PermissionDeniedByUser
        self.page.setFeaturePermission(origin, feature, policy)

    def _failure(self, message):
        self._failure_message = message
        self.status.setText(message)

    def _toggle_audio(self):
        self._audio = not self._audio
        self._js('audio', self._audio)
        self.mic.setText('Выключить микрофон' if self._audio else 'Включить микрофон')

    def _toggle_video(self):
        self._video = not self._video
        self._js('video', self._video)
        self.camera.setText('Выключить камеру' if self._video else 'Включить камеру')

    def _end(self):
        self.hangup.setEnabled(False)
        self.runtime.cancel_start()
        if not self._teardown_timer.isActive():
            self._teardown_timer.start()
        self._js('hangup')

    def _maybe_close(self):
        if self._finished and not self.runtime.busy:
            self._teardown_timer.stop()
        if self._closing and self._finished and not self.runtime.busy:
            self.close()

    def _teardown_timeout(self):
        # Local disposal is bounded; it is not a claim of server hangup ACK.
        message = 'Микрофон и камера остановлены. Результат завершения звонка на сервере неизвестен.'
        self._finished = self._force_dispose = True
        self.gate.armed = False
        self._js('stopTracks')
        self.runtime.close()
        # Replacing/deleting the old renderer also releases capture when its JS
        # event loop is stalled and cannot execute stopTracks itself.
        old_page = self.page
        self.page = _Page(self.profile, self._server.origin, self.view)
        self.view.setPage(self.page)
        old_page.deleteLater()
        self.status.setText(message)
        for button in (self.mic, self.camera, self.hangup):
            button.setEnabled(False)
        self.closure_unknown.emit(message)
        if self._closing:
            self.close()

    def closeEvent(self, event):
        if self._started and not self._finished:
            self._closing = True
            self._end()
            event.ignore()
            return
        if self.runtime.busy and not self._force_dispose:
            self._closing = True
            if not self._teardown_timer.isActive():
                self._teardown_timer.start()
            event.ignore()
            return
        self.runtime.close()
        if self._server:
            self.gate.armed = False
            self._server.close()
            self._server = None
        event.accept()
