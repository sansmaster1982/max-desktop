"""Standalone folder editor and native template contracts, synthetic/offline only."""
import copy
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import unittest
from unittest.mock import patch

from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtWidgets import QApplication, QDialog
from shiboken6 import isValid

from maxclient.protocol.client import AuthenticatedSession, MaxWriteNotStarted
from maxclient.ui.folders_panel import FoldersPanel, ALL_FOLDER
from maxclient.ui.theme import build_stylesheet
from maxclient.ui.widgets.toggle_switch import ToggleSwitch
from test_folders_blocks_ui import folders, service


class FolderEditorParityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.runner = patch('maxclient.ui.folders_panel.run_async')
        self.run = self.runner.start()
        self.addCleanup(self.runner.stop)
        self.service = service()
        self.service.context = (101, object(), AuthenticatedSession(101, 'synthetic', 'WEB', object()))
        self.service._history_read_context.side_effect = lambda: self.service.context
        def require(context):
            current = self.service.context
            if context[0] != current[0] or context[1] is not current[1] or context[2] is not current[2]:
                raise MaxWriteNotStarted('Synthetic session replacement')
        self.service._require_history_context.side_effect = require
        self.panel = FoldersPanel(self.service)
        self.snapshot = folders()
        self.snapshot['writePending'] = False
        self.panel._read_done(copy.deepcopy(self.snapshot))
        self.panel.resize(600, 720)
        self.panel.show()
        self.app.processEvents()

    def tearDown(self):
        self.panel.close()
        self.panel.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()

    def select(self, identity='work'):
        for row in range(self.panel.items.count()):
            item = self.panel.items.item(row)
            if item.data(Qt.ItemDataRole.UserRole) == identity:
                self.panel.items.setCurrentItem(item)
                return
        self.fail('Missing synthetic folder')

    def load_work(self, **changes):
        self.snapshot['orderedFolders'][1].update(changes)
        self.panel._read_done(copy.deepcopy(self.snapshot))
        self.select()

    def choose(self, dialog, kind, identity):
        item = next(dialog.items.item(row) for row in range(dialog.items.count())
                    if dialog.items.item(row).data(Qt.ItemDataRole.UserRole) == (kind, identity))
        dialog._toggle(item)

    def switch_session(self):
        owner, client, _session = self.service.context
        self.service.context = (owner, client, AuthenticatedSession(owner, 'synthetic', 'WEB', object()))

    def run_worker(self, *, deliver=False):
        call = self.run.call_args
        function, *args = call.args
        kwargs = dict(call.kwargs)
        done, failed = kwargs.pop('on_done'), kwargs.pop('on_error')
        try:
            result = function(*args, **kwargs)
        except Exception:
            if not deliver:
                raise
            failed('Synthetic worker failure')
        else:
            if deliver:
                done(result)
            return result

    def test_editor_replaces_overview_and_cancel_discards_only_local_draft(self):
        self.panel.new_button.click()
        self.assertTrue(self.panel.main_card.isHidden())
        self.assertTrue(self.panel.templates.isHidden())
        self.assertFalse(self.panel.editor.isHidden())
        self.assertEqual(self.panel.title.placeholderText(), 'Название папки')
        self.assertEqual(self.panel.chats_heading.text(), 'ЧАТЫ В ПАПКЕ')
        self.assertEqual(self.panel.show_only_heading.text(), 'ПОКАЗЫВАТЬ ТОЛЬКО')
        self.assertEqual(self.panel.save_button.text(), 'Создать папку')
        self.assertEqual(self.panel.cancel_button.text(), 'Отменить')
        self.assertTrue(self.panel.advanced.isHidden())
        self.panel.title.setText('Unsubmitted draft')
        self.panel.cancel_button.click()
        self.assertTrue(self.panel.editor.isHidden())
        self.assertFalse(self.panel.main_card.isHidden())
        self.panel.new_button.click()
        self.assertEqual(self.panel.title.text(), '')
        self.run.assert_not_called()

    def test_existing_editor_has_only_two_primary_toggles_and_keeps_advanced_values(self):
        flags = [1, 4, 5, 6, 7, 8, 9, 12, 13, 14]
        self.load_work(filters=flags, include=[42, 2**48], favorites=[81, 42])
        self.assertIsInstance(self.panel.filters[0], ToggleSwitch)
        self.assertIsInstance(self.panel.filters[11], ToggleSwitch)
        self.assertEqual(self.panel.filters[11].text(), 'Чаты со звуком')
        self.assertEqual(self.panel.filters[0].text(), 'Непрочитанные чаты')
        self.assertTrue(self.panel.advanced.isHidden())
        self.assertEqual(set(self.panel._values()['filters']), set(flags))
        self.assertEqual(self.panel._values()['include'], [42, 2**48])
        self.assertEqual(self.panel._values()['favorites'], [81, 42])
        self.assertEqual(self.panel._patch(), {})
        self.panel.advanced_button.click()
        self.assertFalse(self.panel.advanced.isHidden())
        self.assertTrue(self.panel.up_button.isVisibleTo(self.panel))
        self.run.assert_not_called()

    def test_title_only_update_does_not_drop_unedited_fields(self):
        self.load_work(filters=[1, 7, 12, 8], include=[42, 2**48], favorites=[81, 42], options=[4])
        self.panel.title.setText('Changed title')
        self.panel.save_button.click()
        self.run_worker()
        fields = self.service.update_folder.call_args.kwargs
        self.assertEqual(fields, {'title': 'Changed title', 'context': self.service.context})
        self.assertEqual(self.panel._baseline['options'], [4])

    def test_primary_toggles_change_only_their_bits(self):
        self.load_work(filters=[1, 7, 8, 12, 14])
        self.panel.filters[0].setChecked(True)
        self.panel.filters[11].setChecked(True)
        expected = {0, 1, 7, 8, 11, 12, 14}
        self.assertEqual(set(self.panel._values()['filters']), expected)
        self.assertEqual(set(self.panel.include.filter_values()), expected)
        self.assertEqual(set(self.panel._patch()), {'filters'})
        self.panel.filters[0].setChecked(False)
        self.assertIn(1, self.panel._values()['filters'])
        self.assertIn(12, self.panel._values()['filters'])

    def test_picker_category_commit_touches_filters_without_include(self):
        self.load_work(filters=[7, 12], include=[42])
        dialog = self.panel.include.open_dialog()
        self.assertIsNotNone(dialog)
        self.choose(dialog, 'filter', 8)
        dialog.accept()
        self.assertEqual(dialog.result(), QDialog.DialogCode.Accepted)
        self.assertEqual(self.panel._patch(), {'filters': [7, 8, 12]})
        self.assertEqual(self.panel.include.values(), [42])
        self.run.assert_not_called()

    def test_picker_chat_commit_touches_only_include_and_cancel_changes_nothing(self):
        self.load_work(filters=[0, 8], include=[42])
        dialog = self.panel.include.open_dialog()
        self.choose(dialog, 'chat', 90)
        dialog.reject()
        self.assertEqual(self.panel._patch(), {})
        dialog = self.panel.include.open_dialog()
        self.choose(dialog, 'chat', 90)
        dialog.accept()
        self.assertEqual(self.panel._patch(), {'include': [42, 90]})
        self.assertEqual(set(self.panel.include.filter_values()), {0, 8})

    def test_locked_filters_still_allow_chat_selection_without_category_changes(self):
        self.select('locked')
        self.assertFalse(self.panel.title.isEnabled())
        self.assertFalse(self.panel.show_only_card.isEnabled())
        self.assertFalse(self.panel.filter_widget.isEnabled())
        self.assertTrue(self.panel.include.isEnabled())
        dialog = self.panel.include.open_dialog()
        self.choose(dialog, 'filter', 8)
        self.choose(dialog, 'chat', 90)
        dialog.accept()
        self.assertEqual(self.panel._patch(), {'include': [42, 90]})
        self.assertEqual(self.panel._values()['filters'], [2])
        self.panel.filters[0].setChecked(True)
        self.panel.save_button.click()
        self.run_worker()
        self.assertNotIn('filters', self.service.update_folder.call_args.kwargs)
        self.assertEqual(self.service.update_folder.call_args.kwargs['include'], [42, 90])

    def test_refresh_invalidates_picker_and_old_accept_cannot_change_draft(self):
        self.select()
        dialog = self.panel.include.open_dialog()
        self.choose(dialog, 'chat', 90)
        self.panel.refresh()
        self.assertEqual(dialog.result(), QDialog.DialogCode.Rejected)
        if isValid(dialog):
            dialog.accept()
        self.assertEqual(self.panel.include.values(), [42])
        self.assertEqual(self.panel._touched, set())
        self.assertEqual(self.run.call_count, 1)  # Only an explicitly queued read.
        self.service.create_folder.assert_not_called()

    def test_session_change_invalidates_picker_commit_and_save(self):
        self.select()
        dialog = self.panel.include.open_dialog()
        self.choose(dialog, 'chat', 90)
        self.switch_session()
        dialog.accept()
        self.assertEqual(dialog.result(), QDialog.DialogCode.Rejected)
        self.assertEqual(self.panel.include.values(), [42])
        self.panel.title.setText('Stale draft')
        self.panel._save()
        self.assertFalse(self.panel.save_button.isEnabled())
        self.run.assert_not_called()

    def test_language_and_theme_relabel_without_mutation_and_close_picker(self):
        self.load_work(filters=[0, 7, 8, 12], include=[42, 2**48])
        expected = copy.deepcopy(self.panel._values())
        dialog = self.panel.include.open_dialog()
        self.choose(dialog, 'chat', 90)
        self.panel.set_language('en')
        self.assertEqual(dialog.result(), QDialog.DialogCode.Rejected)
        self.panel.apply_theme('dark')
        self.assertEqual(self.panel._values(), expected)
        self.assertEqual(self.panel._touched, set())
        self.assertNotEqual(self.panel.title.placeholderText(), 'Название папки')
        self.assertEqual(self.panel.title.text(), '<b>Работа</b>')
        self.run.assert_not_called()

    def test_pending_partial_readback_preserves_draft_and_blocks_repeat(self):
        self.select()
        self.panel.title.setText('Pending title')
        self.panel.filters[11].setChecked(True)
        expected = copy.deepcopy(self.panel._values())
        self.panel.save_button.click()
        pending = self.panel._pending
        self.panel._write_done({'accepted': True, 'pending': True, 'folderId': 'work',
                                'snapshot': copy.deepcopy(self.snapshot)})
        self.assertEqual(self.panel._values(), expected)
        self.assertIs(self.panel._pending, pending)
        self.assertFalse(self.panel.save_button.isEnabled())
        self.assertFalse(self.panel.cancel_button.isEnabled())
        self.assertIsNone(self.panel.include.open_dialog())
        self.panel._save()
        self.assertEqual(self.run.call_count, 1)
        self.assertTrue(self.panel.main_card.isHidden())

    def test_unknown_create_keeps_draft_until_actual_service_confirmation(self):
        self.panel.new_button.click()
        self.panel.title.setText('Pending create')
        self.panel.filters[11].setChecked(True)
        self.panel.save_button.click()
        self.panel._write_error('Synthetic unknown')
        snapshot = copy.deepcopy(self.snapshot)
        snapshot['writePending'] = True
        self.panel._read_done(snapshot)
        self.assertTrue(self.panel._creating)
        self.assertEqual(self.panel.title.text(), 'Pending create')
        self.assertTrue(self.panel.filters[11].isChecked())
        self.assertIsNotNone(self.panel._pending)
        self.assertFalse(self.panel.save_button.isEnabled())
        self.assertFalse(self.panel.back_button.isEnabled())
        self.panel._close_editor()
        self.assertEqual(self.panel.title.text(), 'Pending create')
        self.assertTrue(self.panel.filters[11].isChecked())
        self.assertFalse(self.panel.editor.isHidden())
        self.assertEqual(self.run.call_count, 1)

    def test_template_predicates_use_filters_not_names(self):
        self.snapshot['orderedFolders'][1].update(title='Личные', filters=[])
        self.panel._read_done(copy.deepcopy(self.snapshot))
        self.assertFalse(self.panel.template_cards['new'].isHidden())
        self.assertFalse(self.panel.template_cards['personal'].isHidden())
        self.assertEqual(self.panel.templates_heading.text(), 'Шаблоны')
        self.assertFalse(self.panel.template_buttons['personal'].icon().isNull())
        self.assertEqual(self.panel.template_buttons['personal'].text(), '')
        self.assertEqual(self.panel.template_buttons['personal'].accessibleName(), 'Добавить: Личные')
        self.snapshot['orderedFolders'][1].update(title='Different title', filters=[0, 8, 9, 14])
        self.panel._read_done(copy.deepcopy(self.snapshot))
        self.assertTrue(self.panel.template_cards['new'].isHidden())
        self.assertTrue(self.panel.template_cards['personal'].isHidden())
        self.assertNotIn('channels', self.panel.template_cards)

    def test_explicit_new_template_sends_exact_supported_payload_once(self):
        self.snapshot['orderedFolders'][1]['filters'] = []
        self.panel._read_done(copy.deepcopy(self.snapshot))
        self.panel.template_buttons['new'].click()
        self.run_worker()
        self.assertEqual(self.service.create_folder.call_args.args, ('Новые',))
        self.assertEqual(self.service.create_folder.call_args.kwargs['include'], [])
        self.assertEqual(self.service.create_folder.call_args.kwargs['filters'], [0])
        self.assertNotIn('options', self.service.create_folder.call_args.kwargs)
        self.panel._write_error('Synthetic unknown')
        self.panel._create_template('new')
        self.assertEqual(self.run.call_count, 1)
        self.assertIsNotNone(self.panel._pending)

    def test_explicit_personal_template_and_missing_records_are_not_guessed(self):
        self.panel.template_buttons['personal'].click()
        self.run_worker()
        self.assertEqual(self.service.create_folder.call_args.args, ('Личные',))
        self.assertEqual(self.service.create_folder.call_args.kwargs['filters'], [8, 9])
        self.assertEqual(self.service.create_folder.call_args.kwargs['include'], [])
        self.panel._pending = None
        self.snapshot['orderedFolders'][1] = None
        self.panel._read_done(copy.deepcopy(self.snapshot))
        self.assertTrue(self.panel.template_cards['new'].isHidden())
        self.assertTrue(self.panel.template_cards['personal'].isHidden())
        self.panel._create_template('personal')
        self.assertEqual(self.run.call_count, 1)

    def test_template_click_with_stale_session_does_not_queue_write(self):
        self.switch_session()
        self.panel._create_template('personal')
        self.run.assert_not_called()

    def test_all_folder_guards_and_manual_order_controls_remain_available(self):
        self.select(ALL_FOLDER)
        self.assertFalse(self.panel.title.isEnabled())
        self.assertFalse(self.panel.delete_button.isEnabled())
        self.assertFalse(self.panel.up_button.isEnabled())
        self.assertFalse(self.panel.down_button.isEnabled())
        self.assertTrue(self.panel.favorites.isEnabled())
        self.select()
        self.panel.advanced_button.click()
        self.assertTrue(self.panel.down_button.isEnabled())
        self.panel.down_button.click()
        self.run_worker()
        self.assertEqual(self.service.reorder_folders.call_args.args, ([ALL_FOLDER, 'locked', 'work'],))
        self.assertEqual(self.service.reorder_folders.call_args.kwargs['expected_order'], [ALL_FOLDER, 'work', 'locked'])
        self.assertIs(self.service.reorder_folders.call_args.kwargs['context'], self.service.context)

    def test_save_queued_before_reconnect_cannot_invoke_core(self):
        self.select()
        self.panel.title.setText('Queued draft')
        self.panel.save_button.click()
        self.switch_session()
        self.run_worker(deliver=True)
        self.service.update_folder.assert_not_called()
        self.assertIsNone(self.panel._pending)
        self.assertFalse(self.panel._read_ok)
        self.assertFalse(self.panel.save_button.isEnabled())

    def test_template_queued_before_owner_switch_cannot_invoke_core(self):
        self.panel.template_buttons['personal'].click()
        self.service.context = (404, object(), AuthenticatedSession(404, 'synthetic', 'WEB', object()))
        self.run_worker(deliver=True)
        self.service.create_folder.assert_not_called()
        self.assertIsNone(self.panel._pending)
        self.assertFalse(self.panel._read_ok)

    def test_change_after_method_start_remains_unknown_and_blocks_repeat(self):
        self.select()
        self.panel.title.setText('In-flight draft')
        def changed(*_args, **_kwargs):
            self.switch_session()
            return {'accepted': True, 'pending': False}
        self.service.update_folder.side_effect = changed
        self.panel.save_button.click()
        self.run_worker(deliver=True)
        self.service.update_folder.assert_called_once()
        self.assertIsNotNone(self.panel._pending)
        self.panel._save()
        self.assertEqual(self.run.call_count, 1)

    def test_editor_callback_rejects_unbound_success_result(self):
        self.select()
        self.panel.title.setText('Unbound result')
        self.service.update_folder.return_value = {'accepted': True, 'pending': False,
                'folderId': 'work', 'snapshot': copy.deepcopy(self.snapshot)}
        self.panel.save_button.click()
        self.run_worker(deliver=True)
        self.assertIsNotNone(self.panel._pending)
        self.assertFalse(self.panel._read_ok)
        self.assertEqual(self.panel.title.text(), 'Unbound result')

    def test_missing_producing_context_never_queues_editor_or_template_writes(self):
        self.select()
        self.panel.title.setText('Unbound draft')
        self.panel._snapshot_context = None
        self.panel._apply_enabled()
        self.assertFalse(self.panel.save_button.isEnabled())
        self.assertFalse(self.panel.new_button.isEnabled())
        self.assertIsNone(self.panel.include.open_dialog())
        self.panel._save()
        self.panel._create_template('personal')
        self.panel._start_write({'kind': 'create', 'id': None, 'patch': {}}, self.service.create_folder, 'No context')
        self.run.assert_not_called()
        self.service.create_folder.assert_not_called()
        self.service.update_folder.assert_not_called()

