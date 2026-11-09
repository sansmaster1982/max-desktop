"""Offline recovery of folder navigation from an existing row-menu read."""
import copy
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import unittest
from unittest.mock import patch

from PySide6.QtCore import QPoint
from PySide6.QtWidgets import QApplication
from shiboken6 import isValid

import test_chat_row_menu_context as context_fixture
from test_chat_row_read_pin import ReadPinService


def complete_folders():
    identities = ['all.chat.folder', 'synthetic-unread']
    return dict(folderSync=3, foldersOrder=identities, writePending=False,
                missingFolderIds=[], orderedFolders=[
                    dict(id=identity, title=identity, filters=[], include=[], favorites=[])
                    for identity in identities])


class RecoveryService(ReadPinService):
    def __init__(self):
        super().__init__()
        self.startup_reads = 0
        self.folder_reads = []
        self.folder_result = complete_folders()

    def get_folders(self):
        self.startup_reads += 1
        raise ValueError('Synthetic initial folder read failure')

    def get_conversation_folders(self, context):
        if not self.is_current_conversation(context):
            raise ValueError('Synthetic stale folder read')
        self.folder_reads.append(context)
        return dict(copy.deepcopy(self.folder_result), context=context)


class ChatRowFolderRecoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.case = context_fixture.ChatRowMenuContextTests()
        self.case.app = self.app
        self.startup_jobs = []
        original_queue = self.case._queue

        def capture_startup(*args, **kwargs):
            original_queue(*args, **kwargs)
            self.startup_jobs.append(self.case.queue[-1])

        self.case._queue = capture_startup
        with patch('test_chat_row_menu_context.RowService', RecoveryService):
            self.case.setUp()
        self.window = self.case.window
        self.service = self.case.service
        self.queue = self.case.queue
        startup = next(job for job in self.startup_jobs if job[0] == self.service.get_folders)
        self.case._run(startup)
        self.assertEqual(self.service.startup_reads, 1)
        self.assertEqual(self.window._folders, {})

    def tearDown(self):
        self.case.tearDown()

    def popup(self):
        menu = self.window._build_chat_row_menu(1)
        self.assertIsNotNone(menu)
        menu.popup(self.window.mapToGlobal(QPoint(120, 100)))
        self.app.processEvents()
        return menu

    def folder_job(self):
        job = next(job for job in self.queue if job[0] == self.service.get_conversation_folders)
        self.queue.remove(job)
        return job

    def test_failed_startup_recovers_from_existing_read_and_requires_new_popup_for_pin(self):
        menu = self.popup()
        old_context = self.window._row_menu_context
        self.assertIsNone(self.window._row_pin_context)
        self.assertEqual(self.service.pin_contexts, [])
        with patch.object(self.window, '_on_folders', wraps=self.window._on_folders) as accept:
            self.case._run(self.folder_job())
        self.assertIs(accept.call_args.kwargs['context'], old_context.session_context)
        self.assertEqual(self.window._section, 'folder:all.chat.folder')
        self.assertEqual(set(self.window._folders), {'folder:all.chat.folder', 'folder:synthetic-unread'})
        self.assertIsNone(self.window._row_menu_context)
        self.assertTrue(not isValid(menu) or menu._revoked)
        self.assertEqual(self.service.pin_contexts, [])
        self.assertEqual(self.queue, [])  # Recovery adds no RPC or write.
        self.popup()
        self.assertEqual(self.window._row_pin_context.folder_id, 'all.chat.folder')
        self.assertIsNot(self.window._row_menu_context, old_context)
        self.assertEqual(self.service.writes, [])
        self.assertEqual(self.service.startup_reads, 1)

    def test_partial_or_invalid_snapshot_never_fabricates_navigation_or_pin_context(self):
        partials = [
            {'folderSync': 3, 'orderedFolders': []},
            dict(complete_folders(), orderedFolders=[None, None]),
            dict(complete_folders(), foldersOrder=['all.chat.folder', 'all.chat.folder']),
            dict(complete_folders(), folderSync=True),
            dict(complete_folders(), foldersOrder=['other', 'synthetic-unread']),
        ]
        for snapshot in partials:
            with self.subTest(snapshot=snapshot):
                self.service.folder_result = snapshot
                menu = self.popup()
                context = self.window._row_menu_context
                self.case._run(self.folder_job())
                self.assertEqual(self.window._folders, {})
                self.assertEqual(self.window._section, 'all')
                self.assertIs(self.window._row_menu_context, context)
                self.assertIsNone(self.window._row_pin_context)
                self.assertTrue(isValid(menu))
        self.assertEqual(self.service.pin_contexts, [])
        self.assertEqual(self.service.writes, [])

    def test_other_context_payload_cannot_recover_navigation(self):
        self.popup()
        job = self.folder_job()
        result = self.case._run(job, callbacks=False)
        result['context'] = self.service.conversation_context(1, activate_files=False)
        job[3](result)
        self.assertEqual(self.window._folders, {})

    def test_late_result_from_replaced_popup_cannot_recover_navigation(self):
        self.popup()
        job = self.folder_job()
        result = self.case._run(job, callbacks=False)
        self.popup()
        replacement = self.window._row_menu_context
        job[3](result)
        self.assertEqual(self.window._folders, {})
        self.assertIs(self.window._row_menu_context, replacement)

    def test_late_result_after_session_change_cannot_recover_navigation(self):
        self.popup()
        job = self.folder_job()
        result = self.case._run(job, callbacks=False)
        self.service.security_session = object()
        job[3](result)
        self.assertEqual(self.window._folders, {})

    def test_late_result_after_owner_change_cannot_recover_navigation(self):
        self.popup()
        job = self.folder_job()
        result = self.case._run(job, callbacks=False)
        self.service.profile.id += 1
        job[3](result)
        self.assertEqual(self.window._folders, {})

    def test_late_result_after_navigation_change_cannot_recover_navigation(self):
        self.popup()
        job = self.folder_job()
        result = self.case._run(job, callbacks=False)
        self.window._select_section('new')
        job[3](result)
        self.assertEqual(self.window._folders, {})
        self.assertEqual(self.window._section, 'new')

    def test_dismissed_popup_cannot_recover_navigation(self):
        menu = self.popup()
        job = self.folder_job()
        result = self.case._run(job, callbacks=False)
        menu.close()
        job[3](result)
        self.assertEqual(self.window._folders, {})

    def test_existing_navigation_is_not_replaced_by_folder_submenu_read(self):
        established = dict(folderSync=4, foldersOrder=['chosen'], orderedFolders=[
            dict(id='chosen', title='Chosen', filters=[], include=[], favorites=[])])
        self.window._on_folders(established, context=self.service.security_session)
        self.popup()
        context = self.window._row_menu_context
        with patch.object(self.window, '_on_folders', wraps=self.window._on_folders) as accept:
            self.case._run(self.folder_job())
        accept.assert_not_called()
        self.assertEqual(set(self.window._folders), {'folder:chosen'})
        self.assertEqual(self.window._section, 'folder:chosen')
        self.assertIs(self.window._row_menu_context, context)


if __name__ == '__main__':
    unittest.main()
