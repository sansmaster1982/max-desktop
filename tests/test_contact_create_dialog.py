"""Actual Qt input through contact save/readback with fake RPCs and memory storage."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import unittest
from unittest.mock import patch

from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog
from shiboken6 import isValid

from maxclient.core.config import Settings
from maxclient.core.contact_create import ContactCreateResult
from maxclient.core.models import Contact
from maxclient.protocol.client import MaxFrame, MaxTimeout, MaxWriteNotStarted
from maxclient.ui.contact_create_dialog import ContactCreateDialog
from maxclient.ui.main_window import MainWindow
import test_contact_create_core as core_fixture
from test_contact_create_contracts import PHONE
from test_settings_workspace import SyntheticService


def replace_text(field, text):
    field.setFocus()
    field.selectAll()
    QTest.keyClick(field, Qt.Key.Key_Backspace)
    QTest.keyClicks(field, text)


class _ContactCreateHarness(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.fixture = core_fixture.ContactCreateCoreTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.service = self.fixture.service
        self.client = self.fixture.client
        self.store = self.fixture.store
        self.jobs = []
        runner = patch('maxclient.ui.contact_create_dialog.run_async', side_effect=self.queue)
        self.runner = runner.start()
        self.addCleanup(runner.stop)
        self.dialog = ContactCreateDialog(self.service)
        self.saved = []
        self.dialog.contact_saved.connect(self.saved.append)
        self.dialog.show()
        self.app.processEvents()

    def tearDown(self):
        if isValid(self.dialog):
            self.dialog.close()
            self.dialog.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()

    def queue(self, function, *args, on_done, on_error, **kwargs):
        self.jobs.append((function, args, kwargs, on_done, on_error))

    def finish(self, index=0):
        function, args, kwargs, done, failed = self.jobs[index]
        try:
            result = function(*args, **kwargs)
        except Exception as error:
            failed(str(error))
        else:
            done(result)
        self.app.processEvents()

    def fill(self, first='Synthetic', last='Person', phone=PHONE, dialog=None):
        dialog = dialog or self.dialog
        replace_text(dialog.first_name, first)
        replace_text(dialog.last_name, last)
        replace_text(dialog.phone, phone)

    def click_save(self, dialog=None):
        QTest.mouseClick((dialog or self.dialog).save_button, Qt.MouseButton.LeftButton)

    def opcodes(self):
        return [call.args[0] for call in self.client.request.call_args_list]


class ContactCreateDialogTests(_ContactCreateHarness):
    def test_open_typing_and_cancel_have_no_requests_or_saved_rows(self):
        self.assertFalse(self.dialog.save_button.isEnabled())
        self.fill()
        self.assertTrue(self.dialog.save_button.isEnabled())
        QTest.mouseClick(self.dialog.cancel_button, Qt.MouseButton.LeftButton)
        self.assertEqual(self.dialog.result(), QDialog.DialogCode.Rejected)
        self.runner.assert_not_called()
        self.client.request.assert_not_called()
        self.assertEqual(self.store.list_contacts(), [])
        self.assertEqual(self.saved, [])

    def test_required_name_and_malformed_phone_disable_click_and_return(self):
        self.fill(first='')
        self.click_save()
        QTest.keyClick(self.dialog.phone, Qt.Key.Key_Return)
        self.assertFalse(self.dialog.save_button.isEnabled())
        for phone in ('+7', 'abc79990000000', '+7+9990000000', '+012345678', '+1234567890123456'):
            with self.subTest(phone=phone):
                self.fill(phone=phone)
                self.assertFalse(self.dialog.save_button.isEnabled())
                self.click_save()
                QTest.keyClick(self.dialog.phone, Qt.Key.Key_Return)
        self.runner.assert_not_called()
        self.client.request.assert_not_called()
        self.assertEqual(self.store.list_contacts(), [])

    def test_normalized_phone_and_trimmed_names_reach_real_save_then_readback(self):
        self.fill(first=' Synthetic ', last=' Person ', phone='8 (999) 000-00-00')
        self.click_save()
        self.finish()
        self.assertEqual(self.opcodes(), [41, 32])
        self.assertEqual(self.client.request.call_args_list[0].args,
                         (41, {'phone': PHONE, 'firstName': 'Synthetic', 'lastName': 'Person'}))
        rows = self.store.list_contacts()
        self.assertEqual([(row.id, row.name) for row in rows], [(202, 'Synthetic Person')])
        self.assertEqual(self.saved, rows)
        self.assertEqual(self.dialog.result(), QDialog.DialogCode.Accepted)
        self.assertFalse(self.dialog.isVisible())
        self.assertEqual(self.store.list_chats(), [])

    def test_optional_surname_is_omitted_from_write_and_can_be_confirmed(self):
        def reply(opcode, payload, **kwargs):
            frame = self.fixture.reply(opcode, payload, **kwargs)
            records = frame.decoded.get('contacts', [frame.decoded.get('contact')])
            for record in records:
                record['names'][0].pop('lastName', None)
            return frame
        self.client.request.side_effect = reply
        self.fill(last='')
        QTest.keyClick(self.dialog.phone, Qt.Key.Key_Return)
        self.finish()
        self.assertEqual(self.opcodes(), [41, 32])
        self.assertEqual(self.client.request.call_args_list[0].args[1],
                         {'phone': PHONE, 'firstName': 'Synthetic'})
        self.assertEqual(self.saved[0].name, 'Synthetic')

    def test_clicks_and_enter_during_save_start_only_one_write(self):
        self.fill()
        self.click_save()
        self.assertFalse(self.dialog.save_button.isEnabled())
        self.assertFalse(self.dialog.first_name.isEnabled())
        self.assertFalse(self.dialog.phone.isEnabled())
        self.click_save()
        self.click_save()
        QTest.keyClick(self.dialog.phone, Qt.Key.Key_Return)
        self.assertEqual(len(self.jobs), 1)
        self.finish()
        self.assertEqual(self.opcodes(), [41, 32])
        self.assertEqual(len(self.saved), 1)
        self.assertEqual(len(self.store.list_contacts()), 1)

    def test_existing_contact_success_uses_64_bit_id_and_keeps_single_row(self):
        identity = 2**40 + 19
        self.store.upsert_contact(Contact(identity, 'Old local entry', PHONE))
        def reply(opcode, payload, **kwargs):
            frame = self.fixture.reply(opcode, payload, **kwargs)
            if opcode == 41:
                frame.decoded['new'] = False
                frame.decoded['contact']['id'] = identity
            elif opcode == 32:
                frame.decoded['contacts'][0]['id'] = identity
            return frame
        self.client.request.side_effect = reply
        self.fill()
        self.click_save()
        self.finish()
        self.assertEqual(self.saved[0].id, identity)
        self.assertEqual(len(self.store.list_contacts()), 1)
        self.assertEqual(self.client.request.call_args_list[1].args, (32, {'contactIds': [identity]}))

    def test_explicit_rejection_allows_field_correction_and_one_later_retry(self):
        self.client.request.side_effect = lambda *args, **kwargs: MaxFrame(
            3, 1, 41, b'', {'error': 'user.not.found'})
        self.fill(first='Wrong')
        self.click_save()
        self.finish()
        self.assertIn('не найден', self.dialog.result_label.text())
        self.assertTrue(self.dialog.first_name.isEnabled())
        self.assertTrue(self.dialog.save_button.isEnabled())
        self.assertTrue(self.dialog.verify_button.isHidden())
        self.assertEqual(self.store.list_contacts(), [])
        self.assertEqual(self.saved, [])
        replace_text(self.dialog.first_name, 'Synthetic')
        self.client.request.side_effect = self.fixture.reply
        self.click_save()
        self.finish(1)
        self.assertEqual(self.opcodes(), [41, 41, 32])
        self.assertEqual(len(self.saved), 1)

    def test_unknown_save_allows_only_read_verification_without_repeated_write(self):
        self.client.request.side_effect = MaxTimeout('Synthetic timeout with private details')
        self.fill()
        self.click_save()
        self.finish()
        self.assertTrue(self.dialog.save_button.isHidden())
        self.assertTrue(self.dialog.verify_button.isVisible())
        self.assertTrue(self.dialog.verify_button.isEnabled())
        self.assertFalse(self.dialog.phone.isEnabled())
        self.assertNotIn('private details', self.dialog.result_label.text())
        self.assertEqual(self.store.list_contacts(), [])
        QTest.keyClick(self.dialog.phone, Qt.Key.Key_Return)
        self.assertEqual(len(self.jobs), 1)
        self.client.request.side_effect = self.fixture.reply
        QTest.mouseClick(self.dialog.verify_button, Qt.MouseButton.LeftButton)
        QTest.mouseClick(self.dialog.verify_button, Qt.MouseButton.LeftButton)
        self.assertEqual(len(self.jobs), 2)
        self.finish(1)
        self.assertEqual(self.opcodes(), [41, 46, 32])
        self.assertEqual(len(self.saved), 1)
        self.assertEqual(len(self.store.list_contacts()), 1)

    def test_failed_verification_keeps_verify_only_then_can_confirm(self):
        self.client.request.side_effect = MaxTimeout('Synthetic timeout')
        self.fill()
        self.click_save()
        self.finish()
        QTest.mouseClick(self.dialog.verify_button, Qt.MouseButton.LeftButton)
        self.finish(1)
        self.assertTrue(self.dialog.save_button.isHidden())
        self.assertTrue(self.dialog.verify_button.isEnabled())
        self.assertFalse(self.dialog.first_name.isEnabled())
        self.assertEqual(self.saved, [])
        self.client.request.side_effect = self.fixture.reply
        QTest.mouseClick(self.dialog.verify_button, Qt.MouseButton.LeftButton)
        self.finish(2)
        self.assertEqual(self.opcodes(), [41, 46, 46, 32])
        self.assertEqual(len(self.saved), 1)

    def test_unclassified_worker_failure_shows_no_private_details_or_blind_retry(self):
        self.fill()
        self.click_save()
        with patch.object(self.service, 'create_contact_by_phone',
                          side_effect=RuntimeError('private credentials must not be displayed')):
            self.finish()
        self.assertNotIn('private', self.dialog.result_label.text())
        self.assertTrue(self.dialog.save_button.isHidden())
        self.assertTrue(self.dialog.verify_button.isEnabled())
        self.assertEqual(self.saved, [])
        self.client.request.assert_not_called()

    def test_runner_error_has_same_verify_only_behavior(self):
        self.fill()
        self.click_save()
        self.jobs[0][4]('private exception detail')
        self.assertNotIn('private', self.dialog.result_label.text())
        self.assertTrue(self.dialog.save_button.isHidden())
        self.assertTrue(self.dialog.verify_button.isEnabled())
        self.assertEqual(self.saved, [])
        self.client.request.assert_not_called()

    def test_worker_setup_exception_does_not_leave_form_permanently_busy(self):
        self.fill()
        self.runner.side_effect = RuntimeError('Synthetic queue setup failed')
        with patch('sys.excepthook') as unhandled:
            self.click_save()
        unhandled.assert_not_called()
        self.assertTrue(self.dialog.save_button.isHidden())
        self.assertFalse(self.dialog.phone.isEnabled())
        self.assertTrue(self.dialog.verify_button.isEnabled())
        self.assertTrue(self.dialog.result_label.isVisible())
        self.assertNotEqual(self.dialog.result_label.text(), 'Сохранение…')
        self.assertEqual(self.saved, [])
        self.client.request.assert_not_called()
        self.runner.side_effect = self.queue
        QTest.mouseClick(self.dialog.verify_button, Qt.MouseButton.LeftButton)
        self.finish()
        self.client.request.assert_not_called()
        self.assertTrue(self.dialog.verify_button.isEnabled())
        self.assertTrue(self.dialog.save_button.isHidden())
        self.assertEqual(self.saved, [])

    def test_verification_setup_failure_keeps_unknown_write_read_only(self):
        self.client.request.side_effect = MaxTimeout('Synthetic timeout')
        self.fill()
        self.click_save()
        self.finish()
        self.runner.side_effect = RuntimeError('Synthetic verification queue failed')
        with patch('sys.excepthook') as unhandled:
            QTest.mouseClick(self.dialog.verify_button, Qt.MouseButton.LeftButton)
        unhandled.assert_not_called()
        self.assertTrue(self.dialog.save_button.isHidden())
        self.assertFalse(self.dialog.phone.isEnabled())
        self.assertTrue(self.dialog.verify_button.isEnabled())
        self.assertEqual(self.opcodes(), [41])
        self.runner.side_effect = self.queue
        self.client.request.side_effect = self.fixture.reply
        QTest.mouseClick(self.dialog.verify_button, Qt.MouseButton.LeftButton)
        self.finish(1)
        self.assertEqual(self.opcodes(), [41, 46, 32])
        self.assertEqual(len(self.saved), 1)

    def test_definitely_not_started_write_allows_explicit_retry(self):
        self.client.request.side_effect = MaxWriteNotStarted('Соединение изменилось до отправки')
        self.fill()
        self.click_save()
        self.finish()
        self.assertTrue(self.dialog.save_button.isEnabled())
        self.assertTrue(self.dialog.phone.isEnabled())
        self.assertTrue(self.dialog.verify_button.isHidden())
        self.assertEqual(self.saved, [])
        self.assertEqual(self.store.list_contacts(), [])
        self.client.request.side_effect = self.fixture.reply
        self.click_save()
        self.finish(1)
        self.assertEqual(self.opcodes(), [41, 41, 32])
        self.assertEqual(len(self.saved), 1)

    def test_unconfirmed_or_malformed_result_cannot_emit_save(self):
        self.fill()
        self.click_save()
        self.jobs[0][3](ContactCreateResult(Contact(True, 'Invalid identity'), True))
        self.assertEqual(self.saved, [])
        self.assertTrue(self.dialog.isVisible())
        self.assertTrue(self.dialog.save_button.isHidden())
        self.assertTrue(self.dialog.verify_button.isEnabled())
        self.client.request.assert_not_called()

    def test_close_before_success_delivery_suppresses_signal_and_late_error(self):
        self.fill()
        self.click_save()
        function, args, kwargs, done, failed = self.jobs[0]
        result = function(*args, **kwargs)
        QTest.mouseClick(self.dialog.cancel_button, Qt.MouseButton.LeftButton)
        done(result)
        failed('Late error')
        self.assertEqual(self.saved, [])
        self.assertFalse(self.dialog.isVisible())
        self.assertEqual(self.dialog.result(), QDialog.DialogCode.Rejected)

    def test_session_change_before_worker_prevents_rpc_and_expires_form(self):
        self.fill()
        self.click_save()
        self.fixture.switch()
        self.finish()
        self.client.request.assert_not_called()
        self.assertEqual(self.saved, [])
        self.assertFalse(self.dialog.save_button.isEnabled())
        self.assertFalse(self.dialog.verify_button.isEnabled())
        self.assertIn('Сессия изменилась', self.dialog.result_label.text())

    def test_session_change_before_click_does_not_queue_a_write(self):
        self.fill()
        self.fixture.switch()
        self.click_save()
        self.runner.assert_not_called()
        self.client.request.assert_not_called()
        self.assertEqual(self.saved, [])
        self.assertFalse(self.dialog.save_button.isEnabled())
        self.assertIn('Сессия изменилась', self.dialog.result_label.text())

    def test_late_callback_after_qt_deletion_is_harmless(self):
        self.fill()
        self.click_save()
        function, args, kwargs, done, failed = self.jobs[0]
        result = function(*args, **kwargs)
        self.dialog.close()
        self.dialog.deleteLater()
        QCoreApplication.sendPostedEvents(self.dialog, QEvent.Type.DeferredDelete)
        self.assertFalse(isValid(self.dialog))
        done(result)
        failed('Late error')
        self.assertEqual(self.saved, [])

    def test_other_owner_after_reply_cannot_receive_saved_signal(self):
        self.fill()
        self.click_save()
        function, args, kwargs, done, _failed = self.jobs[0]
        result = function(*args, **kwargs)
        self.fixture.switch(404)
        done(result)
        self.assertEqual(self.saved, [])
        self.assertEqual(self.store.list_contacts(), [])
        self.assertFalse(self.dialog.save_button.isEnabled())
        self.assertIn('Сессия изменилась', self.dialog.result_label.text())

    def test_saved_contact_uses_requested_name_when_public_name_precedes_custom(self):
        def reply(opcode, payload, **kwargs):
            frame = self.fixture.reply(opcode, payload, **kwargs)
            records = frame.decoded.get('contacts', [frame.decoded.get('contact')])
            for record in records:
                record['names'].insert(0, {'type': 'ONEME', 'firstName': 'Public', 'lastName': 'Profile'})
            return frame
        self.client.request.side_effect = reply
        self.fill()
        self.click_save()
        self.finish()
        self.assertEqual(self.saved[0].name, 'Synthetic Person')
        self.assertEqual(self.store.list_contacts()[0].name, 'Synthetic Person')


class ContactCreateMenuIntegrationTests(_ContactCreateHarness):
    def setUp(self):
        super().setUp()
        self.dialog.close()
        self.dialog.deleteLater()
        QCoreApplication.sendPostedEvents(self.dialog, QEvent.Type.DeferredDelete)
        self.shell = SyntheticService()
        self.shell.search_context = self.service.search_context
        self.shell.list_contacts = self.service.list_contacts
        self.shell.create_contact_by_phone = self.service.create_contact_by_phone
        self.shell.verify_contact_creation = self.service.verify_contact_creation
        for target in ('maxclient.ui.main_window.run_async', 'maxclient.ui.chat_view.run_async',
                       'maxclient.ui.settings_workspace.run_async',
                       'maxclient.ui.contacts_workspace.Avatar.set_photo'):
            item = patch(target)
            item.start()
            self.addCleanup(item.stop)
        setting_save = patch.object(Settings, 'save')
        setting_save.start()
        self.addCleanup(setting_save.stop)
        self.window = MainWindow(self.shell, Settings())
        self.window.show()
        self.app.processEvents()

    def tearDown(self):
        self.window.close()
        self.window.deleteLater()
        super().tearDown()

    def open_from_menu(self, query=None):
        QTest.mouseClick(self.window.contacts_button, Qt.MouseButton.LeftButton)
        workspace = self.window._contacts_workspace
        if query is not None:
            replace_text(workspace.search, query)
        QTest.mouseClick(workspace.plus_button, Qt.MouseButton.LeftButton)
        menu = self.window._new_menu
        action = next(item for item in menu.actions() if item.text() == 'Добавить контакт')
        self.assertTrue(action.isEnabled())
        QTest.mouseClick(menu, Qt.MouseButton.LeftButton, pos=menu.actionGeometry(action).center())
        self.dialog = self.window._contact_create_dialog
        self.assertIsNotNone(self.dialog)
        return workspace

    def test_menu_to_real_save_reveals_selected_contact_despite_previous_filter(self):
        workspace = self.open_from_menu(query='Unrelated previous query')
        self.assertEqual(workspace.list.count(), 0)
        self.fill()
        self.click_save()
        self.finish()
        self.assertEqual(self.opcodes(), [41, 32])
        self.assertEqual(workspace.search.text(), '')
        self.assertEqual(workspace.list.count(), 1)
        selected = workspace.list.currentItem().data(Qt.ItemDataRole.UserRole)
        self.assertEqual((selected.id, selected.name), (202, 'Synthetic Person'))
        self.assertEqual(self.store.list_contacts()[0].id, 202)
        self.assertEqual(self.store.list_chats(), [])
        self.assertIsNone(self.window._contact_create_dialog)
        self.assertIsNone(self.window._phone_lookup_dialog)
        self.assertIn('Контакт сохранён', self.window.statusBar().currentMessage())

    def test_closing_main_window_discards_pending_dialog_callback(self):
        workspace = self.open_from_menu()
        self.fill()
        self.click_save()
        function, args, kwargs, done, _failed = self.jobs[0]
        result = function(*args, **kwargs)
        self.window.close()
        self.app.processEvents()
        done(result)
        self.assertIsNone(self.window._contact_create_dialog)
        self.assertEqual(workspace.list.count(), 0)
        self.assertNotIn('Контакт сохранён', self.window.statusBar().currentMessage())

    def test_reopening_uncertain_save_cannot_send_a_second_create_rpc(self):
        self.open_from_menu()
        self.client.request.side_effect = MaxTimeout('Synthetic timeout')
        self.fill()
        self.click_save()
        self.finish()
        QTest.mouseClick(self.dialog.cancel_button, Qt.MouseButton.LeftButton)
        self.app.processEvents()
        self.open_from_menu()
        self.fill()
        self.click_save()
        self.finish(1)
        self.assertEqual(self.opcodes(), [41])
        self.assertTrue(self.dialog.save_button.isHidden())
        self.assertTrue(self.dialog.verify_button.isEnabled())
        self.client.request.side_effect = self.fixture.reply
        QTest.mouseClick(self.dialog.verify_button, Qt.MouseButton.LeftButton)
        self.finish(2)
        self.assertEqual(self.opcodes(), [41, 46, 32])
        self.assertEqual(self.window._contacts_workspace.list.count(), 1)


if __name__ == '__main__':
    unittest.main()
