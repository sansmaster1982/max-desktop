"""Loaded-file registry and typed88 resolution: fake clients/in-memory Store."""
from dataclasses import FrozenInstanceError, replace
import unittest
from unittest.mock import Mock

import test_conversation_info_service as fixture
from maxclient.core.conversation_file_service import ConversationFileServiceMixin
from maxclient.core.service import MaxService
from maxclient.protocol.client import AuthenticatedSession, MaxTimeout
from maxclient.protocol.conversation_contracts import parse_info_media
from maxclient.protocol.file_download_contracts import FileDownloadReply


URL = 'https://files.example.test/actual?signature=synthetic-private'


class _Service(ConversationFileServiceMixin, fixture._Service):
    pass


class ConversationFileServiceTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture.ConversationInfoServiceTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.service = _Service()
        self.service.__dict__.update(self.fixture.service.__dict__)
        self.client = self.service.client
        self.client.conversation_file_download = Mock(return_value=FileDownloadReply(URL, False))
        self.context = self.service.conversation_context(303)
        self.changes = self.service.store._db.total_changes

    def tearDown(self):
        self.client.request.assert_not_called()
        self.assertEqual(self.service.store._db.total_changes, self.changes)

    def load(self, *, identity=7, file_id=12.5, name='synthetic-private-name.bin', marker=None, **fields):
        attach = {'_type': 'FILE', 'name': name, 'size': 12}
        if file_id is not None: attach['fileId'] = file_id
        attach.update(fields.pop('attachment', {}))
        raw = fixture.message_dto(identity=identity, attaches=[attach], **fields)
        self.client.conversation_media.return_value = parse_info_media({'total': 1, 'messages': [raw]})
        return self.service.load_conversation_tab(self.context, 'files', marker=marker)['items'][0]

    def descriptor(self, row):
        return self.service.get_conversation_file_descriptor(self.context, row['key'])

    def test_loaded_descriptor_is_frozen_actual_and_mixin_is_wired(self):
        row = self.load()
        descriptor = self.descriptor(row)
        self.assertTrue(issubclass(MaxService, ConversationFileServiceMixin))
        self.assertTrue(row['download_available'])
        self.assertEqual((descriptor.file_id, descriptor.chat_id, descriptor.message_id), (12.5, 303, 7))
        self.assertEqual((descriptor.file_name, descriptor.size, descriptor.item_type),
                         ('synthetic-private-name.bin', 12, 'REGULAR'))
        self.assertIs(descriptor.context, self.context)
        with self.assertRaises(FrozenInstanceError): descriptor.file_id = 999
        self.assertNotIn('synthetic-private-name', repr(descriptor))
        self.client.conversation_file_download.assert_not_called()

    def test_resolution_is_one88_no_download_and_keeps_strict_unsafe(self):
        descriptor = self.descriptor(self.load())
        for unsafe in (False, True):
            self.client.conversation_file_download.reset_mock()
            self.client.conversation_file_download.return_value = FileDownloadReply(URL, unsafe)
            result = self.service.resolve_conversation_file_download(self.context, descriptor)
            self.assertIs(result.descriptor, descriptor)
            self.assertIs(result.unsafe, unsafe)
            self.assertEqual(result.url, URL)
            self.assertNotIn('signature', repr(result))
            self.assertNotIn('synthetic-private-name', repr(result))
            self.client.conversation_file_download.assert_called_once_with(
                12.5, 303, 7, item_type='REGULAR', expected_session=self.context.session_context)

    def test_missing_or_deleted_id_keeps_message_navigation_but_cannot_resolve(self):
        for fields in ({'file_id': None}, {'attachment': {'deleted': True}}):
            row = self.load(**fields)
            self.assertFalse(row['download_available'])
            self.assertEqual(row['action'], 'message')
            with self.assertRaises(ValueError): self.descriptor(row)
        self.client.conversation_file_download.assert_not_called()

    def test_forged_descriptor_or_unknown_key_never_requests88(self):
        descriptor = self.descriptor(self.load())
        for candidate in (replace(descriptor), replace(descriptor, file_id=999), {}, None):
            self.assertFalse(self.service.is_current_conversation_file(self.context, candidate))
            with self.assertRaises(ValueError): self.service.resolve_conversation_file_download(self.context, candidate)
        with self.assertRaises(ValueError): self.service.get_conversation_file_descriptor(self.context, 'unloaded')
        self.client.conversation_file_download.assert_not_called()

    def test_metadata_replacement_invalidates_selected_descriptor_before_save_dialog_returns(self):
        descriptor = self.descriptor(self.load())
        row = self.load(file_id=99, name='changed.bin')
        self.assertFalse(self.service.is_current_conversation_file(self.context, descriptor))
        with self.assertRaises(ValueError): self.service.resolve_conversation_file_download(self.context, descriptor)
        self.assertEqual(self.descriptor(row).file_id, 99)
        self.client.conversation_file_download.assert_not_called()

    def test_paging_keeps_unchanged_loaded_objects_and_eviction_expires_old_descriptor(self):
        first_row = self.load()
        first = self.descriptor(first_row)
        adapter = self.service._conversation_files_adapter()
        adapter.LIMIT = 2
        self.load(identity=8, marker=7)
        self.assertIs(self.descriptor(first_row), first)
        self.load(identity=9, marker=8)
        # get() above made first the LRU's newest, so message8 is evicted.
        self.assertTrue(self.service.is_current_conversation_file(self.context, first))
        self.load(identity=10, marker=9)
        self.assertFalse(self.service.is_current_conversation_file(self.context, first))
        with self.assertRaises(ValueError): self.service.resolve_conversation_file_download(self.context, first)
        self.client.conversation_file_download.assert_not_called()

    def test_clear_revokes_context_and_late_page_cannot_repopulate_registry(self):
        row = self.load()
        descriptor = self.descriptor(row)
        self.service.clear_conversation_files(self.context)
        self.assertFalse(self.service.is_current_conversation_file(self.context, descriptor))
        with self.assertRaises(ValueError): self.service._remember_conversation_files(self.context, [row])
        with self.assertRaises(ValueError): self.service.resolve_conversation_file_download(self.context, descriptor)
        self.client.conversation_file_download.assert_not_called()

    def test_new_card_context_invalidates_old_context_and_late_page(self):
        old_context = self.context
        row = self.load()
        descriptor = self.descriptor(row)
        self.context = self.service.conversation_context(303)
        self.assertFalse(self.service.is_current_conversation_file(old_context, descriptor))
        with self.assertRaises(ValueError): self.service._remember_conversation_files(old_context, [row])
        self.client.conversation_file_download.assert_not_called()

    def test_new_session_rejects_old_descriptor_before_request(self):
        descriptor = self.descriptor(self.load())
        self.client._auth_session = AuthenticatedSession(101, self.client.device_id, 'WEB', self.client._sock)
        self.assertFalse(self.service.is_current_conversation_file(self.context, descriptor))
        with self.assertRaises(ValueError): self.service.resolve_conversation_file_download(self.context, descriptor)
        self.client.conversation_file_download.assert_not_called()

    def test_clear_or_context_change_during88_suppresses_late_location(self):
        for action in ('clear', 'new_card'):
            self.context = self.service.conversation_context(303)
            descriptor = self.descriptor(self.load())
            def during(*args, **kwargs):
                if action == 'clear': self.service.clear_conversation_files(self.context)
                else: self.service.conversation_context(303)
                return FileDownloadReply(URL, False)
            self.client.conversation_file_download.side_effect = during
            with self.subTest(action=action), self.assertRaises(ValueError):
                self.service.resolve_conversation_file_download(self.context, descriptor)
            self.assertEqual(self.service._conversation_files_adapter()._inflight, set())

    def test_inflight_guard_and_timeout_do_not_replay88(self):
        descriptor = self.descriptor(self.load())
        def during(*args, **kwargs):
            with self.assertRaises(ValueError): self.service.resolve_conversation_file_download(self.context, descriptor)
            return FileDownloadReply(URL, False)
        self.client.conversation_file_download.side_effect = during
        self.service.resolve_conversation_file_download(self.context, descriptor)
        self.client.conversation_file_download.assert_called_once()
        self.client.conversation_file_download.reset_mock()
        self.client.conversation_file_download.side_effect = MaxTimeout('synthetic')
        with self.assertRaises(MaxTimeout): self.service.resolve_conversation_file_download(self.context, descriptor)
        self.client.conversation_file_download.assert_called_once()
        self.assertEqual(self.service._conversation_files_adapter()._inflight, set())

    def test_forward_file_and_actual_delayed_type_keep_outer_navigation(self):
        nested = fixture.message_dto(identity=999, timestamp=1, attaches=[{'_type': 'FILE',
            'fileId': 0.25, 'name': 'Forwarded'}])
        self.client.conversation_media.return_value = parse_info_media({'total': 1, 'messages': [
            fixture.message_dto(delayedAttributes={'timeToFire': 1}, link={'type': 'FORWARD', 'chatId': 808, 'message': nested})]})
        row = self.service.load_conversation_tab(self.context, 'files')['items'][0]
        descriptor = self.descriptor(row)
        self.assertEqual((descriptor.file_id, descriptor.chat_id, descriptor.message_id, descriptor.item_type),
                         (0.25, 303, 7, 'DELAYED'))
        self.service.resolve_conversation_file_download(self.context, descriptor)
        self.client.conversation_file_download.assert_called_once_with(
            .25, 303, 7, item_type='DELAYED', expected_session=self.context.session_context)


if __name__ == '__main__':
    unittest.main()
