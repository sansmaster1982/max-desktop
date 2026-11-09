"""Offline creation flows through Qt clicks, with controlled asynchronous replies."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import unittest
from unittest.mock import patch

from PySide6.QtCore import QCoreApplication, QEvent, QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog
from shiboken6 import isValid

from maxclient.core.conversation_create import ConversationCreateResult
from maxclient.core.models import Chat, Contact
from maxclient.protocol.client import MaxFrame, MaxTimeout, MaxWriteOutcomeUnknown
from maxclient.ui.conversation_create_dialog import ConversationCreateDialog, _UI_WRITES
import test_conversation_create_core as core_fixture


class FakeService:
    def __init__(self):
        self.context = object()
        self.my_id = 7
        self.contacts = [Contact(7, 'Self'), Contact(41, 'Alice', '+70000000041'),
                         Contact(2 ** 40 + 11, 'Bob', '+70000000042')]
        self.calls = []
        self.pending = None
        self.create_result = None
        self.verify_result = None
        self.limits = {'max-cname-length': 200, 'max-description-length': 400,
                       'max-added-participants': 100, 'max-participants': 20000}

    def search_context(self):
        return self.context

    def list_contacts(self):
        return list(self.contacts)

    def conversation_creation_limits(self, *, context):
        if context is not self.context:
            raise ValueError('Session changed')
        return dict(self.limits)

    def pending_conversation_creation(self, *, context):
        if context is not self.context:
            raise ValueError('Session changed')
        return self.pending

    def create_conversation(self, kind, title, member_ids=(), description='', *, context):
        self.calls.append(('create', kind, title, member_ids, description, context))
        result = self.create_result
        if isinstance(result, Exception):
            self.pending = {'kind': kind, 'stage': 'create', 'has_chat_id': False, 'chat_id': None}
            raise result
        if result is not None:
            return result
        chat = Chat(-1234567890123, title=title, is_group=kind == 'group',
                    is_channel=kind == 'channel', chat_type='CHAT' if kind == 'group' else 'CHANNEL')
        return ConversationCreateResult(chat, True)

    def verify_conversation_creation(self, *, context):
        self.calls.append(('verify', context))
        result = self.verify_result
        if isinstance(result, Exception):
            raise result
        if isinstance(result, ConversationCreateResult) and result.description_confirmed:
            self.pending = None
        return result


class ConversationCreateDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.service = FakeService()
        self.dialogs = []
        self.jobs = []
        self.saved = []
        runner = patch('maxclient.ui.conversation_create_dialog.run_async', side_effect=self.queue)
        self.runner = runner.start()
        self.addCleanup(runner.stop)

    def tearDown(self):
        for dialog in self.dialogs:
            if isValid(dialog):
                dialog.close()
                dialog.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()
        _UI_WRITES.pop(self.service, None)

    def open(self, kind='group'):
        dialog = ConversationCreateDialog(self.service, kind=kind)
        dialog.conversation_created.connect(self.saved.append)
        self.dialogs.append(dialog)
        dialog.show()
        self.app.processEvents()
        return dialog

    def queue(self, function, *args, on_done, on_error, **kwargs):
        self.jobs.append((function, args, kwargs, on_done, on_error))

    def finish(self, index=0):
        function, args, kwargs, done, error = self.jobs[index]
        try:
            result = function(*args, **kwargs)
        except Exception as exc:
            error(str(exc))
        else:
            done(result)
        self.app.processEvents()

    def click(self, widget):
        QTest.mouseClick(widget, Qt.MouseButton.LeftButton)
        self.app.processEvents()

    def type(self, field, text):
        field.setFocus()
        QTest.keyClicks(field, text)
        self.app.processEvents()

    def select(self, dialog, row):
        item = dialog.member_list.item(row)
        rect = dialog.member_list.visualItemRect(item)
        # Use the real Qt checkbox hit area rather than invoking an internal slot.
        QTest.mouseClick(dialog.member_list.viewport(), Qt.MouseButton.LeftButton,
                         pos=QPoint(rect.left() + 19, rect.center().y()))
        self.app.processEvents()
        self.assertEqual(item.checkState(), Qt.CheckState.Checked)

    def title_page(self, kind='group', title='Synthetic test'):
        dialog = self.open(kind)
        if kind == 'group':
            self.click(dialog.next_button)
        self.type(dialog.title_input, title)
        return dialog

    def use_real_service(self):
        """Use actual Store/MaxService/MaxClient with only the wire boundary faked."""
        fixture = core_fixture.ConversationCreateCoreTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.service = fixture.service
        return fixture

    def test_final_group_click_reaches_real_service_and_caches_actual_readback_chat(self):
        fixture = self.use_real_service()
        fixture.store.upsert_contact(Contact(202, 'Synthetic member'))
        dialog = self.open()
        self.select(dialog, 0)
        self.click(dialog.next_button)
        self.type(dialog.title_input, 'Synthetic title')
        self.click(dialog.create_button)
        fixture.client.request.assert_not_called()
        self.assertEqual(self.saved, [])
        self.finish()
        self.assertEqual(fixture.codes(), [64, 48])
        control = fixture.client.request.call_args_list[0].args[1]['message']['attaches'][0]
        self.assertEqual(control, {'_type': 'CONTROL', 'event': 'new', 'chatType': 'CHAT',
                                   'title': 'Synthetic title', 'userIds': [202]})
        self.assertEqual(self.saved, [fixture.store.get_chat(-701)])
        self.assertTrue(self.saved[0].is_group)
        self.assertIsNone(fixture.store.load_conversation_creation(101))
        self.assertEqual(dialog.result(), QDialog.DialogCode.Accepted)

    def test_final_channel_click_reaches_real_create_description_and_both_readbacks(self):
        fixture = self.use_real_service()
        dialog = self.title_page('channel', 'Synthetic title')
        self.type(dialog.description_input, 'Synthetic description')
        self.click(dialog.create_button)
        self.finish()
        self.assertEqual(fixture.codes(), [64, 48, 55, 48])
        self.assertEqual(fixture.client.request.call_args_list[2].args,
                         (55, {'chatId': -701, 'description': 'Synthetic description'}))
        self.assertEqual(self.saved, [fixture.store.get_chat(-701)])
        self.assertTrue(self.saved[0].is_channel)
        self.assertEqual(self.service._server_chats[-701]['description'], 'Synthetic description')
        self.assertIsNone(fixture.store.load_conversation_creation(101))

    def test_real_description_rejection_can_open_created_channel_without_false_verify_offer(self):
        fixture = self.use_real_service()
        def reply(opcode, *args, **kwargs):
            if opcode == 55:
                return MaxFrame(3, 1, opcode, b'', {'error': 'Synthetic rejection'})
            return fixture.reply(opcode, *args, **kwargs)
        fixture.client.request.side_effect = reply
        dialog = self.title_page('channel', 'Synthetic title')
        self.type(dialog.description_input, 'Synthetic description')
        self.click(dialog.create_button)
        self.finish()
        self.assertEqual(fixture.codes(), [64, 48, 55])
        self.assertEqual(self.saved, [])
        self.assertIsNone(fixture.store.load_conversation_creation(101))
        self.assertTrue(dialog.verify_button.isHidden())
        self.assertNotIn('проверить', dialog.result_label.text())
        self.assertIn('описание не сохранено', dialog.result_label.text())
        self.click(dialog.open_button)
        self.assertEqual(self.saved, [fixture.store.get_chat(-701)])
        self.assertEqual(fixture.codes(), [64, 48, 55])

    def test_real_description_unknown_is_resolved_by_read_only_verification_click(self):
        fixture = self.use_real_service()
        def reply(opcode, *args, **kwargs):
            frame = fixture.reply(opcode, *args, **kwargs)
            if opcode == 55:
                raise MaxTimeout('Synthetic applied update with lost acknowledgement')
            return frame
        fixture.client.request.side_effect = reply
        dialog = self.title_page('channel', 'Synthetic title')
        self.type(dialog.description_input, 'Synthetic description')
        self.click(dialog.create_button)
        self.finish()
        self.assertEqual(fixture.codes(), [64, 48, 55])
        self.assertEqual(self.saved, [])
        self.assertTrue(dialog.verify_button.isEnabled())
        fixture.client.request.side_effect = fixture.reply
        self.click(dialog.verify_button)
        self.finish(1)
        self.assertEqual(fixture.codes(), [64, 48, 55, 48])
        self.assertEqual(self.saved, [fixture.store.get_chat(-701)])
        self.assertIsNone(fixture.store.load_conversation_creation(101))

    def test_group_members_then_title_creates_exactly_once_after_final_click(self):
        dialog = self.open()
        self.assertEqual(dialog.member_list.count(), 2)
        self.select(dialog, 1)
        self.click(dialog.next_button)
        self.assertEqual(dialog.pages.currentIndex(), 1)
        self.assertFalse(dialog.create_button.isEnabled())
        self.type(dialog.title_input, 'Team')
        self.assertEqual(self.jobs, [])
        QTest.keyClick(dialog.title_input, Qt.Key.Key_Return)
        self.assertEqual(self.jobs, [])
        self.click(dialog.create_button)
        self.click(dialog.create_button)
        self.assertEqual(len(self.jobs), 1)
        self.assertEqual(self.service.calls, [])
        self.finish()
        self.assertEqual(self.service.calls, [('create', 'group', 'Team', [2 ** 40 + 11], '', self.service.context)])
        self.assertEqual(len(self.saved), 1)
        self.assertEqual(self.saved[0].id, -1234567890123)
        self.assertEqual(dialog.result(), QDialog.DialogCode.Accepted)

    def test_empty_group_is_explicit_and_supported(self):
        dialog = self.title_page()
        self.click(dialog.create_button)
        self.finish()
        self.assertEqual(self.service.calls[0][3], [])

    def test_channel_title_and_description_do_not_select_or_invite_contacts(self):
        dialog = self.title_page('channel', 'News')
        self.assertEqual(dialog.pages.currentIndex(), 1)
        self.type(dialog.description_input, 'Project notes')
        self.assertTrue(dialog.next_button.isHidden())
        self.assertTrue(dialog.back_button.isHidden())
        self.click(dialog.create_button)
        self.finish()
        self.assertEqual(self.service.calls, [('create', 'channel', 'News', [], 'Project notes', self.service.context)])
        self.assertTrue(self.saved[0].is_channel)

    def test_cancel_before_final_click_never_schedules_or_writes(self):
        for kind in ('group', 'channel'):
            dialog = self.title_page(kind)
            self.click(dialog.cancel_button)
        self.assertEqual(self.jobs, [])
        self.assertEqual(self.service.calls, [])

    def test_selection_survives_search_and_back_navigation(self):
        dialog = self.open()
        self.select(dialog, 0)
        self.type(dialog.member_search, 'Bob')
        self.assertTrue(dialog.member_list.item(0).isHidden())
        self.assertFalse(dialog.member_list.item(1).isHidden())
        self.click(dialog.next_button)
        self.type(dialog.title_input, 'Team')
        self.click(dialog.back_button)
        dialog.member_search.clear()
        self.app.processEvents()
        self.assertEqual(dialog.member_list.item(0).checkState(), Qt.CheckState.Checked)
        self.click(dialog.next_button)
        self.click(dialog.create_button)
        self.finish()
        self.assertEqual(self.service.calls[0][3], [41])

    def test_local_contacts_reject_bool_duplicate_and_own_identifiers(self):
        self.service.contacts += [Contact(True, 'Invalid'), Contact(41, 'Duplicate'),
                                  Contact(0, 'Zero'), Contact(-3, 'Negative'), {'id': 99}]
        dialog = self.open()
        ids = [dialog.member_list.item(i).data(Qt.ItemDataRole.UserRole)
               for i in range(dialog.member_list.count())]
        self.assertEqual(ids, [41, 2 ** 40 + 11])

    def test_empty_and_too_long_names_do_not_schedule(self):
        self.service.limits['max-cname-length'] = 4
        dialog = self.title_page('channel', '     ')
        self.assertFalse(dialog.create_button.isEnabled())
        dialog.title_input.clear()
        self.type(dialog.title_input, 'Longer')
        self.assertFalse(dialog.create_button.isEnabled())
        self.click(dialog.create_button)
        self.assertEqual(self.jobs, [])
        dialog.title_input.clear()
        self.type(dialog.title_input, 'Good')
        self.assertTrue(dialog.create_button.isEnabled())

    def test_utf16_description_limit_has_no_silent_truncation(self):
        self.service.limits['max-description-length'] = 4
        dialog = self.title_page('channel')
        dialog.description_input.setPlainText('\U0001f642' * 3)
        self.app.processEvents()
        self.assertFalse(dialog.create_button.isEnabled())
        self.assertEqual(dialog.description_input.toPlainText(), '\U0001f642' * 3)
        dialog.description_input.setPlainText('\U0001f642' * 2)
        self.app.processEvents()
        self.assertTrue(dialog.create_button.isEnabled())

    def test_local_contact_load_failure_is_not_an_empty_successful_picker(self):
        with patch.object(self.service, 'list_contacts', side_effect=ValueError('cache')):
            dialog = self.open()
        self.assertFalse(dialog.next_button.isEnabled())
        self.assertIn('Не удалось', dialog.member_hint.text())
        self.click(dialog.next_button)
        self.assertEqual(dialog.pages.currentIndex(), 0)
        self.assertEqual(self.jobs, [])

    def test_context_change_before_click_clears_member_rows_and_blocks_create(self):
        dialog = self.title_page()
        self.service.context = object()
        self.click(dialog.create_button)
        self.assertEqual(self.jobs, [])
        self.assertEqual(dialog.member_list.count(), 0)
        self.assertFalse(dialog.create_button.isEnabled())

    def test_context_change_before_worker_runs_prevents_any_request(self):
        dialog = self.title_page()
        self.click(dialog.create_button)
        self.service.context = object()
        self.finish()
        self.assertEqual(self.service.calls, [])
        self.assertEqual(self.saved, [])
        self.assertFalse(dialog.create_button.isEnabled())

    def test_context_change_after_request_discards_old_completion(self):
        dialog = self.title_page()
        self.click(dialog.create_button)
        function, args, kwargs, done, _error = self.jobs[0]
        result = function(*args, **kwargs)
        self.service.context = object()
        done(result)
        self.app.processEvents()
        self.assertEqual(self.saved, [])
        self.assertFalse(dialog.create_button.isEnabled())

    def test_closed_destroyed_dialog_ignores_completion_without_crash(self):
        dialog = self.title_page()
        self.click(dialog.create_button)
        dialog.close()
        dialog.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.finish()
        self.assertEqual(self.saved, [])
        self.assertEqual(len(self.service.calls), 1)

    def test_reopening_during_queue_gap_cannot_schedule_second_create(self):
        dialog = self.title_page()
        self.click(dialog.create_button)
        dialog.close()
        replacement = self.open('channel')
        self.assertFalse(replacement.title_input.isEnabled())
        self.assertTrue(replacement.create_button.isHidden())
        self.assertEqual(len(self.jobs), 1)
        self.finish()
        self.assertEqual(len(self.service.calls), 1)

    def test_unknown_create_without_ack_id_stays_blocked_across_reopen(self):
        self.service.create_result = MaxWriteOutcomeUnknown('Synthetic unknown')
        dialog = self.title_page()
        self.click(dialog.create_button)
        self.finish()
        self.assertTrue(dialog.create_button.isHidden())
        self.assertTrue(dialog.verify_button.isHidden())
        self.assertEqual(self.saved, [])
        dialog.close()
        replacement = self.open('channel')
        self.assertFalse(replacement.title_input.isEnabled())
        self.assertTrue(replacement.create_button.isHidden())
        self.assertEqual(len(self.service.calls), 1)

    def test_known_ack_id_recovery_only_calls_readback_not_create(self):
        self.service.pending = {'kind': 'group', 'stage': 'create', 'has_chat_id': True, 'chat_id': -812}
        chat = Chat(-812, 'Recovered', is_group=True)
        self.service.verify_result = ConversationCreateResult(chat, True)
        dialog = self.open('channel')
        self.assertTrue(dialog.create_button.isHidden())
        self.assertTrue(dialog.verify_button.isEnabled())
        self.click(dialog.verify_button)
        self.click(dialog.verify_button)
        self.assertEqual(len(self.jobs), 1)
        self.finish()
        self.assertEqual(self.service.calls, [('verify', self.service.context)])
        self.assertEqual(self.saved, [chat])

    def test_description_partial_success_shows_warning_and_open_without_recreate(self):
        chat = Chat(-345, 'News', is_channel=True)
        self.service.create_result = ConversationCreateResult(chat, False, 'Synthetic pending')
        dialog = self.title_page('channel', 'News')
        self.type(dialog.description_input, 'Notes')
        self.click(dialog.create_button)
        self.service.pending = {'kind': 'channel', 'stage': 'description', 'has_chat_id': True, 'chat_id': -345}
        self.finish()
        self.assertEqual(self.saved, [])
        self.assertIn('Канал создан', dialog.result_label.text())
        self.assertTrue(dialog.open_button.isEnabled())
        self.assertTrue(dialog.create_button.isHidden())
        self.click(dialog.open_button)
        self.assertEqual(self.saved, [chat])
        self.assertEqual(len(self.service.calls), 1)

    def test_description_readback_remains_read_only_when_not_yet_applied(self):
        chat = Chat(-345, 'News', is_channel=True)
        self.service.pending = {'kind': 'channel', 'stage': 'description', 'has_chat_id': True, 'chat_id': -345}
        self.service.verify_result = ConversationCreateResult(chat, False, 'Still pending')
        dialog = self.open('channel')
        self.click(dialog.verify_button)
        self.finish()
        self.assertEqual(self.service.calls, [('verify', self.service.context)])
        self.assertTrue(dialog.verify_button.isEnabled())
        self.assertTrue(dialog.open_button.isEnabled())
        self.assertEqual(self.saved, [])

    def test_invalid_result_never_emits_success_or_blind_retry(self):
        self.service.create_result = {'id': -123, 'success': True}
        dialog = self.title_page()
        self.click(dialog.create_button)
        self.finish()
        self.assertEqual(self.saved, [])
        self.assertTrue(dialog.create_button.isHidden())
        dialog.close()
        replacement = self.open()
        self.assertTrue(replacement.create_button.isHidden())

    def test_queue_error_is_not_a_license_to_retry_after_reopen(self):
        self.runner.side_effect = RuntimeError('Synthetic queue uncertainty')
        dialog = self.title_page()
        self.click(dialog.create_button)
        self.assertTrue(dialog.create_button.isHidden())
        dialog.close()
        replacement = self.open('channel')
        self.assertTrue(replacement.create_button.isHidden())
        self.assertEqual(self.service.calls, [])

    def test_known_prewrite_rejection_requires_an_explicit_new_click(self):
        dialog = self.title_page('channel', 'News')
        with patch.object(self.service, 'create_conversation', side_effect=ValueError('Synthetic rejected parameters')) as create:
            self.click(dialog.create_button)
            self.finish()
            self.assertEqual(create.call_count, 1)
        self.assertEqual(len(self.jobs), 1)
        self.assertTrue(dialog.create_button.isEnabled())
        self.assertTrue(dialog.title_input.isEnabled())
        self.assertEqual(self.saved, [])
        self.click(dialog.create_button)
        self.assertEqual(len(self.jobs), 2)
        self.finish(1)
        self.assertEqual(len(self.saved), 1)

    def test_malformed_typed_success_with_zero_or_bool_id_is_not_accepted(self):
        for invalid_id in (0, True):
            with self.subTest(invalid_id=invalid_id):
                self.service.create_result = ConversationCreateResult(Chat(invalid_id, 'News'), True)
                dialog = self.title_page('channel', 'News')
                self.click(dialog.create_button)
                self.finish(len(self.jobs) - 1)
                self.assertEqual(self.saved, [])
                self.assertTrue(dialog.create_button.isHidden())
                dialog.close()
                _UI_WRITES.pop(self.service, None)

    def test_missing_authenticated_context_blocks_both_forms(self):
        self.service.context = None
        for kind in ('group', 'channel'):
            dialog = self.open(kind)
            self.assertFalse(dialog.create_button.isEnabled())
            self.assertFalse(dialog.next_button.isEnabled())
            self.assertFalse(dialog.title_input.isEnabled())
        self.assertEqual(self.jobs, [])

    def test_pending_check_failure_blocks_create_instead_of_assuming_none(self):
        with patch.object(self.service, 'pending_conversation_creation', side_effect=RuntimeError('Synthetic storage failure')):
            dialog = self.open('channel')
            self.assertTrue(dialog.create_button.isHidden())
            self.assertFalse(dialog.title_input.isEnabled())
        self.assertEqual(self.jobs, [])


if __name__ == '__main__':
    unittest.main()
