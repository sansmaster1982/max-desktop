"""Темы оформления: палитры + генерация QSS.

Современный мессенджер-лук: боковая лента чатов слева, область переписки справа,
скруглённые пузыри сообщений, исходящие — акцентным цветом. Тёмная и светлая
темы, акцент настраивается.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Palette:
    name: str
    window: str
    sidebar: str
    surface: str
    surface_hi: str
    header: str
    text: str
    text_dim: str
    bubble_in: str
    bubble_in_text: str
    border: str
    hover: str
    selected: str
    scrollbar: str
    danger: str


DARK = Palette(
    name="dark",
    window="#0F1117",
    sidebar="#141821",
    surface="#1A1F2B",
    surface_hi="#222838",
    header="#141821",
    text="#EAECEF",
    text_dim="#8A93A6",
    bubble_in="#232A38",
    bubble_in_text="#EAECEF",
    border="#262D3B",
    hover="#1F2532",
    selected="#202A3D",
    scrollbar="#39414f",
    danger="#FF5C5C",
)

LIGHT = Palette(
    name="light",
    window="#FFFFFF",
    sidebar="#F7F8FA",
    surface="#FFFFFF",
    surface_hi="#EEF0F3",
    header="#FFFFFF",
    text="#0F1620",
    text_dim="#717A89",
    bubble_in="#F0F2F5",
    bubble_in_text="#0F1620",
    border="#E6E9EE",
    hover="#F0F2F5",
    selected="#E8F0FE",
    scrollbar="#C7CDD6",
    danger="#E5484D",
)


def palette_for(theme: str) -> Palette:
    return LIGHT if theme == "light" else DARK


def _lighten(hex_color: str, amount: int) -> str:
    h = hex_color.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    r = min(255, r + amount)
    g = min(255, g + amount)
    b = min(255, b + amount)
    return f"#{r:02X}{g:02X}{b:02X}"


def _darken(hex_color: str, amount: int) -> str:
    return _lighten(hex_color, -amount)


def build_stylesheet(theme: str, accent: str) -> str:
    p = palette_for(theme)
    accent_hi = _lighten(accent, 22)
    accent_lo = _darken(accent, 18)
    return f"""
* {{
    font-family: "Segoe UI", "Inter", "SF Pro Display", system-ui, sans-serif;
    font-size: 14px;
    outline: none;
}}
QWidget {{
    background: {p.window};
    color: {p.text};
}}
QToolTip {{
    background: {p.surface_hi};
    color: {p.text};
    border: 1px solid {p.border};
    border-radius: 6px;
    padding: 4px 8px;
}}

/* ── Боковая лента ── */
#Sidebar {{
    background: {p.sidebar};
    border-right: 1px solid {p.border};
}}
#SidebarHeader, #ChatHeader {{
    background: {p.header};
    border-bottom: 1px solid {p.border};
}}
#AppTitle {{
    font-size: 17px;
    font-weight: 700;
    color: {p.text};
}}
#ChatTitle {{ font-size: 15px; font-weight: 600; }}
#ChatSubtitle {{ font-size: 12px; color: {p.text_dim}; }}

/* ── Поиск ── */
QLineEdit, QPlainTextEdit, QTextEdit {{
    background: {p.surface_hi};
    border: 1px solid transparent;
    border-radius: 10px;
    padding: 8px 12px;
    color: {p.text};
    selection-background-color: {accent};
}}
QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus {{
    border: 1px solid {accent};
    background: {p.surface};
}}

/* ── Кнопки ── */
QPushButton {{
    background: {p.surface_hi};
    color: {p.text};
    border: none;
    border-radius: 10px;
    padding: 9px 16px;
    font-weight: 600;
}}
QPushButton:hover {{ background: {_lighten(p.surface_hi, 10)}; }}
QPushButton:pressed {{ background: {_darken(p.surface_hi, 8)}; }}
QPushButton:disabled {{ color: {p.text_dim}; }}

