"""Synthetic-only folder/block management; run_async is mocked, no RPC occurs."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import copy
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QMessageBox, QScrollArea

from maxclient.core.models import Chat
from maxclient.protocol.client import AuthenticatedSession, MaxWriteNotStarted
from maxclient.ui.blocked_contacts_panel import BlockedContactsPanel
from maxclient.ui.folders_panel import ALL_FOLDER, FoldersPanel
from maxclient.ui.management_widgets import confirm_action
from maxclient.ui.theme import build_stylesheet
from test_desktop_shell import _load_offscreen_font


STAMP = 1793459502608


def folders():
    records = [
        {'id': ALL_FOLDER, 'title': 'Все', 'options': [1, 2, 3]},
        {'id': 'work', 'title': '<b>Работа</b>', 'include': [42], 'filters': [0], 'favorites': [81, 42]},
        {'id': 'locked', 'title': 'Папка MAX', 'options': [1, 2, 3], 'include': [42], 'filters': [2]},
    ]
    return {'folderSync': STAMP, 'foldersOrder': [record['id'] for record in records],
            'orderedFolders': records, 'missingFolderIds': []}


def contacts(*ids):
    return {'contacts': [{'id': identity, 'updateTime': STAMP, 'status': 'BLOCKED',
                          'names': [{'type': 'ONEME', 'firstName': f'<b>Тест {identity}</b>'}]} for identity in ids],
            'from': 0, 'count': 100, 'pagination': 'first_page_only'}


def service():
    value = Mock()
    value.context = (101, object(), AuthenticatedSession(101, 'synthetic', 'WEB', object()))
    value._history_read_context.side_effect = lambda: value.context
    def require(context):
        current = value.context
        if context[0] != current[0] or context[1] is not current[1] or context[2] is not current[2]:
            raise MaxWriteNotStarted('Synthetic context changed')
    value._require_history_context.side_effect = require
    value.list_chats.return_value = [Chat(id=42, title='<b>Чат</b>'), Chat(id=81, title='Второй чат'),
                                    Chat(id=90, title='Третий чат')]
    return value


class ManagementPanelsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        _load_offscreen_font(cls.app)
        cls.app.setStyle('Fusion')

    def setUp(self):
        self.app.setStyleSheet(build_stylesheet('light', '#0085FF'))
        self.service = service()
        self.folder_runner = patch('maxclient.ui.folders_panel.run_async')
        self.block_runner = patch('maxclient.ui.blocked_contacts_panel.run_async')
        self.folder_run, self.block_run = self.folder_runner.start(), self.block_runner.start()
        self.folder_panel = FoldersPanel(self.service)
        self.block_panel = BlockedContactsPanel(self.service)

    def tearDown(self):
        self.folder_panel.close()
        self.block_panel.close()
        self.folder_runner.stop()
        self.block_runner.stop()
        self.app.processEvents()

    def select(self, identity):
        for row in range(self.folder_panel.items.count()):
            item = self.folder_panel.items.item(row)
            if item.data(Qt.ItemDataRole.UserRole) == identity:
                self.folder_panel.items.setCurrentItem(item)
                return
        self.fail('Missing synthetic folder')

    def folder_worker(self):
        call = self.folder_run.call_args
        kwargs = {key: value for key, value in call.kwargs.items() if key not in ('on_done', 'on_error')}
        return call.args[0](*call.args[1:], **kwargs)

    def test_folder_population_noop_and_partial_reads_never_write_or_fake_empty(self):
        panel = self.folder_panel
        panel._read_done(folders())
        self.select('work')
        self.assertEqual(panel.items.currentItem().text(), '<b>Работа</b>')
        self.assertFalse(panel.save_button.isEnabled())
        panel._save()
        self.folder_run.assert_not_called()
        panel._read_done({'folderSync': STAMP})
        self.assertEqual(panel.items.count(), 3)
        self.assertFalse(panel.new_button.isEnabled())
        self.assertIn('часть', panel.status.text())
        panel._read_done({'folderSync': STAMP, 'foldersOrder': [], 'orderedFolders': []})
        self.assertEqual(panel.items.count(), 0)
        self.assertTrue(panel.new_button.isEnabled())

    def test_server_locks_are_field_specific_and_all_folder_is_protected(self):
        panel = self.folder_panel
        panel._read_done(folders())
        self.select('locked')
        self.assertFalse(panel.title.isEnabled())
        self.assertFalse(panel.filter_widget.isEnabled())
        self.assertFalse(panel.delete_button.isEnabled())
        self.assertTrue(panel.include.isEnabled())
        self.assertTrue(panel.favorites.isEnabled())
        panel.title.setText('Injected title')
        panel.filters[0].setChecked(True)
        panel._save()
        self.folder_run.assert_not_called()
        self.select(ALL_FOLDER)
        self.assertFalse(panel.title.isEnabled())
        self.assertTrue(panel.include.isEnabled())
        self.assertTrue(panel.favorites.isEnabled())
        self.assertFalse(panel.up_button.isEnabled())
        self.assertFalse(panel.down_button.isEnabled())

    def test_explicit_update_waits_readback_and_refresh_during_write_is_sequential(self):
        panel = self.folder_panel
        panel._read_done(folders())
        self.select('work')
        panel.title.setText('Рабочие чаты')
        panel.save_button.click()
        self.folder_worker()
        self.service.update_folder.assert_called_once_with('work', title='Рабочие чаты', context=self.service.context)
        panel.refresh()
        self.assertEqual(self.folder_run.call_count, 1)
        panel._write_done({'accepted': True, 'pending': True, 'folderId': 'work', 'snapshot': folders()})
        self.assertEqual(self.folder_run.call_count, 2)
        self.assertEqual(panel.items.item(1).text(), '<b>Работа</b>')
        self.assertFalse(panel.save_button.isEnabled())
        updated = folders()
        updated['orderedFolders'][1]['title'] = 'Рабочие чаты'
        updated['writePending'] = False
        panel._read_done(updated)
        self.assertIsNone(panel._pending)
        self.assertEqual(panel.items.item(1).text(), 'Рабочие чаты')

    def test_unknown_create_is_not_inferred_by_duplicate_title(self):
        panel = self.folder_panel
        panel._read_done(folders())
        panel.new_button.click()
        panel.title.setText('Новая')
        panel.save_button.click()
        self.folder_worker()
        self.service.create_folder.assert_called_once_with('Новая', include=[], filters=[], context=self.service.context)
        panel._write_error('synthetic private detail')
        self.assertFalse(panel.new_button.isEnabled())
        duplicate = folders()
        duplicate['foldersOrder'].append('unrelated')
        duplicate['orderedFolders'].append({'id': 'unrelated', 'title': 'Новая'})
        panel._read_done(duplicate)
        self.assertIsNotNone(panel._pending)
        self.assertFalse(panel.new_button.isEnabled())
        duplicate['writePending'] = False  # Service UUID/field readback proof, not a remote flag.
        panel._read_done(duplicate)
        self.assertIsNone(panel._pending)
        self.assertTrue(panel.new_button.isEnabled())

    def test_confirmed_create_uses_exact_service_uuid_and_title_is_utf16_bounded(self):
        panel = self.folder_panel
        panel._read_done(folders())
        panel.new_button.click()
        panel.title.setText('😀' * 20)
        self.assertLessEqual(len(panel.title.text().encode('utf-16-le')) // 2, 20)
        panel.title.setText('Новая')
        panel.save_button.click()
        updated = folders()
        updated['foldersOrder'].append('synthetic-server-uuid')
        updated['orderedFolders'].append({'id': 'synthetic-server-uuid', 'title': 'Новая',
                                         'include': [], 'filters': []})
        updated['writePending'] = False
        panel._write_done({'accepted': True, 'pending': False, 'folderId': 'synthetic-server-uuid', 'snapshot': updated})
        self.assertIsNone(panel._pending)
        self.assertEqual(panel._selected, 'synthetic-server-uuid')
        self.assertEqual(panel.items.currentItem().text(), 'Новая')

    def test_delete_requires_yes_and_actual_full_order_absence(self):
        panel = self.folder_panel
        panel._read_done(folders())
        self.select('work')
        with patch('maxclient.ui.folders_panel.confirm_action', return_value=False):
            panel.delete_button.click()
        self.folder_run.assert_not_called()
        with patch('maxclient.ui.folders_panel.confirm_action', return_value=True):
            panel.delete_button.click()
        self.folder_worker()
        self.service.delete_folder.assert_called_once_with('work', context=self.service.context)
        panel._write_done({'accepted': True, 'pending': True, 'folderId': 'work', 'snapshot': folders()})
        self.assertEqual(panel.items.count(), 3)
        actual = folders()
        actual['foldersOrder'].pop(1)
        actual['orderedFolders'].pop(1)
        actual['writePending'] = True
        panel._read_done(actual)
        self.assertIsNotNone(panel._pending)
        actual['writePending'] = False
        panel._read_done(actual)
        self.assertIsNone(panel._pending)
        self.assertEqual(panel.items.count(), 2)

    def test_order_sends_complete_ids_keeps_all_first_and_never_reorders_optimistically(self):
        panel = self.folder_panel
        panel._read_done(folders())
        self.select('work')
        self.assertFalse(panel.up_button.isEnabled())
        panel.down_button.click()
        self.folder_worker()
        self.service.reorder_folders.assert_called_once_with([ALL_FOLDER, 'locked', 'work'],
                context=self.service.context, expected_order=[ALL_FOLDER, 'work', 'locked'])
        self.assertEqual(panel.items.item(1).data(Qt.ItemDataRole.UserRole), 'work')

    def test_favorites_preserve_order_and_noop_filter_sets_do_not_write(self):
        panel = self.folder_panel
        snapshot = folders()
        snapshot['orderedFolders'][1]['filters'] = [4, 0]
        panel._read_done(snapshot)
        self.select('work')
        panel.filters[4].setChecked(False)
        panel.filters[4].setChecked(True)
        self.assertFalse(panel.save_button.isEnabled())
        panel.favorites.items.setCurrentRow(1)
        panel.favorites._move(-1)
        panel.save_button.click()
        self.folder_worker()
        self.assertEqual(self.service.update_folder.call_args.kwargs['favorites'], [42, 81])
        self.assertNotIn('filters', self.service.update_folder.call_args.kwargs)

    def test_all_folder_can_edit_favorites_without_renaming_or_moving_it(self):
        panel = self.folder_panel
        panel._read_done(folders())
        self.select(ALL_FOLDER)
        panel.favorites._add()
        panel.save_button.click()
        self.folder_worker()
        self.service.update_folder.assert_called_once_with(ALL_FOLDER, favorites=[42], context=self.service.context)

    def test_missing_folder_metadata_and_invalid_identity_do_not_enable_edits(self):
        snapshot = folders()
        snapshot['orderedFolders'][1] = None
        self.folder_panel._read_done(snapshot)
        self.select('work')
        self.assertFalse(self.folder_panel.title.isEnabled())
        self.assertFalse(self.folder_panel.delete_button.isEnabled())
        bad = folders()
        bad['orderedFolders'][1]['include'] = [True]
        self.folder_panel._read_done(bad)
        self.assertFalse(self.folder_panel.new_button.isEnabled())

    def test_authoritative_service_noop_is_not_an_unknown_write(self):
        panel = self.folder_panel
        panel._read_done(folders())
        self.select('work')
        panel.title.setText('Уже изменено')
        panel.save_button.click()
        state = folders()
        state['orderedFolders'][1]['title'] = 'Уже изменено'
        state['writePending'] = False
        panel._write_done({'accepted': False, 'pending': False, 'unchanged': True,
                           'folderId': 'work', 'snapshot': state})
        self.assertIsNone(panel._pending)

    def test_block_list_is_first_page_only_and_names_are_literal(self):
        panel = self.block_panel
        panel._read_done(contacts(42))
        self.assertEqual(panel.items.item(0).text(), '<b>Тест 42</b>')
        self.assertIn('1', panel.status.text())
        self.assertFalse(panel.unblock_button.isEnabled())
        panel.items.setCurrentRow(0)
        self.assertTrue(panel.unblock_button.isEnabled())
        self.block_run.assert_not_called()

    def test_unblock_waits_target_status_not_first_page_absence(self):
        panel = self.block_panel
        panel._read_done(contacts(42))
        panel.items.setCurrentRow(0)
        with patch('maxclient.ui.blocked_contacts_panel.confirm_action', return_value=True):
            panel.unblock_button.click()
        self.assertIs(self.block_run.call_args.args[0], self.service.unblock_contact)
        panel._write_done({'accepted': True, 'pending': True, 'contactId': 42,
                           'blocked': None, 'snapshot': contacts()})
        self.assertIsNotNone(panel._pending)
        self.assertFalse(panel.unblock_button.isEnabled())
        panel.refresh()
        panel._read_done(contacts())
        self.assertIs(self.block_run.call_args.args[0], self.service.get_contact_block_status)
        panel._target_done({'contactId': 42, 'blocked': True, 'contact': contacts(42)['contacts'][0]})
        self.assertIsNotNone(panel._pending)
        panel._target_done({'contactId': 42, 'blocked': False,
                            'contact': {'id': 42, 'updateTime': STAMP, 'names': []}})
        self.assertIsNone(panel._pending)
        self.assertIn('разблокирован', panel.status.text())

    def test_block_action_requires_confirmation_exact_id_and_no_repeat_after_unknown(self):
        panel = self.block_panel
        with patch('maxclient.ui.blocked_contacts_panel.confirm_action', return_value=False):
            self.assertFalse(panel.request_block(42, '<b>Тест</b>'))
        self.block_run.assert_not_called()
        with patch('maxclient.ui.blocked_contacts_panel.confirm_action', return_value=True):
            self.assertTrue(panel.request_block(42, '<b>Тест</b>'))
            self.assertIs(self.block_run.call_args.args[0], self.service.block_contact)
            self.assertEqual(self.block_run.call_args.args[1], 42)
            panel._write_error('synthetic secret detail')
            self.assertFalse(panel.request_block(42, 'Тест'))
        self.assertEqual(self.block_run.call_count, 1)
        self.assertNotIn('synthetic secret detail', panel.status.text())
        self.assertFalse(panel.request_block(True, 'Тест'))

    def test_invalid_block_snapshot_or_mismatched_ack_never_reports_success(self):
        panel = self.block_panel
        panel._read_done(contacts(42))
        with patch('maxclient.ui.blocked_contacts_panel.confirm_action', return_value=True):
            panel.request_block(81, 'Тест')
        panel._write_done({'accepted': True, 'pending': False, 'contactId': 81,
                           'blocked': True, 'snapshot': {'contacts': []}})
        self.assertIsNotNone(panel._pending)
        self.assertNotIn('Контакт заблокирован.', panel.status.text())

    def test_confirmed_block_noop_uses_service_status_and_does_not_stay_locked(self):
        panel = self.block_panel
        with patch('maxclient.ui.blocked_contacts_panel.confirm_action', return_value=True):
            panel.request_block(42, 'Тест')
        panel._write_done({'accepted': False, 'pending': False, 'unchanged': True,
                           'contactId': 42, 'blocked': True, 'snapshot': contacts(42)})
        self.assertIsNone(panel._pending)
        self.assertIn('Контакт заблокирован.', panel.status.text())

    def test_unblock_conflicting_list_membership_keeps_unknown_locked(self):
        panel = self.block_panel
        panel._read_done(contacts(42))
        panel.items.setCurrentRow(0)
        with patch('maxclient.ui.blocked_contacts_panel.confirm_action', return_value=True):
            panel.unblock_button.click()
        panel._write_error('unknown')
        panel._read_done(contacts(42))
        panel._target_done({'contactId': 42, 'blocked': False,
                            'contact': {'id': 42, 'updateTime': STAMP, 'names': []}})
        self.assertIsNotNone(panel._pending)
        self.assertFalse(panel.unblock_button.isEnabled())

    def test_product_confirmation_defaults_no_and_renders_names_as_plain_text(self):
        inspected = []
        def close_without_confirmation(box):
            inspected.append(True)
            self.assertEqual(box.textFormat(), Qt.TextFormat.PlainText)
            self.assertIs(box.defaultButton(), box.button(QMessageBox.StandardButton.No))
            self.assertIn('<b>literal</b>', box.text())
            return QMessageBox.StandardButton.No
        with patch.object(QMessageBox, 'exec', new=close_without_confirmation):
            self.assertFalse(confirm_action(None, 'Проверка', 'Удалить «<b>literal</b>»?'))
        self.assertTrue(inspected)

    def test_synthetic_panels_render_inside_scrollable_settings_widths(self):
        for panel, name, width in [(self.folder_panel, 'folders_management', 624),
                                   (self.block_panel, 'blocked_contacts', 624),
                                   (self.folder_panel, 'folders_management_minimum', 360)]:
            if isinstance(panel, FoldersPanel):
                panel._read_done(folders())
                self.select('work')
            else:
                panel._read_done(contacts(42, 81))
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setWidget(panel)
            scroll.resize(width, 720)
            scroll.show()
            self.app.processEvents()
            self.app.processEvents()
            try:
                output = Path('.test-tmp') / f'{name}.png'
                output.parent.mkdir(exist_ok=True)
                self.assertTrue(scroll.grab().save(str(output)))
                self.assertLessEqual(panel.minimumSizeHint().width(), width)
                scroll.verticalScrollBar().setValue(scroll.verticalScrollBar().maximum())
                self.app.processEvents()
                self.assertTrue(scroll.grab().save(str(output.with_name(name + '_bottom.png'))))
            finally:
                scroll.takeWidget()
                scroll.close()


if __name__ == '__main__':
    unittest.main()
