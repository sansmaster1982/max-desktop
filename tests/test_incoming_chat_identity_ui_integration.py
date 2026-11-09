"""Real push/cache/hydration/Qt pipeline with bounded fake48/32, never network."""
import threading
import time
import unittest
from unittest.mock import Mock

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest

import test_message_change_ui as pipeline
from maxclient.core.service import _ChatMetadataHydration
from maxclient.protocol.client import MaxFrame


class IncomingChatIdentityUIIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        pipeline.MessageChangeUITests.setUpClass()

    def setUp(self):
        self.case = pipeline.MessageChangeUITests()
        self.case.setUp()
        self.service, self.store, self.client = self.case.service, self.case.store, self.case.client
        self.window, self.view = self.case.window, self.case.view
        self.adapter = _ChatMetadataHydration(self.service)
        self.service._chat_metadata_hydration = self.adapter
        self.started48, self.started32 = threading.Event(), threading.Event()
        self.release48, self.release32 = threading.Event(), threading.Event()
        self.rpc_threads, self.push_threads, self.push_errors = [], [], []
        self.original48, self.original32 = self.client.chat_info, self.client.contact_info
        self.client.chat_info = self.fake48 = Mock(side_effect=self.chat_info)
        self.client.contact_info = self.fake32 = Mock(side_effect=self.contact_info)
        self.name = '<b>Синтетический новый контакт & имя</b>'
        self.context = self.case.case.context
        self.raw = dict(self.case.case.raw(901, sender=606, text='Synthetic incoming body'), updateTime=100)

    def tearDown(self):
        # idle alone is insufficient after close(), which also sets the event.
        # Release both fake reads, then join actual worker threads before the
        # parent fixture closes the in-memory Store.
        self.release48.set()
        self.release32.set()
        self.adapter.close()
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            with self.adapter.lock:
                workers = tuple(self.adapter.workers)
            if not workers:
                break
            for worker in workers:
                worker.join(max(0, min(.1, deadline - time.monotonic())))
        with self.adapter.lock:
            remaining = tuple(self.adapter.workers)
        self.assertFalse(remaining, 'Metadata worker must stop before closing Store')
        self.client.chat_info, self.client.contact_info = self.original48, self.original32
        self.case.tearDown()

    def chat_info(self, chat_ids, *, expected_session):
        self.rpc_threads.append((48, threading.get_ident(), threading.current_thread().name))
        self.assertEqual(chat_ids, [505])
        self.assertIs(expected_session, self.context)
        self.started48.set()
        if not self.release48.wait(5):
            raise AssertionError('Synthetic48 release timed out')
        return {'chats': [{'id': 505, 'type': 'DIALOG',
                           'participants': {101: self.raw['time'], 606: self.raw['time']}}]}

    def contact_info(self, user_ids, *, expected_session):
        self.rpc_threads.append((32, threading.get_ident(), threading.current_thread().name))
        self.assertEqual(user_ids, [606])
        self.assertIs(expected_session, self.context)
        self.started32.set()
        if not self.release32.wait(5):
            raise AssertionError('Synthetic32 release timed out')
        return {'contacts': [{'id': 606, 'updateTime': 200,
                              'names': [{'type': 'ONEME', 'name': self.name}]}]}

    def push(self):
        def read_push():
            self.push_threads.append(threading.get_ident())
            try:
                self.service._on_push(MaxFrame(0, 8, 128, b'', {'chatId': 505, 'message': self.raw},
                                               auth_context=self.context))
            except Exception as error:
                self.push_errors.append(error)
        thread = threading.Thread(target=read_push, name='synthetic-max-push-reader')
        thread.start()
        thread.join(1)
        self.assertFalse(thread.is_alive(), 'Push reader must not wait for48/32')
        self.assertFalse(self.push_errors)

    def item(self):
        return next((self.window.chat_list.item(index) for index in range(self.window.chat_list.count())
                     if self.window.chat_list.item(index).data(Qt.ItemDataRole.UserRole) == 505), None)

    def wait_until(self, predicate):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            self.case.app.processEvents()
            if predicate():
                return
            QTest.qWait(5)
        self.fail('Synthetic queued metadata/UI pipeline timed out')

    def test_new_incoming_chat_hydrates_off_reader_and_repairs_visible_identity_once(self):
        self.push()
        self.assertTrue(self.started48.wait(1))
        self.assertFalse(self.started32.is_set())
        self.assertEqual(self.store.get_chat(505).unread, 1)
        # Replay while metadata is pending must not schedule another read or
        # count the message twice.
        self.push()
        self.assertEqual(self.store.get_chat(505).unread, 1)
        self.wait_until(lambda: self.item() is not None)
        item = self.item()
        self.window._on_chat_clicked(item)  # Explicit synthetic user navigation.
        self.assertEqual(self.view.h_title.text(), 'Чат 505')
        self.view.input.setPlainText('Синтетический черновик не отправлен')
        reply = self.view._rows[0]['msg']
        self.view._set_reply_target(reply)
        generation, scope = self.view._history_generation, self.view._file_context
        worker_count = len(self.case.callbacks)
        subtitle = self.view.h_sub.text()

        self.release48.set()
        self.assertTrue(self.started32.wait(1))
        self.release32.set()
        self.assertTrue(self.adapter.idle.wait(2))
        # The Store commit precedes the queued GUI delivery.
        self.assertEqual(next(contact for contact in self.store.list_contacts() if contact.id == 606).name,
                         self.name)
        self.assertEqual(self.view.h_title.text(), 'Чат 505')
        self.wait_until(lambda: self.view.h_title.text() == self.name
                        and not self.window._reload_timer.isActive())

        row = self.window.chat_list.itemWidget(self.item())
        self.assertEqual(row.title._full_text, self.name)
        self.assertEqual(self.view.h_title.textFormat(), Qt.TextFormat.PlainText)
        self.assertEqual(self.view.chat.peer_user_id, 606)
        self.assertEqual(self.view.input.toPlainText(), 'Синтетический черновик не отправлен')
        self.assertIs(self.view._reply_target, reply)
        self.assertEqual(self.view.h_sub.text(), subtitle)
        self.assertEqual(self.view._history_generation, generation)
        self.assertIs(self.view._file_context, scope)
        self.assertEqual(len(self.case.callbacks), worker_count)
        self.assertEqual(self.window.chat_list.currentItem().data(Qt.ItemDataRole.UserRole), 505)

        # A later identical replay stays deduplicated after hydration too.
        self.push()
        self.wait_until(lambda: not self.window._reload_timer.isActive())
        self.assertEqual(self.store.get_chat(505).unread, 1)
        self.assertEqual([message.id for message in self.store.messages(505)], [901])
        self.fake48.assert_called_once_with([505], expected_session=self.context)
        self.fake32.assert_called_once_with([606], expected_session=self.context)
        self.assertEqual(self.window._notifications.receive.call_count, 1)
        self.assertTrue(all(identity not in self.push_threads and identity != threading.get_ident()
                            and name == 'max-chat-metadata' for _opcode, identity, name in self.rpc_threads))
        self.assertEqual([opcode for opcode, _identity, _name in self.rpc_threads], [48, 32])
        self.client.chat_history_window.assert_not_called()
        for effect in self.case.effects.values():
            effect.assert_not_called()


if __name__ == '__main__':
    unittest.main()
