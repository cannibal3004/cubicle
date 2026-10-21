"""Recall: messages are embedded as stored and the ones that left the context come back when they match."""

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.services import recall, retrieval
from app.services import storage_service as storage_module
from app.services.context_builder import COMPACTION_ITEM_TYPE
from app.services.retrieval import RetrievalConfig, toy_embedding
from app.services.storage_service import StorageService

CONFIG = RetrievalConfig(enabled=True, embedding_model="toy", rerank_model="", base_url="http://model.test/v1", top_k=4, chunk_chars=2000)


async def fake_embed(texts, config=None):
    return [toy_embedding(text) for text in texts]


def _msg(mid, sender, text, when, item_type=None, **payload):
    message = {"id": mid, "thread_id": "bot-1", "bot_id": "bot-1", "sender": sender, "text": text, "created_at": when}
    if item_type:
        message["item_type"] = item_type
        message["raw_payload"] = payload
    return message


class RecallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.storage = StorageService(Path(self.temp.name))
        self.patch = mock.patch.object(storage_module, "storage_service", self.storage)
        self.patch.start()
        recall._backfilled.clear()
        # An old exchange, a compaction covering it, then recent turns.
        self.history = [
            _msg("m1", "user", "Please remember the staging database password lives in the vault under 'staging-db'.", "2026-10-01T09:00:00"),
            _msg("m2", "bot", "Noted: the staging database password is in the vault entry 'staging-db'.", "2026-10-01T09:00:10"),
            _msg("m3", "user", "Also the deploy runs from the ops laptop, never from CI.", "2026-10-01T09:05:00"),
            _msg("m4", "bot", "Understood, deploys only from the ops laptop.", "2026-10-01T09:05:10"),
            _msg("c1", "system", "Summary: we discussed deployment practices.", "2026-10-02T08:00:00", item_type=COMPACTION_ITEM_TYPE, covers_through="m4", messages_compacted=4),
            _msg("m5", "user", "What's the weather like for the picnic?", "2026-10-03T10:00:00"),
            _msg("m6", "bot", "Sunny and mild, a good day for it.", "2026-10-03T10:00:05"),
        ]

    def tearDown(self):
        self.patch.stop()
        try:
            self.temp.cleanup()
        except PermissionError:
            pass

    def test_only_user_bot_and_summary_messages_are_indexable(self):
        self.assertTrue(recall.indexable(self.history[0]))
        self.assertTrue(recall.indexable(self.history[4]))  # the compaction summary
        self.assertFalse(recall.indexable({"id": "x", "sender": "system", "text": "a note", "item_type": "memory"}))
        self.assertFalse(recall.indexable({"id": "x", "sender": "user", "text": "   "}))
        self.assertEqual(recall._meta(self.history[4])["sender"], "summary")
        self.assertEqual(recall._label(self.history[0]), "the user, 2026-10-01 09:00")

    def test_recall_brings_back_what_left_the_context_and_skips_what_is_still_there(self):
        for message in self.history:
            asyncio.run(recall.index_message("bot-1", message, config=CONFIG, storage=self.storage, embed=fake_embed))
        with mock.patch.object(retrieval, "rerank", return_value=None):
            block = asyncio.run(recall.recall_for_turn("bot-1", "where is the staging database password kept?", self.history, config=CONFIG, storage=self.storage, embed=fake_embed))
        self.assertIn("Earlier in this conversation", block)
        self.assertIn("staging-db", block)
        self.assertIn("the user, 2026-10-01 09:00", block)
        self.assertNotIn("picnic", block)       # still in the context: not recalled
        self.assertNotIn("Summary: we discussed", block)  # the live summary is the context itself
        # Without a compaction nothing has left the context, so nothing is recalled and no call is made.
        with mock.patch.object(retrieval, "retrieve") as retrieve:
            self.assertEqual(asyncio.run(recall.recall_for_turn("bot-1", "password?", self.history[:4], config=CONFIG, storage=self.storage, embed=fake_embed)), "")
            retrieve.assert_not_called()
        # A reranker, when there is one, picks and rescores.
        async def fake_rerank(query, documents, top_n, config=None):
            laptop = next(i for i, d in enumerate(documents) if "laptop" in d)
            return [(laptop, 0.9)]
        with mock.patch.object(retrieval, "rerank", fake_rerank):
            block = asyncio.run(recall.recall_for_turn("bot-1", "where do deploys run from", self.history, config=CONFIG, storage=self.storage, embed=fake_embed, limit=1))
        self.assertIn("ops laptop", block)
        self.assertEqual(block.count("\n- "), 1)
        self.assertIn('"', block)  # quoted, so it reads as a past message, not the request

    def test_a_bot_with_history_but_no_vectors_is_backfilled_once(self):
        async def scenario():
            with mock.patch.object(retrieval, "embed_texts", fake_embed):
                scheduled = recall.ensure_backfilled("bot-1", self.history, config=CONFIG, storage=self.storage)
                self.assertTrue(scheduled)
                await asyncio.sleep(0.05)
                self.assertFalse(recall.ensure_backfilled("bot-1", self.history, config=CONFIG, storage=self.storage))
            return retrieval.index_for(self.storage).count(kind="message", ref_prefix="bot-1:")

        self.assertEqual(asyncio.run(scenario()), 7)
        # Already indexed elsewhere: nothing to schedule.
        recall._backfilled.clear()
        self.assertFalse(recall.ensure_backfilled("bot-1", self.history, config=CONFIG, storage=self.storage))
        self.assertFalse(recall.ensure_backfilled("bot-2", [], config=CONFIG, storage=self.storage))


if __name__ == "__main__":
    unittest.main()
