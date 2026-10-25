"""Tool calls written as text (inside the thinking, or unparsed in the answer) are run, and the model never gets to guess their result."""

import json
import unittest
from unittest import mock

from app.services import stray_calls
from app.services.llm_tools import available_tools

import test_chat_router as chat_harness
from test_chat_router import _scripted_stream

XML_CALL = (
    "<tool_call>\n<function=memory_write>\n<parameter=text>\nRepo: ~/proj\nBranch: main\n</parameter>\n</function>\n</tool_call>"
)


class ParsingTests(unittest.TestCase):
    def test_both_qwen_formats_are_read_with_typed_values(self):
        specs = available_tools(computer=True)
        text = (
            "<tool_call>\n<function=computer_terminal_execute>\n<parameter=command>\necho \"${cf:-<none>}\"\n</parameter>\n"
            "<parameter=timeout_seconds>\n150\n</parameter>\n<parameter=background>\ntrue\n</parameter>\n</function>\n</tool_call>\n"
            "<tool_call>\n{\"name\": \"write_file\", \"arguments\": {\"path\": \"a.txt\", \"content\": \"hi\"}}\n</tool_call>"
        )
        calls, unreadable = stray_calls.parse_blocks(text, specs)
        self.assertEqual(unreadable, [])
        self.assertEqual([c["name"] for c in calls], ["computer_terminal_execute", "write_file"])
        self.assertEqual(json.loads(calls[0]["arguments"]), {"command": "echo \"${cf:-<none>}\"", "timeout_seconds": 150, "background": True})
        self.assertEqual(json.loads(calls[1]["arguments"]), {"path": "a.txt", "content": "hi"})

    def test_the_watch_cuts_after_consecutive_calls_and_before_any_other_text(self):
        text = "Let me check.\n\n" + XML_CALL + "\n" + XML_CALL + "The output shows the repo is clean."
        watch = stray_calls.CallWatch()
        consumed = 0
        for i in range(0, len(text), 5):
            consumed = i + 5
            if watch.feed(text[i:i + 5]):
                break
        self.assertTrue(watch.cut)
        self.assertLess(consumed, text.index("The output shows") + 6)
        self.assertEqual(watch.before_calls(), "Let me check.\n\n")
        self.assertEqual(len(stray_calls.parse_blocks(watch.text)[0]), 2)

    def test_prose_that_names_the_tags_is_not_a_call(self):
        watch = stray_calls.CallWatch()
        self.assertFalse(any(watch.feed(ch) for ch in "Qwen wraps calls in <tool_call> ... </tool_call> tags, I think."))
        self.assertFalse(watch.holds_calls())


