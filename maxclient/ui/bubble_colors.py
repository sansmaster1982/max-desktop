"""Captured official bubble tokens and CSS-angle geometry shared by Qt views.

Data provenance and regeneration: research/BUBBLE_COLORS_20261103.md.
CSS #RRGGBBAA is converted explicitly; QColor's #AARRGGBB is not used.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
import re

from PySide6.QtCore import QPointF, QRectF
from PySide6.QtGui import QBrush, QColor, QLinearGradient

from ..core.appearance import chat_theme


BUBBLE_GRADIENT_ANGLE = 239.0
BUBBLE_GRADIENT_STOPS = (0.0, 0.5, 1.0)
_CSS_HEX = re.compile(r'#[0-9a-fA-F]{3}(?:[0-9a-fA-F]|[0-9a-fA-F]{3}|[0-9a-fA-F]{5})?\Z')


@dataclass(frozen=True)
class BubbleColors:
    stops: tuple[str, str, str]
    text: str
    time: str
    # "others" is the ordinary unselected reaction; "my" is own/selected.
    reaction_bg: str | None = None
    reaction_text: str | None = None
    reaction_my_bg: str | None = None
    reaction_my_text: str | None = None
    link_text: str | None = None


# BEGIN CAPTURED BUBBLE TOKENS
_TOKENS = {
    ('space', 'light', 'incoming'): (('#fff', '#fff', '#fff'), '#060708', '#06070885', '#0f8ec214', '#0784b8', '#0f8ec2', '#fff', '#0784b8'),
    ('space', 'light', 'outgoing'): (('#e9fdff', '#e9fdff', '#e9fdff'), '#011c29', '#0784b8', '#0f8ec21f', '#0784b8', '#0f8ec2', '#fff', '#0784b8'),
    ('space', 'dark', 'incoming'): (('#3b3f5e', '#373a4e', '#32343d'), '#fff', '#ffffffa3', '#ffffff29', '#fff', '#007aff', '#fff', '#409cff'),
    ('space', 'dark', 'outgoing'): (('#8849b4', '#735acd', '#5d6ae5'), '#fff', '#ffffffa3', '#ffffff29', '#fff', '#fff', '#5d6ae5', '#fff'),
    ('feb23', 'light', 'incoming'): (('#fff', '#fff', '#fff'), '#060808', '#06080885', '#148a8e14', '#0d7f85', '#148a8e', '#fff', '#0d7f85'),
    ('feb23', 'light', 'outgoing'): (('#e0fff9', '#e0fff9', '#e0fff9'), '#01191f', '#0d7f85', '#148a8e1f', '#0d7f85', '#148a8e', '#fff', '#0d7f85'),
    ('feb23', 'dark', 'incoming'): (('#232b2b', '#212a2b', '#182926'), '#fff', '#ffffffa3', '#ffffff29', '#fff', '#2e86a3', '#fff', '#148a8e'),
    ('feb23', 'dark', 'outgoing'): (('#429696', '#428896', '#5498ba'), '#fff', '#ffffffa3', '#ffffff29', '#fff', '#fff', '#2e86a3', '#fff'),
    ('mar8', 'light', 'incoming'): (('#fff', '#fff', '#fff'), '#080607', '#08060785', '#d0538f14', '#c74883', '#d0538f', '#fff', '#c74883'),
    ('mar8', 'light', 'outgoing'): (('#f6e0ff', '#f6e0ff', '#f6e0ff'), '#1f011e', '#c74883', '#d0538f1f', '#c74883', '#d0538f', '#fff', '#c74883'),
    ('mar8', 'dark', 'incoming'): (('#2e272d', '#382436', '#301b2f'), '#fff', '#ffffffa3', '#ffffff29', '#fff', '#cd3782', '#fff', '#c74883'),
    ('mar8', 'dark', 'outgoing'): (('#dc655e', '#d2606c', '#c05386'), '#fff', '#ffffffa3', '#ffffff29', '#fff', '#fff', '#cd3782', '#fff'),
    ('nature', 'light', 'incoming'): (('#fff', '#fff', '#fff'), '#060806', '#06080685', '#2da33414', '#25992e', '#2da334', '#fff', '#25992e'),
    ('nature', 'light', 'outgoing'): (('#e2ffc9', '#e2ffc9', '#e2ffc9'), '#011f09', '#008c08', '#2da3341f', '#25992e', '#2da334', '#fff', '#008c08'),
    ('nature', 'dark', 'incoming'): (('#1c2923', '#1c2923', '#1c2923'), '#fff', '#ffffffa3', '#ffffff29', '#fff', '#2da334', '#fff', '#fff'),
    ('nature', 'dark', 'outgoing'): (('#195939', '#175033', '#14472e'), '#fff', '#ffffffa3', '#ffffff29', '#fff', '#fff', '#195939', '#fff'),
    ('neon', 'light', 'incoming'): (('#fff', '#fff', '#fff'), '#060608', '#06060885', '#4930c714', '#6140f5', '#4930c7', '#fff', '#6140f5'),
    ('neon', 'light', 'outgoing'): (('#dae0ff', '#dae0ff', '#dae0ff'), '#07011f', '#4930c7', '#4930c71f', '#4930c7', '#4930c7', '#fff', '#4930c7'),
    ('neon', 'dark', 'incoming'): (('#32343d', '#32343d', '#32343d'), '#fff', '#ffffffa3', '#ffffff29', '#fff', '#6a4dff', '#fff', '#fff'),
    ('neon', 'dark', 'outgoing'): (('#e56b53', '#8d33cc', '#394ae5'), '#fff', '#ffffffa3', '#ffffff29', '#fff', '#fff', '#6a4dff', '#fff'),
    ('moscow', 'light', 'incoming'): (('#fff', '#fff', '#fff'), '#080606', '#08060685', '#d9363614', '#cf2e2b', '#d93636', '#fff', '#cf2e2b'),
    ('moscow', 'light', 'outgoing'): (('#bf3f3f', '#bf3f3f', '#bf3f3f'), '#fff', '#ffffffa3', '#ffffff29', '#fff', '#fff', '#cf2e2b', '#fff'),
    ('moscow', 'dark', 'incoming'): (('#1c2840', '#1c2840', '#1c2840'), '#fff', '#ffffffa3', '#ffffff29', '#fff', '#a63a3a', '#fff', '#cf2e2b'),
    ('moscow', 'dark', 'outgoing'): (('#9d2f40', '#9d2f40', '#c63b3b'), '#fff', '#ffffffa3', '#ffffff29', '#fff', '#fff', '#a63a3a', '#fff'),
    ('lebedev', 'light', 'incoming'): (('#fff', '#fff', '#fff'), '#080606', '#08060685', '#de675a14', '#d45e4e', '#de675a', '#fff', '#d45e4e'),
    ('lebedev', 'light', 'outgoing'): (('#fbe1d7', '#fbe1d7', '#fbe1d7'), '#1f0401', '#e88c80', '#de675a1f', '#d45e4e', '#de675a', '#fff', '#d45e4e'),
    ('lebedev', 'dark', 'incoming'): (('#302c2c', '#302c2c', '#362b2b'), '#fff', '#ffffffa3', '#ffffff29', '#fff', '#bb483f', '#fff', '#d45e4e'),
    ('lebedev', 'dark', 'outgoing'): (('#d19379', '#b87466', '#9f5452'), '#fff', '#ffffffa3', '#ffffff29', '#fff', '#fff', '#bb483f', '#fff'),
    ('simple', 'light', 'incoming'): (('#f2f2f2', '#f2f2f2', '#f2f2f2'), '#060708', '#06070885', '#007aff29', '#0072f5', '#007aff', '#fff', '#0072f5'),
    ('simple', 'light', 'outgoing'): (('#17a8e5', '#0b8ce8', '#0070eb'), '#fff', '#ffffffa3', '#ffffff29', '#fff', '#fff', '#0072f5', '#fff'),
    ('simple', 'dark', 'incoming'): (('#202022', '#202022', '#202022'), '#fff', '#ffffffa3', '#ffffff29', '#fff', '#007aff', '#fff', '#0072f5'),
    ('simple', 'dark', 'outgoing'): (('#4429cc', '#2251e5', '#08c'), '#fff', '#ffffffa3', '#ffffff29', '#fff', '#fff', '#0072f5', '#fff'),
}
# END CAPTURED BUBBLE TOKENS


def bubble_colors(preset, scheme, outgoing) -> BubbleColors:
    """Return immutable captured values; unknown preset follows space default."""
    # Local import avoids a theme/helper initialization cycle in consumers.
    from .theme import resolve_theme

    variant = 'outgoing' if outgoing else 'incoming'
    values = _TOKENS[(chat_theme(preset), resolve_theme(scheme), variant)]
    return BubbleColors(*values)


def _rgba(value: str) -> tuple[int, int, int, int]:
    if not isinstance(value, str) or _CSS_HEX.fullmatch(value) is None:
        raise ValueError('A CSS hexadecimal color is required')
    digits = value[1:]
    if len(digits) in (3, 4):
        digits = ''.join(char * 2 for char in digits)
    if len(digits) == 6:
        digits += 'ff'
    return tuple(int(digits[index:index + 2], 16) for index in (0, 2, 4, 6))


def css_qt_color(value: str) -> str:
    """Convert only literal CSS hex to safe Qt QSS rgba(), with byte alpha."""
    red, green, blue, alpha = _rgba(value)
    return f'rgba({red}, {green}, {blue}, {alpha})'


def _extent(value) -> float:
    """Unlaid-out widgets may have zero size; never emit NaN/inf into QSS."""
    try:
        number = float(value) if not isinstance(value, bool) else 0.0
    except (TypeError, ValueError, OverflowError):
        return 1.0
    return number if math.isfinite(number) and number > 0 else 1.0


def _endpoints(width, height) -> tuple[QPointF, QPointF]:
    width, height = _extent(width), _extent(height)
    angle = math.radians(BUBBLE_GRADIENT_ANGLE)
    dx, dy = math.sin(angle), -math.cos(angle)
    # CSS gradient line reaches the perpendicular projections of the box's
    # corners. A fixed normalized diagonal distorts this angle after resizing.
    half_length = (width * abs(dx) + height * abs(dy)) / 2
    center_x, center_y = width / 2, height / 2
    return (QPointF(center_x - dx * half_length, center_y - dy * half_length),
            QPointF(center_x + dx * half_length, center_y + dy * half_length))


def background_css(colors: BubbleColors, width, height) -> str:
    """Qt QSS gradient value with CSS 239-degree geometry for this box."""
    width, height = _extent(width), _extent(height)
    # QSS interpolates in normalized coordinates BEFORE its nonuniform box
    # transform. Dividing logical endpoints by w/h gets the angle visibly
    # wrong. Choose d so d / dot(d,d) equals the CSS scalar-field slope in
    # normalized coordinates; scale w/h first to avoid squared-size overflow.
    scale = max(width, height)
    w, h = width / scale, height / scale
    angle = math.radians(BUBBLE_GRADIENT_ANGLE)
    dx, dy = math.sin(angle), -math.cos(angle)
    length = w * abs(dx) + h * abs(dy)
    norm = (w * dx) ** 2 + (h * dy) ** 2
    vx, vy = w * dx * length / norm, h * dy * length / norm
    coordinates = (.5 - vx / 2, .5 - vy / 2, .5 + vx / 2, .5 + vy / 2)
    parts = [f'{name}:{value:.12g}' for name, value in zip(('x1', 'y1', 'x2', 'y2'), coordinates)]
    parts.extend(f'stop:{position:g} {css_qt_color(color)}'
                 for position, color in zip(BUBBLE_GRADIENT_STOPS, colors.stops))
    return 'qlineargradient(' + ', '.join(parts) + ')'


def background_brush(colors: BubbleColors, rect: QRectF) -> QBrush:
    """Painter equivalent of background_css, using logical rectangle pixels."""
    start, end = _endpoints(rect.width(), rect.height())
    origin = QPointF(rect.x(), rect.y())
    gradient = QLinearGradient(origin + start, origin + end)
    for position, color in zip(BUBBLE_GRADIENT_STOPS, colors.stops):
        gradient.setColorAt(position, QColor(*_rgba(color)))
    return QBrush(gradient)
