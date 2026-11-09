"""Independent durable creation reservation race, using an isolated database."""
from pathlib import Path
import tempfile
import threading
import unittest

from maxclient.core.store import Store
from maxclient.protocol.client import MaxWriteNotStarted
import test_conversation_create_core as core_cases


class ConversationCreateJournalReviewTests(unittest.TestCase):
    def test_description_known_not_sent_keeps_created_chat_and_releases_journal(self):
        case = core_cases.ConversationCreateCoreTests(methodName='runTest')
        case.setUp()
        self.addCleanup(case.doCleanups)

        def reply(opcode, *args, **kwargs):
            if opcode == 55:
                raise MaxWriteNotStarted('Synthetic pre-send description failure')
            return case.reply(opcode, *args, **kwargs)

        case.client.request.side_effect = reply
        result = case.create('channel', description='Synthetic description')
        self.assertEqual(result.chat.id, -701)
        self.assertTrue(result.chat.is_channel)
        self.assertFalse(result.description_confirmed)
        self.assertIsNone(case.store.load_conversation_creation(101))
        self.assertEqual(case.codes(), [64, 48, 55])
        self.assertEqual(case.store.get_chat(-701).id, -701)

    def test_two_connections_cannot_both_reserve_the_same_owner(self):
        # Per-service locks do not coordinate separate service/store objects.
        # SQLite's reservation must still allow only one write operation to start.
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'creation-review.sqlite'
            stores = [Store(path), Store(path)]
            try:
                for store in stores:
                    store.bind_owner(101)
                barrier = threading.Barrier(2)
                outcomes = []

                def reserve(index):
                    payload = {'version': 1, 'kind': 'group', 'stage': 'create',
                               'cid': 700 + index, 'values': {'kind': 'group',
                               'title': 'Synthetic review', 'member_ids': [], 'description': ''}}
                    try:
                        barrier.wait(timeout=5)
                        stores[index].reserve_conversation_creation(101, payload)
                    except ValueError:
                        outcomes.append(('blocked', index))
                    except Exception as error:
                        outcomes.append(('unexpected', type(error).__name__))
                    else:
                        outcomes.append(('reserved', index))

                workers = [threading.Thread(target=reserve, args=(index,)) for index in range(2)]
                for worker in workers:
                    worker.start()
                for worker in workers:
                    worker.join(timeout=8)
                self.assertFalse(any(worker.is_alive() for worker in workers))
                self.assertEqual(sorted(item[0] for item in outcomes), ['blocked', 'reserved'])
                winner = next(index for result, index in outcomes if result == 'reserved')
                for store in stores:
                    self.assertEqual(store.load_conversation_creation(101)['cid'], 700 + winner)
            finally:
                for store in stores:
                    store.close()


if __name__ == '__main__':
    unittest.main()