class StrayCallTurnTests(chat_harness.ChatStreamRouteTests):
    def _bot(self, text="remember the repo"):
        bot = self.storage.get_bots()[0]
        self.storage.add_message({"id": "m1", "thread_id": bot["id"], "bot_id": bot["id"], "sender": "user", "text": text})
        return bot

    def _gateway_with_memory(self):
        from app.services.bot_memory import register_memory_actions

        gateway, _ = self._gateway("allow")
        register_memory_actions(gateway)
        return gateway

    def test_a_call_inside_the_thinking_runs_and_the_invented_result_never_streams(self):
        bot = self._bot()
        recorder = {}
        thinking = [{"type": "reasoning.delta", "delta": "I should save it.\n\n"}]
        thinking += [{"type": "reasoning.delta", "delta": XML_CALL[i:i + 9]} for i in range(0, len(XML_CALL), 9)]
        thinking += [{"type": "reasoning.delta", "delta": "The notes were saved and now read 'Repo: ~/proj' as expected."},
                     {"type": "content.delta", "delta": "Saved your repo."}, {"type": "turn.completed", "ok": True}]
        stream = _scripted_stream(recorder, [
            thinking,
            [{"type": "content.delta", "delta": "Noted the repo and branch."}, {"type": "turn.completed", "ok": True}],
        ])
        with mock.patch("app.services.storage_service.storage_service", self.storage):
            _, events = self._run_turn(bot["id"], stream=stream, gateway=self._gateway_with_memory())
        self.assertEqual(next(b for b in self.storage.get_bots() if b["id"] == bot["id"])["memory"], "Repo: ~/proj\nBranch: main")
        streamed = "".join(e.get("delta", "") for e in events if e["type"] in ("reasoning.delta", "content.delta"))
        self.assertNotIn("now read", streamed)  # the invented result was never generated past the cut
        self.assertNotIn("Saved your repo", streamed)
        self.assertIn("turn.stray_calls", [e["type"] for e in events])
        # The model's next round sees a real call and its real result.
        second = recorder["rounds"][1]["messages"]
        assistant = next(m for m in second if m.get("tool_calls"))
        self.assertEqual(assistant["tool_calls"][0]["name"], "memory_write")
        self.assertIn("Repo: ~/proj", second[-1]["content"])
        saved = self.storage.get_messages(bot["id"])[-1]
        self.assertEqual(saved["text"], "Noted the repo and branch.")
        self.assertEqual(saved["raw_payload"]["stray_calls"], 1)
        self.assertEqual(saved["raw_payload"]["tool_calls"][0]["written_as_text"], "thinking")
        self.assertNotIn("<tool_call>", saved["raw_payload"]["reasoning"])

    def test_a_call_left_as_text_in_the_answer_runs_too(self):
        bot = self._bot()
        recorder = {}
        call = "<tool_call>\n{\"name\": \"memory_write\", \"arguments\": {\"text\": \"Repo: ~/proj\"}}\n</tool_call>"
        stream = _scripted_stream(recorder, [
            [{"type": "content.delta", "delta": "Saving it now.\n" + call}, {"type": "turn.completed", "ok": True}],
            [{"type": "content.delta", "delta": "Done."}, {"type": "turn.completed", "ok": True}],
        ])
        with mock.patch("app.services.storage_service.storage_service", self.storage):
            _, events = self._run_turn(bot["id"], stream=stream, gateway=self._gateway_with_memory())
        self.assertEqual(next(b for b in self.storage.get_bots() if b["id"] == bot["id"])["memory"], "Repo: ~/proj")
        saved = self.storage.get_messages(bot["id"])[-1]
        self.assertEqual(saved["text"], "Saving it now.\n\nDone.")
        self.assertEqual(saved["raw_payload"]["tool_calls"][0]["written_as_text"], "answer")

    def test_an_unreadable_call_in_the_thinking_is_reported_not_guessed(self):
        bot = self._bot()
        recorder = {}
        broken = "<tool_call>\n{\"name\": \"memory_write\", \"arguments\": {\"text\": \"Repo\"\n</tool_call>"
        stream = _scripted_stream(recorder, [
            [{"type": "reasoning.delta", "delta": broken}, {"type": "reasoning.delta", "delta": " It worked, the notes are saved."},
             {"type": "turn.completed", "ok": True}],
            [{"type": "content.delta", "delta": "Let me try that properly."}, {"type": "turn.completed", "ok": True}],
        ])
        with mock.patch("app.services.storage_service.storage_service", self.storage):
            _, events = self._run_turn(bot["id"], stream=stream, gateway=self._gateway_with_memory())
        nudge = recorder["rounds"][1]["messages"][-1]
        self.assertEqual(nudge["role"], "user")
        self.assertIn("did not run", nudge["content"])
        streamed = "".join(e.get("delta", "") for e in events if e["type"] == "reasoning.delta")
        self.assertNotIn("It worked", streamed)


for _name in [n for n in dir(chat_harness.ChatStreamRouteTests) if n.startswith("test_")]:
    if _name not in StrayCallTurnTests.__dict__:
        setattr(StrayCallTurnTests, _name, None)


if __name__ == "__main__":
    unittest.main()
