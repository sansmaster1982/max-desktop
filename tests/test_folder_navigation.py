"""Independent folder navigation regressions; fake UI and isolated cache only."""
import copy
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from maxclient.core.config import Settings
from maxclient.core.folder_view import build_folder_view
from maxclient.core.models import Chat
from maxclient.core.service import MaxService
from maxclient.core.store import Store
from maxclient.ui.main_window import MainWindow
from test_settings_workspace import SyntheticService, immediate


def chat_raw(identity, kind='DIALOG', count=0):
    return {'id': identity, 'type': kind, 'owner': 77, 'status': 'ACTIVE',
            'participants': {'123': 1, '77': 1}, 'created': 1, 'lastEventTime': 2,
            'lastMessage': {'id': 90, 'time': 2}, 'newMessages': count}


def folder(identity='custom', **values):
    return {'id': identity, 'title': 'Synthetic folder', 'include': [], 'filters': [],
            'favorites': [], 'options': [], **values}


def snapshot(*records):
    return {'folderSync': 100, 'folders': copy.deepcopy(list(records)),
            'foldersOrder': [record['id'] for record in records],
            'orderedFolders': copy.deepcopy(list(records)), 'missingFolderIds': []}


class FolderService(SyntheticService):
    def __init__(self):
        super().__init__()
        self.chats = [Chat(id=1, title='Dialog one', chat_type='DIALOG', peer_user_id=77),
                      Chat(id=2, title='Channel two', chat_type='CHANNEL', is_channel=True, unread=2),
                      Chat(id=3, title='Group three', chat_type='CHAT', is_group=True, unread=1),
                      Chat(id=4, title='Dialog four', chat_type='DIALOG', peer_user_id=77, unread=3)]
        self.raws = {chat.id: chat_raw(chat.id, chat.chat_type, chat.unread) for chat in self.chats}
        self.chat_settings = {}
        self.folder_reads = 0
        self.snapshot = snapshot(folder('all.chat.folder'), folder())

    def get_folders(self):
        self.folder_reads += 1
        return copy.deepcopy(self.snapshot)

    def get_folder_view(self, dto, chats=None):
        return build_folder_view(dto, self.chats if chats is None else chats,
                                 raw_chats=self.raws, viewer_id=123,
                                 chat_settings=self.chat_settings, now_ms=1000)


class FolderNavigationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.patches = [patch('maxclient.ui.main_window.run_async'),
                        patch('maxclient.ui.chat_view.run_async'),
                        patch('maxclient.ui.main_window.DesktopNotifications'),
                        patch('maxclient.ui.settings_workspace.run_async', side_effect=immediate),
                        patch('maxclient.ui.folders_panel.run_async'),
                        patch('maxclient.ui.blocked_contacts_panel.run_async')]
        for item in self.patches:item.start()
        self.service = FolderService()
        self.settings = Settings(theme='light')
        self.settings.save = Mock()
        self.window = MainWindow(self.service, self.settings)
        self.window._on_folders(self.service.snapshot)

    def tearDown(self):
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()
        for item in reversed(self.patches):item.stop()

    def ids(self):
        return [self.window.chat_list.item(index).data(Qt.ItemDataRole.UserRole)
                for index in range(self.window.chat_list.count())]

    def open_first_chat(self):
        self.window._select_section('all')
        self.window._on_chat_clicked(self.window.chat_list.item(0))

    def test_folder_panel_refresh_does_not_leave_settings_or_change_settings_page(self):
        self.window._open_settings()
        workspace = self.window._settings_workspace
        workspace.select_page('folders')
        workspace.folder_list._read_done(self.service.snapshot)
        self.assertIs(self.window._workspace_pages.currentWidget(), workspace)
        self.assertEqual(workspace.current_page, 'folders')
        self.assertTrue(self.window.settings_button.isChecked())
        self.assertFalse(any(button.isChecked() for button in self.window.nav_buttons.values()))

    def test_selected_chat_and_draft_survive_active_folder_refresh(self):
        self.open_first_chat()
        self.window.chat_view.input.setPlainText('Synthetic draft')
        self.window._on_folders(snapshot(folder('all.chat.folder'), folder(include=[1, 2], favorites=[2])))
        self.window._select_section('folder:custom')
        self.assertEqual(self.ids(), [2, 1])
        self.window._on_folders(snapshot(folder('all.chat.folder'), folder(title='Updated', include=[1, 2], favorites=[1])))
        self.assertEqual(self.window._section, 'folder:custom')
        self.assertEqual(self.window._current_chat_id, 1)
        self.assertEqual(self.window.chat_list.currentItem().data(Qt.ItemDataRole.UserRole), 1)
        self.assertEqual(self.window.chat_view.input.toPlainText(), 'Synthetic draft')
        self.assertEqual(self.window.section_title.text(), 'Updated')

    def test_partial_or_malformed_snapshot_cannot_replace_complete_navigation(self):
        before = copy.deepcopy(self.window._folders)
        for partial in ({'folderSync': 101}, {'foldersOrder': ['new'], 'orderedFolders': [None]},
                        {'foldersOrder': ['new'], 'orderedFolders': []},
                        {'foldersOrder': ['new'], 'orderedFolders': [folder('wrong')]}):
            with self.subTest(partial=partial):
                self.window._on_folders({'folderSync': 101, **partial})
                self.assertEqual(self.window._folders, before)
                self.assertIn('folder:custom', self.window.nav_buttons)

    def test_duplicate_folder_ids_cannot_collapse_complete_navigation(self):
        before = copy.deepcopy(self.window._folders)
        self.window._on_folders({'folderSync': 101, 'foldersOrder': ['duplicate', 'duplicate'],
                                 'orderedFolders': [folder('duplicate'), folder('duplicate')]})
        self.assertEqual(self.window._folders, before)

    def test_custom_folder_type_filter_favorites_and_search_combine(self):
        self.window._on_folders(snapshot(folder('all.chat.folder'), folder(filters=[4], favorites=[2])))
        self.window._select_section('folder:custom')
        self.assertEqual(self.ids(), [2, 1, 4])
        self.assertEqual(self.window.nav_buttons['folder:custom']._counter, 2)
        self.window.search.setText('four')
        self.assertEqual(self.ids(), [4])

    def test_hide_empty_and_unresolved_folder_membership_remain_distinct(self):
        self.window._on_folders(snapshot(folder('all.chat.folder'), folder('empty', options=[0]),
                                         folder('unknown', filters=[7], options=[0])))
        self.assertTrue(self.window.nav_buttons['folder:empty'].isHidden())
        self.service.chat_settings = None
        self.window._reload_chats()
        self.assertFalse(self.window.nav_buttons['folder:unknown'].isHidden())
        self.window._select_section('folder:unknown')
        self.assertFalse(self.window.folder_status.isHidden())
        self.assertEqual(self.ids(), [])

    def test_removed_active_folder_falls_back_without_hiding_settings(self):
        self.window._select_section('folder:custom')
        self.window._open_settings()
        workspace = self.window._settings_workspace
        self.window._on_folders(snapshot(folder('all.chat.folder')))
        self.assertEqual(self.window._section, 'folder:all.chat.folder')
        self.assertIs(self.window._workspace_pages.currentWidget(), workspace)
        self.assertTrue(self.window.settings_button.isChecked())

    def test_settings_uses_standalone_panels_and_lazy_folder_read(self):
        self.window._open_settings()
        workspace = self.window._settings_workspace
        with patch('maxclient.ui.folders_panel.run_async') as worker:
            self.assertEqual(self.service.folder_reads, 0)
            workspace.select_page('folders')
            worker.assert_called_once()
            self.assertEqual(self.service.folder_reads, 0)
            self.assertTrue(workspace.folder_list._busy)
            call = worker.call_args
            kwargs = {key: value for key, value in call.kwargs.items() if key not in ('on_done', 'on_error')}
            result = call.args[0](*call.args[1:], **kwargs)
            self.assertEqual(self.service.folder_reads, 1)
            self.assertEqual(result['foldersOrder'], self.service.snapshot['foldersOrder'])
            call.kwargs['on_done'](result)
            self.assertFalse(workspace.folder_list._busy)
            self.assertTrue(workspace.folder_list._loaded)
            self.assertEqual(workspace.folder_list._snapshot['foldersOrder'], result['foldersOrder'])
            workspace.folder_list.ensure_loaded()
            worker.assert_called_once()
            self.assertEqual(self.service.folder_reads, 1)
        self.assertTrue(hasattr(workspace, 'blocked_contacts'))
        self.assertIn('blocked', workspace.page_widgets)

    def fresh_window(self):
        window = MainWindow(self.service, self.settings)
        self.addCleanup(window.deleteLater)
        self.addCleanup(window.close)
        return window

    def visible_folder_keys(self, window=None):
        window = self.window if window is None else window
        return [key for key in window._folders if not window.nav_buttons[key].isHidden()]

    def test_initial_static_fallback_survives_unavailable_and_empty_snapshot(self):
        window = self.fresh_window()
        self.assertEqual(window._section, 'all')
        for result in ({}, {'folderSync': 101, 'foldersOrder': ['unknown'], 'orderedFolders': [None]}, snapshot()):
            window._on_folders(result)
            self.assertFalse(window._folders)
            self.assertTrue(all(not window.nav_buttons[key].isHidden() for key in ('all', 'new', 'channels')))

    def test_authoritative_server_order_replaces_all_static_fallback_buttons(self):
        unread_id = '11111111-2222-4333-8444-555555555555'
        records = [folder('all.chat.folder', title='Все'),
                   folder(unread_id, title='Новые', filters=[0]),
                   folder('chat.channel.folder', title='Каналы', filters=[2])]
        self.window._on_folders(snapshot(*records))
        expected = ['folder:' + record['id'] for record in records]
        self.assertEqual(self.visible_folder_keys(), expected)
        self.assertTrue(all(self.window.nav_buttons[key].isHidden() for key in ('all', 'new', 'channels')))
        visible_new = [button for button in self.window.nav_buttons.values()
                       if not button.isHidden() and button.label.text() == 'Новые']
        self.assertEqual(len(visible_new), 1)
        self.assertEqual(self.window._section, 'folder:all.chat.folder')

    def test_canonical_all_and_channels_use_real_folder_membership_and_favorites(self):
        self.window._on_folders(snapshot(folder('all.chat.folder', favorites=[4]),
                                         folder('chat.channel.folder', filters=[2])))
        self.window._select_section('all')
        self.assertEqual(self.window._section, 'folder:all.chat.folder')
        self.assertEqual(self.ids(), [4, 1, 2, 3])
        self.assertEqual(self.window.nav_buttons['folder:all.chat.folder']._counter, 3)
        self.window._select_section('channels')
        self.assertEqual(self.window._section, 'folder:chat.channel.folder')
        self.assertEqual(self.ids(), [2])

    def test_distinct_uuid_folders_with_same_title_are_not_merged_or_title_classified(self):
        unread_id, read_id = 'synthetic-unread-uuid', 'synthetic-read-uuid'
        self.window._on_folders(snapshot(folder('all.chat.folder'),
                                         folder(unread_id, title='Новые', filters=[0]),
                                         folder(read_id, title='Новые', filters=[1])))
        self.assertEqual(len(self.visible_folder_keys()), 3)
        self.window.nav_buttons['folder:' + unread_id].click()
        self.assertEqual(self.window._section, 'folder:' + unread_id)
        self.assertEqual(self.ids(), [2, 3, 4])
        self.window.nav_buttons['folder:' + read_id].click()
        self.assertEqual(self.ids(), [1])
        self.window._select_section('new')  # No canonical unread ID exists.
        self.assertEqual(self.window._section, 'folder:all.chat.folder')

    def test_first_authoritative_snapshot_preserves_settings_chat_and_draft(self):
        window = self.fresh_window()
        window._on_chat_clicked(window.chat_list.item(0))
        window.chat_view.input.setPlainText('Synthetic initial draft')
        window._open_settings()
        workspace = window._settings_workspace
        workspace.select_page('folders')
        window._on_folders(snapshot(folder('all.chat.folder'), folder('uuid-new', filters=[0])))
        self.assertEqual(window._section, 'folder:all.chat.folder')
        self.assertEqual(window._current_chat_id, 1)
        self.assertEqual(window.chat_view.input.toPlainText(), 'Synthetic initial draft')
        self.assertIs(window._workspace_pages.currentWidget(), workspace)
        self.assertTrue(window.settings_button.isChecked())
        self.assertFalse(any(button.isChecked() for button in window.nav_buttons.values()))

    def test_empty_or_untyped_sync_snapshot_keeps_last_good_authoritative_rail(self):
        before = copy.deepcopy(self.window._folders)
        invalid = [snapshot()]
        for value in (None, True, '101', 2 ** 63):
            candidate = snapshot(folder('replacement'))
            if value is None:candidate.pop('folderSync')
            else:candidate['folderSync'] = value
            invalid.append(candidate)
        for result in invalid:
            with self.subTest(result=result):
                self.window._on_folders(result)
                self.assertEqual(self.window._folders, before)
                self.assertTrue(all(self.window.nav_buttons[key].isHidden() for key in ('all', 'new', 'channels')))


class FolderProjectionOwnerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='max-folder-nav-')
        self.store = Store(Path(self.temporary.name) / 'cache.db')
        self.store.bind_owner(123)
        self.service = MaxService.__new__(MaxService)
        self.service.session = SimpleNamespace(my_user_id=123)
        self.service._server_metadata_owner = 123
        self.service._metadata_lock = threading.RLock()
        self.service._server_chats = {1: chat_raw(1, count=2)}
        self.service._server_contacts = {77: {'id': 77, 'flags': 512}}
        self.service.store = self.store
        self.service.list_chats = Mock(return_value=[Chat(id=1, chat_type='DIALOG', peer_user_id=77, unread=2)])
        self.service.client = Mock()

    def tearDown(self):
        self.assertEqual(self.service.client.mock_calls, [])
        self.store.close()
        self.temporary.cleanup()

    def test_projection_uses_matching_owner_contacts_and_actual_per_chat_dnd(self):
        self.store.merge_config(123, {'chats': {'1': {'dontDisturbUntil': -1}}})
        result = self.service.get_folder_view(folder(filters=[4, 8, 7]))
        self.assertEqual(result['chatIds'], [1])
        self.assertTrue(result['complete'])

    def test_metadata_owner_mismatch_fails_before_loading_chats(self):
        self.service._server_metadata_owner = 99
        with self.assertRaises(ValueError):self.service.get_folder_view(folder(include=[1]))
        self.service.list_chats.assert_not_called()

    def test_persistent_cache_owner_mismatch_fails_closed(self):
        self.store.bind_owner(99)
        with self.assertRaises(ValueError):self.service.get_folder_view(folder(id='all.chat.folder'))

    def test_owner_switch_during_config_read_discards_projection(self):
        original = self.store.load_config
        def switch(owner):
            self.service.session.my_user_id = 99
            self.service._server_metadata_owner = 99
            return original(owner)
        with patch.object(self.store, 'load_config', side_effect=switch):
            with self.assertRaises(ValueError):self.service.get_folder_view(folder(include=[1]))

    def test_typed_chat_dnd_and_calls_server_strings_persist_without_defaults(self):
        self.service._remember_config({'chats': {'1': {'dontDisturbUntil': -1}},
                                      'server': {'calls-endpoint': 'https://calls.example.invalid',
                                                 'calls-test-domain': 'synthetic.invalid'}})
        before = self.store.load_config(123)
        self.assertEqual(before['chats'], {'1': {'dontDisturbUntil': -1}})
        self.assertEqual(before['server']['calls-test-domain'], 'synthetic.invalid')
        self.service._remember_config({'chats': {'1': {'unhandledSound': 'synthetic'}}})
        self.assertEqual(self.store.load_config(123), before)

    def test_invalid_dnd_or_server_type_never_overwrites_confirmed_config(self):
        self.service._remember_config({'chats': {'1': {'dontDisturbUntil': -1}}})
        before = self.store.load_config(123)
        for invalid in ({'chats': {'1': {'dontDisturbUntil': True}}},
                        {'chats': {'1': {'dontDisturbUntil': '0'}}},
                        {'chats': {'1': {'dontDisturbUntil': -2}}},
                        {'server': {'calls-endpoint': 77}},
                        {'server': {'calls-test-domain': False}}):
            with self.subTest(config=invalid), self.assertRaises(ValueError):
                self.service._remember_config(invalid)
            self.assertEqual(self.store.load_config(123), before)


if __name__ == '__main__':unittest.main()
