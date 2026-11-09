"""Local appearance samples; no account data, transport, or message actions."""
from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QFrame, QLabel, QPushButton, QScrollArea, QSlider, QStyle, QStyleOptionSlider, QVBoxLayout

from ...core.appearance import chat_message_font_pixels
from ..appearance_tokens import PRESET_LABELS
from ..bubble_colors import background_brush, background_css, bubble_colors, css_qt_color
from ..theme import resolve_theme
from .chat_wallpaper import ChatWallpaper


# Public WEB localization and demo-message order; the native screen shows the
# same three static messages. Never use messages from the current account here.
PREVIEW_MESSAGES = (
    'Выберите тему, чтобы изменить фон и цвет сообщений 🎨',
    'Посмотрите, как с ней будут выглядеть ваши чаты',
    'Меняйте тему в любое время',
)


class AppearanceFontSlider(QSlider):
    """Keep the eight positions visible under Qt stylesheets as in the source."""
    def __init__(self, parent=None):
        super().__init__(Qt.Orientation.Horizontal, parent)

    def paintEvent(self, event):  # noqa: N802
        super().paintEvent(event)
        option = QStyleOptionSlider()
        self.initStyleOption(option)
        style = self.style()
        handle = style.subControlRect(QStyle.ComplexControl.CC_Slider, option, QStyle.SubControl.SC_SliderHandle, self)
        groove = style.subControlRect(QStyle.ComplexControl.CC_Slider, option, QStyle.SubControl.SC_SliderGroove, self)
        span = max(0, self.width() - handle.width())
        painter = QPainter(self)
        for value in range(self.minimum(), self.maximum() + 1):
            if value == self.value():
                continue
            position = QStyle.sliderPositionFromValue(self.minimum(), self.maximum(), value, span, option.upsideDown)
            x = handle.width() // 2 + position
            painter.setPen(QPen(QColor('#0085FF' if value < self.value() else '#92979F'), 1))
            painter.drawLine(x, groove.center().y() - 3, x, groove.center().y() + 3)
        painter.end()


class AppearancePresetStrip(QScrollArea):
    def wheelEvent(self, event):  # noqa: N802
        delta = event.pixelDelta().y() or event.angleDelta().y()
        bar = self.horizontalScrollBar()
        if delta and bar.maximum() > bar.minimum() and not event.modifiers():
            bar.setValue(bar.value() - delta)
            event.accept()
            return
        super().wheelEvent(event)


class _PresetWallpaper(ChatWallpaper):
    selected = False

    def set_selected(self, selected):
        self.selected = bool(selected)
        self.update()

    def paintEvent(self, event):  # noqa: N802
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        incoming_rect = QRectF(8, 11, 72, 24)
        outgoing_rect = QRectF(self.width() - 80, 44, 72, 24)
        scheme = resolve_theme(self.theme)
        painter.setBrush(background_brush(bubble_colors(self.preset, scheme, False), incoming_rect))
        painter.drawRoundedRect(incoming_rect, 7, 7)
        painter.setBrush(background_brush(bubble_colors(self.preset, scheme, True), outgoing_rect))
        painter.drawRoundedRect(outgoing_rect, 7, 7)
        if self.selected:
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(QColor('#0085FF'), 2))
            painter.drawRoundedRect(QRectF(self.rect()).adjusted(1, 1, -1, -1), 8, 8)
        painter.end()


class AppearancePresetButton(QPushButton):
    def __init__(self, preset, theme, parent=None):
        super().__init__(parent)
        self.preset = preset
        self.setObjectName('AppearancePreset')
        self.setCheckable(True)
        self.setFixedSize(124, 126)
        self.setAccessibleName('Фон чата: ' + PRESET_LABELS[preset])
        self.setToolTip(PRESET_LABELS[preset])
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 4, 6, 4)
        layout.setSpacing(4)
        self.preview = _PresetWallpaper(theme, preset=preset, corner_radius=8)
        self.preview.setFixedSize(112, 80)
        self.preview.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        layout.addWidget(self.preview)
        self.label = QLabel(PRESET_LABELS[preset])
        self.label.setTextFormat(Qt.TextFormat.PlainText)
        self.label.setWordWrap(True)
        self.label.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignHCenter)
        self.label.setFixedHeight(34)
        self.label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.label.setObjectName('AppearancePresetName')
        layout.addWidget(self.label)
        self.toggled.connect(self._selection_changed)

    def _selection_changed(self, selected):
        self.preview.set_selected(selected)
        self.label.setProperty('selected', bool(selected))
        self.label.style().unpolish(self.label)
        self.label.style().polish(self.label)
        self.label.update()

    def focusInEvent(self, event):  # noqa: N802
        super().focusInEvent(event)
        parent = self.parentWidget()
        while parent is not None:
            if isinstance(parent, QScrollArea):
                parent.ensureWidgetVisible(self, 12, 0)
                break
            parent = parent.parentWidget()


