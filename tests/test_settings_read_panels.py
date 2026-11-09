import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import unittest
from unittest.mock import Mock,patch
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication
from maxclient.ui.settings_read_panels import SettingsReadPanel


class SettingsReadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.app=QApplication.instance() or QApplication([])

    def test_server_folder_order_and_unavailable_dtos_stay_distinct(self):
        panel=SettingsReadPanel(Mock(),'folders')
        panel._folders({'orderedFolders':[{'id':'b','title':'Second','include':[1],'favorites':[]},None,
                                          {'id':'a','title':'First','include':[]}], 'missingFolderIds':['missing']})
        self.assertEqual(panel.items.count(),3)
        self.assertEqual(panel.items.item(0).data(Qt.ItemDataRole.UserRole),'b')
        self.assertIn('недоступны',panel.items.item(1).text())
        self.assertEqual(panel.items.item(2).data(Qt.ItemDataRole.UserRole),'a')
        panel.close()

    def test_archive_cursor_is_forwarded_exactly_and_duplicate_stories_are_not_added(self):
        service=Mock();panel=SettingsReadPanel(service,'stories')
        story={'id':1,'time':1793459502000,'viewsCount':7,'expiration':42}
        panel._stories({'stories':[story],'marker':1793459400000})
        with patch('maxclient.ui.settings_read_panels.run_async') as run:
            panel._load(more=True)
            self.assertEqual(run.call_args.kwargs['marker'],1793459400000)
        panel._stories({'stories':[story,{'id':2,'time':1793459302000}]},append=True)
        self.assertEqual(panel.items.count(),2)
        self.assertIsNone(panel._marker)
        panel.close()

    def test_invalid_archive_result_is_not_reported_as_empty(self):
        panel=SettingsReadPanel(Mock(),'stories');panel.items.addItem('old synthetic row')
        panel._stories({'stories':[{'id':{},'time':1793459302000}]})
        self.assertEqual(panel.items.count(),1)
        self.assertIn('Не удалось',panel.status.text())
        panel.close()

    def test_lazy_load_is_single_and_never_requests_a_story_view_mark(self):
        service=Mock();panel=SettingsReadPanel(service,'stories')
        with patch('maxclient.ui.settings_read_panels.run_async') as run:
            panel.ensure_loaded();panel.ensure_loaded()
            run.assert_called_once()
            self.assertEqual(run.call_args.args,(service.list_own_stories,))
        panel.close()


if __name__=='__main__':unittest.main()
