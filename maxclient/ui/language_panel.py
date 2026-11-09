"""Local language selection. No service, account or transport access."""
from PySide6.QtCore import Qt, Signal, QRectF
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QSizePolicy, QVBoxLayout, QWidget
from shiboken6 import isValid

from ..core.i18n import SUPPORTED_LANGUAGES, language_rows, normalize_language, tr
from .theme import resolve_theme
from .widgets.security_setting_row import forward_wheel


class _LanguageRadio(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(24, 24)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.checked = False
        self.dark = False

    def paintEvent(self, _event):  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        color = '#0085FF' if self.checked else '#89909A' if self.dark else '#B7BDC5'
        painter.setPen(QPen(QColor(color), 2))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawEllipse(QRectF(2, 2, 20, 20))
        if self.checked:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(color))
            painter.drawEllipse(QRectF(7, 7, 10, 10))
        painter.end()


class _LanguageRow(QPushButton):
    def __init__(self, code, title, subtitle, parent=None):
        super().__init__(parent)
        self.code = code
        self.setObjectName('LanguageChoiceRow')
        self.setCheckable(True)
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.setMinimumHeight(58)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 8, 16, 8)
        layout.setSpacing(16)
        labels = QVBoxLayout()
        labels.setSpacing(1)
        labels.setContentsMargins(0, 0, 0, 0)
        self.title = QLabel(title)
        self.title.setObjectName('LanguageChoiceTitle')
        self.subtitle = QLabel(subtitle)
        self.subtitle.setObjectName('LanguageChoiceSubtitle')
        for label in (self.title, self.subtitle):
            label.setTextFormat(Qt.TextFormat.PlainText)
            label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            label.setWordWrap(True)
            labels.addWidget(label)
        layout.addLayout(labels, 1)
        self.radio = _LanguageRadio(self)
        layout.addWidget(self.radio)
        self.setAccessibleName(title)
        self.setAccessibleDescription(subtitle)

    def set_selected(self, selected):
        self.setChecked(selected)
        self.radio.checked = selected
        self.radio.update()

    def wheelEvent(self, event):  # noqa: N802
        forward_wheel(self, event)


class LanguagePanel(QWidget):
    language_changed = Signal(str)

    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.settings = settings
        self._language = normalize_language(getattr(settings, 'ui_language', 'ru'))
        self._saving = False
        self._save_failed = False
        self.rows = {}
        self.setMaximumWidth(640)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        self.card = QFrame()
        self.card.setObjectName('LanguageChoiceCard')
        choices = QVBoxLayout(self.card)
        choices.setContentsMargins(0, 5, 0, 5)
        choices.setSpacing(0)
        for code, title, subtitle in language_rows(self._language):
            row = _LanguageRow(code, title, subtitle)
            row.clicked.connect(lambda _checked=False, language=code: self._choose(language))
            choices.addWidget(row)
            self.rows[code] = row
        layout.addWidget(self.card)
        self.footer = self._note('LanguageFooter')
        self.coverage_note = self._note('LanguageCoverage')
        self.error = self._note('LanguageError')
        for label in (self.footer, self.coverage_note, self.error):
            layout.addWidget(label)
        layout.addStretch(1)
        self.retranslate(self._language)
        self.apply_theme(getattr(settings, 'theme', 'light'))

    def _note(self, name):
        label = QLabel()
        label.setObjectName(name)
        label.setTextFormat(Qt.TextFormat.PlainText)
        label.setWordWrap(True)
        label.setContentsMargins(12, 0, 12, 0)
        return label

    def _alive(self):
        return isValid(self) and all(isValid(widget) for widget in (
            self.footer, self.coverage_note, self.error, self.card,
            *(widget for row in self.rows.values() for widget in (row, row.title, row.subtitle, row.radio))))

    def _choose(self, language):
        if not self._alive() or self._saving or language not in SUPPORTED_LANGUAGES:
            return
        previous = normalize_language(getattr(self.settings, 'ui_language', 'ru'))
        if previous == language:
            self._refresh_selection()
            return
        self._saving = True
        self._save_failed = False
        try:
            self.settings.save_ui_language(language)
        except Exception:
            self.settings.ui_language = previous
            self._save_failed = True
        finally:
            self._saving = False
        if not self._alive():
            return
        if self._save_failed:
            self.retranslate(previous)
            return
        self.retranslate(language)
        self.language_changed.emit(language)

    def _refresh_selection(self):
        selected = normalize_language(getattr(self.settings, 'ui_language', 'ru'))
        for code, row in self.rows.items():
            row.set_selected(code == selected)

    def retranslate(self, language):
        """Refresh presentation only; never saves or emits language_changed."""
        if not self._alive():
            return
        self._language = normalize_language(language)
        for code, title, subtitle in language_rows(self._language):
            row = self.rows[code]
            row.title.setText(title)
            row.subtitle.setText(subtitle)
            row.setAccessibleName(title)
            row.setAccessibleDescription(subtitle)
        self.footer.setText(tr('Язык изменится только на этом устройстве', self._language))
        self.coverage_note.setText(tr('Некоторые элементы интерфейса пока остаются на русском языке.', self._language))
        self.coverage_note.setVisible(self._language != 'ru')
        self.error.setText(tr('Не удалось сохранить язык. Попробуйте ещё раз.', self._language))
        self.error.setVisible(self._save_failed)
        self._refresh_selection()

    def apply_theme(self, theme):
        if not self._alive():
            return
        dark = resolve_theme(theme) == 'dark'
        surface, text, secondary, hover = (('#141821', '#EAECEF', '#AAB3C2', '#222B39') if dark
                                           else ('#FFFFFF', '#17191D', '#878C95', '#F4F7FA'))
        self.setStyleSheet(f'''
            #LanguageChoiceCard {{background:{surface};border:0;border-radius:16px;}}
            #LanguageChoiceRow {{background:transparent;border:1px solid transparent;border-radius:12px;padding:0;}}
            #LanguageChoiceRow:hover {{background:{hover};}}
            #LanguageChoiceRow:focus {{border:1px solid #0085FF;}}
            #LanguageChoiceTitle {{background:transparent;color:{text};font-size:16px;font-weight:600;border:0;padding:0;}}
            #LanguageChoiceSubtitle {{background:transparent;color:{secondary};font-size:14px;font-weight:400;border:0;padding:0;}}
            #LanguageFooter, #LanguageCoverage {{background:transparent;color:{secondary};font-size:14px;border:0;padding:0;}}
            #LanguageError {{background:transparent;color:#E44C5B;font-size:14px;border:0;padding:0;}}
        ''')
        for row in self.rows.values():
            row.radio.dark = dark
            row.radio.update()
