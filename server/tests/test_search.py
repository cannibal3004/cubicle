"""Search across messages, attachments and notes; bots ranked for a task; notes indexed on save."""

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi import HTTPException

from app.routers import bots as bots_router
from app.routers import search as search_router
from app.services import orchestrator, retrieval, search
from app.services import storage_service as storage_module
from app.services.action_gateway import ActionInvocation
from app.services.orchestrator import ensure_orchestrator, is_orchestrator
from app.services.retrieval import RetrievalConfig, toy_embedding
from app.services.storage_service import StorageService

CONFIG = RetrievalConfig(enabled=True, embedding_model="toy", rerank_model="", base_url="http://model.test/v1", top_k=6, chunk_chars=2000)


async def fake_embed(texts, config=None):
    return [toy_embedding(text) for text in texts]


class SearchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.storage = StorageService(Path(self.temp.name))
        self.patch = mock.patch.object(storage_module, "storage_service", self.storage)
        self.patch.start()
        self.cubie = ensure_orchestrator(self.storage) or next(b for b in self.storage.get_bots() if is_orchestrator(b))
        self.bots = [
            {"id": "bot-dev", "name": "Developer", "role": "Software developer", "description": "Writes and tests code", "system_prompt": "You build software: Python, JavaScript, tests, git.", "model": "m", "tools": []},
            {"id": "bot-res", "name": "Researcher", "role": "Market researcher", "description": "Finds and summarises sources", "system_prompt": "You research markets, competitors and pricing, and summarise sources.", "model": "m", "tools": []},
        ]
        self.storage.save_bots(self.storage.get_bots() + self.bots)
        search._bot_vectors.clear()

    def tearDown(self):
        self.patch.stop()
        try:
            self.temp.cleanup()
        except PermissionError:
            pass

    def test_search_spans_kinds_and_can_be_scoped_to_a_bot(self):
        idx = retrieval.index_for(self.storage)
        asyncio.run(retrieval.index_text("message", "bot-dev:m1", "The deploy pipeline failed because the staging database password expired.", meta={"name": "the user, 2026-10-01", "message_id": "m1", "sender": "user", "created_at": "2026-10-01T09:00:00"}, config=CONFIG, storage=self.storage, embed=fake_embed))
        asyncio.run(retrieval.index_text("attachment", "bot-res:computer:pricing.pdf", "Competitor pricing table: Acme charges 400 dollars per month for the enterprise plan.", meta={"name": "pricing.pdf", "path": "pricing.pdf", "source": "computer"}, config=CONFIG, storage=self.storage, embed=fake_embed))
        asyncio.run(retrieval.index_text("notes", "bot-dev", "Standing rule: deploys run only from the ops laptop, never from CI.", meta={"name": "notes of Developer", "bot_id": "bot-dev"}, config=CONFIG, storage=self.storage, embed=fake_embed))
        self.assertEqual(idx.count(), 3)
        with mock.patch.object(retrieval, "rerank", return_value=None):
            results = asyncio.run(search.search("staging database password expired deploy", config=CONFIG, storage=self.storage, embed=fake_embed, bots=self.storage.get_bots()))
        self.assertEqual(results[0]["kind"], "message")
        self.assertEqual((results[0]["bot_id"], results[0]["bot_name"], results[0]["message_id"]), ("bot-dev", "Developer", "m1"))
        self.assertIn("password", results[0]["snippet"])
        kinds = {r["kind"] for r in results}
        self.assertEqual(kinds, {"message", "attachment", "notes"})
        with mock.patch.object(retrieval, "rerank", return_value=None):
            scoped = asyncio.run(search.search("pricing", bot_id="bot-res", config=CONFIG, storage=self.storage, embed=fake_embed, bots=self.storage.get_bots()))
        self.assertEqual([r["kind"] for r in scoped], ["attachment"])
        self.assertEqual((scoped[0]["name"], scoped[0]["path"]), ("pricing.pdf", "pricing.pdf"))
        # The router validates the bot and returns the same shape.
        with mock.patch.object(search_router.search_service.retrieval, "current_config", return_value=CONFIG), \
             mock.patch.object(search_router.search_service.retrieval, "embed_texts", fake_embed), \
             mock.patch.object(retrieval, "rerank", return_value=None):
            body = asyncio.run(search_router.search(q="ops laptop deploys", bot_id="", limit=5))
            self.assertEqual(body["results"][0]["kind"], "notes")
            with self.assertRaises(HTTPException):
                asyncio.run(search_router.search(q="x", bot_id="bot-nope", limit=5))
            self.assertEqual(asyncio.run(search_router.search(q="  ", bot_id="", limit=5))["results"], [])

    def test_snippets_centre_on_the_match(self):
        text = "filler " * 100 + "the critical passage about invoices is here " + "more " * 100
        snippet = search._snippet(text, "invoices")
        self.assertIn("invoices", snippet)
        self.assertTrue(snippet.startswith("…") and snippet.endswith("…"))
        self.assertLessEqual(len(snippet), search.SNIPPET_CHARS + 2)

    def test_bots_are_ranked_for_a_task_and_the_orchestrator_lists_them_that_way(self):
        ranked = asyncio.run(search.rank_bots(self.bots, "summarise what our competitors charge for their enterprise plans", config=CONFIG, embed=fake_embed))
        self.assertEqual([bot["id"] for bot, _ in ranked], ["bot-res", "bot-dev"])
        self.assertGreater(ranked[0][1], ranked[1][1])
        self.assertEqual(asyncio.run(search.rank_bots(self.bots, "", config=CONFIG, embed=fake_embed)), [])
        with mock.patch.object(orchestrator.search, "current_config_for_ranking", return_value=CONFIG, create=True), \
             mock.patch.object(retrieval, "current_config", return_value=CONFIG), \
             mock.patch.object(retrieval, "embed_texts", fake_embed):
            listed = asyncio.run(orchestrator.execute_list(ActionInvocation(name="bots.list", arguments={"task": "write unit tests for the Python service"}, target={"bot_id": self.cubie["id"]}, preview="")))
        self.assertEqual(listed["bots"][0]["id"], "bot-dev")
        self.assertIn("relevance", listed["bots"][0])
        self.assertIn("best match first", listed["note"])
        plain = asyncio.run(orchestrator.execute_list(ActionInvocation(name="bots.list", arguments={}, target={"bot_id": self.cubie["id"]}, preview="")))
        self.assertNotIn("relevance", plain["bots"][0])

    def test_saving_notes_schedules_their_indexing(self):
        async def scenario():
            with mock.patch.object(retrieval, "current_config", return_value=CONFIG), mock.patch.object(retrieval, "embed_texts", fake_embed):
                await bots_router.update_bot("bot-dev", bots_router.BotUpdate(memory="Deploys only from the ops laptop."))
                await asyncio.sleep(0.05)
                count = retrieval.index_for(self.storage).count(kind="notes", ref_prefix="bot-dev")
                await bots_router.update_bot("bot-dev", bots_router.BotUpdate(memory=""))
                await asyncio.sleep(0.05)
                return count, retrieval.index_for(self.storage).count(kind="notes", ref_prefix="bot-dev")

        self.assertEqual(asyncio.run(scenario()), (1, 0))


if __name__ == "__main__":
    unittest.main()
