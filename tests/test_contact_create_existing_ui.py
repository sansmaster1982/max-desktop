"""Existing-contact feedback with fake RPCs, in-memory storage and offline Qt."""
import copy

from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QDialog
from shiboken6 import isValid

from maxclient.protocol.client import MaxFrame, MaxTimeout
import test_contact_create_dialog as ui_fixture
from test_contact_create_contracts import PHONE, record


EXISTING_NOTICE = 'Контакт уже есть в MAX.'
KEPT_NAME_NOTICE = (EXISTING_NOTICE
                    + ' Сохранено прежнее имя; изменить его можно в карточке контакта.')


class ExistingContactFeedbackTests(ui_fixture._ContactCreateHarness):
    # The shared private harness has no test methods; old suites are not inherited.
    def setUp(self):
        super().setUp()
        self.notices = []
        self.events = []
        self.dialog.contact_saved.connect(lambda contact: self.events.append(('saved', contact.name)))
        self.dialog.save_notice.connect(self.notices.append)
        self.dialog.save_notice.connect(lambda text: self.events.append(('notice', text)))

    def install_reply(self, *, first='Existing', last='Name', is_new=False):
        def reply(opcode, payload, **kwargs):
            self.assertIs(kwargs['expected_session'], self.fixture.context)
            raw = record()
            raw['names'][0] = {'type': 'CUSTOM', 'firstName': first, 'lastName': last}
            if opcode == 41:
                data = {'contact': raw, 'new': is_new}
            elif opcode == 46:
                data = {'contact': raw}
            elif opcode == 32:
                data = {'contacts': [raw]}
            else:
                raise AssertionError('Unexpected synthetic operation')
            return MaxFrame(1, 1, opcode, b'', data)
        self.client.request.side_effect = reply

    def assert_pending_without_notice(self):
        self.assertEqual(self.saved, [])
        self.assertEqual(self.notices, [])
        self.assertTrue(self.dialog.isVisible())
        self.assertTrue(self.dialog.save_button.isHidden())
        self.assertTrue(self.dialog.verify_button.isVisible())
        self.assertTrue(self.dialog.verify_button.isEnabled())
        self.assertFalse(self.dialog.first_name.isEnabled())
        self.assertTrue(self.service._contact_create_pending)
        self.assertEqual(self.store.list_contacts(), [])

    def test_existing_different_name_is_saved_before_honest_notice(self):
        self.install_reply()
        self.fill()
        self.click_save()
        self.finish()
        self.assertEqual(self.opcodes(), [41, 32])
        self.assertEqual([(row.id, row.name) for row in self.saved], [(202, 'Existing Name')])
        self.assertEqual(self.notices, [KEPT_NAME_NOTICE])
        self.assertEqual(self.events, [('saved', 'Existing Name'), ('notice', KEPT_NAME_NOTICE)])
        self.assertEqual(self.store.list_contacts()[0].name, 'Existing Name')
        self.assertEqual(self.store.list_chats(), [])
        self.assertEqual(self.service._contact_create_pending, {})
        self.assertEqual(self.dialog.result(), QDialog.DialogCode.Accepted)

    def test_existing_case_difference_keeps_actual_spelling_and_reports_it(self):
        self.install_reply(first='Я', last='')
        self.fill(first='Synthetic', last='')
        # QTest.keyClicks supports ASCII, so set a synthetic Unicode field directly.
        self.dialog.first_name.setText('я')
        self.click_save()
        self.finish()
        self.assertEqual(self.client.request.call_args_list[0].args[1]['firstName'], 'я')
        self.assertEqual(self.saved[0].name, 'Я')
        self.assertEqual(self.notices, [KEPT_NAME_NOTICE])
        self.assertEqual(self.opcodes(), [41, 32])

    def test_existing_matching_name_has_no_false_name_preservation_warning(self):
        self.install_reply(first='Synthetic', last='Person')
        self.fill()
        self.click_save()
        self.finish()
        self.assertEqual(self.saved[0].name, 'Synthetic Person')
        self.assertEqual(self.notices, [EXISTING_NOTICE])

    def test_new_contact_matching_name_has_no_existing_contact_notice(self):
        self.fill()
        self.click_save()
        self.finish()
        self.assertEqual(self.saved[0].name, 'Synthetic Person')
        self.assertEqual(self.notices, [])
        self.assertEqual(self.dialog.result(), QDialog.DialogCode.Accepted)

    def test_new_contact_name_mismatch_stays_pending_after_read_only_verification(self):
        self.install_reply(is_new=True)
        self.fill()
        self.click_save()
        self.finish()
        self.assert_pending_without_notice()
        pending = copy.deepcopy(self.service._contact_create_pending)
        QTest.mouseClick(self.dialog.verify_button, Qt.MouseButton.LeftButton)
        self.finish(1)
        self.assert_pending_without_notice()
        self.assertEqual(self.opcodes(), [41, 32, 32])
        self.assertEqual(self.service._contact_create_pending, pending)

    def test_unknown_ack_does_not_infer_existing_from_readback_with_another_name(self):
        self.client.request.side_effect = MaxTimeout('Synthetic unavailable acknowledgement')
        self.fill()
        self.click_save()
        self.finish()
        self.assert_pending_without_notice()
        self.install_reply(is_new=False)
        QTest.mouseClick(self.dialog.verify_button, Qt.MouseButton.LeftButton)
        self.finish(1)
        self.assert_pending_without_notice()
        self.assertEqual(self.opcodes(), [41, 46, 32])
        pending = self.service._contact_create_pending[(101, PHONE)]
        self.assertNotIn('is_new', pending)
        self.assertEqual(pending['payload']['firstName'], 'Synthetic')

    def test_existing_ack_then_read_timeout_can_verify_without_another_write(self):
        self.install_reply()
        normal_reply = self.client.request.side_effect
        def unavailable_read(opcode, payload, **kwargs):
            if opcode == 32:
                raise MaxTimeout('Synthetic read timeout')
            return normal_reply(opcode, payload, **kwargs)
        self.client.request.side_effect = unavailable_read
        self.fill()
        self.click_save()
        self.finish()
        self.assert_pending_without_notice()
        self.install_reply()
        QTest.mouseClick(self.dialog.verify_button, Qt.MouseButton.LeftButton)
        self.finish(1)
        self.assertEqual(self.opcodes(), [41, 32, 32])
        self.assertEqual(self.saved[0].name, 'Existing Name')
        self.assertEqual(self.notices, [KEPT_NAME_NOTICE])
        self.assertEqual(self.service._contact_create_pending, {})

    def prepared_success(self):
        self.install_reply()
        self.fill()
        self.click_save()
        function, args, kwargs, done, failed = self.jobs[0]
        return function(*args, **kwargs), done, failed

    def test_closed_dialog_suppresses_late_existing_notice(self):
        result, done, failed = self.prepared_success()
        self.dialog.reject()
        done(result)
        failed('Synthetic late failure')
        self.assertEqual(self.saved, [])
        self.assertEqual(self.notices, [])
        self.assertEqual(self.dialog.result(), QDialog.DialogCode.Rejected)

    def test_deleted_dialog_suppresses_late_existing_notice(self):
        result, done, failed = self.prepared_success()
        self.dialog.close()
        self.dialog.deleteLater()
        QCoreApplication.sendPostedEvents(self.dialog, QEvent.Type.DeferredDelete)
        self.assertFalse(isValid(self.dialog))
        done(result)
        failed('Synthetic late failure')
        self.assertEqual(self.saved, [])
        self.assertEqual(self.notices, [])

    def test_session_change_before_delivery_suppresses_existing_notice(self):
        result, done, _failed = self.prepared_success()
        self.fixture.switch()
        done(result)
        self.assertEqual(self.saved, [])
        self.assertEqual(self.notices, [])
        self.assertIn('Сессия изменилась', self.dialog.result_label.text())
        self.assertFalse(self.dialog.save_button.isEnabled())
        self.assertFalse(self.dialog.verify_button.isEnabled())

    def test_other_owner_before_delivery_suppresses_existing_notice(self):
        result, done, _failed = self.prepared_success()
        self.fixture.switch(404)
        done(result)
        self.assertEqual(self.saved, [])
        self.assertEqual(self.notices, [])
        self.assertEqual(self.store.list_contacts(), [])
        self.assertFalse(self.dialog.save_button.isEnabled())

