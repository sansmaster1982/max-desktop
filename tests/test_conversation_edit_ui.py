"""Offline server detail editor and Info routing with captured synthetic sessions."""
import ast
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QLabel
from shiboken6 import isValid

from maxclient.core.i18n import SUPPORTED_LANGUAGES, translation_available
from maxclient.protocol.client import MaxWriteOutcomeUnknown
from maxclient.ui.conversation_edit_dialog import (ConversationEditDialog,
    conversation_edit_available, conversation_edit_result, conversation_edit_notice)
import test_conversation_actions_shell as info_base


def phase(context, **changes):
    state = dict(context=context, chat_id=context.chat_id, peer_id=None, action='edit',
        lifecycle_kind='conversation_edit', pending=False, confirmed=True, accepted=True,
        acknowledged=True, unchanged=False, confirmed_fields=['title', 'description'], pending_fields=[])
    state.update(changes)
    return state


def pending(context, **changes):
    values = dict(pending=True, confirmed=False, confirmed_fields=[], pending_fields=['title', 'description'])
    values.update(changes)
    return phase(context, **values)


def snapshot(context, state=None):
    return dict(context=context, chat_id=context.chat_id, peer_id=None, title='Локальный псевдоним',
        edit_mode='server', availability={'edit': True}, action_capabilities={'kind': 'group'},
        conversation_edit_state=state, action_pending=state is not None and state['pending'])


class SyntheticEditService:
    def __init__(self, context):
        self.context, self.current, self.state = context, True, None
        self.ticket = object()
        self.title, self.description = '<b>Серверное название</b>', 'Серверное описание'
        self.prepare_conversation_edit = Mock(side_effect=self.prepare)
        self.submit_conversation_edit = Mock(side_effect=self.complete)
        self.refresh_conversation_controls = Mock(side_effect=lambda _ctx:
            dict(context=self.context, lifecycle_result=self.state))

    def is_current_conversation(self, context):
        return self.current and context is self.context

    def conversation_snapshot(self, context):
        if not self.is_current_conversation(context):
            raise ValueError('Synthetic stale context')
        return snapshot(context, self.state)

    def prepare(self, context):
        is_pending = self.state is not None and self.state['pending'] is True
        return dict(context=context, chat_id=context.chat_id, kind='group', title=self.title,
            description=self.description, title_limit=200, description_limit=400,
            pending=is_pending, can_submit=not is_pending, ticket=None if is_pending else self.ticket,
            conversation_edit_state=self.state)

    def complete(self, context, **_kwargs):
        self.state = phase(context)
        return self.state


class ConversationEditDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.context = SimpleNamespace(chat_id=303, peer_id=None)
        self.service = SyntheticEditService(self.context)
        self.dialogs, self.jobs = [], []
        self.patcher = patch('maxclient.ui.conversation_edit_dialog.run_async', side_effect=self.queue)
        self.patcher.start()

    def tearDown(self):
        for dialog in reversed(self.dialogs):
            if isValid(dialog):
                dialog.reject()
                dialog.deleteLater()
                QCoreApplication.sendPostedEvents(dialog, QEvent.Type.DeferredDelete)
        self.app.processEvents()
        self.patcher.stop()

    def queue(self, fn, *args, **kwargs):
        self.jobs.append((fn, args, kwargs))

    def run_job(self):
        fn, args, kwargs = self.jobs.pop(0)
        result = fn(*args)
        kwargs['on_done'](result)
        return result

    def dialog(self, *, load=True, language='ru'):
        dialog = ConversationEditDialog(self.service, self.context, self.service.conversation_snapshot(self.context),
            language=language)
        self.dialogs.append(dialog)
        dialog.open()
        self.app.processEvents()
        dialog.start_loading()
        if load:
            self.run_job()
        return dialog

    def change(self, dialog):
        dialog.title_edit.setText(' Новое название ')
        dialog.description_edit.setPlainText(' Новое описание ')

    def test_load_actual_server_values_not_local_alias_without_write(self):
        dialog = self.dialog()
        self.assertEqual(dialog.title_edit.text(), self.service.title)
        self.assertEqual(dialog.description_edit.toPlainText(), self.service.description)
        self.assertFalse(dialog.submit_button.isEnabled())
        self.assertTrue(all(label.textFormat() == Qt.TextFormat.PlainText for label in dialog.findChildren(QLabel)))
        self.service.submit_conversation_edit.assert_not_called()

    def test_default_cancel_and_enter_in_title_never_save(self):
        for target in ('dialog', 'title'):
            dialog = self.dialog()
            self.change(dialog)
            self.assertTrue(dialog.cancel_button.isDefault())
            self.assertFalse(dialog.submit_button.isDefault())
            self.assertFalse(dialog.submit_button.autoDefault())
            if target == 'title':
                dialog.title_edit.setFocus()
            QTest.keyClick(dialog if target == 'dialog' else dialog.title_edit, Qt.Key.Key_Return)
            self.assertEqual(dialog.result(), QDialog.DialogCode.Rejected)
            self.assertFalse(dialog.isVisible())
        self.service.submit_conversation_edit.assert_not_called()

    def test_multiline_enter_edits_description_without_save(self):
        dialog = self.dialog()
        dialog.description_edit.setFocus()
        dialog.description_edit.setPlainText('Первая строка')
        QTest.keyClick(dialog.description_edit, Qt.Key.Key_End)
        QTest.keyClick(dialog.description_edit, Qt.Key.Key_Return)
        self.assertIn('\n', dialog.description_edit.toPlainText())
        self.assertTrue(dialog.isVisible())
        self.service.submit_conversation_edit.assert_not_called()

    def test_explicit_save_trim_bom_and_one_captured_submission(self):
        dialog = self.dialog()
        dialog.title_edit.setText('\ufeff Новое название\u00a0')
        dialog.description_edit.setPlainText('\ufeff Описание \ufeff')
        dialog.submit()
        dialog.submit()
        self.assertEqual(len(self.jobs), 1)
        self.run_job()
        self.service.submit_conversation_edit.assert_called_once_with(self.context,
            ticket=self.service.ticket, title='Новое название', description='Описание')
        self.assertEqual(dialog.status.text(), 'Сведения обновлены.')
        self.assertFalse(dialog.refresh_button.isVisible())

    def test_empty_description_can_clear_empty_title_cannot_save(self):
        dialog = self.dialog()
        dialog.description_edit.setPlainText('')
        self.assertTrue(dialog.submit_button.isEnabled())
        dialog.title_edit.setText('\ufeff  ')
        self.assertFalse(dialog.submit_button.isEnabled())
        self.assertEqual(dialog.validation.text(), 'Введите название.')
        dialog.submit()
        self.assertFalse(self.jobs)
        dialog.title_edit.setText('Название')
        self.assertFalse(dialog.validation.isVisible())
        self.assertEqual(dialog.status.text(), 'Пустое описание удалит прежнее описание.')
        dialog.submit()
        self.run_job()
        self.assertEqual(self.service.submit_conversation_edit.call_args.kwargs['description'], '')

    def test_utf16_emoji_limits_and_counters(self):
        dialog = self.dialog()
        dialog.title_edit.setText('😀' * 100)
        self.assertTrue(dialog.submit_button.isEnabled())
        self.assertTrue(dialog.title_count.text().startswith('200 из 200'))
        dialog.title_edit.setText('😀' * 101)
        self.assertFalse(dialog.submit_button.isEnabled())
        self.assertEqual(dialog.validation.text(), 'Название слишком длинное.')
        dialog.title_edit.setText('Название')
        dialog.description_edit.setPlainText('😀' * 201)
        self.assertFalse(dialog.submit_button.isEnabled())
        self.assertEqual(dialog.validation.text(), 'Описание слишком длинное.')

    def test_focus_out_trims_both_fields_without_write(self):
        dialog = self.dialog()
        self.change(dialog)
        self.app.sendEvent(dialog.title_edit, QEvent(QEvent.Type.FocusOut))
        self.app.sendEvent(dialog.description_edit, QEvent(QEvent.Type.FocusOut))
        self.assertEqual(dialog.title_edit.text(), 'Новое название')
        self.assertEqual(dialog.description_edit.toPlainText(), 'Новое описание')
        self.service.submit_conversation_edit.assert_not_called()

    def test_malformed_prepare_fails_closed(self):
        for changes in ({'title_limit': True}, {'title_limit': 0}, {'description_limit': -1},
                        {'description': {}}, {'title': None}, {'chat_id': 404}, {'kind': 'dialog'},
                        {'context': SimpleNamespace(chat_id=303, peer_id=None)}, {'ticket': None}):
            with self.subTest(changes=changes):
                dto = self.service.prepare(self.context)
                dto.update(changes)
                self.service.prepare_conversation_edit.return_value = dto
                self.service.prepare_conversation_edit.side_effect = None
                dialog = self.dialog()
                self.change(dialog)
                dialog.submit()
                self.assertFalse(dialog.submit_button.isEnabled())
                dialog.reject()
        self.service.submit_conversation_edit.assert_not_called()

    def test_close_before_queued_prepare_never_reads_and_late_result_ignored(self):
        dialog = self.dialog(load=False)
        fn, args, kwargs = self.jobs.pop()
        dialog.reject()
        self.assertIsNone(fn(*args))
        kwargs['on_done'](self.service.prepare(self.context))
        self.assertIsNone(dialog._ticket)
        self.service.prepare_conversation_edit.assert_not_called()

    def test_session_change_before_queued_save_never_submits(self):
        dialog = self.dialog()
        self.change(dialog)
        dialog.submit()
        self.service.current = False
        self.run_job()
        dialog._check_scope()
        self.assertFalse(dialog.isVisible())
        self.service.submit_conversation_edit.assert_not_called()

    def test_rights_lost_before_save_prevents_submission(self):
        dialog = self.dialog()
        self.change(dialog)
        self.service.conversation_snapshot = Mock(return_value={**snapshot(self.context), 'availability': {'edit': False}})
        dialog.submit()
        self.assertFalse(self.jobs)
        self.assertFalse(dialog.submit_button.isEnabled())
        self.service.submit_conversation_edit.assert_not_called()

    def test_unknown_preserves_draft_refresh_only_and_double_refresh_is_one_read(self):
        dialog = self.dialog()
        self.change(dialog)
        self.service.submit_conversation_edit.side_effect = MaxWriteOutcomeUnknown('Synthetic unknown')
        dialog.submit()
        self.run_job()
        self.assertTrue(dialog.refresh_button.isVisible())
        self.assertFalse(dialog.validation.isVisible())
        self.assertFalse(dialog.submit_button.isEnabled())
        before = dialog.title_edit.text(), dialog.description_edit.toPlainText()
        self.service.state = pending(self.context)
        dialog.refresh_state()
        dialog.refresh_state()
        dialog.submit()
        self.assertEqual(len(self.jobs), 1)
        self.run_job()
        self.assertEqual((dialog.title_edit.text(), dialog.description_edit.toPlainText()), before)
        self.service.submit_conversation_edit.assert_called_once()
        self.service.prepare_conversation_edit.assert_called_once()
        self.service.refresh_conversation_controls.assert_called_once_with(self.context)

    def test_pending_reopen_reads_fresh_values_and_never_gets_ticket_or_submits(self):
        self.service.state = pending(self.context)
        dialog = self.dialog()
        self.assertEqual(dialog.title_edit.text(), self.service.title)
        self.assertIsNone(dialog._ticket)
        self.assertFalse(dialog.title_edit.isEnabled())
        self.change(dialog)
        dialog.submit()
        self.assertFalse(self.jobs)
        self.assertTrue(dialog.refresh_button.isVisible())
        self.service.submit_conversation_edit.assert_not_called()

    def test_partial_refresh_then_confirmation_never_repeats_save(self):
        self.service.state = pending(self.context, confirmed_fields=['title'], pending_fields=['description'])
        dialog = self.dialog()
        self.assertIn('Название подтверждено; описание', dialog.status.text())
        self.service.state = phase(self.context)
        dialog.refresh_state()
        self.run_job()
        self.assertEqual(dialog.status.text(), 'Сведения обновлены.')
        self.assertFalse(dialog.submit_button.isEnabled())
        self.service.submit_conversation_edit.assert_not_called()

    def test_unrelated_result_not_success(self):
        dialog = self.dialog()
        self.change(dialog)
        self.service.submit_conversation_edit.side_effect = None
        self.service.submit_conversation_edit.return_value = phase(self.context, chat_id=404)
        dialog.submit()
        self.run_job()
        self.assertNotEqual(dialog.status.text(), 'Сведения обновлены.')

    def test_failed_refresh_preserves_known_partial_and_never_enables_save(self):
        self.service.state = pending(self.context, confirmed_fields=['title'], pending_fields=['description'])
        dialog = self.dialog()
        self.service.refresh_conversation_controls.side_effect = RuntimeError('Synthetic failed read')
        dialog.refresh_state()
        self.run_job()
        self.assertIn('последний известный результат', dialog.status.text())
        self.assertIn('Название подтверждено; описание', dialog.status.text())
        self.assertFalse(dialog.submit_button.isEnabled())
        self.assertTrue(dialog.refresh_button.isVisible())
        self.service.submit_conversation_edit.assert_not_called()

    def test_direct_accept_has_no_write(self):
        dialog = self.dialog()
        self.change(dialog)
        dialog.accept()
        self.assertFalse(self.jobs)
        self.service.submit_conversation_edit.assert_not_called()

    def test_real_core_dto_and_explicit_save_only(self):
        from test_conversation_edit_core import ConversationEditCoreTests
        fixture = ConversationEditCoreTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.service, self.context = fixture.s, fixture.context
        dialog = self.dialog()
        self.assertEqual(dialog.title_edit.text(), 'Synthetic group')
        self.service.client.edit_conversation_fields.assert_not_called()
        self.change(dialog)
        dialog.submit()
        self.run_job()
        self.assertEqual(dialog.status.text(), 'Сведения обновлены.')
        self.service.client.edit_conversation_fields.assert_called_once()
        self.service.client.request.assert_not_called()

    def test_real_core_unknown_partial_readback_keeps_single_write(self):
        from test_conversation_edit_core import ConversationEditCoreTests
        fixture = ConversationEditCoreTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.service, self.context = fixture.s, fixture.context
        dialog = self.dialog()
        self.change(dialog)
        def unknown(_chat, _kind, patch, **_kw):
            fixture.remote['title'] = patch['theme']
            raise MaxWriteOutcomeUnknown('Synthetic unknown')
        self.service.client.edit_conversation_fields.side_effect = unknown
        dialog.submit()
        self.run_job()
        dialog.refresh_state()
        self.run_job()
        self.assertIn('Название подтверждено; описание', dialog.status.text())
        self.assertFalse(dialog.submit_button.isEnabled())
        self.service.client.edit_conversation_fields.assert_called_once()
        self.service.client.request.assert_not_called()

    def test_new_dialog_strings_are_translated_for_all_seven_languages(self):
        import maxclient.ui.conversation_edit_dialog as module
        tree = ast.parse(Path(module.__file__).read_text(encoding='utf-8'))
        strings = [node.args[0].value for node in ast.walk(tree)
                   if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == 'tr'
                   and node.args and isinstance(node.args[0], ast.Constant)]
        strings += ['Изменения не отправлены. Откройте форму заново.', 'MAX отклонил изменение сведений.']
        for language in SUPPORTED_LANGUAGES:
            for text in strings:
                self.assertTrue(translation_available(text, language), (text, language))


