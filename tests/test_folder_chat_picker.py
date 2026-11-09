"""Synthetic local chooser only: no service, network or account fixtures."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import unittest
from unittest.mock import Mock

from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLabel, QToolButton, QVBoxLayout, QWidget

from maxclient.ui.theme import build_stylesheet
from maxclient.ui.widgets.folder_chat_picker import CATEGORIES, FolderChatPicker
from test_desktop_shell import _load_offscreen_font


class FolderChatPickerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        _load_offscreen_font(cls.app)
        cls.app.setStyle('Fusion')

    def setUp(self):
        self.app.setStyleSheet(build_stylesheet('light', '#0085FF'))
        self.host = QWidget()
        layout = QVBoxLayout(self.host)
        self.picker = FolderChatPicker()
        layout.addWidget(self.picker)
        layout.addStretch()
        self.host.resize(400, 420)
        self.picker.set_ids({0: 'Saved synthetic', -42: 'Alpha', 81: 'Beta'}, [-42])
        self.picker.set_filters([0, 11, 14])
        self.changed = Mock()
        self.picker.changed.connect(self.changed)
        self.host.show()
        self.app.processEvents()

    def tearDown(self):
        self.host.close()
        self.app.processEvents()

    def open(self):
        dialog = self.picker.open_dialog()
        self.assertIsNotNone(dialog)
        self.app.processEvents()
        return dialog

    def row(self, dialog, kind, identity):
        return next(dialog.items.item(index) for index in range(dialog.items.count())
                    if dialog.items.item(index).data(Qt.ItemDataRole.UserRole) == (kind, identity))

    def toggle(self, dialog, kind, identity):
        dialog._toggle(self.row(dialog, kind, identity))

    def chip(self, kind, identity):
        return next(self.picker._flow.itemAt(index).widget() for index in range(self.picker._flow.count())
                    if self.picker._flow.itemAt(index).widget().property('selectionKind') == kind
                    and self.picker._flow.itemAt(index).widget().property('selectionId') == identity)

    def test_population_both_call_shapes_deduplicate_without_signals(self):
        self.picker.set_ids({0: 'Zero', -42: 'Same'}, [0, -42, 0, 999])
        self.assertEqual(self.picker.values(), [0, -42, 999])
        self.picker.set_ids([999, 0], {0: 'Zero'})
        self.assertEqual(self.picker.values(), [999, 0])
        self.assertEqual(self.picker.filter_values(), [0, 11, 14])
        values = self.picker.values()
        values.clear()
        self.assertEqual(self.picker.values(), [999, 0])
        self.changed.assert_not_called()

    def test_invalid_ids_or_filters_leave_previous_draft_intact(self):
        for known, selected in [({True: 'Boolean'}, [True]), ({1: 'Valid'}, [True]),
                                ({2**63: 'Overflow'}, []), ({1: 42}, [1]), ({}, [-(2**63)-1])]:
            with self.subTest(known=known, selected=selected):
                with self.assertRaises(ValueError):
                    self.picker.set_ids(known, selected)
                self.assertEqual(self.picker.values(), [-42])
        for values in ([True], [-1], [15], '0'):
            with self.assertRaises(ValueError):
                self.picker.set_filters(values)
        self.assertEqual(self.picker.filter_values(), [0, 11, 14])
        self.changed.assert_not_called()

    def test_categories_are_rules_and_add_commits_once_without_expanding_ids(self):
        dialog = self.open()
        self.assertEqual(dialog.cancel_button.text(), 'Отменить')
        self.assertLess(dialog.add_button.x(), dialog.cancel_button.x())
        self.assertEqual([dialog.items.item(index).data(Qt.ItemDataRole.UserRole) for index in range(5)],
                         [('filter', identity) for identity in (8, 9, 3, 2, 10)])
        self.toggle(dialog, 'filter', 8)
        self.toggle(dialog, 'chat', 0)
        self.assertEqual(self.picker.values(), [-42])
        self.assertEqual(self.picker.filter_values(), [0, 11, 14])
        dialog.add_button.click()
        self.assertEqual(self.picker.values(), [-42, 0])
        self.assertEqual(self.picker.filter_values(), [0, 11, 14, 8])
        self.assertEqual(self.chip('filter', 8).findChild(QLabel).text(), 'Контакты')
        self.changed.assert_called_once_with()

    def test_cancel_and_escape_discard_both_transient_selections(self):
        for escape in (False, True):
            dialog = self.open()
            self.toggle(dialog, 'chat', 81)
            self.toggle(dialog, 'filter', 10)
            if escape:
                QTest.keyClick(dialog, Qt.Key.Key_Escape)
            else:
                dialog.cancel_button.click()
            self.assertEqual(self.picker.values(), [-42])
            self.assertEqual(self.picker.filter_values(), [0, 11, 14])
        self.changed.assert_not_called()

    def test_search_is_casefolded_and_does_not_drop_hidden_selection(self):
        dialog = self.open()
        self.toggle(dialog, 'chat', 81)
        dialog.search.setText('aLPHa')
        self.assertFalse(self.row(dialog, 'chat', -42).isHidden())
        self.assertTrue(self.row(dialog, 'chat', 81).isHidden())
        dialog.search.setText('No synthetic matches')
        self.assertFalse(dialog.empty.isHidden())
        dialog.add_button.click()
        self.assertEqual(self.picker.values(), [-42, 81])
        self.changed.assert_called_once_with()

    def test_keyboard_cannot_toggle_a_selected_row_hidden_by_search(self):
        dialog = self.open()
        row = self.row(dialog, 'chat', -42)
        dialog.items.setCurrentItem(row)
        dialog.search.setText('No synthetic matches')
        for key in (Qt.Key.Key_Space, Qt.Key.Key_Return, Qt.Key.Key_Enter):
            # Even a stale accessibility/programmatic current row is rejected.
            dialog.items.setCurrentItem(row)
            QTest.keyClick(dialog.items, key)
            self.assertEqual(dialog.selected, [-42])
        dialog.add_button.click()
        self.assertEqual(self.picker.values(), [-42])
        self.changed.assert_not_called()

    def test_old_chip_callback_cannot_mutate_a_replaced_or_rerendered_draft(self):
        old_remove = self.chip('chat', -42).findChild(QToolButton)
        self.picker.set_ids({-42: 'Other draft', 81: 'Beta'}, [-42, 81])
        old_remove.click()
        self.assertEqual(self.picker.values(), [-42, 81])
        translated_remove = self.chip('chat', -42).findChild(QToolButton)
        self.picker.set_language('en')
        translated_remove.click()
        self.assertEqual(self.picker.values(), [-42, 81])
        self.changed.assert_not_called()

    def test_duplicate_titles_keep_distinct_ids_and_unknown_selection_survives(self):
        self.picker.set_ids({0: 'Same', -42: 'Same'}, [999])
        dialog = self.open()
        self.assertEqual(self.row(dialog, 'chat', 999).text(), 'Данные чата недоступны')
        self.toggle(dialog, 'chat', -42)
        dialog.add_button.click()
        self.assertEqual(self.picker.values(), [999, -42])
        self.assertNotIn('999', self.chip('chat', 999).findChild(QLabel).text())

    def test_whole_row_mouse_and_keyboard_toggle_once_without_auto_accept(self):
        dialog = self.open()
        row = self.row(dialog, 'filter', 8)
        rect = dialog.items.visualItemRect(row)
        QTest.mouseClick(dialog.items.viewport(), Qt.MouseButton.LeftButton,
                         pos=QPoint(rect.center().x(), rect.center().y()))
        self.assertIn(8, dialog.filters)
        QTest.keyClick(dialog.items, Qt.Key.Key_Space)
        self.assertNotIn(8, dialog.filters)
        QTest.keyClick(dialog.items, Qt.Key.Key_Return)
        self.assertIn(8, dialog.filters)
        self.assertTrue(dialog.isVisible())
        self.changed.assert_not_called()

    def test_remove_chat_or_category_chip_only_changes_that_draft_field(self):
        self.picker.set_filters([8, 0, 11, 14])
        self.chip('filter', 8).findChild(QToolButton).click()
        self.assertEqual(self.picker.values(), [-42])
        self.assertEqual(self.picker.filter_values(), [0, 11, 14])
        self.chip('chat', -42).findChild(QToolButton).click()
        self.assertEqual(self.picker.values(), [])
        self.assertEqual(self.changed.call_count, 2)

    def test_readonly_categories_still_allow_explicit_chat_selection(self):
        self.picker.set_filters([8, 0, 14])
        self.picker.set_filters_enabled(False)
        self.assertFalse(self.chip('filter', 8).findChild(QToolButton).isEnabled())
        dialog = self.open()
        self.toggle(dialog, 'filter', 8)
        self.toggle(dialog, 'filter', 9)
        self.toggle(dialog, 'chat', 0)
        dialog.add_button.click()
        self.assertEqual(self.picker.values(), [-42, 0])
        self.assertEqual(self.picker.filter_values(), [8, 0, 14])
        self.changed.assert_called_once_with()

    def test_population_disable_hide_language_theme_invalidate_open_dialog(self):
        operations = [lambda: self.picker.set_ids({-42: 'Alpha'}, [-42]),
                      lambda: self.picker.set_filters([0, 11, 14]),
                      lambda: self.picker.set_filters_enabled(False),
                      lambda: self.picker.setEnabled(False), lambda: self.host.hide(),
                      lambda: self.picker.set_language('en'), lambda: self.picker.apply_theme('dark')]
        for operation in operations:
            self.host.show()
            self.picker.setEnabled(True)
            dialog = self.open()
            self.toggle(dialog, 'chat', -42)
            operation()
            self.assertIsNone(self.picker._dialog)
            self.assertFalse(dialog.isVisible())
            self.assertFalse(self.picker._commit_dialog(dialog))
            self.assertEqual(self.picker.values(), [-42])
        self.changed.assert_not_called()

    def test_context_change_or_exception_rejects_add_without_committing(self):
        current = [True]
        self.picker.set_context_guard(lambda: current[0])
        dialog = self.open()
        self.toggle(dialog, 'chat', 81)
        current[0] = False
        dialog.add_button.click()
        self.assertEqual(self.picker.values(), [-42])
        self.assertIsNone(self.picker.open_dialog())
        def broken():
            raise ValueError('Synthetic stale context')
        self.picker.set_context_guard(broken)
        self.assertIsNone(self.picker.open_dialog())
        self.changed.assert_not_called()

    def test_noop_add_and_opening_twice_never_emit_changed(self):
        dialog = self.open()
        self.assertIsNone(self.picker.open_dialog())
        self.toggle(dialog, 'chat', -42)
        self.toggle(dialog, 'chat', -42)
        dialog.add_button.click()
        self.changed.assert_not_called()

    def test_parent_changed_callback_can_repopulate_without_rejecting_committed_dialog(self):
        dialog = self.open()
        self.toggle(dialog, 'filter', 8)
        accepted, rejected = Mock(), Mock()
        dialog.accepted.connect(accepted)
        dialog.rejected.connect(rejected)
        def parent_update():
            self.picker.set_filters_enabled(True)
            self.picker.set_ids(self.picker._known, self.picker.values())
            self.picker.set_filters(self.picker.filter_values())
        self.picker.changed.connect(parent_update)
        dialog.add_button.click()
        self.assertIn(8, self.picker.filter_values())
        self.changed.assert_called_once_with()
        accepted.assert_called_once_with()
        rejected.assert_not_called()
        self.assertFalse(self.picker._commit_dialog(dialog))

    def test_injected_unknown_chat_noncategory_rule_or_readonly_filter_change_rejects(self):
        for mutate in (lambda d: d.selected.append(12345), lambda d: d.selected.append(True),
                       lambda d: d.filters.append(7), lambda d: d.filters.remove(14)):
            dialog = self.open()
            mutate(dialog)
            dialog.add_button.click()
            self.assertEqual(self.picker.values(), [-42])
            self.assertEqual(self.picker.filter_values(), [0, 11, 14])
        self.picker.set_filters_enabled(False)
        dialog = self.open()
        dialog.filters.append(8)
        dialog.add_button.click()
        self.assertEqual(self.picker.filter_values(), [0, 11, 14])
        self.changed.assert_not_called()

    def test_english_labels_are_exact_and_server_titles_are_literal(self):
        self.picker.set_ids({0: '<b>& synthetic</b>'}, [0])
        self.picker.set_language('en')
        self.assertEqual(self.picker.add_button.text(), 'Add chats')
        text = self.chip('chat', 0).findChild(QLabel)
        self.assertEqual(text.text(), '<b>& synthetic</b>')
        self.assertEqual(text.textFormat(), Qt.TextFormat.PlainText)
        dialog = self.open()
        self.assertEqual(dialog.search.placeholderText(), 'Search by name')
        self.assertEqual(self.row(dialog, 'filter', 8).text(), 'Contacts')
        self.assertEqual(dialog.cancel_button.text(), 'Cancel')
        self.assertEqual(dialog.add_button.text(), 'Add')
        self.changed.assert_not_called()

    def test_long_chips_wrap_at_360_and_dark_dialog_remains_bounded(self):
        self.host.resize(360, 580)
        self.picker.set_ids({index: '<b>Long synthetic name</b> ' * 5 for index in range(6)}, list(range(6)))
        self.picker.set_filters([8, 9, 3, 2, 10])
        self.picker.apply_theme('dark')
        self.app.processEvents()
        self.picker._flow.activate()
        self.app.processEvents()
        positions = []
        for index in range(self.picker._flow.count()):
            chip = self.picker._flow.itemAt(index).widget()
            self.assertLessEqual(chip.geometry().right(), self.picker.chips.width())
            self.assertGreater(chip.findChild(QLabel).width(), 15)
            positions.append(chip.y())
        self.assertGreater(len(set(positions)), 2)
        self.assertLessEqual(self.host.width(), 360)
        dialog = self.open()
        self.assertEqual(dialog.size().width(), 400)
        self.assertEqual(dialog.size().height(), 644)
        self.assertEqual(dialog.items.horizontalScrollBar().maximum(), 0)
        self.assertIn('#141821', dialog.styleSheet())


if __name__ == '__main__':
    unittest.main()
