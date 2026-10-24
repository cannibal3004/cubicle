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


for _name in [n for n in dir(chat_harness.ChatStreamRouteTests) if n.startswith("test_")]:
    if _name not in InlineToolTests.__dict__:
        setattr(InlineToolTests, _name, None)


if __name__ == "__main__":
    unittest.main()
