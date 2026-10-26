"""The start of the prompt stays the same from turn to turn, so the model server's prompt cache holds."""

import asyncio
import unittest
from unittest import mock

from app.routers import chat as chat_router
from app.services.context_builder import TURN_CONTEXT_LEAD, build_history_messages

import test_chat_router as chat_harness
from test_chat_router import _scripted_stream


class TurnContextTests(chat_harness.ChatStreamRouteTests):
    def _lookups(self, calls):
        async def retrieved(bot_id, query):
            calls.append(("retrieved", query))
            return f"Passages from the attached files that match this message:\n--- from notes.pdf (part 1) ---\nabout {query}"

        async def recalled(bot_id, query, history):
            calls.append(("recalled", query))
            return f"Earlier in this conversation (recalled):\n- the user: \"something about {query}\""

        async def procedures(bot_id, query):
            calls.append(("procedures", query))
            return ""

        return [
            mock.patch.object(chat_router, "retrieved_context", retrieved),
            mock.patch.object(chat_router, "recalled_context", recalled),
            mock.patch.object(chat_router, "procedure_context", procedures),
        ]

    def _turn(self, bot_id, reply, lookups):
        recorder = {}
        stream = _scripted_stream(recorder, [[{"type": "content.delta", "delta": reply}, {"type": "turn.completed", "ok": True}]])
        for p in lookups:
            p.start()
        try:
            _, events = self._run_turn(bot_id, stream=stream)
        finally:
            for p in reversed(lookups):
                p.stop()
        return recorder, events

    def test_looked_up_context_rides_with_the_message_and_the_system_prompt_stays_put(self):
        bot = self.storage.get_bots()[0]["id"]
        self.storage.delete_messages(bot)
        calls = []
        self.storage.add_message({"id": "u1", "thread_id": bot, "bot_id": bot, "sender": "user", "text": "first question", "created_at": "2026-10-26T10:00:00"})
        first, events = self._turn(bot, "First answer.", self._lookups(calls))
        self.assertIn("recall.used", [e["type"] for e in events])
        self.storage.add_message({"id": "u2", "thread_id": bot, "bot_id": bot, "sender": "user", "text": "second question", "created_at": "2026-10-26T10:05:00"})
        second, _ = self._turn(bot, "Second answer.", self._lookups(calls))

        sys1 = first["rounds"][0]["system_prompt"]
        sys2 = second["rounds"][0]["system_prompt"]
        self.assertEqual(sys1, sys2)  # nothing per-turn in the system prompt any more
        self.assertNotIn("about first question", sys1)
        sent1 = first["rounds"][0]["messages"]
        sent2 = second["rounds"][0]["messages"]
        # The first exchange is replayed exactly as it was sent: the prompt only grows at the end.
        self.assertEqual(sent2[: len(sent1)], sent1)
        self.assertIn(TURN_CONTEXT_LEAD, sent1[-1]["content"])
        self.assertIn("about first question", sent1[-1]["content"])
        self.assertTrue(sent1[-1]["content"].startswith("first question"))
        self.assertIn("about second question", sent2[-1]["content"])
        # Stored on the message, with what was found; the chat gets the counts, not the text.
        stored = next(m for m in self.storage.get_messages(bot) if m["id"] == "u1")
        self.assertEqual((stored["raw_payload"]["turn_context"]["passages"], stored["raw_payload"]["turn_context"]["recalled"]), (1, 1))
        with mock.patch("app.services.storage_service.storage_service", self.storage):
            shown = next(m for m in asyncio.run(chat_router.get_history(bot)) if m["id"] == "u1")
        self.assertNotIn("text", shown["raw_payload"]["turn_context"])
        # Each message was looked up once.
        self.assertEqual(sorted(q for kind, q in calls if kind == "recalled"), ["first question", "second question"])

    def test_running_the_turn_again_reuses_what_was_looked_up(self):
        bot = self.storage.get_bots()[0]["id"]
        self.storage.delete_messages(bot)
        calls = []
        self.storage.add_message({"id": "u1", "thread_id": bot, "bot_id": bot, "sender": "user", "text": "a question", "created_at": "2026-10-26T10:00:00"})
        first, _ = self._turn(bot, "An answer.", self._lookups(calls))
        # The reply is undone and the same message runs again.
        self.storage.delete_message_ids(bot, [m["id"] for m in self.storage.get_messages(bot) if m["sender"] == "bot"])
        again, _ = self._turn(bot, "Another answer.", self._lookups(calls))
        self.assertEqual(len([c for c in calls if c[0] == "recalled"]), 1)
        self.assertEqual(first["rounds"][0]["messages"], again["rounds"][0]["messages"])

    def test_a_message_with_nothing_found_reads_as_before(self):
        history = [{"id": "u1", "sender": "user", "text": "hello", "raw_payload": {"turn_context": {"text": ""}}}]
        self.assertEqual(build_history_messages(history, replay_tools=True)[0]["content"], "hello")


for _name in [n for n in dir(chat_harness.ChatStreamRouteTests) if n.startswith("test_")]:
    if _name not in TurnContextTests.__dict__:
        setattr(TurnContextTests, _name, None)


if __name__ == "__main__":
    unittest.main()
