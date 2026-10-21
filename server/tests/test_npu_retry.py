"""Calls to the NPU models retry a crashed or hung attempt, and give up after the configured tries."""

import asyncio
import unittest
from unittest import mock

import httpx

from app.services import approval_gate, npu, retrieval
from app.services.npu import NpuError, post_json_with_retry
from app.services.retrieval import RetrievalConfig, RetrievalError

TIMEOUT = httpx.Timeout(5.0, connect=2.0)


def scripted(outcomes):
    """A client factory whose transport replays `outcomes`: an exception class, a status, or (status, json)."""
    calls = []

    def handler(request):
        calls.append(request)
        outcome = outcomes.pop(0) if outcomes else (200, {"ok": True})
        if isinstance(outcome, type) and issubclass(outcome, Exception):
            raise outcome("boom", request=request)
        if isinstance(outcome, tuple):
            status, body = outcome
            return httpx.Response(status, json=body)
        return httpx.Response(outcome, json={"error": "nope"})

    def factory(timeout, headers=None):
        return httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=timeout, headers=headers or {})

    return factory, calls


class RetryTests(unittest.TestCase):
    def test_a_crash_then_a_restart_is_one_retry(self):
        factory, calls = scripted([httpx.ConnectError, 503, (200, {"answer": 1})])
        with mock.patch.object(npu, "client_factory", factory), mock.patch("app.services.npu.asyncio.sleep") as sleep:
            response = asyncio.run(post_json_with_retry("http://npu.test/v1/x", {"q": 1}, timeout=TIMEOUT, label="test", attempts=3))
        self.assertEqual((response.status_code, response.json()), (200, {"answer": 1}))
        self.assertEqual(len(calls), 3)
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [1.0, 3.0])  # the restart gets its few seconds

    def test_a_hang_is_cut_off_and_retried_and_then_it_gives_up(self):
        factory, calls = scripted([httpx.ReadTimeout, httpx.ReadTimeout, httpx.ReadTimeout])
        with mock.patch.object(npu, "client_factory", factory), mock.patch("app.services.npu.asyncio.sleep"):
            with self.assertRaisesRegex(NpuError, "after 3 attempts"):
                asyncio.run(post_json_with_retry("http://npu.test/v1/x", {}, timeout=TIMEOUT, label="test", attempts=3))
        self.assertEqual(len(calls), 3)
        # A real error from the server (400) is not retried: it is the caller's problem.
        factory, calls = scripted([400])
        with mock.patch.object(npu, "client_factory", factory):
            response = asyncio.run(post_json_with_retry("http://npu.test/v1/x", {}, timeout=TIMEOUT, label="test", attempts=3))
        self.assertEqual((response.status_code, len(calls)), (400, 1))

    def test_embeddings_rerank_and_decider_all_go_through_the_retry(self):
        config = RetrievalConfig(enabled=True, embedding_model="", rerank_model="", base_url="http://npu.test/v1")
        vectors = {"data": [{"index": 0, "embedding": [0.1, 0.2]}]}
        factory, calls = scripted([503, (200, vectors)])
        with mock.patch.object(npu, "client_factory", factory), mock.patch("app.services.npu.asyncio.sleep"):
            self.assertEqual(asyncio.run(retrieval.embed_texts(["hi"], config)), [[0.1, 0.2]])
        self.assertEqual(len(calls), 2)
        factory, calls = scripted([httpx.ConnectError, (200, {"results": [{"index": 1, "relevance_score": 0.9}]})])
        with mock.patch.object(npu, "client_factory", factory), mock.patch("app.services.npu.asyncio.sleep"):
            self.assertEqual(asyncio.run(retrieval.rerank("q", ["a", "b"], 1, config)), [(1, 0.9)])
        self.assertEqual(len(calls), 2)
        # Exhausted: embeddings raise RetrievalError (callers fail soft); the reranker returns None (embedding order stands).
        factory, calls = scripted([503, 503, 503])
        with mock.patch.object(npu, "client_factory", factory), mock.patch("app.services.npu.asyncio.sleep"):
            with self.assertRaises(RetrievalError):
                asyncio.run(retrieval.embed_texts(["hi"], config))
        factory, calls = scripted([503, 503, 503])
        with mock.patch.object(npu, "client_factory", factory), mock.patch("app.services.npu.asyncio.sleep"):
            self.assertIsNone(asyncio.run(retrieval.rerank("q", ["a", "b"], 1, config)))
        answers = {"answers": {"destroy": {"noul": 0.1}}}
        factory, calls = scripted([502, (200, answers)])
        with mock.patch.object(npu, "client_factory", factory), mock.patch("app.services.npu.asyncio.sleep"):
            scores = asyncio.run(approval_gate.DeciderClient("http://npu.test", 5.0).score({"tool": "x"}, {"destroy": "Does it destroy?"}))
        self.assertEqual((scores, len(calls)), ({"destroy": 0.1}, 2))
        factory, calls = scripted([httpx.ReadTimeout, httpx.ReadTimeout, httpx.ReadTimeout])
        with mock.patch.object(npu, "client_factory", factory), mock.patch("app.services.npu.asyncio.sleep"):
            with self.assertRaisesRegex(approval_gate.DeciderError, "unreachable"):
                asyncio.run(approval_gate.DeciderClient("http://npu.test", 5.0).score({"tool": "x"}, {"destroy": "Does it destroy?"}))


