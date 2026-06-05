"""Медиа: запись голосовых (QMediaRecorder) и просмотр фото/видео/аудио.

QtMultimedia подключается лениво. Если PySide6-Addons не установлен,
MULTIMEDIA_AVAILABLE=False: запись голоса отключается, видео/аудио открываются
во внешнем приложении, фото показываются через QPixmap (мультимедиа не нужно).
"""
from __future__ import annotations

import os
import time
from typing import Optional

from PySide6.QtCore import QUrl, Qt, QTimer, Signal, QObject
from PySide6.QtGui import QDesktopServices, QPixmap
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from ..core.config import Settings
from ..core.models import Attach
from .async_runner import run_async
from .widgets.common import fmt_duration

try:
    from PySide6.QtMultimedia import (
        QAudioInput,
        QAudioOutput,
        QMediaCaptureSession,
        QMediaPlayer,
        QMediaRecorder,
    )
    from PySide6.QtMultimediaWidgets import QVideoWidget

    MULTIMEDIA_AVAILABLE = True
except Exception:  # noqa: BLE001
    MULTIMEDIA_AVAILABLE = False


# ───────────────────────── voice recorder ─────────────────────────

if MULTIMEDIA_AVAILABLE:

    class VoiceRecorder(QObject):
        recorded = Signal(str, int)   # (path, duration_ms)
        cancelled = Signal()

        def __init__(self, settings: Settings) -> None:
            super().__init__()
            self._settings = settings
            self._session = QMediaCaptureSession()
            self._audio_in = QAudioInput()
            self._recorder = QMediaRecorder()
            self._session.setAudioInput(self._audio_in)
            self._session.setRecorder(self._recorder)
            self._recording = False
            self._started_at = 0.0
            self._out_path = ""
            self._recorder.recorderStateChanged.connect(self._on_state)

        def is_recording(self) -> bool:
            return self._recording

        def start(self) -> None:
            media_dir = self._settings.download_dir or "."
            os.makedirs(media_dir, exist_ok=True)
            stamp = int(time.time() * 1000)
            self._out_path = os.path.join(media_dir, f"voice_{stamp}.m4a")
            self._recorder.setOutputLocation(QUrl.fromLocalFile(self._out_path))
            self._started_at = time.time()
            self._recording = True
            self._recorder.record()

        def stop(self) -> None:
            if self._recording:
                self._recording = False
                self._recorder.stop()

        def _on_state(self, state) -> None:
            if state == QMediaRecorder.RecorderState.StoppedState and self._out_path:
                duration = int((time.time() - self._started_at) * 1000)
                actual = self._recorder.actualLocation().toLocalFile() or self._out_path
                # Дать файловой системе дописать файл.
                QTimer.singleShot(150, lambda: self._emit(actual, duration))

        def _emit(self, path: str, duration: int) -> None:
            if path and os.path.isfile(path) and os.path.getsize(path) > 0:
                self.recorded.emit(path, duration)
            else:
                self.cancelled.emit()

else:  # заглушка
    VoiceRecorder = None  # type: ignore


# ───────────────────────── media viewer ─────────────────────────

class MediaViewer(QDialog):
    def __init__(self, src: str, attach: Attach, settings: Settings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._src = src
        self._attach = attach
        self._settings = settings
        self._player = None
        self.setWindowTitle(attach.label)
        self.resize(820, 600)

        self._root = QVBoxLayout(self)
        self._root.setContentsMargins(0, 0, 0, 0)

        if attach.is_image:
            self._setup_image()
        elif MULTIMEDIA_AVAILABLE and (attach.is_video or attach.is_voice):
            self._setup_player()
        else:
            self._setup_external()

    # image — QPixmap, мультимедиа не требуется
    def _setup_image(self) -> None:
        self._img = QLabel("Загрузка…")
        self._img.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._root.addWidget(self._img, 1)
        run_async(_fetch_bytes, self._src, on_done=self._on_image, on_error=self._on_img_err)

    def _on_image(self, data: bytes) -> None:
        pix = QPixmap()
        if data and pix.loadFromData(data):
            self._img.setPixmap(
                pix.scaled(
                    self.width() - 20, self.height() - 20,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )
        else:
            self._on_img_err("не удалось декодировать изображение")

    def _on_img_err(self, message: str) -> None:
        self._img.setText(f"Не удалось загрузить: {message}")

    # video/audio — QMediaPlayer
    def _setup_player(self) -> None:
        self._player = QMediaPlayer()
        self._audio_out = QAudioOutput()
        self._player.setAudioOutput(self._audio_out)

        if self._attach.is_video:
            self._video = QVideoWidget()
            self._player.setVideoOutput(self._video)
            self._root.addWidget(self._video, 1)
        else:
            icon = QLabel("🎤")
            icon.setStyleSheet("font-size:72px;")
            icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self._root.addWidget(icon, 1)

        controls = QWidget()
        row = QHBoxLayout(controls)
        row.setContentsMargins(14, 10, 14, 14)
        self._play_btn = QPushButton("⏸")
        self._play_btn.setObjectName("IconButton")
        self._play_btn.clicked.connect(self._toggle)
        row.addWidget(self._play_btn)

        self._slider = QSlider(Qt.Orientation.Horizontal)
        self._slider.sliderMoved.connect(self._player.setPosition)
        row.addWidget(self._slider, 1)

        self._time = QLabel("0:00")
        row.addWidget(self._time)
        self._root.addWidget(controls)

        self._player.positionChanged.connect(self._on_pos)
        self._player.durationChanged.connect(self._on_dur)
        self._player.playbackStateChanged.connect(self._on_play_state)

        self._player.setSource(_to_qurl(self._src))
        self._player.play()

    def _toggle(self) -> None:
        if self._player is None:
            return
        if self._player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self._player.pause()
        else:
            self._player.play()

    def _on_pos(self, pos: int) -> None:
        self._slider.setValue(pos)
        self._time.setText(fmt_duration(pos))

    def _on_dur(self, dur: int) -> None:
        self._slider.setRange(0, dur)

    def _on_play_state(self, state) -> None:
        playing = state == QMediaPlayer.PlaybackState.PlayingState
        self._play_btn.setText("⏸" if playing else "▶")

    def _setup_external(self) -> None:
        box = QVBoxLayout()
        lbl = QLabel(
            "Для воспроизведения видео и голосовых установите PySide6-Addons.\n"
            "Можно открыть файл во внешнем приложении."
        )
        lbl.setWordWrap(True)
        lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        btn = QPushButton("Открыть во внешнем приложении")
        btn.setObjectName("Primary")
        btn.clicked.connect(lambda: QDesktopServices.openUrl(_to_qurl(self._src)))
        wrap = QWidget()
        wrap.setLayout(box)
        box.addStretch(1)
        box.addWidget(lbl)
        box.addWidget(btn, 0, Qt.AlignmentFlag.AlignCenter)
        box.addStretch(1)
        self._root.addWidget(wrap, 1)

    def closeEvent(self, event) -> None:  # noqa: N802
        if self._player is not None:
            self._player.stop()
        super().closeEvent(event)


def _to_qurl(src: str) -> QUrl:
    if os.path.isfile(src):
        return QUrl.fromLocalFile(src)
    return QUrl(src)


def _fetch_bytes(src: str) -> bytes:
    if os.path.isfile(src):
        with open(src, "rb") as fh:
            return fh.read()
    import requests

    resp = requests.get(src, timeout=60)
    resp.raise_for_status()
    return resp.content
