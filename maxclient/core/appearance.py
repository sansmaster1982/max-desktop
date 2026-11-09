"""Local appearance preferences from the captured official WEB theme model.

These values are local UI settings, never remote account/protocol fields.
Source identity and exact CSS mappings: research/APPEARANCE_PARITY_EVIDENCE.md.
"""
from __future__ import annotations


THEME_SCHEMES = ('system', 'light', 'dark')
CHAT_THEME_IDS = ('space', 'feb23', 'mar8', 'nature', 'neon', 'moscow', 'lebedev', 'simple')
CHAT_FONT_SCALES = ('small', 'medium', 'large', 'xlarge', 'xxlarge', 'xxxlarge', 'ax1', 'ax2')
# The official CSS has a separate body/UI scale. These are the markdown
# MESSAGE base sizes, which the chat font control is meant to adjust.
CHAT_MESSAGE_FONT_PIXELS = (14, 15, 16, 18, 20, 24, 28, 32)
DEFAULT_THEME_SCHEME = 'system'
DEFAULT_CHAT_THEME = 'space'
DEFAULT_CHAT_FONT_SCALE = 'large'


def theme_scheme(value) -> str:
    return value if isinstance(value, str) and value in THEME_SCHEMES else DEFAULT_THEME_SCHEME


def chat_theme(value) -> str:
    return value if isinstance(value, str) and value in CHAT_THEME_IDS else DEFAULT_CHAT_THEME


def chat_font_scale(value) -> str:
    return value if isinstance(value, str) and value in CHAT_FONT_SCALES else DEFAULT_CHAT_FONT_SCALE


def chat_font_index(value) -> int:
    return CHAT_FONT_SCALES.index(chat_font_scale(value))


def chat_message_font_pixels(value) -> int:
    return CHAT_MESSAGE_FONT_PIXELS[chat_font_index(value)]
