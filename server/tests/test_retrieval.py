"""Embeddings in SQLite, chunking, search, reranker fallback, attachment indexing and the turn's retrieved block."""

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.services import attachments as attachment_service
from app.services import retrieval
from app.services import storage_service as storage_module
from app.services.database import Database
from app.services.retrieval import Hit, RetrievalConfig, VectorIndex, chunk_text, index_text, render_hits, retrieve, toy_embedding
from app.services.storage_service import StorageService

CONFIG = RetrievalConfig(enabled=True, embedding_model="toy", rerank_model="", base_url="http://model.test/v1", top_k=3, chunk_chars=120)


async def fake_embed(texts, config=None):
    return [toy_embedding(text) for text in texts]


class ChunkTests(unittest.TestCase):
    def test_chunks_cut_at_breaks_and_overlap(self):
        self.assertEqual(chunk_text(""), [])
        self.assertEqual(chunk_text("short text"), ["short text"])
        paragraphs = "\n\n".join(f"Paragraph {i}. " + "word " * 40 for i in range(12))
        chunks = chunk_text(paragraphs, size=600, overlap=100)
        self.assertGreater(len(chunks), 3)
        self.assertTrue(all(len(c) <= 600 for c in chunks))
        joined = " ".join(chunks)
        for i in range(12):
            self.assertIn(f"Paragraph {i}.", joined)
        # Cuts land on a break when one is near, so a chunk ends a sentence.
        self.assertTrue(chunks[0].rstrip().endswith("word") or chunks[0].rstrip().endswith("."))


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temp.name) / "t.sqlite3")
        self.index = VectorIndex(self.database, "owner-a")
        self.other = VectorIndex(self.database, "owner-b")

    def tearDown(self):
        try:
            self.temp.cleanup()
        except PermissionError:
            pass

    def test_add_search_replace_delete_and_owner_isolation(self):
        docs = ["the quarterly sales report shows revenue growth", "a recipe for banana bread with walnuts", "revenue by region for the third quarter"]
        self.index.add("attachment", "bot-1:computer:report.pdf", docs, [toy_embedding(d) for d in docs], meta={"name": "report.pdf"})
        self.other.add("attachment", "bot-9:computer:x.txt", ["revenue revenue revenue"], [toy_embedding("revenue revenue revenue")])
        hits = self.index.search(toy_embedding("quarter revenue"), kind="attachment", limit=2)
        self.assertEqual([h.chunk for h in hits], [2, 0])
        self.assertEqual(hits[0].meta, {"name": "report.pdf"})
        self.assertTrue(all(h.ref.startswith("bot-1:") for h in hits))  # never the other account's rows
        self.assertEqual(self.index.count(kind="attachment", ref_prefix="bot-1:"), 3)
        self.assertEqual(self.index.refs("attachment"), ["bot-1:computer:report.pdf"])
        # Re-adding the same ref replaces its chunks.
        self.index.add("attachment", "bot-1:computer:report.pdf", ["only one now"], [toy_embedding("only one now")])
        self.assertEqual(self.index.count(), 1)
        # A vector of another size is skipped, not compared.
        self.index.add("attachment", "bot-1:computer:odd.txt", ["odd"], [[1.0, 2.0]])
        self.assertEqual(len(self.index.search(toy_embedding("one"), kind="attachment", limit=5)), 1)
        self.index.delete("attachment", "bot-1:computer:report.pdf")
        self.assertEqual(self.index.refs("attachment", "bot-1:"), ["bot-1:computer:odd.txt"])


class RetrieveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.storage = StorageService(Path(self.temp.name))
        self.patch = mock.patch.object(storage_module, "storage_service", self.storage)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        try:
            self.temp.cleanup()
        except PermissionError:
            pass

    def test_index_then_retrieve_with_and_without_a_reranker(self):
        text = "\n\n".join([
            "Chapter one is about pricing: the enterprise plan costs 400 dollars a month.",
            "Chapter two covers support hours: weekdays nine to five, Central time.",
            "Chapter three lists the integrations: Slack, GitHub and Jira.",
            "Chapter four explains refunds: full refund within thirty days.",
        ])
        count = asyncio.run(index_text("attachment", "bot-1:computer:guide.md", text, meta={"name": "guide.md"}, config=CONFIG, storage=self.storage, embed=fake_embed))
        self.assertGreaterEqual(count, 4)
        # No reranker reachable: the embedding order stands, best first.
        with mock.patch.object(retrieval, "rerank", return_value=None) as reranker:
            hits = asyncio.run(retrieve("when do I get a refund", kind="attachment", ref_prefix="bot-1:", limit=2, config=CONFIG, storage=self.storage, embed=fake_embed))
        self.assertEqual(len(hits), 2)
        self.assertTrue(any("refund" in hit.text for hit in hits))
        self.assertTrue(reranker.called)
        # A reranker reply reorders and rescores.
        async def fake_rerank(query, documents, top_n, config=None):
            slack = next(i for i, d in enumerate(documents) if "Slack" in d)
            return [(slack, 0.99)]
        with mock.patch.object(retrieval, "rerank", fake_rerank):
            hits = asyncio.run(retrieve("which apps does it integrate with", kind="attachment", ref_prefix="bot-1:", limit=1, config=CONFIG, storage=self.storage, embed=fake_embed))
        self.assertEqual((len(hits), hits[0].score), (1, 0.99))
        self.assertIn("Slack", hits[0].text)
        block = render_hits(hits, heading="Relevant passages:")
        self.assertTrue(block.startswith("Relevant passages:\n--- from guide.md (part "))
        self.assertIn("Slack", block)
        # Context added unasked keeps only what clears the floor: what the reranker scores
        # low is dropped (an unrelated message gets nothing), and without a reranker the
        # embedding similarity decides.
        async def judging_rerank(query, documents, top_n, config=None):
            return [(i, 0.97 if "refund" in d and "refund" in query else 0.01) for i, d in enumerate(documents)][:top_n]
        with mock.patch.object(retrieval, "rerank", judging_rerank):
            kept = asyncio.run(retrieve("when do I get a refund", kind="attachment", ref_prefix="bot-1:", limit=3, config=CONFIG, storage=self.storage, embed=fake_embed, relevant_only=True))
            unrelated = asyncio.run(retrieve("what is my public IP address", kind="attachment", ref_prefix="bot-1:", limit=3, config=CONFIG, storage=self.storage, embed=fake_embed, relevant_only=True))
        self.assertTrue(kept and all("refund" in hit.text for hit in kept))
        self.assertEqual(unrelated, [])
        with mock.patch.object(retrieval, "rerank", return_value=None), mock.patch.object(retrieval.settings, "RETRIEVAL_MIN_SCORE", 1.01):
            self.assertEqual(asyncio.run(retrieve("refund", kind="attachment", ref_prefix="bot-1:", config=CONFIG, storage=self.storage, embed=fake_embed, relevant_only=True)), [])
        # Off, or nothing indexed for the prefix: nothing retrieved, no call made.
        off = RetrievalConfig(**{**CONFIG.__dict__, "enabled": False})
        self.assertEqual(asyncio.run(retrieve("refund", config=off, storage=self.storage, embed=fake_embed)), [])
        self.assertEqual(asyncio.run(retrieve("refund", kind="attachment", ref_prefix="bot-2:", config=CONFIG, storage=self.storage, embed=fake_embed)), [])

    def test_attaching_a_long_file_indexes_all_of_it_and_the_turn_gets_passages(self):
        root = Path(self.temp.name)
        shared_dir = root / "shared"
        shared_dir.mkdir()
        body = "\n\n".join(f"Section {i}. " + ("Alpha beta gamma. " * 30) for i in range(1, 30))
        body += "\n\nSection 30. The launch date is the fourth of November and the code name is Heron."
        (shared_dir / "plan.txt").write_bytes(body.encode("utf-8"))
        from app.services import file_share
        from app.services.file_share import resolve_shared_file

        indexed = {}

        async def capture_index(kind, ref, text, **kwargs):
            indexed[ref] = (kind, len(text), kwargs.get("meta"))
            return await index_text(kind, ref, text, config=CONFIG, storage=self.storage, embed=fake_embed, meta=kwargs.get("meta"))

        with mock.patch.object(file_share.settings, "WORKSPACE_ROOT", shared_dir), \
             mock.patch.object(attachment_service.settings, "ATTACHMENT_INLINE_MAX_CHARS", 500), \
             mock.patch.object(attachment_service.retrieval, "current_config", return_value=CONFIG), \
             mock.patch.object(attachment_service.retrieval, "index_text", capture_index):
            shared = resolve_shared_file("workspace", "plan.txt", None)
            record = asyncio.run(attachment_service.attach(shared, "bot-1"))
            # The indexing runs in the background; give it a moment inside the loop.
            asyncio.run(asyncio.sleep(0.05))
        self.assertTrue(record["extract"]["truncated"])
        self.assertEqual(len(record["extract"]["text"]), 500)
        self.assertEqual(indexed["bot-1:workspace:plan.txt"][1], len(body))
        self.assertEqual(indexed["bot-1:workspace:plan.txt"][2]["name"], "plan.txt")
        # The turn asks about something far past the inlined part.
        from app.routers import chat as chat_router

        with mock.patch.object(chat_router.retrieval, "current_config", return_value=CONFIG), \
             mock.patch.object(chat_router.retrieval, "embed_texts", fake_embed), \
             mock.patch.object(chat_router.retrieval, "rerank", return_value=None):
            block = asyncio.run(chat_router.retrieved_context("bot-1", "what is the code name and launch date"))
        self.assertIn("Heron", block)
        self.assertIn("plan.txt", block)
        self.assertEqual(asyncio.run(chat_router.retrieved_context("bot-2", "anything")), "")


if __name__ == "__main__":
    unittest.main()
