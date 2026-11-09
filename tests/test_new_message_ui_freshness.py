"""Delayed latest-history snapshots versus newer local presentation; no workers."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import unittest

from maxclient.core.models import Chat
import test_search_history_navigation as navigation


class NewMessageUIFreshnessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        navigation.SearchHistoryNavigationTests.setUpClass.__func__(cls)

    def setUp(self):
        navigation.SearchHistoryNavigationTests.setUp(self)

    def tearDown(self):
        navigation.SearchHistoryNavigationTests.tearDown(self)

    open_target = navigation.SearchHistoryNavigationTests.open_target

    def ids(self):
        return [row['msg'].id for row in self.view._rows]

    def test_same_chat_push_remains_after_stale_latest_reply_including_self_chat_zero(self):
        for chat_id in (0, 42):
            with self.subTest(chat_id=chat_id):
                self.view.show_placeholder()
                old = navigation.message(11, chat_id)
                fresh = navigation.message(22, chat_id, '<b>Новый буквальный текст</b>')
                self.service.local_history.return_value = [old]
                self.view.open_chat(Chat(id=chat_id))
                pending = self.callbacks[-1]
                self.service.local_history.return_value = [old, fresh]
                self.view.on_incoming(fresh)
                self.assertEqual(self.ids(), [11, 22])
                pending['done']([old])
                self.assertEqual(self.ids(), [11, 22])
                self.assertIs(self.view._rows[-1]['msg'], fresh)

    def test_late_reply_rereads_merged_cache_so_older_history_and_fresh_push_both_show(self):
        old = navigation.message(11)
        fresh = navigation.message(22)
        self.view.open_chat(Chat(id=42))
        pending = self.callbacks[-1]
        self.service.local_history.return_value = [fresh]
        self.view.on_incoming(fresh)
        self.assertEqual(self.ids(), [22])
        # The fake cache has merged history by delivery time; its returned list
        # was captured before that newer cache state, as in a queued callback.
        self.service.local_history.return_value = [old, fresh]
        pending['done']([old])
        self.assertEqual(self.ids(), [11, 22])

    def test_fresh_same_id_content_cannot_be_rolled_back_by_history_snapshot(self):
        old = navigation.message(11, text='Старая версия')
        fresh = navigation.message(11, text='<b>Свежая версия</b>')
        self.service.local_history.return_value = [old]
        self.view.open_chat(Chat(id=42))
        pending = self.callbacks[-1]
        self.service.local_history.return_value = [fresh]
        self.view.on_incoming(fresh)
        pending['done']([old])
        self.assertEqual(self.ids(), [11])
        self.assertIs(self.view._rows[0]['msg'], fresh)

    def test_normal_latest_reply_is_used_without_an_unnecessary_cache_reread(self):
        old, returned = navigation.message(11), navigation.message(12)
        self.service.local_history.return_value = [old]
        self.view.open_chat(Chat(id=42))
        pending = self.callbacks[-1]
        reads = self.service.local_history.call_count
        pending['done']([old, returned])
        self.assertEqual(self.ids(), [11, 12])
        self.assertEqual(self.service.local_history.call_count, reads)

    def test_foreign_chat_push_does_not_invalidate_this_latest_reply(self):
        self.view.open_chat(Chat(id=0))
        pending = self.callbacks[-1]
        reads = self.service.local_history.call_count
        self.view.on_incoming(navigation.message(33, 99))
        pending['done']([navigation.message(11, 0)])
        self.assertEqual(self.ids(), [11])
        self.assertEqual(self.service.local_history.call_count, reads)

    def test_target_window_keeps_exact_timestamp_result_after_a_live_cache_reload(self):
        pending = self.open_target(chat_id=0)
        fresh = navigation.message(200, 0)
        self.service.local_history.return_value = [fresh]
        self.view.on_incoming(fresh)
        reads = self.service.local_history.call_count
        window = [navigation.message(index, 0) for index in (98, 99, 100)]
        pending['done'](window)
        self.assertEqual(self.ids(), [98, 99, 100])
        self.assertEqual(self.view._spotlight_id, 99)
        self.assertEqual(self.service.local_history.call_count, reads)
        self.assertEqual(pending['params']['from_cursor'], 1793459500099)

    def test_stale_owner_callback_after_push_is_rejected_before_rereading_cache(self):
        self.view.open_chat(Chat(id=42))
        pending = self.callbacks[-1]
        fresh = navigation.message(22)
        self.service.local_history.return_value = [fresh]
        self.view.on_incoming(fresh)
        reads = self.service.local_history.call_count
        self.client.authenticated_session = navigation.auth()
        pending['done']([navigation.message(11)])
        self.assertEqual(self.ids(), [22])
        self.assertEqual(self.service.local_history.call_count, reads)

    def test_owner_change_during_freshness_reread_cannot_render_foreign_cache(self):
        self.view.open_chat(Chat(id=42))
        pending = self.callbacks[-1]
        fresh = navigation.message(22)
        self.service.local_history.return_value = [fresh]
        self.view.on_incoming(fresh)
        def changed(_chat_id):
            self.client.authenticated_session = navigation.auth()
            return [navigation.message(33, text='Другой контекст')]
        self.service.local_history.side_effect = changed
        pending['done']([navigation.message(11)])
        self.assertEqual(self.ids(), [22])

    def test_foreign_rows_from_freshness_reread_preserve_current_visible_push(self):
        self.view.open_chat(Chat(id=42))
        pending = self.callbacks[-1]
        fresh = navigation.message(22)
        self.service.local_history.return_value = [fresh]
        self.view.on_incoming(fresh)
        self.service.local_history.return_value = [navigation.message(33, 999)]
        pending['done']([navigation.message(11)])
        self.assertEqual(self.ids(), [22])
        self.assertEqual(self.view.h_sub.text(), 'Не удалось обновить сообщения')


if __name__ == '__main__':
    unittest.main()
