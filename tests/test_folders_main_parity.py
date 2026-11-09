"""Native-shaped folder overview with entirely synthetic snapshots/workers."""
import copy
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMenu, QScrollArea

from maxclient.ui.folders_panel import ALL_FOLDER, FoldersPanel
from maxclient.ui.theme import build_stylesheet
from test_desktop_shell import _load_offscreen_font
from test_folders_blocks_ui import folders, service


class FoldersMainParityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        _load_offscreen_font(cls.app)
        cls.app.setStyle('Fusion')

    def setUp(self):
        self.app.setStyleSheet(build_stylesheet('light', '#0085FF'))
        self.runner = patch('maxclient.ui.folders_panel.run_async')
        self.run = self.runner.start()
        self.service = service()
        self.panel = FoldersPanel(self.service)
        self.panel._read_done(folders())

    def tearDown(self):
        self.panel.close()
        self.runner.stop()
        self.app.processEvents()

    def test_main_has_native_rows_and_no_editor_until_selection(self):
        panel = self.panel
        panel.resize(640, 600)
        panel.show()
        self.app.processEvents()
        self.assertEqual(panel.maximumWidth(), 640)
        self.assertEqual(panel.intro_icon.size().width(), 72)
        self.assertEqual(panel.new_button.height(), 40)
        self.assertEqual(panel.intro_text.text(), 'Создавайте папки для чатов\nи легко переключайтесь между ними')
        self.assertTrue(panel.editor.isHidden())
        self.assertTrue(panel.status.isHidden())
        self.assertTrue(panel.refresh_button.isHidden())
        row = panel._row_widgets['work']
        self.assertEqual(row.height(), 52)
        self.assertEqual(row.title_label.textFormat(), Qt.TextFormat.PlainText)
        self.assertEqual(row.title_label.text(), '<b>Работа</b>')
        self.assertEqual(panel.items.item(1).foreground().color().alpha(), 0)
        self.assertLess(panel.new_button.y(), 230)
        self.assertTrue(panel._row_widgets[ALL_FOLDER].more.isHidden())
        QTest.mouseClick(row.title_label, Qt.MouseButton.LeftButton, pos=QPoint(40, 15))
        self.app.processEvents()
        self.assertEqual(panel._selected, 'work')
        self.assertFalse(panel.editor.isHidden())
        self.assertEqual(panel.title.text(), '<b>Работа</b>')
        self.run.assert_not_called()

    def test_close_and_new_only_change_local_draft(self):
        panel = self.panel
        panel.items.setCurrentRow(1)
        panel.title.setText('Local draft')
        panel.back_button.click()
        self.assertTrue(panel.editor.isHidden())
        self.assertIsNone(panel._selected)
        panel.new_button.click()
        self.assertFalse(panel.editor.isHidden())
        self.assertTrue(panel._creating)
        panel.title.setText('New folder')
        self.assertTrue(panel.save_button.isEnabled())
        self.run.assert_not_called()

    def test_more_dismissed_or_snapshot_replaced_never_writes(self):
        panel = self.panel
        row = panel._row_widgets['work']
        with patch.object(panel, '_exec_folder_menu', return_value=None):
            panel._open_row_menu('work', row.more)
        self.assertIsNone(panel._selected)
        self.assertTrue(panel.editor.isHidden())
        def refreshed_menu(menu, *_args):
            action = menu.actions()[1]
            panel._read_done(copy.deepcopy(folders()))
            return action
        with patch.object(panel, '_exec_folder_menu', side_effect=refreshed_menu), patch('maxclient.ui.folders_panel.confirm_action') as confirm:
            panel._open_row_menu('work', panel._row_widgets['work'].more)
            confirm.assert_not_called()
        self.run.assert_not_called()

    def test_more_edit_and_delete_keep_real_identity_and_confirmation(self):
        panel = self.panel
        with patch.object(panel, '_exec_folder_menu', side_effect=lambda menu, *_args: menu.actions()[0]):
            panel._open_row_menu('work', panel._row_widgets['work'].more)
        self.assertEqual(panel._selected, 'work')
        self.assertFalse(panel.editor.isHidden())
        self.run.assert_not_called()
        with patch.object(panel, '_exec_folder_menu', side_effect=lambda menu, *_args: menu.actions()[1]), \
                patch('maxclient.ui.folders_panel.confirm_action', return_value=False):
            panel._open_row_menu('work', panel._row_widgets['work'].more)
        self.run.assert_not_called()
        with patch.object(panel, '_exec_folder_menu', side_effect=lambda menu, *_args: menu.actions()[1]), \
                patch('maxclient.ui.folders_panel.confirm_action', return_value=True):
            panel._open_row_menu('work', panel._row_widgets['work'].more)
        self.service.delete_folder.assert_not_called()
        self.run.call_args.args[0]()
        self.service.delete_folder.assert_called_once_with('work', context=panel._snapshot_context)
        self.assertEqual(panel.items.item(1).text(), '<b>Работа</b>')
        self.assertTrue(panel.status.isVisibleTo(panel))

    def test_built_in_and_locked_folders_cannot_be_deleted_from_menu(self):
        panel = self.panel
        for identity in (ALL_FOLDER, 'locked'):
            def attempt_disabled(menu, *_args):
                self.assertFalse(menu.actions()[1].isEnabled())
                return menu.actions()[1]
            with patch.object(panel, '_exec_folder_menu', side_effect=attempt_disabled), \
                    patch('maxclient.ui.folders_panel.confirm_action') as confirm:
                panel._open_row_menu(identity, panel._row_widgets[identity].more)
                confirm.assert_not_called()
        self.run.assert_not_called()

    def test_translation_theme_refresh_do_not_translate_server_titles_or_write(self):
        panel = self.panel
        panel.set_language('en')
        panel.apply_theme('dark')
        self.assertEqual(panel.section_label.text(), 'Your folders')
        self.assertEqual(panel._row_widgets['work'].title_label.text(), '<b>Работа</b>')
        self.assertEqual(panel.items.item(0).text(), 'Все')
        self.assertIn('Create folders', panel.intro_text.text())
        self.run.assert_not_called()

    def test_fake_main_captures_and_narrow_width_are_bounded(self):
        panel = self.panel
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(panel)
        for theme, language, width in (('light', 'ru', 642), ('dark', 'ru', 642), ('light', 'en', 360)):
            self.app.setStyleSheet(build_stylesheet(theme, '#0085FF'))
            panel.apply_theme(theme)
            panel.set_language(language)
            scroll.resize(width, 650)
            scroll.show()
            self.app.processEvents()
            self.app.processEvents()
            self.assertLessEqual(panel.minimumSizeHint().width(), width)
            self.assertEqual(scroll.horizontalScrollBar().maximum(), 0)
            target = Path('.test-tmp') / f'folders_main_{theme}_{language}_{width}_20261103.png'
            target.parent.mkdir(exist_ok=True)
            self.assertTrue(scroll.grab().save(str(target)))
        scroll.takeWidget()
        scroll.close()
        self.run.assert_not_called()


if __name__ == '__main__':
    unittest.main()
