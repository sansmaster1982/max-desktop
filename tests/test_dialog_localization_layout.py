"""Offscreen dialog layouts and raw drafts, using synthetic services only."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from PySide6.QtCore import QPoint, QRect
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QApplication, QAbstractButton, QLabel, QLineEdit, QListWidget,
    QPushButton, QWidget,
)

from maxclient.core.i18n import SUPPORTED_LANGUAGES
from maxclient.ui.contact_create_dialog import ContactCreateDialog
from maxclient.ui.phone_lookup_dialog import PhoneLookupDialog
from maxclient.ui.contacts_dialog import ContactsDialog
from maxclient.ui.settings_dialog import (
    Change2FADialog, Enable2FADialog, RecoveryEmailDialog,
)
from maxclient.ui.theme import build_stylesheet
from qt_test_lifecycle import capture_qt_state, restore_qt_state
from test_desktop_shell import _load_offscreen_font


class _LayoutService:
    """Only local list/context reads exist; no account or transport is created."""

    def __init__(self):
        self.my_id = 101
        self.context = (101, object(), object())
        self.list_contacts = Mock(return_value=[])

    def search_context(self):
        return self.context

    def _history_read_context(self):
        return self.context

    def _require_history_context(self, context):
        if context is not self.context:
            raise ValueError('Synthetic context changed')
        return context[1]


class DialogLocalizationLayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        # Also restore the globals when this module is run without the shared
        # QtLifecycleTextResult. Its outer cleanup remains compatible with this.
        state = capture_qt_state()
        self.addCleanup(restore_qt_state, state)
        _load_offscreen_font(self.app)
        self.app.setFont(QFont('Segoe UI', 10))
        self.app.setStyle('Fusion')
        self.app.setStyleSheet(build_stylesheet('light', '#0085FF'))
        self.service = _LayoutService()
        self.parent = QWidget()
        self.parent.settings = SimpleNamespace(ui_language='ru')
        self.runners = []
        for module in ('contact_create_dialog', 'phone_lookup_dialog',
                       'contacts_dialog', 'settings_dialog'):
            runner = patch('maxclient.ui.' + module + '.run_async')
            self.runners.append(runner.start())
            self.addCleanup(runner.stop)
        for name in ('socket.create_connection', 'socket.getaddrinfo',
                     'socket.socket.connect', 'socket.socket.connect_ex'):
            blocker = patch(name, side_effect=AssertionError('Network forbidden'))
            blocker.start()
            self.addCleanup(blocker.stop)

    def _settle(self, dialog):
        dialog.layout().activate()
        self.app.processEvents()
        dialog.layout().activate()
        self.app.processEvents()

    def _assert_layout(self, dialog, language):
        client = dialog.rect()
        buttons = [button for button in dialog.findChildren(QPushButton)
                   if button.isVisibleTo(dialog)]
        self.assertTrue(buttons, (type(dialog).__name__, language))
        for button in buttons:
            with self.subTest(button=button.text()):
                hint = button.sizeHint()
                self.assertGreaterEqual(button.width(), hint.width())
                self.assertGreaterEqual(button.height(), hint.height())
        # Compare widget geometry with its own dialog, rather than desktop/DPI
        # coordinates or an arbitrary fixed width. Hidden stages are excluded.
        controls = (QAbstractButton, QLabel, QLineEdit, QListWidget)
        for widget in dialog.findChildren(QWidget):
            if isinstance(widget, controls) and widget.isVisibleTo(dialog):
                bounds = QRect(widget.mapTo(dialog, QPoint(0, 0)), widget.size())
                self.assertTrue(client.contains(bounds),
                                (type(dialog).__name__, language,
                                 type(widget).__name__, bounds, client))

    def _exercise(self, kind, language):
        dialog = kind(self.service, self.parent)
        raw_draft = {}
        for index, field in enumerate(dialog.findChildren(QLineEdit)):
            # Deliberately include whitespace, markup-looking text and a source
            # translation key. These are input data, not localization strings.
            value = ('+79990000000' if field is getattr(dialog, 'phone', None)
                     else f'  Контакт найден <raw> {{draft}} {index}  ')
            field.setText(value)
            raw_draft[field] = field.text()
        reads = self.service.list_contacts.call_count
        dialog.show()
        self._settle(dialog)
        retranslate = getattr(dialog, 'set_language', dialog.retranslate)
        for current in (language, 'ru', language):
            self.parent.settings.ui_language = current
            retranslate(current)
            self._settle(dialog)
            self._assert_layout(dialog, current)
            self.assertEqual({field: field.text() for field in raw_draft}, raw_draft)
        self.assertEqual(self.service.list_contacts.call_count, reads)
        for runner in self.runners:
            runner.assert_not_called()
        dialog.close()


def _layout_case(kind, language):
    def test(self):
        self._exercise(kind, language)
    test.__doc__ = f'{kind.__name__}: {language}, layout and raw draft preserved.'
    return test


for _kind in (ContactCreateDialog, PhoneLookupDialog, ContactsDialog,
              Change2FADialog, Enable2FADialog, RecoveryEmailDialog):
    for _language in SUPPORTED_LANGUAGES:
        setattr(DialogLocalizationLayoutTests,
                f'test_{_kind.__name__}_{_language}_layout_and_draft',
                _layout_case(_kind, _language))


if __name__ == '__main__':
    unittest.main()
