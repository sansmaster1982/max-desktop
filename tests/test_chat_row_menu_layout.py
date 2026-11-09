"""Native menu variants from recorded observations; all inputs are synthetic."""
import unittest

from maxclient.ui.chat_row_menu import chat_row_entries, flatten_entries
from maxclient.core.i18n import SUPPORTED_LANGUAGES, tr


def snapshot(kind='dialog', *, unread=False, status='ACTIVE', pending=False):
    actions = {
        'dialog': ('folder', 'clear', 'block', 'delete'),
        'saved': ('folder', 'clear', 'delete'),
        'bot': ('folder', 'clear', 'report', 'stop_bot', 'delete'),
        'group': ('folder', 'clear', 'leave'),
        'channel': ('folder', 'report', 'leave'),
    }[kind]
    return dict(
        action_capabilities=dict(supported=True, kind=kind, mute=True,
                                 last_message=True, recipient_blocked=False),
        more_actions=[dict(action=action, available=True, value=None) for action in actions],
        row_menu={'unread': unread, 'chat_status': status},
        availability={'mute': True}, muted=False, action_pending=pending,
    )


class ChatRowMenuLayoutTests(unittest.TestCase):
    def labels(self, value):
        return [row['label'] for row in chat_row_entries(value)]

    def test_native_read_dialog_order_and_explicit_unavailable_boundaries(self):
        value = snapshot()
        value['row_menu']['pinned'] = False
        rows = chat_row_entries(value)
        self.assertEqual([row['label'] for row in rows], [
            'Предпросмотр', 'Добавить в папку', 'Отметить непрочитанным',
            'Закрепить', 'Отключить уведомления', 'Стереть переписку',
            'Заблокировать', 'Удалить чат'])
        self.assertEqual([row['action'] for row in rows if row.get('separator_before')], ['clear'])
        # A service capability must not advertise a handler this UI does not own.
        for row in rows:
            if row['action'] in ('preview', 'toggle_read', 'toggle_pin'):
                self.assertIs(row['available'], False)
            elif row['action'] in ('clear', 'delete'):
                self.assertIs(row['available'], True)
        self.assertTrue(next(row for row in rows if row['action'] == 'block')['available'])

    def test_native_unread_channel_order_has_report_before_leave(self):
        self.assertEqual(self.labels(snapshot('channel', unread=True)), [
            'Предпросмотр', 'Добавить в папку', 'Прочитать всё', 'Закрепление чата',
            'Отключить уведомления', 'Пожаловаться', 'Отписаться от канала'])

    def test_native_pinned_bot_variant_and_exact_mute_durations(self):
        value = snapshot('bot')
        value['row_menu']['pinned'] = True
        rows = chat_row_entries(value)
        self.assertEqual([row['label'] for row in rows], [
            'Предпросмотр', 'Добавить в папку', 'Отметить непрочитанным', 'Открепить',
            'Отключить уведомления', 'Очистить историю', 'Пожаловаться',
            'Остановить', 'Удалить и остановить'])
        choices = next(row['children'] for row in rows if row['action'] == 'mute_choices')
        self.assertEqual([(row['label'], row['value']) for row in choices], [
            ('На 1 час', 3600000), ('На 4 часа', 14400000),
            ('На 24 часа', 86400000), ('Навсегда', -1)])

    def test_missing_read_and_pin_state_never_guesses_opposite_command(self):
        value = snapshot()
        value.pop('row_menu')
        self.assertIn('Отметка о прочтении', self.labels(value))
        self.assertIn('Закрепление чата', self.labels(value))

    def test_raw_blocked_status_does_not_equal_recipient_block(self):
        value = snapshot(status='BLOCKED')
        self.assertEqual([row['action'] for row in chat_row_entries(value)],
                         ['toggle_pin', 'mute_choices', 'delete'])
        value = snapshot()
        value['action_capabilities']['recipient_blocked'] = True
        value['more_actions'].append(dict(action='unblock', available=True))
        self.assertIn('Предпросмотр', self.labels(value))
        self.assertIn('Разблокировать', self.labels(value))

    def test_pending_disables_all_folder_membership_and_action_writes(self):
        folder = dict(action='folder_include', label='Тест', available=True, write=True,
                      checked=False, value={'folder_id': 'synthetic', 'included': True})
        rows = chat_row_entries(snapshot(pending=True), folders=[folder])
        self.assertTrue(all(not row['available'] for row in flatten_entries(rows)
                            if row.get('write') is True))
        self.assertTrue(folder['available'])  # caller-owned snapshot is unchanged

    def test_busy_disables_all_writes_without_disabling_server_state_projection(self):
        rows = chat_row_entries(snapshot(), busy=True)
        self.assertTrue(all(not row['available'] for row in flatten_entries(rows)
                            if row.get('write') is True))
        self.assertFalse(next(row for row in flatten_entries(rows)
                              if row['action'] == 'folder_create')['available'])

    def test_malformed_or_unsupported_snapshot_is_empty(self):
        for value in (None, [], {}, {'action_capabilities': []},
                      {'action_capabilities': {'supported': False}}):
            self.assertEqual(chat_row_entries(value), [])
        value = snapshot()
        value['action_capabilities']['kind'] = 'video'
        self.assertEqual(chat_row_entries(value), [])
        value = snapshot()
        value['more_actions'] = None
        self.assertFalse(any(row['action'] == 'folder' for row in chat_row_entries(value)))

    def test_all_seven_locales_translate_owned_rows_but_keep_user_folder_name(self):
        folder_name = 'Моя личная папка & Words'
        folder = dict(action='folder_include', label=folder_name, available=True, write=True,
                      value={'folder_id': 'synthetic', 'included': True}, checked=False)
        original = list(flatten_entries(chat_row_entries(snapshot('bot'), folders=[folder])))
        for language in SUPPORTED_LANGUAGES:
            with self.subTest(language=language):
                translated = list(flatten_entries(chat_row_entries(
                    snapshot('bot'), folders=[folder], language=language)))
                self.assertEqual([row['action'] for row in translated],
                                 [row['action'] for row in original])
                for before, after in zip(original, translated):
                    if before['action'] == 'folder_include':
                        self.assertEqual(after['label'], folder_name)
                    else:
                        self.assertEqual(after['label'], tr(before['label'], language))
                if language != 'ru':
                    self.assertNotEqual(translated[0]['label'], original[0]['label'])


if __name__ == '__main__':
    unittest.main()
