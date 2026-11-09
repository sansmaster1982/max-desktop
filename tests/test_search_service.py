"""Synthetic owner-isolation and search transport contracts; no sockets or files."""
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from maxclient.core.search_service import SearchServiceMixin, SearchUnavailable
from maxclient.protocol.client import AuthenticatedSession, MaxClient, MaxFrame
from maxclient.protocol.search_contracts import SearchContractError


class FakeSearchService(SearchServiceMixin):
    def __init__(self):
        self.context = AuthenticatedSession(101, 'synthetic-device', 'WEB', object())
        self.client = SimpleNamespace(authenticated_session=self.context,
                                      search_messages=Mock(return_value={'result': [], 'total': 0}),
                                      search_global=Mock(return_value={'result': [], 'total': 0}))
        self.session = SimpleNamespace(device_id='synthetic-device')
        self.store = SimpleNamespace(cache_owner=101, insert_message=Mock(), ensure_chat=Mock())
        self.owner = 101
        self.chats = [SimpleNamespace(id=303, display='Синтетический проект')]

    def _confirmed_profile_id(self): return self.owner
    def list_chats(self): return self.chats


class SearchServiceTests(unittest.TestCase):
    def setUp(self): self.service = FakeSearchService()

    def test_empty_has_no_rpc_and_whitespace_trimmed_only_once(self):
        self.assertEqual(self.service.search_all('   ')['items'], [])
        self.service.client.search_messages.assert_not_called()
        self.service.client.search_global.assert_not_called()
        page = self.service.search_all('  Проект  ')
        self.service.client.search_messages.assert_called_once_with('Проект', count=30, marker=None)
        self.service.client.search_global.assert_called_once_with('Проект', count=40, search_type='ALL')
        self.assertEqual([row['chat_id'] for row in page['items']], [303])
        self.assertIs(page['context'], self.service.context)

    def test_pagination_uses_only_opaque_68_cursor(self):
        self.service.client.search_messages.return_value = {
            'result': [{'chatId': 303, 'message': {'id': 505, 'time': 123456.5, 'text': '<b>plain</b>'},
                        'section': 'MESSAGES'}], 'total': 1, 'marker': 'next-opaque'}
        page = self.service.search_all('needle', marker='opaque-current')
        self.service.client.search_global.assert_not_called()
        self.assertEqual(page['marker'], 'next-opaque')
        self.assertEqual(page['items'][0]['time_ms'], 123456.5)
        self.assertEqual(page['items'][0]['preview'], '<b>plain</b>')
        self.service.store.insert_message.assert_not_called()
        self.service.store.ensure_chat.assert_not_called()

    def test_directory_contact_is_not_a_chat_or_join_operation(self):
        self.service.client.search_global.return_value = {'result': [
            {'contact': {'contact': {'id': 808, 'flags': 2, 'names': [{'name': 'Тестовый бот'}]}}},
            {'chat': {'id': 909, 'title': 'Канал из справочника'}}], 'total': 2}
        rows = self.service.search_all('needle')['items']
        self.assertEqual((rows[0]['section'], rows[0]['user_id'], rows[0]['chat_id']), ('BOTS', 808, None))
        self.assertEqual((rows[1]['chat_id'], rows[1]['title']), (909, 'Канал из справочника'))
        self.service.store.ensure_chat.assert_not_called()

    def test_empty_opaque_cursor_is_preserved_and_does_not_restart_global_search(self):
        self.service.client.search_messages.return_value = {'result': [], 'total': 0, 'marker': ''}
        first = self.service.search_all('needle')
        self.assertEqual(first['marker'], '')
        self.service.client.search_global.reset_mock()
        next_page = self.service.search_all('needle', marker=first['marker'])
        self.service.client.search_messages.assert_called_with('needle', count=30, marker='')
        self.service.client.search_global.assert_not_called()
        self.assertEqual(next_page['marker'], '')

    def test_same_message_duplicate_dedupes_but_distinct_message_remains(self):
        first = {'chatId': 303, 'section': 'MESSAGES', 'message': {'id': 1, 'time': 100}}
        second = {'chatId': 303, 'section': 'MESSAGES', 'message': {'id': 2, 'time': 101}}
        self.service.client.search_messages.return_value = {'result': [first, first, second], 'total': 2}
        self.assertEqual([row['message_id'] for row in self.service.search_all('needle')['items']], [1, 2])

    def test_invalid_projection_reports_partial_without_fabricated_identity(self):
        self.service.client.search_messages.return_value = {
            'result': [{'unavailable': ('chatId',)}, {'chatId': 303, 'section': 'CHATS'}], 'total': 2}
        page = self.service.search_all('needle')
        self.assertEqual(page['unavailable_count'], 1)
        self.assertEqual([row['chat_id'] for row in page['items']], [303])

    def test_context_swap_during_68_aborts_before_second_rpc(self):
        def changed(*args, **kwargs):
            self.service.client.authenticated_session = AuthenticatedSession(101, 'synthetic-device', 'WEB', object())
            return {'result': [], 'total': 0}
        self.service.client.search_messages.side_effect = changed
        with self.assertRaises(SearchUnavailable): self.service.search_all('needle')
        self.service.client.search_global.assert_not_called()

    def test_context_swap_during_60_discards_page(self):
        def changed(*args, **kwargs):
            self.service.store.cache_owner = 404
            return {'result': [], 'total': 0}
        self.service.client.search_global.side_effect = changed
        with self.assertRaises(SearchUnavailable): self.service.search_all('needle')
        self.service.store.ensure_chat.assert_not_called()

    def test_replaced_client_cannot_return_old_results(self):
        old = self.service.client
        def changed(*args, **kwargs):
            self.service.client = SimpleNamespace(authenticated_session=self.service.context)
            return {'result': [], 'total': 0}
        old.search_messages.side_effect = changed
        with self.assertRaises(SearchUnavailable): self.service.search_all('needle')
        old.search_global.assert_not_called()

    def test_bad_owner_device_type_or_explicit_old_context_stops_before_rpc(self):
        for mutate in (
            lambda s: setattr(s, 'owner', 404),
            lambda s: setattr(s.session, 'device_id', 'changed-device'),
            lambda s: setattr(s.store, 'cache_owner', 404),
            lambda s: setattr(s.client, 'authenticated_session', None),
            lambda s: setattr(s.client, 'authenticated_session', AuthenticatedSession(101, 'synthetic-device', 'ANDROID', object())),
        ):
            service = FakeSearchService()
            mutate(service)
            with self.assertRaises(SearchUnavailable): service.search_all('needle')
            service.client.search_messages.assert_not_called()
        with self.assertRaises(SearchUnavailable): self.service.search_all('needle', context=object())