class ConversationEditResultTests(unittest.TestCase):
    def test_strict_context_target_action_phase_and_fields(self):
        context = SimpleNamespace(chat_id=303, peer_id=None)
        self.assertIsNotNone(conversation_edit_result(phase(context), context))
        for changes in ({'context': object()}, {'chat_id': True}, {'chat_id': 404}, {'peer_id': 202},
                        {'action': 'delete'}, {'lifecycle_kind': None}, {'pending': True},
                        {'pending_fields': ['title']}, {'confirmed_fields': ['other']},
                        {'confirmed_fields': ['title', 'title']}, {'pending_fields': None}):
            self.assertIsNone(conversation_edit_result({**phase(context), **changes}, context), changes)
        self.assertIsNone(conversation_edit_result({'context': object(), 'lifecycle_result': phase(context)}, context, refresh=True))

    def test_notice_does_not_invent_an_unrequested_pending_field(self):
        context = SimpleNamespace(chat_id=303, peer_id=None)
        state = phase(context, confirmed=False, pending=True, confirmed_fields=['title'], pending_fields=[])
        self.assertNotIn('описание', conversation_edit_notice(state))


class ConversationEditInfoTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        info_base.ConversationActionsShellTests.setUpClass()

    def fixture(self):
        fixture = info_base.ConversationActionsShellTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        return fixture

    def test_group_routes_server_dialog_and_preserves_contact_local_route(self):
        fixture = self.fixture()
        window, context = fixture.window, fixture.context
        group = SimpleNamespace(id=context.chat_id, is_group=True, is_channel=False, peer_user_id=None)
        with patch.object(window, '_display_chat', return_value=group), \
             patch.object(window, '_open_conversation_editor') as server, patch.object(window, '_rename_chat') as local:
            window._info_action('edit', context)
            server.assert_called_once_with(context)
            local.assert_not_called()
        contact = SimpleNamespace(id=context.chat_id, is_group=False, is_channel=False, peer_user_id=202)
        with patch.object(window, '_display_chat', return_value=contact), \
             patch.object(window, '_edit_conversation_contact', return_value=True) as local, \
             patch.object(window, '_open_conversation_editor') as server:
            window._info_action('edit', context)
            local.assert_called_once_with(202)
            server.assert_not_called()

    def test_pending_edit_entry_opens_read_only_even_when_write_actions_blocked(self):
        fixture = self.fixture()
        context = fixture.context
        context.peer_id = None
        data = snapshot(context, pending(context))
        data['read_only'] = True
        fixture.window._info_panel.clear()
        fixture.window._info_panel.set_conversation(data)
        self.assertTrue(conversation_edit_available(data, context))
        self.assertTrue(fixture.window._info_panel.edit_button.isEnabled())
        data['conversation_edit_state'] = None
        fixture.window._info_panel.set_conversation(data)
        self.assertFalse(fixture.window._info_panel.edit_button.isEnabled())

    def test_info_close_invalidates_queued_editor_before_prepare(self):
        fixture = self.fixture()
        context = fixture.context
        context.peer_id = None
        service = fixture.service
        service.conversation_snapshot = Mock(return_value=snapshot(context))
        service.prepare_conversation_edit = Mock(side_effect=AssertionError('No read after close'))
        with patch('maxclient.ui.conversation_edit_dialog.run_async') as jobs:
            fixture.window._open_conversation_editor(context)
            self.assertIsNotNone(fixture.window._conversation_edit_dialog)
            fixture.window._close_conversation_info()
            self.assertIsNone(fixture.window._conversation_edit_dialog)
            self.assertIsNone(jobs.call_args.args[0]())
        service.prepare_conversation_edit.assert_not_called()
