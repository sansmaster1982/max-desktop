"""Incoming chat identity hydration; fake RPCs, memory cache and barriers only."""
import threading
import unittest
from unittest.mock import Mock

import test_history_reconnect_review as fixture
from maxclient.core.service import _ChatMetadataHydration
from maxclient.protocol.client import MaxFrame


class IncomingChatMetadataTests(unittest.TestCase):
    def setUp(self):
        self.case = fixture.HistoryReconnectReviewTests()
        self.case.setUp()
        self.service, self.store, self.client = self.case.service, self.case.store, self.case.client
        self.worker = self.service._chat_metadata_hydration = _ChatMetadataHydration(self.service)
        self.releases = []
        self.client.chat_info = Mock(return_value={'chats': [self.chat()]})
        self.client.contact_info = Mock(return_value={'contacts': [self.contact()]})

    def tearDown(self):
        for event in self.releases:
            event.set()
        self.worker.close()
        self.client._auth_session = None
        for thread in tuple(self.worker.workers):
            thread.join(timeout=3)
            self.assertFalse(thread.is_alive())
        self.case.tearDown()

    def chat(self, identity=304, peer=404, **extra):
        return {'id': identity, 'type': 'DIALOG', 'participants': {101: 1, peer: 1}, **extra}

    def contact(self, identity=404, name='Actual peer', **extra):
        return {'id': identity, 'names': [{'firstName': name}], **extra}

    def push(self, identity=7, *, chat_id=304, inline=None, **extra):
        raw = self.case.raw(identity, fixture.STAMP + identity)
        raw.update(extra)
        data = {'chatId': chat_id, 'message': raw}
        if inline is not None:
            data['chat'] = inline
        context = self.client.authenticated_session
        self.service._on_push(MaxFrame(0, 1, 128, b'', data, auth_context=context))

    def wait(self):
        self.assertTrue(self.worker.idle.wait(3), 'Synthetic metadata worker did not finish')

    def title(self, chat_id=304):
        return next(chat.title for chat in self.service.list_chats() if chat.id == chat_id)

    def gate(self):
        started, release = threading.Event(), threading.Event()
        self.releases.append(release)
        return started, release

    def test_inline_known_contact_sets_peer_name_and_server_counter_without_rpc(self):
        self.push(inline=self.chat(peer=202, newMessages=4))
        self.wait()
        self.assertEqual(self.store.get_chat(304).peer_user_id, 202)
        self.assertEqual(self.store.get_chat(304).chat_type, 'DIALOG')
        self.assertEqual(self.title(), 'Synthetic peer')
        self.assertEqual(self.store.get_chat(304).unread, 4)
        self.client.chat_info.assert_not_called()
        self.client.contact_info.assert_not_called()
        self.store.set_unread(304, 0)
        self.push(inline=self.chat(peer=202, newMessages=4))
        self.assertEqual(self.store.get_chat(304).unread, 0)
        self.push(8, inline={'id': 304})
        self.assertEqual(self.store.get_chat(304).unread, 1)
        self.assertEqual(self.store.get_chat(304).peer_user_id, 202)
        self.assertEqual(self.store.get_chat(304).chat_type, 'DIALOG')

    def test_inline_unknown_contact_uses_only_bound_32_and_phone_only_is_resolved(self):
        self.client.contact_info.return_value = {'contacts': [{'id': 404, 'phone': 79990000000}]}
        self.push(inline=self.chat())
        self.wait()
        self.client.chat_info.assert_not_called()
        self.client.contact_info.assert_called_once_with([404], expected_session=self.case.context)
        self.assertEqual(self.title(), '+79990000000')
        self.push(8, inline=self.chat())
        self.wait()
        self.assertEqual(self.client.contact_info.call_count, 1)

    def test_missing_chat_reads_48_then_actual_peer32_off_reader_and_locks(self):
        started, release = self.gate()
        delivered = threading.Event()
        rpc_threads = []
        def chat_info(*args, **kwargs):
            rpc_threads.append(threading.current_thread())
            self.assertFalse(self.service._metadata_lock._is_owned())
            self.assertFalse(self.store._lock._is_owned())
            self.assertFalse(self.worker.lock.locked())
            started.set()
            self.assertTrue(release.wait(3))
            return {'chats': [self.chat()]}
        self.client.chat_info.side_effect = chat_info
        receiver = threading.Thread(target=lambda: (self.push(), delivered.set()))
        receiver.start()
        self.assertTrue(delivered.wait(1), 'Push reader waited for metadata RPC')
        self.assertTrue(started.wait(1))
        self.assertIsNotNone(self.store.message(304, 7))
        release.set()
        receiver.join(timeout=2)
        self.wait()
        self.assertTrue(all(thread is not receiver for thread in rpc_threads))
        self.client.chat_info.assert_called_once_with([304], expected_session=self.case.context)
        # Message sender is 202; only the actual participants establish peer404.
        self.client.contact_info.assert_called_once_with([404], expected_session=self.case.context)
        self.assertEqual(self.title(), 'Actual peer')
        self.assertLessEqual(len(self.worker.workers), self.worker.MAX_WORKERS)

    def test_identity_read_preserves_new_push_unread_and_preview(self):
        def chat_info(*args, **kwargs):
            self.push(8, time=fixture.STAMP + 100, text='Newer synthetic push')
            return {'chats': [self.chat(newMessages=0, lastMessage=self.case.raw(
                timestamp=fixture.STAMP - 100, text='Older metadata preview'))]}
        self.client.chat_info.side_effect = chat_info
        self.push()
        self.wait()
        chat = self.store.get_chat(304)
        self.assertEqual((chat.unread, chat.last_time_ms, chat.last_preview),
                         (2, fixture.STAMP + 100, 'Newer synthetic push'))
        self.assertEqual(self.client.chat_info.call_count, 1)

    def test_old_unseen_push_counter_cannot_roll_back_newer_preview_or_unread(self):
        self.push(time=fixture.STAMP + 100, text='Latest preview',
                  inline=self.chat(peer=202, newMessages=4))
        raw = self.case.raw(8, fixture.STAMP - 100, text='Older unseen body')
        data = {'chatId': 304, 'chat': self.chat(peer=202, newMessages=0, lastMessage=raw),
                'message': raw, 'unread': 0}
        frame = MaxFrame(0, 1, 128, b'', data, auth_context=self.case.context)
        self.service._on_push(frame)
        chat = self.store.get_chat(304)
        self.assertEqual((chat.unread, chat.last_time_ms, chat.last_preview),
                         (5, fixture.STAMP + 100, 'Latest preview'))
        self.service._on_push(frame)
        self.assertEqual(self.store.get_chat(304).unread, 5)
        self.assertEqual(self.service.on_message.call_count, 2)

    def test_late_32_does_not_replace_newer_131_name(self):
        def contact_info(*args, **kwargs):
            self.service._on_push(MaxFrame(0, 1, 131, b'', {'contact': self.contact(name='Newer name')},
                                           auth_context=self.case.context))
            return {'contacts': [self.contact(name='Older readback')]}
        self.client.contact_info.side_effect = contact_info
        self.push(inline=self.chat())
        self.wait()
        self.assertEqual(self.title(), 'Newer name')

    def test_late_48_does_not_replace_concurrent_inline_identity(self):
        self.service._server_contacts[405] = self.contact(405, 'Newer inline peer')
        def chat_info(*args, **kwargs):
            self.push(8, inline=self.chat(peer=405))
            return {'chats': [self.chat(peer=404)]}
        self.client.chat_info.side_effect = chat_info
        self.push()
        self.wait()
        self.assertEqual(self.store.get_chat(304).peer_user_id, 405)
        self.assertEqual(self.title(), 'Newer inline peer')
        self.client.contact_info.assert_not_called()

    def test_group_metadata_never_uses_sender_as_dialog_peer(self):
        self.client.chat_info.return_value = {'chats': [{'id': 304, 'type': 'CHAT', 'title': 'Actual group'}]}
        self.push()
        self.wait()
        self.assertEqual(self.title(), 'Actual group')
        self.assertTrue(self.store.get_chat(304).is_group)
        self.assertIsNone(self.store.get_chat(304).peer_user_id)
        self.client.contact_info.assert_not_called()

    def test_mismatched_or_malformed_readback_is_not_ingested_or_retried_forever(self):
        self.client.chat_info.return_value = {'chats': [self.chat(identity=999)]}
        self.push(inline=self.chat(identity=999))
        self.wait()
        self.assertIsNone(self.store.get_chat(999))
        self.assertIsNone(self.store.get_chat(304).peer_user_id)
        self.push(8)
        self.wait()
        self.assertEqual(self.client.chat_info.call_count, 1)
        self.client.contact_info.assert_not_called()
        self.worker.invalidate()
        self.client.chat_info.return_value = {'chats': [self.chat(participants=[404])]}
        self.push(9)
        self.wait()
        self.assertIsNone(self.store.get_chat(304).peer_user_id)

    def test_pending_identity_recovers_on_reauth_without_new_push_and_old_worker_cannot_block_it(self):
        started, release = self.gate()
        old_context = self.case.context
        def chat_info(*args, **kwargs):
            if kwargs['expected_session'] is old_context:
                started.set()
                self.assertTrue(release.wait(3))
                return {'chats': [self.chat(peer=999)]}
            return {'chats': [self.chat()]}
        self.client.chat_info.side_effect = chat_info
        self.push()
        self.assertTrue(started.wait(1))
        newer = self.case.context_for(self.client)
        self.client._auth_session = newer
        frame = MaxFrame(1, 2, 19, b'', {'profile': {'contact': {'id': 101}}, 'chats': []})
        self.assertTrue(self.service._on_reauthenticated(self.client, frame, newer))
        self.wait()  # Current context resolves while the old fake RPC is still blocked.
        self.assertEqual(self.title(), 'Actual peer')
        release.set()
        for thread in tuple(self.worker.workers): thread.join(timeout=2)
        self.assertEqual(self.store.get_chat(304).peer_user_id, 404)
        self.assertNotIn(999, self.service._server_contacts)

    def test_shutdown_during_readback_prevents_late_cache_or_callback(self):
        started, release = self.gate()
        def contact_info(*args, **kwargs):
            started.set()
            self.assertTrue(release.wait(3))
            return {'contacts': [self.contact()]}
        self.client.contact_info.side_effect = contact_info
        self.push(inline=self.chat())
        self.assertTrue(started.wait(1))
        self.service.on_chat_changed.reset_mock()
        self.worker.close()
        release.set()
        for thread in tuple(self.worker.workers): thread.join(timeout=2)
        self.assertNotIn(404, self.service._server_contacts)
        self.service.on_chat_changed.assert_not_called()

    def test_stale_schedule_cannot_reset_newer_context_queue(self):
        self.store.ensure_chat(304)
        first_passed, release_old = self.gate()
        rpc_started, release_rpc = self.gate()
        original = self.service._require_history_context
        old_context = self.case.context
        seen, errors = [], []
        def require(binding):
            result = original(binding)
            if threading.current_thread().name == 'old-schedule' and not seen:
                seen.append(True); first_passed.set(); self.assertTrue(release_old.wait(3))
            return result
        self.service._require_history_context = require
        def old_schedule():
            try: self.worker.schedule((101, self.client, old_context), [305])
            except ValueError: errors.append('stale rejected')
        old = threading.Thread(target=old_schedule, name='old-schedule')
        old.start()
        self.assertTrue(first_passed.wait(1))
        newer = self.case.context_for(self.client)
        self.client._auth_session = newer
        def chat_info(*args, **kwargs):
            rpc_started.set(); self.assertTrue(release_rpc.wait(3)); return {'chats': [self.chat()]}
        self.client.chat_info.side_effect = chat_info
        self.worker.schedule((101, self.client, newer), [304])
        self.assertTrue(rpc_started.wait(1))
        release_old.set(); old.join(timeout=2)
        self.assertEqual(errors, ['stale rejected'])
        self.assertIs(self.worker.context, newer)
        release_rpc.set(); self.wait()
        self.assertEqual(self.title(), 'Actual peer')

    def test_opening_old_timestamp_placeholder_hydrates_and_sync_is_session_bound(self):
        self.store.ensure_chat(304)
        self.service._server_chats[304] = {'id': 304, 'lastMessage': self.case.raw()}
        self.service.open_chat(304, force=True)
        self.wait()
        self.assertEqual(self.title(), 'Actual peer')
        self.client.chat_info.assert_called_once_with([304], expected_session=self.case.context)
        self.client.contact_info.assert_called_once_with([404], expected_session=self.case.context)
        self.client.chat_info.reset_mock(); self.client.contact_info.reset_mock()
        self.service.sync_chats()
        self.assertIs(self.client.chat_info.call_args.kwargs['expected_session'], self.case.context)
        self.client.contact_info.assert_not_called()
