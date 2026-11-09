"""Unread-chat rail counts, separate from per-row unread-message counters."""
import unittest
from unittest.mock import Mock

import test_folder_navigation as fixture
from maxclient.core.models import Chat


class UnreadNavigationCountTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture.FolderNavigationTests.setUpClass()

    def setUp(self):
        self.case = fixture.FolderNavigationTests()
        self.case.setUp()
        self.addCleanup(self.case.tearDown)
        self.service, self.window = self.case.service, self.case.window
        # Exercise the built-in fallback rail before an authoritative folder
        # snapshot; actual UUID folder routing is covered by its own suite.
        self.window._folders = {}

    def counters(self):
        self.window._reload_chats()
        return tuple(self.window.nav_buttons[key]._counter for key in ('all','new','channels'))

    def test_rail_counts_chats_while_rows_retain_message_count(self):
        self.service.chats[1].unread=152
        self.service.raws[2]['newMessages']=152
        self.assertEqual(self.counters(),(3,3,1))
        self.assertEqual(self.service.chats[1].unread,152)

    def test_all_excludes_saved_and_muted_but_new_counts_marked_unread(self):
        self.service.chats.append(Chat(id=0,chat_type='DIALOG',unread=5))
        self.service.raws[0]=fixture.chat_raw(0,count=5)
        self.service.raws[1]['markedAsUnread']=True  # zero new messages, explicit unread
        self.service.chat_settings={'3':{'dontDisturbUntil':-1}}
        self.assertEqual(self.counters(),(3,5,1))
        self.window._select_section('new')
        self.assertIn(1,self.case.ids())
        self.assertIn(0,self.case.ids())
        self.assertIn(3,self.case.ids())

    def test_acknowledged_zero_removes_chat_from_new_and_channel_badge(self):
        self.service.chats[1].unread=0
        self.service.raws[2].update(newMessages=0,markedAsUnread=False)
        self.assertEqual(self.counters(),(2,2,0))
        self.window._select_section('new')
        self.assertNotIn(2,self.case.ids())

    def test_unknown_canonical_mute_does_not_show_fabricated_all_count(self):
        self.service.chat_settings=None
        self.assertEqual(self.counters(),(0,3,1))

    def test_local_adapter_fallback_counts_rows_not_messages(self):
        self.service.get_folder_view=Mock(side_effect=AttributeError('No canonical adapter'))
        self.service.chats[1].unread=152
        self.service.chats[2].is_muted=True
        self.assertEqual(self.counters(),(2,3,1))
