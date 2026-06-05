"""Пузырь сообщения: текст, медиа-карточки, голосовое, время и статус.

Сетевую работу (скачать/проиграть) делегируем наружу через колбэки
on_open_media / on_play_voice, чтобы пузырь оставался «глупым» виджетом.
"""
from __future__ import annotations

import os
from collections import OrderedDict
from typing import Callable, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QPixmap

# LRU-кэш отмасштабированных миниатюр: одно фото не декодируем повторно.
_THUMB_CACHE: "OrderedDict[str, QPixmap]" = OrderedDict()
_THUMB_MAX = 200


def _cached_thumb(path: str) -> Optional[QPixmap]:
    pm = _THUMB_CACHE.get(path)
    if pm is not None:
        _THUMB_CACHE.move_to_end(path)
        return pm
    src = QPixmap(path)
    if src.isNull():
        return None
    pm = src.scaled(
        300, 300, Qt.AspectRatioMode.KeepAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    )
    _THUMB_CACHE[path] = pm
    if len(_THUMB_CACHE) > _THUMB_MAX:
        _THUMB_CACHE.popitem(last=False)
    return pm
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from ...core.models import Attach, Message
from ..theme import Palette
from .common import fmt_duration, fmt_time_hm, human_size

_STATUS = {"pending": "🕓", "sent": "✓", "failed": "⚠"}
_ICON = {"PHOTO": "🖼", "VIDEO": "🎬", "VIDEO_MSG": "🎬", "AUDIO": "🎤", "FILE": "📎", "STICKER": "🌟"}


class MessageBubble(QWidget):
    def __init__(
        self,
        msg: Message,
        accent: str,
        palette: Palette,
        *,
        on_open_media: Optional[Callable[[Message, Attach], None]] = None,
        on_play_voice: Optional[Callable[[Message, Attach], None]] = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.msg = msg
        self._on_open_media = on_open_media
        self._on_play_voice = on_play_voice

        out = QHBoxLayout(self)
        out.setContentsMargins(14, 2, 14, 2)

        bubble = QFrame()
        bubble.setObjectName("Bubble")
        bubble.setMaximumWidth(520)
        if msg.outgoing:
            bg, fg, sub = accent, "#FFFFFF", "rgba(255,255,255,0.78)"
        else:
            bg, fg, sub = palette.bubble_in, palette.bubble_in_text, palette.text_dim
        bubble.setStyleSheet(
            f"#Bubble {{ background:{bg}; border-radius:16px; }}"
            f"#Bubble QLabel {{ background:transparent; color:{fg}; }}"
        )

        v = QVBoxLayout(bubble)
        v.setContentsMargins(12, 8, 12, 6)
        v.setSpacing(5)

        for attach in msg.attaches:
            v.addWidget(self._build_attach(attach, fg, sub))

        if msg.text:
            text = QLabel(msg.text)
            text.setWordWrap(True)
            text.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            text.setStyleSheet("font-size:14.5px;")
            v.addWidget(text)

        footer = QLabel(self._footer_text())
        footer.setAlignment(Qt.AlignmentFlag.AlignRight)
        footer.setStyleSheet(f"color:{sub}; font-size:11px;")
        v.addWidget(footer)

        if msg.outgoing:
            out.addStretch(1)
            out.addWidget(bubble)
        else:
            out.addWidget(bubble)
            out.addStretch(1)

    def _footer_text(self) -> str:
        t = fmt_time_hm(self.msg.time_ms)
        if self.msg.outgoing:
            return f"{t}  {_STATUS.get(self.msg.status, '')}"
        return t

    def _build_attach(self, attach: Attach, fg: str, sub: str) -> QWidget:
        if attach.is_voice:
            return self._voice_widget(attach, fg, sub)
        if attach.is_image and attach.local_path and os.path.isfile(attach.local_path):
            return self._image_thumb(attach)
        return self._media_card(attach, fg, sub)

    def _image_thumb(self, attach: Attach) -> QWidget:
        lbl = _ClickableLabel()
        pix = _cached_thumb(attach.local_path)
        if pix is not None:
            lbl.setPixmap(pix)
        else:
            lbl.setText("🖼 Фото")
        lbl.setStyleSheet("border-radius:12px;")
        lbl.clicked.connect(lambda: self._open_media(attach))
        return lbl

    def _media_card(self, attach: Attach, fg: str, sub: str) -> QWidget:
        card = _ClickableFrame()
        card.setStyleSheet("border-radius:10px;")
        lay = QHBoxLayout(card)
        lay.setContentsMargins(6, 6, 10, 6)
        lay.setSpacing(10)

        icon = QLabel(_ICON.get(attach.type, "📎"))
        icon.setStyleSheet("font-size:24px;")
        lay.addWidget(icon)

        col = QVBoxLayout()
        col.setSpacing(1)
        name = QLabel(attach.label)
        name.setStyleSheet(f"color:{fg}; font-weight:600;")
        col.addWidget(name)
        meta = []
        if attach.size:
            meta.append(human_size(attach.size))
        meta.append("нажмите, чтобы открыть")
        sub_lbl = QLabel(" · ".join(meta))
        sub_lbl.setStyleSheet(f"color:{sub}; font-size:12px;")
        col.addWidget(sub_lbl)
        lay.addLayout(col, 1)

        card.clicked.connect(lambda: self._open_media(attach))
        return card

    def _voice_widget(self, attach: Attach, fg: str, sub: str) -> QWidget:
        card = _ClickableFrame()
        lay = QHBoxLayout(card)
        lay.setContentsMargins(4, 4, 10, 4)
        lay.setSpacing(10)
        play = QLabel("▶")
        play.setFixedSize(36, 36)
        play.setAlignment(Qt.AlignmentFlag.AlignCenter)
        play.setStyleSheet(
            "background: rgba(255,255,255,0.18); border-radius:18px; font-size:15px;"
        )
        lay.addWidget(play)
        info = QLabel(f"Голосовое · {fmt_duration(attach.duration_ms)}")
        info.setStyleSheet(f"color:{fg};")
        lay.addWidget(info, 1)
        card.clicked.connect(lambda: self._play_voice(attach))
        return card

    def _open_media(self, attach: Attach) -> None:
        if self._on_open_media:
            self._on_open_media(self.msg, attach)

    def _play_voice(self, attach: Attach) -> None:
        if self._on_play_voice:
            self._on_play_voice(self.msg, attach)


class _ClickableLabel(QLabel):
    clicked = Signal()

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)


class _ClickableFrame(QFrame):
    clicked = Signal()

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)
