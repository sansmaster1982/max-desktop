"""Pure current-folder projections, no service/store/network/GUI."""
import copy
import unittest

from maxclient.core.folder_view import build_folder_view
from maxclient.core.models import Chat


def raw(identity, kind='DIALOG', **fields):
    return {'id': identity, 'type': kind, 'status': 'ACTIVE', 'owner': 77,
            'participants': {'42': 100, '77': 100}, 'created': 100, 'lastEventTime': 200,
            'lastMessage': {'id': 90, 'time': 200}, 'newMessages': 0, **fields}


def folder(**fields):
    return {'id': 'synthetic', 'title': 'Synthetic', **fields}


class FolderViewTests(unittest.TestCase):
    def setUp(self):
        self.chats = [Chat(id=i, chat_type=kind, peer_user_id=77) for i, kind in ((1, 'DIALOG'), (2, 'CHAT'), (3, 'CHANNEL'))]
        self.raw = {chat.id: raw(chat.id, chat.chat_type) for chat in self.chats}

    def view(self, dto, **kwargs):
        return build_folder_view(dto, self.chats, raw_chats=self.raw, viewer_id=42, **kwargs)

    def test_explicit_include_and_favorites_override_filters_preserving_order(self):
        result = self.view(folder(include=[2], favorites=[3, 1], filters=[4]))
        self.assertEqual(result['chatIds'], [3, 1, 2])
        self.assertTrue(result['complete'])

    def test_missing_favorite_never_manufactures_a_chat_or_hidden_empty(self):
        result = self.view(folder(favorites=[999], options=[0]))
        self.assertEqual(result['chatIds'], [])
        self.assertEqual(result['unavailableChatIds'], [999])
        self.assertFalse(result['complete'])
        self.assertIsNone(result['hidden'])

    def test_type_filters_are_or_and_required_filters_are_and(self):
        self.raw[1]['newMessages'] = 2
        self.raw[2]['newMessages'] = 1
        self.raw[3]['newMessages'] = 1
        result = self.view(folder(filters=[4, 2, 0]))
        self.assertEqual(result['chatIds'], [1, 3])
        self.assertEqual(result['unreadCount'], 2)  # chats, not message count3.
        self.assertEqual(self.view(folder(filters=[0, 1]))['chatIds'], [])

    def test_marked_unread_is_not_read_even_when_new_messages_zero(self):
        self.raw[1]['markedAsUnread'] = True
        self.assertEqual(self.view(folder(filters=[4, 0, 12]))['chatIds'], [1])
        self.assertNotIn(1, self.view(folder(filters=[1]))['chatIds'])

    def test_bot_dialog_and_contact_masks_follow_distinct_actual_branches(self):
        self.raw[1]['hasBots'] = True
        contacts = {77: {'flags': 512}}
        self.assertEqual(self.view(folder(filters=[10]), contacts=contacts)['chatIds'], [1])
        self.assertEqual(self.view(folder(filters=[8]), contacts=contacts)['chatIds'], [])
        self.raw[1]['hasBots'] = False
        self.assertEqual(self.view(folder(filters=[8]), contacts=contacts)['chatIds'], [1])
        self.assertEqual(self.view(folder(filters=[9]), contacts={77: {'flags': 0}})['chatIds'], [1])

    def test_missing_contact_metadata_is_unresolved_not_not_contact(self):
        result = self.view(folder(filters=[9]))
        self.assertEqual(result['chatIds'], [])
        self.assertEqual(result['unresolvedChatIds'], [1])
        self.assertFalse(result['complete'])

    def test_owner_admin_and_org_subject_intersection(self):
        self.raw[2]['owner'] = 42
        self.raw[3]['adminParticipants'] = {'42': {}}
        self.raw[2]['subject'] = {'organizationIds': [91]}
        self.assertEqual(self.view(folder(filters=[5]))['chatIds'], [2])
        self.assertEqual(self.view(folder(filters=[6]))['chatIds'], [2, 3])
        self.assertEqual(self.view(folder(filters=[13], filterSubjects={'13': [91]}))['chatIds'], [2])

    def test_muted_filters_require_actual_config_or_explicit_provenance(self):
        unknown = self.view(folder(filters=[7]))
        self.assertEqual(unknown['chatIds'], [])
        self.assertEqual(unknown['unresolvedChatIds'], [1, 2, 3])
        settings = {'1': {'dontDisturbUntil': -1}, 2: {'dontDisturbUntil': 500}}
        self.assertEqual(self.view(folder(filters=[7]), chat_settings=settings, now_ms=400)['chatIds'], [1, 2])
        self.assertEqual(self.view(folder(filters=[11]), chat_settings=settings, now_ms=600)['chatIds'], [2, 3])

    def test_future_mute_without_now_is_unknown_but_forever_is_known(self):
        result = self.view(folder(filters=[7]), chat_settings={1: {'dontDisturbUntil': 500}, 2: {'dontDisturbUntil': -1}})
        self.assertEqual(result['chatIds'], [2])
        self.assertEqual(result['unresolvedChatIds'], [1])

    def test_all_folder_filter_is_always_true_and_badge_excludes_muted_and_self(self):
        chats = [Chat(id=0, chat_type='DIALOG'), *self.chats]
        raws = {0: raw(0, newMessages=5), **self.raw}
        raws[1]['newMessages'] = 8;raws[2]['newMessages'] = 1;raws[3]['newMessages'] = 3
        result = build_folder_view(folder(id='all.chat.folder', filters=[]), chats, raw_chats=raws,
                                   viewer_id=42, chat_settings={2: {'dontDisturbUntil': -1}})
        self.assertEqual(result['chatIds'], [0, 1, 2, 3])
        self.assertEqual(result['unreadCount'], 2)

    def test_unknown_read_metadata_never_becomes_a_zero_badge(self):
        result = build_folder_view(folder(include=[1]), [self.chats[0]])
        self.assertEqual(result['chatIds'], [1])
        self.assertIsNone(result['unreadCount'])

    def test_partial_raw_record_does_not_supply_missing_model_defaults(self):
        result = build_folder_view(folder(filters=[0]), [self.chats[0]],
                                   raw_chats={1: {'id': 1, 'type': 'DIALOG'}})
        self.assertEqual(result['chatIds'], [])
        self.assertEqual(result['unresolvedChatIds'], [1])
        self.assertIsNone(result['unreadCount'])

    def test_default_empty_folder_and_special_channels_visibility(self):
        result = self.view(folder(options=[0]))
        self.assertEqual(result['chatIds'], [])
        self.assertTrue(result['hidden'])
        self.assertFalse(self.view(folder(id='chat.channel.folder', options=[0]))['hidden'])

    def test_upstream_mention_constant_is_ignored_and_reported_not_invented(self):
        result = self.view(folder(filters=[14]))
        self.assertEqual(result['chatIds'], [1, 2, 3])
        self.assertEqual(result['ignoredFilterIds'], [14])

    def test_unproven_exclude_does_not_change_membership_widgets_are_separate(self):
        dto = folder(include=[1], exclude=[1], widgets=[{'id': 77}], elements=[{'type': 'synthetic'}])
        before = copy.deepcopy(dto)
        result = self.view(dto)
        self.assertEqual(result['chatIds'], [1])
        self.assertEqual(result['unsupportedFields'], ['exclude'])
        self.assertEqual(result['widgets'], [{'id': 77}])
        result['widgets'][0]['id'] = 99
        self.assertEqual(dto, before)

    def test_removed_viewer_rows_excluded_unless_explicitly_favorited(self):
        self.raw[1]['status'] = 'REMOVED'
        self.assertEqual(self.view(folder(include=[1]))['chatIds'], [])
        self.assertEqual(self.view(folder(favorites=[1]))['chatIds'], [1])


if __name__ == '__main__':unittest.main()
