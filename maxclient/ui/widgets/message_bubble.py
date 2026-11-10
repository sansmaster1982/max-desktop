"""Пузырь сообщения: текст, медиа-карточки, голосовое, время и статус.

Сетевую работу (скачать/проиграть) делегируем наружу через колбэки
on_open_media / on_play_voice, чтобы пузырь оставался «глупым» виджетом.
"""
from __future__ import annotations

import os
import math
from collections import OrderedDict
from html import escape
from types import MappingProxyType
from typing import Callable, Optional

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QPixmap
from shiboken6 import isValid

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
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ...core.models import Attach, Message, control_event_caption
from ...core.i18n import normalize_language, tr
from ...core.message_presentation import normalized_button_rows, safe_web_url
from ..message_links import open_web_link, set_literal_message_text
from ..theme import Palette
from ..conversation_actions_menu import ConversationActionsMenu
from ...core.appearance import chat_message_font_pixels
from .common import fmt_duration, fmt_time_hm, human_size
from .chat_list_item import _ElidedLabel
from .line_icon import line_icon
from ..timeline_photos import PhotoPreview, photo_identity

_STATUS = {"pending": "🕓", "sent": "✓", "failed": "⚠", "unknown": "?"}
_ICON = {"PHOTO": "🖼", "VIDEO": "🎬", "VIDEO_MSG": "🎬", "AUDIO": "🎤", "FILE": "📎", "STICKER": "🌟"}


class _MessageText(QLabel):
    """A scrollable message must retain its full wrapped height at large scales."""
    def resizeEvent(self, event):
        super().resizeEvent(event)
        required = self.heightForWidth(event.size().width())
        if required > 0 and self.minimumHeight() != required:
            self.setMinimumHeight(required)
            self.updateGeometry()


class _FilePrimaryButton(QPushButton):
    def sizeHint(self):  # noqa: N802
        # QPushButton measures its own text/icon, not an embedded child layout.
        # Give the filename column room while allowing the bubble to shrink.
        return QSize(270, 54)

    def minimumSizeHint(self):  # noqa: N802
        return QSize(100, 54)

    def keyPressEvent(self, event):  # noqa: N802
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            if self.isEnabled() and not event.isAutoRepeat():
                self.click()
            event.accept()
            return
        super().keyPressEvent(event)


class _ServiceText(_MessageText):
    def sizeHint(self):  # noqa: N802
        width = min(360, self.fontMetrics().horizontalAdvance(self.text()))
        return QSize(width, max(self.fontMetrics().height(), self.heightForWidth(max(1, width))))


class _ThemedBubbleFrame(QFrame):
    """Resize-aware CSS gradient; search highlighting cannot erase the theme."""
    def __init__(self, colors):
        super().__init__()
        self.colors = colors
        self._spotlight = False
        self.setObjectName('Bubble')
        self._refresh_style()

    def _refresh_style(self):
        from ..bubble_colors import background_css, css_qt_color
        background = background_css(self.colors, self.width(), self.height())
        base = (f'#Bubble {{background:{background};border-radius:10px;}}'
                f'#Bubble QLabel {{background:transparent;color:{css_qt_color(self.colors.text)};}}')
        self.setProperty('searchBaseStyle', base)
        styled = base + ('\n#Bubble {border:2px solid #0085FF;}' if self._spotlight else '')
        if self.styleSheet() != styled:
            self.setStyleSheet(styled)

    def set_spotlight(self, active):
        self._spotlight = bool(active)
        self._refresh_style()

    def resizeEvent(self, event):  # noqa: N802
        super().resizeEvent(event)
        self._refresh_style()