class RequestShapeTests(unittest.TestCase):
    """What filled the NPU's heap: too many items in one request, and two at once."""

    def test_reranks_send_at_most_sixteen_documents_and_embeddings_batch_sixteen(self):
        config = RetrievalConfig(enabled=True, embedding_model="", rerank_model="", base_url="http://npu.test/v1")
        seen = []

        def handler(request):
            body = request.read()
            import json
            payload = json.loads(body)
            seen.append(payload)
            if request.url.path.endswith("/rerank"):
                return httpx.Response(200, json={"results": [{"index": i, "relevance_score": 1.0 - i / 100} for i in range(len(payload["documents"]))][: payload.get("top_n", 10)]})
            return httpx.Response(200, json={"data": [{"index": i, "embedding": [0.1]} for i in range(len(payload["input"]))]})

        def factory(timeout, headers=None):
            return httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=timeout, headers=headers or {})

        with mock.patch.object(npu, "client_factory", factory):
            ranked = asyncio.run(retrieval.rerank("q", [f"doc {i}" for i in range(40)], 6, config))
            vectors = asyncio.run(retrieval.embed_texts([f"text {i}" for i in range(40)], config))
        self.assertEqual(len(seen[0]["documents"]), 16)
        self.assertEqual(seen[0]["documents"][0], "doc 0")  # the best-first head is what is kept
        self.assertEqual(len(ranked), 6)
        self.assertEqual([len(p["input"]) for p in seen[1:]], [16, 16, 8])
        self.assertEqual(len(vectors), 40)

    def test_npu_calls_never_overlap(self):
        config = RetrievalConfig(enabled=True, embedding_model="", rerank_model="", base_url="http://npu.test/v1")
        in_flight = {"now": 0, "max": 0}

        async def handler(request):
            in_flight["now"] += 1
            in_flight["max"] = max(in_flight["max"], in_flight["now"])
            await asyncio.sleep(0.02)
            in_flight["now"] -= 1
            return httpx.Response(200, json={"data": [{"index": 0, "embedding": [0.1]}]})

        def factory(timeout, headers=None):
            return httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=timeout, headers=headers or {})

        async def scenario():
            await asyncio.gather(*(retrieval.embed_texts(["x"], config) for _ in range(5)))

        with mock.patch.object(npu, "client_factory", factory):
            asyncio.run(scenario())
        self.assertEqual(in_flight["max"], 1)


if __name__ == "__main__":
    unittest.main()
