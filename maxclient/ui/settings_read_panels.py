"""Account-owned, read-only folder and story archive views."""
from __future__ import annotations

from datetime import datetime
from PySide6.QtCore import QPointF, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen, QRadialGradient
from PySide6.QtWidgets import QLabel, QListWidget, QListWidgetItem, QPushButton, QSizePolicy, QVBoxLayout, QWidget
from shiboken6 import isValid

from .async_runner import run_async
from .theme import resolve_theme


class _StoryHistoryIllustration(QWidget):
    """Local vector illustration; no archive media or external assets are loaded."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.dark = False
        self.setFixedSize(184, 124)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        center = QPointF(self.width() / 2, self.height() / 2 + 4)
        glow = QRadialGradient(center, 68)
        color = QColor('#C9A9F4' if self.dark else '#E9D5FB')
        color.setAlpha(82 if self.dark else 175)
        glow.setColorAt(0, color)
        color.setAlpha(0)
        glow.setColorAt(1, color)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(glow)
        painter.drawEllipse(QRectF(center.x() - 82, center.y() - 58, 164, 116))
        pen = QPen(QColor('#C9CFDB' if self.dark else '#858C9B'), 3.2,
                   Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawArc(QRectF(center.x() - 24, center.y() - 24, 48, 48), 145 * 16, -300 * 16)
        arrow = QPainterPath(QPointF(center.x() - 26, center.y() - 22))
        arrow.lineTo(center.x() - 26, center.y() - 9)
        arrow.lineTo(center.x() - 13, center.y() - 9)
        painter.drawPath(arrow)
        hands = QPainterPath(QPointF(center.x(), center.y() - 12))
        hands.lineTo(center)
        hands.lineTo(center.x() + 10, center.y() + 5)
        painter.drawPath(hands)


class _StoryCardText(QLabel):
    def heightForWidth(self, width):
        return super().heightForWidth(min(width, self.maximumWidth()))

    def sizeHint(self):
        return QSize(360, self.heightForWidth(360))

    def minimumSizeHint(self):
        return QSize(80, self.fontMetrics().height())


class _StoryEmptyCard(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName('StoryArchiveEmptyCard')
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        self.setAccessibleName('Архив историй пуст')
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMinimumHeight(320)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 28, 24, 34)
        layout.setSpacing(0)
        layout.addStretch(1)
        self.illustration = _StoryHistoryIllustration(self)
        layout.addWidget(self.illustration, 0, Qt.AlignmentFlag.AlignHCenter)
        layout.addSpacing(12)
        self.title = _StoryCardText('Место для воспоминаний')
        self.title.setObjectName('StoryArchiveEmptyTitle')
        title_font = QFont(self.font())
        title_font.setPixelSize(19)
        title_font.setWeight(QFont.Weight.DemiBold)
        self.title.setFont(title_font)
        self.subtitle = _StoryCardText('Здесь можно будет пересмотреть истории и вспомнить, как всё было')
        self.subtitle.setObjectName('StoryArchiveEmptySubtitle')
        subtitle_font = QFont(self.font())
        subtitle_font.setPixelSize(14)
        self.subtitle.setFont(subtitle_font)
        for label in (self.title, self.subtitle):
            label.setTextFormat(Qt.TextFormat.PlainText)
            label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            label.setWordWrap(True)
            label.setMaximumWidth(360)
        layout.addWidget(self.title, 0, Qt.AlignmentFlag.AlignHCenter)
        layout.addSpacing(8)
        layout.addWidget(self.subtitle, 0, Qt.AlignmentFlag.AlignHCenter)
        layout.addStretch(1)

    def sizeHint(self):
        return QSize(460, 360)

    def apply_theme(self, theme):
        if not all(isValid(widget) for widget in (self, self.illustration, self.title, self.subtitle)):
            return
        dark = resolve_theme(theme) == 'dark'
        self.illustration.dark = dark
        self.illustration.update()
        surface, text, secondary = (('#141821', '#EAECEF', '#AAB3C2') if dark
                                    else ('#FFFFFF', '#17191D', '#656C77'))
        self.setStyleSheet(f'''
            #StoryArchiveEmptyCard {{background:{surface};border:0;border-radius:18px;}}
            #StoryArchiveEmptyTitle {{background:transparent;color:{text};font-size:19px;font-weight:600;}}
            #StoryArchiveEmptySubtitle {{background:transparent;color:{secondary};font-size:14px;}}
        ''')


class SettingsReadPanel(QWidget):
    def __init__(self, service, kind, parent=None):
        super().__init__(parent)
        self.service, self.kind = service, kind
        self._busy = False
        self._loaded = False
        self._marker = None
        self._story_ids = set()
        self._confirmed_empty = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.status = QLabel('Нажмите «Обновить», чтобы загрузить данные аккаунта.')
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.items = QListWidget()
        self.items.setMinimumHeight(220)
        layout.addWidget(self.items, 1)
        self.empty_card = _StoryEmptyCard(self) if kind == 'stories' else None
        if self.empty_card is not None:
            self.empty_card.hide()
            layout.addWidget(self.empty_card, 1)
        self.refresh_button = QPushButton('Обновить')
        self.refresh_button.setObjectName('SettingsContentAction')
        self.refresh_button.clicked.connect(self.refresh)
        if kind == 'stories':
            self.refresh_button.setObjectName('StoryArchiveRefresh')
            self.refresh_button.setAccessibleName('Обновить архив историй')
            self.refresh_button.setMinimumHeight(36)
            layout.addWidget(self.refresh_button, 0, Qt.AlignmentFlag.AlignRight)
        else:
            layout.addWidget(self.refresh_button)
        self.more_button = QPushButton('Загрузить ещё')
        self.more_button.setObjectName('SettingsContentAction')
        self.more_button.clicked.connect(lambda: self._load(more=True))
        self.more_button.hide()
        layout.addWidget(self.more_button)
        self.note = QLabel('Создание и изменение папок пока доступны в официальном MAX.' if kind == 'folders'
                           else 'Просмотр медиа и публикация историй пока доступны в официальном MAX.')
        self.note.setWordWrap(True)
        self.note.setTextFormat(Qt.TextFormat.PlainText)
        self.note.setObjectName('SettingsSecondary')
        layout.addWidget(self.note)
        theme = getattr(getattr(service, 'settings', None), 'theme', 'light')
        self.apply_theme(theme if isinstance(theme, str) else 'light')

    def apply_theme(self, theme):
        if self.empty_card is None or not self._ui_alive():
            return
        self.empty_card.apply_theme(theme)
        dark = resolve_theme(theme) == 'dark'
        text, hover = ('#C2CAD8', '#222B39') if dark else ('#656C77', '#E3E6EC')
        self.refresh_button.setStyleSheet(f'''
            QPushButton#StoryArchiveRefresh {{background:transparent;color:{text};border:0;
                border-radius:9px;padding:7px 12px;font-size:13px;}}
            QPushButton#StoryArchiveRefresh:hover {{background:{hover};}}
            QPushButton#StoryArchiveRefresh:focus {{border:1px solid #0085FF;}}
        ''')

    def _set_confirmed_empty(self, confirmed):
        if self.empty_card is None:
            return
        self._confirmed_empty = bool(confirmed)
        self.empty_card.setVisible(self._confirmed_empty)
        self.items.setVisible(not self._confirmed_empty)
        self.status.setVisible(not self._confirmed_empty)
        self.note.setVisible(not self._confirmed_empty)

    def _ui_alive(self):
        widgets = (self.status, self.items, self.refresh_button, self.more_button, self.note)
        if self.empty_card is not None:
            widgets += (self.empty_card, self.empty_card.illustration,
                        self.empty_card.title, self.empty_card.subtitle)
        return isValid(self) and all(isValid(widget) for widget in widgets)

    def ensure_loaded(self):
        if not self._ui_alive():
            return
        if not self._loaded:
            self.refresh()

    def refresh(self):
        self._load(more=False)

    def _load(self, *, more):
        if not self._ui_alive() or self._busy:
            return
        self._busy = True
        self._set_confirmed_empty(False)
        self.refresh_button.setEnabled(False)
        self.more_button.setEnabled(False)
        self.status.setText('Загрузка…')
        if self.kind == 'folders':
            run_async(self.service.get_folders, on_done=self._folders, on_error=self._failed)
        else:
            run_async(self.service.list_own_stories, marker=self._marker if more else 0,
                      on_done=lambda result: self._stories(result, append=more), on_error=self._failed)

    def _folders(self, result):
        if not self._ui_alive():
            return
        if not isinstance(result, dict) or not isinstance(result.get('orderedFolders'), list):
            self._failed('invalid folder result')
            return
        records = result['orderedFolders']
        self.items.clear()
        for record in records:
            if record is None:
                self.items.addItem('Данные папки пока недоступны')
                continue
            if not isinstance(record, dict):
                continue
            title = record.get('title')
            title = title if isinstance(title, str) and title else 'Папка'
            details = []
            if isinstance(record.get('include'), list):
                details.append(f"Выбранных чатов: {len(record['include'])}")
            if isinstance(record.get('favorites'), list):
                details.append(f"Закреплено: {len(record['favorites'])}")
            item = QListWidgetItem(title + ('\n' + ' · '.join(details) if details else ''))
            item.setData(Qt.ItemDataRole.UserRole, record.get('id'))
            self.items.addItem(item)
        self._done()
        missing = result.get('missingFolderIds') or []
        self.status.setText(('Папки не созданы.' if not records else f'Папок: {len(records)}')
                            + (' Некоторые данные недоступны.' if missing else ''))

    def _stories(self, result, *, append=False):
        if not self._ui_alive():
            return
        if not isinstance(result, dict) or not isinstance(result.get('stories'), list):
            self._failed('invalid story result')
            return
        if any(not isinstance(record, dict) or type(record.get('id')) is not int
               for record in result['stories']):
            self._failed('invalid story identity')
            return
        if 'marker' in result and (type(result['marker']) is not int or not -(2**63) <= result['marker'] < 2**63):
            self._failed('invalid story marker')
            return
        if not append:
            self.items.clear()
            self._story_ids.clear()
        for record in result['stories']:
            identity = record.get('id')
            if identity is None or identity in self._story_ids:
                continue
            self._story_ids.add(identity)
            stamp = 'Время не передано'
            if type(record.get('time')) is int and record['time'] > 0:
                try:
                    stamp = datetime.fromtimestamp(record['time'] / 1000).astimezone().strftime('%d.%m.%Y %H:%M')
                except (ValueError, OSError, OverflowError):
                    pass
            details = []
            for key, label in (('viewsCount', 'Просмотров'), ('reactionsCount', 'Реакций')):
                value = record.get(key)
                if type(value) is int and value >= 0:
                    details.append(f'{label}: {value}')
            item = QListWidgetItem('История · ' + stamp + ('\n' + ' · '.join(details) if details else ''))
            item.setData(Qt.ItemDataRole.UserRole, identity)
            self.items.addItem(item)
        marker = result.get('marker')
        self._marker = marker if type(marker) is int and marker > 0 else None
        self.more_button.setVisible(self._marker is not None)
        self._done()
        confirmed_end = 'marker' not in result or result['marker'] == 0
        self._set_confirmed_empty(self.items.count() == 0 and confirmed_end)
        self.status.setText(f'Историй: {self.items.count()}' if self.items.count()
                            else 'Архив загружен не полностью.' if not confirmed_end
                            else 'В архиве пока нет историй.')

    def _done(self):
        if not self._ui_alive():
            return
        self._busy = False
        self._set_confirmed_empty(False)
        self._loaded = True
        self.refresh_button.setEnabled(True)
        self.more_button.setEnabled(True)

    def _failed(self, _error):
        if not self._ui_alive():
            return
        self._busy = False
        self._set_confirmed_empty(False)
        self.refresh_button.setEnabled(True)
        self.more_button.setEnabled(True)
        self.status.setText('Не удалось обновить данные. Попробуйте позже.')
