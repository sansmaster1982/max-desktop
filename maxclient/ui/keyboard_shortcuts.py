"""Native-observed keyboard reference with explicit implementation boundaries.

Only NAVIGATION_SHORTCUTS are registered here. ChatView installs the local
reply shortcut. Other rows remain a visibly qualified reference.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from PySide6.QtCore import Qt
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QVBoxLayout, QWidget

from ..core.i18n import tr
from .theme import resolve_theme


@dataclass(frozen=True)
class ShortcutSpec:
    identifier: str
    label: str
    sequence: str
    supported: bool = True
    scope: str = ''


NAVIGATION_SHORTCUTS = (
    ShortcutSpec('search_in_chat', 'Поиск по сообщениям внутри чата', 'Ctrl+F',
                 scope='Поиск выполняется в загруженных сообщениях чата.'),
    ShortcutSpec('search_chats', 'Быстрый поиск по чатам', 'Ctrl+K'),
    ShortcutSpec('close_chat', 'Закрыть текущий чат', 'Esc',
                 scope='Черновик и выбранный ответ сохраняются до закрытия приложения.'),
)
MESSAGE_SHORTCUTS = (
    ShortcutSpec('delete_selected', 'Удалить сообщение (в режиме выбора)', 'Ctrl+Backspace', False),
    ShortcutSpec('reply_last', 'Быстрый ответ на последнее сообщение', 'Ctrl+R',
                 scope='Ответ на последнее загруженное сообщение открытого чата. Сообщение не отправляется.'),
    ShortcutSpec('record_voice', 'Начать запись голосового сообщения', 'Ctrl+Shift+U', False),
)
FORMATTING_SHORTCUTS = tuple(
    ShortcutSpec(identifier, label, sequence, False)
    for identifier, label, sequence in (
        ('format_heading', 'Заголовок', 'Ctrl+J'),
        ('format_bold', 'Жирный', 'Ctrl+B'),
        ('format_italic', 'Курсив', 'Ctrl+I'),
        ('format_underline', 'Подчёркнутый', 'Ctrl+U'),
        ('format_strike', 'Зачёркнутый', 'Ctrl+Shift+X'),
        ('format_mono', 'Моноширинный', 'Ctrl+Shift+J'),
        ('format_quote', 'Цитата', 'Ctrl+Shift+I'),
        ('format_link', 'Ссылка', 'Ctrl+Shift+L'),
        ('format_plain', 'Обычный', 'Ctrl+Shift+N'),
    )
)
SHORTCUT_SECTIONS = (
    ('ПОИСК И НАВИГАЦИЯ', NAVIGATION_SHORTCUTS),
    ('РАБОТА С СООБЩЕНИЯМИ', MESSAGE_SHORTCUTS),
    ('ФОРМАТИРОВАНИЕ ТЕКСТА', FORMATTING_SHORTCUTS),
)
UNAVAILABLE_NOTE = 'Эти сочетания пока недоступны в этом клиенте.'


def install_navigation_shortcuts(
        parent: QWidget, *, search_chats: Callable[[], None],
        search_in_chat: Callable[[], None],
        close_chat: Callable[[], None] | None = None) -> tuple[QShortcut, ...]:
    """Install only provided window-local callbacks, without auto-repeat."""
    callbacks = {'search_chats': search_chats, 'search_in_chat': search_in_chat,
                 'close_chat': close_chat}
    shortcuts = []
    for spec in NAVIGATION_SHORTCUTS:
        callback = callbacks[spec.identifier]
        if callback is None:
            continue
        shortcut = QShortcut(QKeySequence(spec.sequence), parent)
        shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
        shortcut.setAutoRepeat(False)
        shortcut.activated.connect(callback)
        shortcuts.append(shortcut)
    return tuple(shortcuts)


class KeyboardReference(QWidget):
    """Observed three-section guide; unavailable actions are visibly qualified."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName('KeyboardReference')
        self.setMaximumWidth(640)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 8, 0, 0)
        layout.setSpacing(0)
        self.cards = []
        self.shortcut_rows = {}
        self.row_widgets = {}
        self._translations = []
        self._specs = {}
        for section_index, (title, specs) in enumerate(SHORTCUT_SECTIONS):
            if section_index:
                layout.addSpacing(32)
            caption = self._translated_label(title, 'KeyboardSectionCaption')
            layout.addWidget(caption)
            layout.addSpacing(12)
            card = QFrame()
            card.setObjectName('KeyboardReferenceCard')
            rows = QVBoxLayout(card)
            rows.setContentsMargins(0, 0, 0, 0)
            rows.setSpacing(0)
            for spec in specs:
                row = QWidget()
                row.setObjectName('KeyboardReferenceRow')
                row.setMinimumHeight(56)
                row.setProperty('available', spec.supported)
                line = QHBoxLayout(row)
                line.setContentsMargins(12, 10, 12, 10)
                line.setSpacing(16)
                label = self._translated_label(spec.label, 'KeyboardReferenceText')
                label.setWordWrap(True)
                label.setMinimumWidth(0)
                label.setProperty('available', spec.supported)
                line.addWidget(label, 1)
                keys = self._label(spec.sequence.replace('+', ' + '), 'KeyboardReferenceKeys')
                keys.setProperty('available', spec.supported)
                keys.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                keys.setAccessibleName(spec.sequence.replace('+', ' + '))
                line.addWidget(keys)
                rows.addWidget(row)
                self.shortcut_rows[spec.identifier] = (label, keys)
                self.row_widgets[spec.identifier] = row
                self._specs[spec.identifier] = spec
            self.cards.append(card)
            layout.addWidget(card)
            if section_index:
                note = self._translated_label(UNAVAILABLE_NOTE, 'KeyboardReferenceNote')
                note.setWordWrap(True)
                layout.addSpacing(8)
                layout.addWidget(note)
        layout.addStretch(1)
        self.card = self.cards[0]  # Existing consumer's first-card accessor.
        self.retranslate('ru')

    @staticmethod
    def _label(text, name):
        label = QLabel(text)
        label.setTextFormat(Qt.TextFormat.PlainText)
        label.setObjectName(name)
        return label

    def _translated_label(self, text, name):
        label = self._label(text, name)
        self._translations.append((label, text))
        return label

    def retranslate(self, language):
        for label, source in self._translations:
            label.setText(tr(source, language))
        for identifier, spec in self._specs.items():
            explanation = tr(spec.scope if spec.supported else UNAVAILABLE_NOTE, language)
            row = self.row_widgets[identifier]
            row.setToolTip(explanation)
            row.setAccessibleDescription(explanation)
            for label in self.shortcut_rows[identifier]:
                label.setToolTip(explanation)
                label.setAccessibleDescription(explanation)

    set_language = retranslate

    def apply_theme(self, theme):
        dark = resolve_theme(theme) == 'dark'
        card = '#141821' if dark else '#FFFFFF'
        text = '#EAECEF' if dark else '#17191D'
        secondary = '#AAB3C2' if dark else '#656C77'
        unavailable = '#8D97A6' if dark else '#7A828E'
        self.setStyleSheet(f'''
            #KeyboardReference, #KeyboardReferenceRow {{background:transparent;border:0;}}
            #KeyboardSectionCaption {{background:transparent;color:{secondary};font-size:12px;padding-left:12px;}}
            #KeyboardReferenceCard {{background:{card};border:0;border-radius:12px;}}
            #KeyboardReferenceText {{background:transparent;color:{text};font-size:14px;font-weight:600;}}
            #KeyboardReferenceKeys {{background:transparent;color:{secondary};border:0;padding:0;font-size:13px;}}
            #KeyboardReferenceText[available="false"], #KeyboardReferenceKeys[available="false"] {{color:{unavailable};}}
            #KeyboardReferenceNote {{background:transparent;color:{secondary};font-size:12px;padding-left:12px;}}
        ''')