class _PreviewBubble(QFrame):
    def __init__(self, text, time, *, outgoing=False, parent=None):
        super().__init__(parent)
        self.outgoing = outgoing
        self.setObjectName('AppearanceSampleBubble')
        self.message_label = QLabel(text, self)
        self.message_label.setObjectName('AppearanceSampleText')
        self.message_label.setTextFormat(Qt.TextFormat.PlainText)
        self.message_label.setWordWrap(True)
        self.message_label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        self.meta_label = QLabel(time + ('  ✓✓' if outgoing else ''), self)
        self.meta_label.setTextFormat(Qt.TextFormat.PlainText)
        self.meta_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.reaction_labels = [QLabel(text, self) for text in ('🔥 1', '❤️ 1')] if outgoing else []
        for reaction in self.reaction_labels:
            reaction.setTextFormat(Qt.TextFormat.PlainText)
            reaction.setAlignment(Qt.AlignmentFlag.AlignCenter)

    def set_appearance(self, theme, preset, font_scale):
        self.colors = bubble_colors(preset, resolve_theme(theme), self.outgoing)
        foreground = css_qt_color(self.colors.text)
        self.message_label.setStyleSheet(
            f'background:transparent;color:{foreground};font-size:{chat_message_font_pixels(font_scale)}px;padding:0;border:0;')
        self.meta_label.setStyleSheet(f'background:transparent;color:{css_qt_color(self.colors.time)};font-size:10px;padding:0;border:0;')
        for reaction in self.reaction_labels:
            reaction.setStyleSheet(
                f'background:{css_qt_color(self.colors.reaction_bg)};'
                f'color:{css_qt_color(self.colors.reaction_text)};font-size:13px;padding:0;border:0;border-radius:10px;')
        self.ensurePolished()
        self.message_label.ensurePolished()

    def layout_width(self, width):
        text_width = max(1, width - 20)
        text_height = max(self.message_label.fontMetrics().lineSpacing(), self.message_label.heightForWidth(text_width))
        footer_height = 20 if self.outgoing else 12
        height = 16 + text_height + footer_height
        self.setStyleSheet(
            f'QFrame#AppearanceSampleBubble {{background:{background_css(self.colors, width, height)};border:0;border-radius:10px;}}')
        self.message_label.setGeometry(10, 8, text_width, text_height)
        self.meta_label.setGeometry(max(10, width - 72), 8 + text_height, min(62, text_width), footer_height)
        reaction_width = min(46, max(1, (text_width - 70) // 2))
        for index, reaction in enumerate(self.reaction_labels):
            reaction.setGeometry(10 + index * (reaction_width + 6), 8 + text_height, reaction_width, footer_height)
        return height


class ConversationAppearancePreview(ChatWallpaper):
    """A static three-bubble sample that expands for accessibility font sizes."""
    def __init__(self, theme, preset, font_scale, parent=None):
        super().__init__(theme, parent, preset=preset, corner_radius=12)
        self.setAccessibleName('Пример оформления чата')
        self.setAccessibleDescription('Три демонстрационных сообщения. Переписка аккаунта не используется.')
        self.bubbles = [
            _PreviewBubble(text, time, outgoing=index == 1, parent=self)
            for index, (text, time) in enumerate(zip(PREVIEW_MESSAGES, ('12:48', '12:51', '12:54')))
        ]
        self._font_scale = font_scale
        self.setMinimumHeight(230)
        self.apply_appearance(theme, preset, font_scale)

    def apply_appearance(self, theme, preset, font_scale):
        self._font_scale = font_scale
        self.set_appearance(theme, preset)
        for bubble in self.bubbles:
            bubble.set_appearance(theme, preset, font_scale)
        self._layout_bubbles()

    def _layout_bubbles(self):
        available = max(40, self.width() - 24)
        top = 12
        for bubble, maximum in zip(self.bubbles, (480, 400, 295)):
            width = min(available, maximum)
            height = bubble.layout_width(width)
            left = self.width() - 12 - width if bubble.outgoing else 12
            bubble.setGeometry(left, top, width, height)
            top += height + 8
        required = max(230, top + 4)
        if self.minimumHeight() != required:
            self.setMinimumHeight(required)
            self.updateGeometry()

    def resizeEvent(self, event):  # noqa: N802
        super().resizeEvent(event)
        self._layout_bubbles()
