"""Each stored tool call knows where in the reply it came, so the chat can draw it there."""

import unittest
from unittest import mock

import test_chat_router as chat_harness
from test_chat_router import _scripted_stream


class InlineToolTests(chat_harness.ChatStreamRouteTests):
    def test_calls_are_placed_between_the_rounds_of_text(self):
        from app.services.bot_memory import register_memory_actions

        bot = self.storage.get_bots()[0]
        self.storage.add_message({"id": "m1", "thread_id": bot["id"], "bot_id": bot["id"], "sender": "user", "text": "note two things"})
        gateway, _ = self._gateway("allow")
        register_memory_actions(gateway)
        recorder = {}
        stream = _scripted_stream(recorder, [
            # Round 1: calls with no text before them.
            [{"type": "tool_calls", "calls": [{"id": "a1", "name": "memory_read", "arguments": "{}"}]}, {"type": "turn.completed", "ok": True}],
            # Round 2: a preamble, then a call.
            [{"type": "content.delta", "delta": "Let me write the first one down.\n"},
             {"type": "tool_calls", "calls": [{"id": "b1", "name": "memory_write", "arguments": "{\"text\": \"one\"}"},
                                              {"id": "b2", "name": "todo_list", "arguments": "{}"}]},
             {"type": "turn.completed", "ok": True}],
            # Round 3: more text and a call.
            [{"type": "content.delta", "delta": "Now the second."},
             {"type": "tool_calls", "calls": [{"id": "c1", "name": "memory_edit", "arguments": "{\"old_text\": \"one\", \"new_text\": \"one, two\"}"}]},
             {"type": "turn.completed", "ok": True}],
            # The answer.
            [{"type": "content.delta", "delta": "Both are in your notes."}, {"type": "turn.completed", "ok": True}],
        ])
        with mock.patch("app.services.storage_service.storage_service", self.storage):
            self._run_turn(bot["id"], stream=stream, gateway=gateway)
        saved = self.storage.get_messages(bot["id"])[-1]
        text = saved["text"]
        self.assertEqual(text, "Let me write the first one down.\n\nNow the second.\n\nBoth are in your notes.")
        at = {r["id"]: r["at"] for r in saved["raw_payload"]["tool_calls"]}
        self.assertEqual(at["a1"], 0)
        self.assertEqual(at["b1"], at["b2"])
        self.assertEqual(text[: at["b1"]], "Let me write the first one down.")
        self.assertEqual(text[: at["c1"]], "Let me write the first one down.\n\nNow the second.")
        # What follows the last call is the answer.
        self.assertEqual(text[at["c1"]:].strip(), "Both are in your notes.")

    def test_each_rounds_thinking_is_placed_where_it_began(self):
        from app.services.bot_memory import register_memory_actions

        bot = self.storage.get_bots()[0]
        self.storage.add_message({"id": "m1", "thread_id": bot["id"], "bot_id": bot["id"], "sender": "user", "text": "check my notes"})
        gateway, _ = self._gateway("allow")
        register_memory_actions(gateway)
        recorder = {}
        stream = _scripted_stream(recorder, [
            # Round 1: thinks, says a word, calls.
            [{"type": "reasoning.delta", "delta": "First I should read them."},
             {"type": "content.delta", "delta": "Reading."},
             {"type": "tool_calls", "calls": [{"id": "a1", "name": "memory_read", "arguments": "{}"}]}, {"type": "turn.completed", "ok": True}],
            # Round 2: calls without thinking.
            [{"type": "tool_calls", "calls": [{"id": "b1", "name": "todo_list", "arguments": "{}"}]}, {"type": "turn.completed", "ok": True}],
            # Round 3: thinks again, then answers.
            [{"type": "reasoning.delta", "delta": "They are empty."}, {"type": "content.delta", "delta": "Your notes are empty."}, {"type": "turn.completed", "ok": True}],
        ])
        with mock.patch("app.services.storage_service.storage_service", self.storage):
            self._run_turn(bot["id"], stream=stream, gateway=gateway)
        saved = self.storage.get_messages(bot["id"])[-1]
        payload = saved["raw_payload"]
        self.assertEqual(payload["reasoning"], "First I should read them.They are empty.")
        parts = payload["reasoning_parts"]
        # The second round thought nothing, so there are two parts; the third round's comes after
        # the reply's first sentence and both calls.
        self.assertEqual(parts, [{"at": 0, "after": 0, "start": 0}, {"at": len("Reading."), "after": 2, "start": len("First I should read them.")}])
        self.assertEqual(saved["text"][parts[1]["at"]:].strip(), "Your notes are empty.")


for _name in [n for n in dir(chat_harness.ChatStreamRouteTests) if n.startswith("test_")]:
    if _name not in InlineToolTests.__dict__:
        setattr(InlineToolTests, _name, None)


if __name__ == "__main__":
    unittest.main()
