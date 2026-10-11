import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.services import context_compactor
from app.services.context_builder import build_history_messages, is_compaction
from app.services.context_compactor import (
    SUMMARY_INSTRUCTION,
    compact_thread,
    last_prompt_tokens,
    should_compact,
    split_for_compaction,
)
from app.services.storage_service import StorageService


def _user(i, text):
    return {"id": f"u{i}", "thread_id": "t", "bot_id": "t", "sender": "user", "text": text, "created_at": f"2026-10-11T00:00:{i:02d}", "item_type": "user_text"}


def _bot(i, text, prompt_tokens=None, calls=None):
    payload = {}
    if prompt_tokens is not None:
        payload["usage"] = {"prompt_tokens": prompt_tokens, "completion_tokens": 10}
    if calls:
        payload["tool_calls"] = calls
    return {"id": f"b{i}", "thread_id": "t", "bot_id": "t", "sender": "bot", "text": text, "created_at": f"2026-10-11T00:01:{i:02d}", "item_type": "assistant_text", "raw_payload": payload or None}


def _compaction(i, text):
    return {"id": f"c{i}", "thread_id": "t", "bot_id": "t", "sender": "system", "text": text, "created_at": f"2026-10-11T00:02:{i:02d}", "item_type": "compaction", "raw_payload": {"reason": "test"}}


class SplitTests(unittest.TestCase):
    def test_keeps_the_last_n_user_turns_and_everything_after_them(self):
        history = [_user(1, "a"), _bot(1, "A"), _user(2, "b"), _bot(2, "B"), _user(3, "c"), _bot(3, "C"), _user(4, "d")]
        older, recent = split_for_compaction(history, keep_recent_turns=2)
        self.assertEqual([m["id"] for m in older], ["u1", "b1", "u2", "b2"])
        self.assertEqual([m["id"] for m in recent], ["u3", "b3", "u4"])

    def test_short_threads_have_nothing_to_compact(self):
        history = [_user(1, "a"), _bot(1, "A"), _user(2, "b")]
        older, recent = split_for_compaction(history, keep_recent_turns=4)
        self.assertEqual(older, [])
        self.assertEqual(len(recent), 3)

    def test_only_messages_after_the_last_compaction_are_live(self):
        history = [_user(1, "a"), _bot(1, "A"), _compaction(1, "sum"), _user(2, "b"), _bot(2, "B"), _user(3, "c"), _bot(3, "C"), _user(4, "d")]
        older, recent = split_for_compaction(history, keep_recent_turns=1)
        self.assertEqual([m["id"] for m in older], ["u2", "b2", "u3", "b3"])
        self.assertEqual([m["id"] for m in recent], ["u4"])

    def test_threshold_uses_the_last_reported_prompt(self):
        history = [_user(1, "a"), _bot(1, "A", prompt_tokens=150_000), _user(2, "b")]
        self.assertEqual(last_prompt_tokens(history), 150_000)
        with mock.patch.object(context_compactor.settings, "CONTEXT_COMPACT_AT", 0.7):
            self.assertTrue(should_compact(history, 200_000))
            self.assertFalse(should_compact(history, 262_144))
            self.assertFalse(should_compact(history, None))
        with mock.patch.object(context_compactor.settings, "CONTEXT_COMPACT_AT", 0):
            self.assertFalse(should_compact(history, 1000))
        self.assertIsNone(last_prompt_tokens([_user(1, "a")]))


class CompactThreadTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.storage = StorageService(Path(self.temp_dir.name))
        self.requests = []

    def tearDown(self):
        try:
            self.temp_dir.cleanup()
        except PermissionError:
            pass  # Windows keeps the SQLite file open until the process exits.

    def _fake_stream(self, summary="## Task\nBuild the thing.\n## Open items\nTests."):
        async def stream(model, messages, system_prompt="", config=None, tools=None):
            self.requests.append({"model": model, "messages": messages, "system_prompt": system_prompt, "tools": tools})
            yield {"type": "content.delta", "delta": summary[:10]}
            yield {"type": "content.delta", "delta": summary[10:]}
            yield {"type": "usage", "prompt_tokens": 5000, "completion_tokens": 40}
            yield {"type": "turn.completed", "ok": True}
        return stream

    async def test_compaction_stores_a_summary_and_the_next_context_starts_there(self):
        calls = [{"id": "k1", "name": "shell", "raw_arguments": '{"command": "ls"}', "status": "completed", "result": '{"files": 3}'}]
        for message in [_user(1, "list the files"), _bot(1, "Three files.", prompt_tokens=90_000, calls=calls), _user(2, "now test"), _bot(2, "Tests pass.", prompt_tokens=95_000), _user(3, "ship it")]:
            self.storage.add_message(message)
        with mock.patch.object(context_compactor.llm_service, "stream_chat_completion", self._fake_stream()):
            stored = await compact_thread("t", reason="manual", model="m", system_prompt="SYS", storage=self.storage, keep_recent_turns=1)

        self.assertTrue(is_compaction(stored))
        self.assertEqual(stored["raw_payload"]["messages_compacted"], 4)
        self.assertEqual(stored["raw_payload"]["covers_through"], "b2")
        self.assertEqual(stored["raw_payload"]["prompt_tokens_before"], 95_000)
        self.assertEqual(stored["raw_payload"]["summary_usage"], {"prompt_tokens": 5000, "completion_tokens": 40})
        # The summariser saw the older turns with their tool results, then the instruction, and no tools.
        request = self.requests[0]
        self.assertEqual(request["system_prompt"], "SYS")
        self.assertIsNone(request["tools"])
        roles = [m["role"] for m in request["messages"]]
        self.assertEqual(roles, ["user", "assistant", "tool", "assistant", "user", "assistant", "user"])
        self.assertEqual(request["messages"][2]["content"], '{"files": 3}')
        self.assertEqual(request["messages"][-1]["content"], SUMMARY_INSTRUCTION)

        # Stored order: the summary lands after the compacted messages, before the kept turn.
        history = self.storage.get_messages(thread_id="t")
        # Stored after the newest user message (insertion order), yet the
        # context keeps that message because the compaction says what it covers.
        self.assertEqual([m["id"] for m in history][-2:], ["u3", stored["id"]])
        context = build_history_messages(history, replay_tools=True)
        self.assertEqual([m["role"] for m in context], ["user", "assistant", "user"])
        self.assertIn("Build the thing.", context[0]["content"])
        self.assertTrue(context[0]["content"].startswith("Summary of the conversation so far"))
        self.assertEqual(context[-1]["content"], "ship it")

    async def test_a_second_compaction_feeds_the_first_summary_to_the_model(self):
        for message in [_user(1, "a"), _bot(1, "A"), _compaction(1, "FIRST SUMMARY"), _user(2, "b"), _bot(2, "B"), _user(3, "c")]:
            self.storage.add_message(message)
        with mock.patch.object(context_compactor.llm_service, "stream_chat_completion", self._fake_stream("SECOND")):
            stored = await compact_thread("t", reason="auto", model="m", system_prompt="SYS", storage=self.storage, keep_recent_turns=1)
        self.assertEqual(stored["raw_payload"]["messages_compacted"], 2)
        contents = [m["content"] for m in self.requests[0]["messages"]]
        self.assertIn("FIRST SUMMARY", contents[0])
        self.assertEqual(contents[2:4], ["b", "B"])

    async def test_nothing_to_compact_returns_none_without_calling_the_model(self):
        for message in [_user(1, "a"), _bot(1, "A"), _user(2, "b")]:
            self.storage.add_message(message)
        with mock.patch.object(context_compactor.llm_service, "stream_chat_completion", self._fake_stream()):
            self.assertIsNone(await compact_thread("t", reason="manual", model="m", system_prompt="SYS", storage=self.storage, keep_recent_turns=4))
        self.assertEqual(self.requests, [])

    async def test_an_empty_summary_is_an_error_and_stores_nothing(self):
        for message in [_user(1, "a"), _bot(1, "A"), _user(2, "b"), _bot(2, "B"), _user(3, "c")]:
            self.storage.add_message(message)
        with mock.patch.object(context_compactor.llm_service, "stream_chat_completion", self._fake_stream("   ")):
            with self.assertRaises(RuntimeError):
                await compact_thread("t", reason="manual", model="m", system_prompt="SYS", storage=self.storage, keep_recent_turns=1)
        self.assertFalse(any(is_compaction(m) for m in self.storage.get_messages(thread_id="t")))


if __name__ == "__main__":
    unittest.main()