QPushButton#Primary {{
    background: {accent};
    color: #FFFFFF;
}}
QPushButton#Primary:hover {{ background: {accent_hi}; }}
QPushButton#Primary:pressed {{ background: {accent_lo}; }}
QPushButton#Primary:disabled {{ background: {p.surface_hi}; color: {p.text_dim}; }}

QPushButton#Ghost {{ background: transparent; }}
QPushButton#Ghost:hover {{ background: {p.hover}; }}

QPushButton#IconButton {{
    background: transparent;
    border-radius: 20px;
    padding: 0;
    min-width: 40px; min-height: 40px;
    font-size: 18px;
}}
QPushButton#IconButton:hover {{ background: {p.hover}; }}
QPushButton#IconButton:pressed {{ background: {p.selected}; }}

QPushButton#Danger {{ background: transparent; color: {p.danger}; }}
QPushButton#Danger:hover {{ background: {p.hover}; }}

/* ── Списки ── */
QListWidget {{
    background: {p.sidebar};
    border: none;
    padding: 4px;
}}
QListWidget::item {{
    border-radius: 12px;
    margin: 2px 4px;
    padding: 0;
}}
QListWidget::item:hover {{ background: {p.hover}; }}
QListWidget::item:selected {{ background: {p.selected}; }}

/* ── Скролл ── */
QScrollBar:vertical {{
    background: transparent;
    width: 10px;
    margin: 2px;
}}
QScrollBar::handle:vertical {{
    background: {p.scrollbar};
    border-radius: 5px;
    min-height: 30px;
}}
QScrollBar::handle:vertical:hover {{ background: {_lighten(p.scrollbar, 20)}; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: transparent; }}
QScrollBar:horizontal {{ height: 0; }}

/* ── Области ── */
#ChatArea {{ background: {p.window}; }}
#MessageScroll {{ background: {p.window}; border: none; }}
#InputBar {{
    background: {p.header};
    border-top: 1px solid {p.border};
}}
#EmptyHint {{ color: {p.text_dim}; font-size: 15px; }}

/* ── Вкладки/диалоги ── */
QDialog {{ background: {p.window}; }}
QTabBar::tab {{
    background: transparent;
    color: {p.text_dim};
    padding: 8px 18px;
    border-bottom: 2px solid transparent;
    font-weight: 600;
}}
QTabBar::tab:selected {{ color: {p.text}; border-bottom: 2px solid {accent}; }}
QTabWidget::pane {{ border: none; }}

QCheckBox {{ spacing: 8px; }}
QCheckBox::indicator {{
    width: 18px; height: 18px;
    border-radius: 5px;
    border: 1px solid {p.border};
    background: {p.surface_hi};
}}
QCheckBox::indicator:checked {{ background: {accent}; border: 1px solid {accent}; }}

QComboBox {{
    background: {p.surface_hi};
    border-radius: 8px;
    padding: 7px 12px;
    border: 1px solid transparent;
}}
QComboBox:focus {{ border: 1px solid {accent}; }}
QComboBox QAbstractItemView {{
    background: {p.surface};
    border: 1px solid {p.border};
    border-radius: 8px;
    selection-background-color: {p.selected};
    outline: none;
}}

QLabel#FieldLabel {{ color: {p.text_dim}; font-size: 12px; font-weight: 600; }}
QProgressBar {{
    background: {p.surface_hi};
    border-radius: 6px;
    height: 8px;
    text-align: center;
}}
QProgressBar::chunk {{ background: {accent}; border-radius: 6px; }}

QMenu {{
    background: {p.surface};
    border: 1px solid {p.border};
    border-radius: 10px;
    padding: 6px;
}}
QMenu::item {{ padding: 8px 28px 8px 16px; border-radius: 6px; }}
QMenu::item:selected {{ background: {p.selected}; }}
"""