class SearchTransportTests(unittest.TestCase):
    def setUp(self):
        self.client = MaxClient(auto_reconnect=False)
        self.client.request = Mock()

    def test_wire_shapes_and_separate_cursor_types(self):
        self.client.request.return_value = MaxFrame(1, 1, 68, b'', {'result': [], 'total': 0, 'marker': 'later'})
        self.assertEqual(self.client.search_messages(' x ', marker='cursor')['marker'], 'later')
        self.client.request.assert_called_once_with(68, {'query': ' x ', 'count': 30, 'marker': 'cursor'}, timeout=30)
        self.client.request.reset_mock()
        self.client.request.return_value = MaxFrame(1, 2, 60, b'', {'result': [], 'total': 0, 'marker': 0})
        self.assertEqual(self.client.search_global('x', marker=0)['marker'], 0)
        self.client.request.assert_called_once_with(60, {'query': 'x', 'count': 40, 'type': 'ALL', 'marker': 0}, timeout=30)

    def test_invalid_input_does_not_request_or_coerce(self):
        for method, kwargs in ((self.client.search_messages, {'marker': 7}),
                               (self.client.search_global, {'marker': '7'}),
                               (self.client.search_global, {'count': True})):
            with self.assertRaises(SearchContractError): method('x', **kwargs)
        self.client.request.assert_not_called()

    def test_missing_search_array_cannot_be_success(self):
        self.client.request.return_value = MaxFrame(1, 1, 68, b'', {})
        with self.assertRaises(SearchContractError): self.client.search_messages('x')

    def test_history_timestamp_preserves_fraction_and_rejects_nonfinite(self):
        self.client.request.return_value = MaxFrame(1, 1, 49, b'', {'messages': []})
        self.client.chat_history_window(303, from_cursor=1793459502608.5, backward=15, forward=30)
        payload = self.client.request.call_args.args[1]
        self.assertEqual(payload['from'], 1793459502608.5)
        self.client.request.reset_mock()
        for invalid in (True, float('nan'), float('inf'), '1793459502608', 10**1000):
            with self.assertRaises(ValueError): self.client.chat_history_window(303, from_cursor=invalid)
        self.client.request.assert_not_called()


if __name__ == '__main__': unittest.main()