class MessageBubble(QWidget):
    file_action_requested = Signal(str, object, int)  # save/copy_name, exact Message, normalized index

    def __init__(
        self,
        msg: Message,
        accent: str,
        palette: Palette,
        *,
        on_open_media: Optional[Callable[[Message, Attach], None]] = None,
        on_play_voice: Optional[Callable[[Message, Attach], None]] = None,
        on_reaction: Optional[Callable[[Message, Optional[str]], None]] = None,
        on_open_app: Optional[Callable[[Message, int, int, int], None]] = None,
        file_actions: Optional[dict[int, dict]] = None,
        font_scale: str = 'large',
        chat_preset: Optional[str] = None,
        language: str = 'ru',
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.msg = msg
        self._photo_language = normalize_language(language)
        self._file_language = normalize_language(language)
        self._photo_bindings = {}
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        self._on_open_media = on_open_media
        self._on_play_voice = on_play_voice
        self._on_reaction = on_reaction
        self._on_open_app = on_open_app
        self._file_message = msg
        self._file_message_identity = self._message_file_identity(msg)
        self._file_bindings = {}
        self._file_presentations = {}
        self._file_actions = MappingProxyType({
            index: MappingProxyType({
                'download_available': value.get('download_available') is True,
                'deleted': value.get('deleted') is True,
                'file_name': value.get('file_name') if isinstance(value.get('file_name'), str) else None,
            }) for index, value in (file_actions.items() if isinstance(file_actions, dict) else [])
            if type(index) is int and index >= 0 and isinstance(value, dict)
        })
        self._file_menu = None
        self._file_menu_epoch = 0
        self._file_revoked = False
        self._palette = palette
        self._conversation_colors = None

        out = QHBoxLayout(self)
        out.setContentsMargins(14, 2, 14, 2)

        if chat_preset is not None:
            from ..bubble_colors import bubble_colors, css_qt_color
            self._conversation_colors = bubble_colors(chat_preset, palette.name, msg.outgoing)
            bubble = _ThemedBubbleFrame(self._conversation_colors)
            fg = css_qt_color(self._conversation_colors.text)
            sub = css_qt_color(self._conversation_colors.time)
        elif msg.outgoing:
            bg, fg, sub = palette.bubble_out, palette.bubble_out_text, palette.text_dim
        else:
            bg, fg, sub = palette.bubble_in, palette.bubble_in_text, palette.text_dim
        if self._conversation_colors is None:
            bubble = QFrame()
            bubble.setObjectName('Bubble')
            bubble.setStyleSheet(
                f'#Bubble {{ background:{bg}; border-radius:10px; }}'
                f'#Bubble QLabel {{ background:transparent; color:{fg}; }}')
        bubble.setMaximumWidth(520)
        self._link_color = None
        if self._conversation_colors is not None and self._conversation_colors.link_text is not None:
            self._link_color = css_qt_color(self._conversation_colors.link_text)

        v = QVBoxLayout(bubble)
        v.setContentsMargins(12, 8, 12, 6)
        v.setSpacing(5)

        # Display selection is shared with the bound FILE service. A forwarded
        # attachment index must never accidentally select the outer array.
        selected_attaches = msg.display_attaches
        if msg.display_source != 'outer':
            header = _ElidedLabel()
            header.setObjectName('MessageForwardHeader')
            forwarded = msg.forwarded
            name = forwarded.chat_name if forwarded is not None and forwarded.available else None
            caption = f'Переслано: {name}' if isinstance(name, str) and name else 'Пересланное сообщение'
            header.setText(caption)
            header.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
            header.setAccessibleName(caption)
            header.setToolTip('<qt>' + escape(caption) + '</qt>')
            header.setStyleSheet(f'color:{fg};font-size:13px;font-weight:600;')
            v.addWidget(header)
            if msg.display_source == 'unavailable_forward':
                unavailable = _ServiceText('Содержимое пересылки недоступно.')
                unavailable.setTextFormat(Qt.TextFormat.PlainText)
                unavailable.setObjectName('MessageForwardUnavailable')
                unavailable.setWordWrap(True)
                v.addWidget(unavailable)

        controls = [attach for attach in selected_attaches if attach.type == 'CONTROL']
        localized_control = getattr(msg, 'control_display_text', None) if msg.display_source == 'outer' else None
        localized_control = localized_control if isinstance(localized_control, str) and localized_control else None
        control_caption = localized_control
        if controls and control_caption is None:
            first = controls[0]
            control_caption = (first.control_message if getattr(first, 'control_metadata_confirmed', False) is True
                               and getattr(first, 'control_event', None) == 'system'
                               and isinstance(getattr(first, 'control_message', None), str)
                               and first.control_message
                               else control_event_caption(getattr(first, 'control_event', None)))

        for index, attach in enumerate(selected_attaches):
            if attach.type in ('CONTROL', 'INLINE_KEYBOARD'):
                continue  # Service metadata is not a downloadable media card.
            v.addWidget(self._build_attach(attach, fg, sub, index))

        if control_caption is not None:
            service = _ServiceText()
            set_literal_message_text(service, control_caption, link_color=self._link_color)
            service.setWordWrap(True)
            service.setObjectName('ServiceMessage')
            service.setStyleSheet(f'font-size:{chat_message_font_pixels(font_scale)}px;')
            v.addWidget(service)

        # The shared helper requires confirmed CONTROL/system content; it never
        # recognizes a user text key alone. Keep all unrelated outer text intact.
        visible_text = '' if localized_control is not None else msg.display_text
        if (controls and getattr(controls[0], 'control_metadata_confirmed', False) is True
                and visible_text == control_caption):
            visible_text = ''  # Display the same actual service text once.
        if visible_text:
            text = _MessageText()
            set_literal_message_text(text, visible_text, link_color=self._link_color)
            text.setWordWrap(True)
            text.setObjectName('MessageText')
            text.setStyleSheet(f"font-size:{chat_message_font_pixels(font_scale)}px;")
            v.addWidget(text)

        # Native inline actions are below the message, not generic attachments.
        for index, attach in enumerate(selected_attaches):
            if attach.type == 'INLINE_KEYBOARD':
                v.addWidget(self._inline_keyboard(attach, index, fg, sub))

        info = msg.reaction_info if isinstance(msg.reaction_info, dict) else {}
        counters = info.get('counters')
        if isinstance(counters, list):
            valid = [counter for counter in counters if isinstance(counter, dict)
                     and isinstance(counter.get('reaction'), str)
                     and type(counter.get('count')) is int and counter['count'] > 0]
            valid.sort(key=lambda counter: (-counter['count'], counter['reaction'] != info.get('yourReaction')))
            if valid:
                reaction_row = QHBoxLayout()
                reaction_row.setSpacing(4)
                for counter in valid:
                    emoji = counter['reaction']
                    selected = emoji == info.get('yourReaction')
                    chip = QPushButton(f"{emoji} {counter['count']}")
                    chip.setObjectName('MessageReaction')
                    chip.setToolTip('Убрать мою реакцию' if selected else 'Поставить реакцию')
                    chip.setCheckable(True)
                    chip.setChecked(selected)
                    chip.setEnabled(on_reaction is not None and type(msg.id) is int and msg.id > 0)
                    chip_bg = palette.selected if selected else palette.surface_hi
                    chip_fg = palette.text
                    colors = self._conversation_colors
                    if colors is not None:
                        from ..bubble_colors import css_qt_color
                        captured_bg = colors.reaction_my_bg if selected else colors.reaction_bg
                        captured_fg = colors.reaction_my_text if selected else colors.reaction_text
                        if captured_bg is not None and captured_fg is not None:
                            chip_bg, chip_fg = css_qt_color(captured_bg), css_qt_color(captured_fg)
                    chip.setStyleSheet(f'padding:3px 7px;border-radius:9px;background:{chip_bg};color:{chip_fg};')
                    def clicked(_checked=False, value=None if selected else emoji, button=chip, confirmed=selected):
                        # Selection is server-owned; do not leave a speculative
                        # checked state visible while the request is pending.
                        button.setChecked(confirmed)
                        self._react(value)
                    chip.clicked.connect(clicked)
                    reaction_row.addWidget(chip)
                reaction_row.addStretch(1)
                v.addLayout(reaction_row)

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

    def _react(self, emoji: Optional[str]) -> None:
        if self._on_reaction:
            self._on_reaction(self.msg, emoji)

    def retranslate_photo_previews(self, language):
        """Relabel existing photo previews without rebuilding message content."""
        if not isValid(self):
            return
        self._photo_language = normalize_language(language)
        for _attach, preview, _snapshot in self._photo_bindings.values():
            if isValid(preview):
                preview.retranslate(self._photo_language)

    def _build_attach(self, attach: Attach, fg: str, sub: str, attachment_index=None) -> QWidget:
        if attach.type == 'CONTROL':
            label = QLabel('Системное сообщение')
            label.setTextFormat(Qt.TextFormat.PlainText)
            label.setObjectName('ServiceMessage')
            label.setWordWrap(True)
            return label
        if attach.type == 'FILE':
            return self._file_card(attach, attachment_index, fg, sub)
        if self.msg.display_source != 'outer':
            return self._forward_attachment(attach, fg, sub)
        if attach.type == 'SHARE':
            return self._share_card(attach, fg, sub)
        if attach.is_voice:
            return self._voice_widget(attach, fg, sub)
        if attach.is_image and not attach.deleted and attach.local_path and os.path.isfile(attach.local_path):
            return self._image_thumb(attach)
        if attach.is_image:
            preview = PhotoPreview(attach, sub, language=self._photo_language)
            self._photo_bindings[attachment_index] = (
                attach, preview, photo_identity(self.msg, attachment_index, attach))
            preview.clicked.connect(lambda: self._open_media(attach))
            return preview
        return self._media_card(attach, fg, sub)

    def _inline_keyboard(self, attach, index, fg, sub):
        frame = QFrame()
        frame.setObjectName('MessageInlineKeyboard')
        frame.setStyleSheet('QFrame#MessageInlineKeyboard {background:transparent;border:none;}')
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(0, 3, 0, 0)
        layout.setSpacing(4)
        rows = (normalized_button_rows(attach.inline_buttons)
                if attach.inline_metadata_confirmed is True and self.msg.display_source == 'outer' else None)
        if rows is None or not any(rows):
            label = QLabel('Кнопки сообщения недоступны')
            label.setTextFormat(Qt.TextFormat.PlainText)
            label.setWordWrap(True)
            layout.addWidget(label)
            return frame
        for row_index, row in enumerate(rows):
            row_layout = QHBoxLayout()
            row_layout.setSpacing(4)
            for button_index, item in enumerate(row):
                button = QPushButton()
                # A child label keeps server '&' characters literal (no Qt mnemonic).
                caption = QLabel(item['text'])
                caption.setTextFormat(Qt.TextFormat.PlainText)
                caption.setWordWrap(True)
                caption.setAlignment(Qt.AlignmentFlag.AlignCenter)
                caption.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
                inner = QVBoxLayout(button)
                inner.setContentsMargins(10, 8, 10, 8)
                inner.addWidget(caption)
                button.setMinimumHeight(38)
                button.setObjectName('MessageInlineButton')
                button.setAccessibleName(item['text'])
                button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
                button.setStyleSheet(
                    f'QPushButton#MessageInlineButton {{background:{self._palette.surface_hi};border:1px solid {self._palette.border};border-radius:7px;}}'
                    'QPushButton#MessageInlineButton QLabel {border:none;padding:0;background:transparent;}')
                caption.setStyleSheet(f'color:{self._palette.text};')
                action = item['type']
                value = item.get('payload', '') if action == 'CLIPBOARD' else item.get('url')
                enabled = (action == 'CLIPBOARD' and bool(value)
                           or action == 'LINK' and safe_web_url(value) is not None
                           or action == 'OPEN_APP' and callable(self._on_open_app)
                           and attach.inline_open_app_metadata_confirmed is True)
                button.setEnabled(enabled and self.msg.status != 'removed')
                if not enabled:
                    caption.setStyleSheet(f'color:{self._palette.text_dim};')
                    button.setToolTip('Это действие пока недоступно в этом клиенте.')
                elif action == 'CLIPBOARD':
                    button.setToolTip('Копировать текст кнопки')
                elif action == 'OPEN_APP':
                    button.setToolTip('Открыть мини-приложение')
                else:
                    button.setToolTip('Открыть ссылку в браузере')
                def activate(_checked=False, kind=action, data=value, expected=attach, idx=index,
                             expected_message=self.msg, source=rows, control=button,
                             row_idx=row_index, button_idx=button_index):
                    if (not isValid(self) or not control.isVisible() or not control.isEnabled()
                            or self.msg is not expected_message or self.msg.status == 'removed'
                            or self.msg.display_source != 'outer' or expected.inline_metadata_confirmed is not True
                            or normalized_button_rows(expected.inline_buttons) != source
                            or idx >= len(self.msg.display_attaches) or self.msg.display_attaches[idx] is not expected):
                        return
                    if kind == 'CLIPBOARD' and isinstance(data, str) and data:
                        QApplication.clipboard().setText(data)
                    elif kind == 'LINK':
                        open_web_link(data)
                    elif kind == 'OPEN_APP' and callable(self._on_open_app):
                        self._on_open_app(self.msg, idx, row_idx, button_idx)
                button.clicked.connect(activate)
                row_layout.addWidget(button)
            layout.addLayout(row_layout)
        return frame

    def _share_card(self, attach, fg, sub):
        frame = QFrame()
        frame.setObjectName('MessageShare')
        frame.setStyleSheet('QFrame#MessageShare {background:transparent;border:none;}')
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(0, 0, 0, 3)
        if attach.share_metadata_confirmed is not True:
            title = QLabel('Ссылка')
            title.setTextFormat(Qt.TextFormat.PlainText)
            layout.addWidget(title)
            return frame
        for name, value in (('ShareTitle', attach.share_title), ('ShareDescription', attach.share_description),
                            ('ShareUrl', attach.share_url)):
            if not isinstance(value, str) or not value:
                continue
            label = _MessageText()
            label.setObjectName(name)
            if name == 'ShareUrl':
                set_literal_message_text(label, value, link_color=self._link_color)
            else:
                label.setTextFormat(Qt.TextFormat.PlainText)
                label.setText(value)
                label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            label.setWordWrap(True)
            if name == 'ShareTitle':
                label.setStyleSheet('font-weight:600;')
            layout.addWidget(label)
        if layout.count() == 0:
            layout.addWidget(QLabel('Ссылка'))
        return frame

    @staticmethod
    def _message_file_identity(message):
        forwarded = message.forwarded
        return (tuple((type(value), value) for value in
                      (message.chat_id, message.id, message.time_ms, message.display_source)),
                id(forwarded) if forwarded is not None else None,
                tuple((type(value), value) for value in
                      (forwarded.available, forwarded.source_chat_id, forwarded.source_message_id,
                       forwarded.source_time_ms, forwarded.source_sender_id, forwarded.source_type,
                       forwarded.text, forwarded.chat_name)) if forwarded is not None else None)

    @staticmethod
    def _file_identity(attach):
        # Equality guards only. Legacy Attach.file_id is never permission.
        return tuple((type(value), value) for value in
                     (attach.type, attach.file_id, attach.file_name, attach.size,
                      getattr(attach, 'deleted', None),
                      getattr(attach, 'file_metadata_confirmed', None),
                      getattr(attach, 'file_item_type', None)))

    def _file_current(self, index):
        if (not isValid(self) or self._file_revoked or self.msg is not self._file_message
                or self._message_file_identity(self.msg) != self._file_message_identity
                or type(index) is not int or not 0 <= index < len(self.msg.display_attaches)):
            return False
        binding = self._file_bindings.get(index)
        return (binding is not None and self.msg.display_attaches[index] is binding[0]
                and self._file_identity(binding[0]) == binding[1])

    def _file_allowed(self, action, index):
        if not self._file_current(index):
            return False
        capability = self._file_actions.get(index, {})
        if action == 'save':
            return capability.get('download_available') is True and capability.get('deleted') is not True
        if action == 'copy_name':
            return isinstance(capability.get('file_name'), str) and bool(capability['file_name'])
        return False

    def _request_file(self, action, index):
        if self._file_allowed(action, index):
            self.file_action_requested.emit(action, self.msg, index)

    def retranslate_file_actions(self, language):
        """Relabel captured FILE presentation, never recompute permission or IO."""
        if not isValid(self):
            return
        language = normalize_language(language)
        if language != self._file_language:
            self._file_language = language
            self._dismiss_file_menu()
        if not isValid(self):
            return
        for presentation in tuple(self._file_presentations.values()):
            self._render_file_presentation(presentation)

    def _render_file_presentation(self, presentation):
        primary, more, title, details, size, status_source, title_source = presentation
        if not all(isValid(widget) for widget in (primary, more, title, details)):
            return
        primary.setAccessibleName(tr('Сохранить файл', self._file_language))
        more.setAccessibleName(tr('Действия с файлом', self._file_language))
        more.setToolTip(tr('Действия с файлом', self._file_language))
        if title_source is not None:
            caption = tr(title_source, self._file_language)
            title.setText(caption)
            title.setAccessibleName(caption)
            title.setToolTip('<qt>' + escape(caption) + '</qt>')
        metadata = []
        if size is not None:
            metadata.append('0 ' + tr('Б', self._file_language) if size == 0 else
                            human_size(size, self._file_language))
        metadata.append(tr(status_source, self._file_language))
        details.setText(' · '.join(metadata))

    def _file_card(self, attach, index, fg, sub):
        self._file_bindings[index] = (attach, self._file_identity(attach))
        capability = self._file_actions.get(index, {})
        frame = QFrame()
        frame.setObjectName('MessageFileCard')
        frame.setStyleSheet('QFrame#MessageFileCard {background:transparent;border:0;}')
        frame.setMinimumWidth(0)
        layout = QHBoxLayout(frame)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        primary = _FilePrimaryButton()
        primary.setObjectName('MessageFilePrimary')
        primary.setProperty('attachmentIndex', index)
        primary.setMinimumWidth(0)
        primary.setMinimumHeight(54)
        primary.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        primary.setAutoDefault(False)
        primary.setEnabled(self._file_allowed('save', index))
        hover = self._palette.hover
        if self._conversation_colors is not None:
            # Preserve contrast on saturated/gradient message backgrounds.
            hover = 'rgba(0,0,0,0.06)'
        primary.setStyleSheet(f'''
            QPushButton#MessageFilePrimary {{background:transparent;border:0;border-radius:8px;padding:0;}}
            QPushButton#MessageFilePrimary:hover {{background:{hover};}}
            QPushButton#MessageFilePrimary:focus {{border:1px solid #0085FF;}}
        ''')
        body = QHBoxLayout(primary)
        body.setContentsMargins(6, 7, 6, 7)
        body.setSpacing(10)
        icon = QLabel()
        icon.setFixedSize(36, 36)
        icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon.setPixmap(line_icon('attach', '#0085FF', 24).pixmap(24, 24))
        icon.setStyleSheet(f'background:{self._palette.surface_hi};border-radius:8px;')
        body.addWidget(icon)
        column = QVBoxLayout()
        column.setSpacing(3)
        title = _ElidedLabel()
        title.setObjectName('MessageFileName')
        filename = capability.get('file_name')
        title_source = 'Файл' if not (isinstance(filename, str) and filename) and not attach.file_name else None
        title.setText(filename if isinstance(filename, str) and filename else attach.label)
        title.setAccessibleName(filename if isinstance(filename, str) else attach.label)
        title.setToolTip('<qt>' + escape(filename if isinstance(filename, str) else attach.label) + '</qt>')
        title.setStyleSheet(f'color:{fg};font-weight:600;font-size:14px;')
        column.addWidget(title)
        display_size = (attach.size if type(attach.size) in (int, float)
                        and math.isfinite(attach.size) and attach.size >= 0 else None)
        status_source = ('Файл удалён' if capability.get('deleted') is True else
                         'Сохранить файл' if self._file_allowed('save', index) else 'Скачивание недоступно')
        details = _ElidedLabel()
        details.setObjectName('MessageFileMeta')
        secondary = '#66717D' if self._palette.name == 'light' else '#A9B7C6'
        if self._conversation_colors is not None:
            secondary = sub
        details.setStyleSheet(f'color:{secondary};font-size:12px;')
        column.addWidget(details)
        body.addLayout(column, 1)
        for child in (icon, title, details):
            child.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        primary.clicked.connect(lambda _checked=False, bound=index: self._request_file('save', bound))
        layout.addWidget(primary, 1)
        more = QPushButton()
        more.setObjectName('MessageFileMore')
        more.setProperty('attachmentIndex', index)
        more.setFixedSize(30, 32)
        icon_color = self._conversation_colors.text if self._conversation_colors is not None else fg
        more.setIcon(line_icon('more', icon_color, 20))
        more.setAutoDefault(False)
        more.setEnabled(self._file_allowed('save', index) or self._file_allowed('copy_name', index))
        more.setStyleSheet(f'''QPushButton#MessageFileMore {{background:transparent;border:0;border-radius:8px;}}
            QPushButton#MessageFileMore:hover {{background:{hover};}}
            QPushButton#MessageFileMore:focus {{border:1px solid #0085FF;}}''')
        more.clicked.connect(lambda _checked=False, bound=index, anchor=more:
                             self.open_file_menu(bound, anchor.mapToGlobal(anchor.rect().bottomLeft())))
        layout.addWidget(more)
        for widget in (frame, primary):
            widget.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            widget.customContextMenuRequested.connect(lambda point, bound=index, anchor=widget:
                                                     self.open_file_menu(bound, anchor.mapToGlobal(point)))
        presentation = (primary, more, title, details, display_size, status_source, title_source)
        self._file_presentations[index] = presentation
        self._render_file_presentation(presentation)
        return frame

    def _dismiss_file_menu(self):
        self._file_menu_epoch += 1
        menu, self._file_menu = self._file_menu, None
        if menu is not None and isValid(menu):
            menu.cancel()
            if isValid(menu):
                menu.deleteLater()

    def open_file_menu(self, index, global_position=None):
        if not self._file_current(index):
            return None
        self._dismiss_file_menu()
        epoch = self._file_menu_epoch
        menu = ConversationActionsMenu(self, [
            {'action': 'save', 'label': 'Сохранить файл', 'label_source': 'Сохранить файл',
             'available': True, 'write': False},
            {'action': 'copy_name', 'label': 'Копировать имя файла', 'label_source': 'Копировать имя файла',
             'available': True, 'write': False},
        ], self, theme=self._palette.name, language=self._file_language,
            allowed=lambda row: (epoch == self._file_menu_epoch and self._file_allowed(row.get('action'), index)))
        self._file_menu = menu
        menu.requested.connect(lambda action, _value, owner:
                               self._file_menu_request(epoch, action, index, owner))
        menu.popup(global_position if global_position is not None else self.mapToGlobal(self.rect().center()))
        return menu

    def _file_menu_request(self, epoch, action, index, owner):
        if isValid(self) and owner is self and epoch == self._file_menu_epoch:
            self._request_file(action, index)

    def closeEvent(self, event):  # noqa: N802
        self._file_revoked = True
        self._dismiss_file_menu()
        super().closeEvent(event)

    def hideEvent(self, event):  # noqa: N802
        self._dismiss_file_menu()
        super().hideEvent(event)

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

    def _forward_attachment(self, attach: Attach, fg: str, sub: str) -> QWidget:
        """Metadata only until a source-bound non-FILE resolver is available."""
        card = QFrame()
        card.setObjectName('MessageForwardAttachment')
        layout = QVBoxLayout(card)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(3)
        name = _ElidedLabel()
        name.setObjectName('MessageForwardAttachmentName')
        name.setText(attach.label)
        name.setStyleSheet(f'color:{fg};font-weight:600;')
        layout.addWidget(name)
        details = QLabel('Пересланное вложение · открытие пока недоступно')
        details.setTextFormat(Qt.TextFormat.PlainText)
        details.setWordWrap(True)
        details.setStyleSheet(f'color:{fg};font-size:12px;')
        layout.addWidget(details)
        return card

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
        if self.msg.display_source != 'outer' or attach.type in ('FILE', 'CONTROL'):
            return  # FILE has explicit intent; CONTROL is never a media action.
        if self._on_open_media:
            self._on_open_media(self.msg, attach)

    def _play_voice(self, attach: Attach) -> None:
        if self.msg.display_source == 'outer' and self._on_play_voice:
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
