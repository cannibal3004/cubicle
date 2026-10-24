"""The file tools' new names, the old and habitual names that still reach them, and the turn's handling of odd calls."""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.routers import chat as chat_router
from app.services import tool_names
from app.services.context_builder import build_history_messages
from app.services.llm_tools import ToolCallError, available_tools, build_invocation, filter_tools, trust_tools
from app.services.storage_service import StorageService

import test_chat_router as chat_harness
from test_chat_router import _scripted_stream


class NameTests(unittest.TestCase):
    def test_the_file_tools_have_the_names_models_reach_for(self):
        names = [spec.name for spec in available_tools(computer=True)]
        for name in ("read_file", "write_file", "edit_file", "list_files", "computer_terminal_execute", "computer_screenshot"):
            self.assertIn(name, names)
        for old in tool_names.RENAMED:
            self.assertNotIn(old, names)

    def test_former_and_habitual_names_resolve_only_to_tools_on_offer(self):
        offered = ["write_file", "edit_file", "computer_terminal_execute"]
        self.assertEqual(tool_names.resolve("write_file", offered), "write_file")
        self.assertEqual(tool_names.resolve("computer_write_file", offered), "write_file")
        self.assertEqual(tool_names.resolve("write_to_file", offered), "write_file")
        self.assertEqual(tool_names.resolve("str_replace", offered), "edit_file")
        self.assertEqual(tool_names.resolve("bash", offered), "computer_terminal_execute")
        # read_file is not on offer (switched off for this bot): the habit does not reach it.
        self.assertEqual(tool_names.resolve("view_file", offered), "view_file")
        # A held-back app tool is still callable by its own name.
        self.assertEqual(tool_names.resolve("exa_web_search", offered, callable_names=offered + ["exa_web_search"]), "exa_web_search")
        self.assertEqual(tool_names.resolve("made_up", offered), "made_up")

    def test_an_unknown_name_gets_the_closest_real_ones(self):
        offered = ["write_file", "edit_file", "read_file", "computer_terminal_execute", "memory_write"]
        self.assertIn("did you mean write_file", tool_names.unknown_message("computer_write", offered))
        self.assertIn("memory_write", tool_names.unknown_message("memory_writ", offered))
        self.assertIn("Call only the tools listed", tool_names.unknown_message("qqq", offered))
        with self.assertRaisesRegex(ToolCallError, "did you mean"):
            build_invocation("writefile", {}, "bot-1", specs=[s for s in available_tools(computer=True)])

    def test_former_names_still_build_the_same_call(self):
        old = build_invocation("computer_write_file", {"path": "a.txt", "content": "x"}, "bot-1")
        new = build_invocation("write_file", {"path": "a.txt", "content": "x"}, "bot-1")
        self.assertEqual((old.name, old.arguments), (new.name, new.arguments))

    def test_stored_settings_with_former_names_keep_working(self):
        specs = available_tools(computer=True)
        trusted = {s.name for s in trust_tools(specs, ["computer_write_file"]) if s.trusted}
        self.assertEqual(trusted, {"write_file"})
        kept = [s.name for s in filter_tools(specs, {"tools": {"computer_edit_file": False}})]
        self.assertNotIn("edit_file", kept)
        self.assertIn("write_file", kept)
        temp = tempfile.TemporaryDirectory()
        try:
            storage = StorageService(Path(temp.name))
            bots = storage.get_bots()
            bots[0]["trusted_tools"] = ["computer_write_file", "write_file", "computer_screenshot"]
            bots[0]["tool_settings"] = {"tools": {"computer_files_list": False}}
            storage.save_bots(bots)
            bot = storage.get_bots()[0]
            self.assertEqual(bot["trusted_tools"], ["write_file", "computer_screenshot"])
            self.assertEqual(bot["tool_settings"], {"tools": {"list_files": False}})
        finally:
            try:
                temp.cleanup()
            except PermissionError:
                pass

    def test_old_turns_are_replayed_with_todays_names(self):
        history = [
            {"id": "u1", "sender": "user", "text": "write it"},
            {"id": "b1", "sender": "bot", "text": "done", "raw_payload": {"tool_calls": [
                {"id": "c1", "name": "computer_write_file", "raw_arguments": "{\"path\": \"a\", \"content\": \"x\"}", "status": "completed", "result": "{}"},
            ]}},
        ]
        messages = build_history_messages(history, replay_tools=True)
        call = next(m for m in messages if m.get("tool_calls"))
        self.assertEqual(call["tool_calls"][0]["name"], "write_file")


class TurnTests(chat_harness.ChatStreamRouteTests):
    """Reuses the chat route harness; only the tests below run here."""

    def _bot_with_message(self, text):
        bot = self.storage.get_bots()[0]
        self.storage.add_message({"id": "m1", "thread_id": bot["id"], "bot_id": bot["id"], "sender": "user", "text": text})
        return bot

    def test_a_call_repeated_in_one_round_runs_once(self):
        from app.services.bot_memory import register_memory_actions

        bot = self._bot_with_message("remember the repo")
        gateway, _ = self._gateway("allow")
        register_memory_actions(gateway)
        recorder = {}
        same = "{\"text\": \"Repo: ~/proj\"}"
        stream = _scripted_stream(recorder, [
            [{"type": "tool_calls", "calls": [
                {"id": "n1", "name": "memory_write", "arguments": same},
                {"id": "n2", "name": "memory_write", "arguments": same},
            ]}, {"type": "turn.completed", "ok": True}],
            [{"type": "content.delta", "delta": "Noted."}, {"type": "turn.completed", "ok": True}],
        ])
        with mock.patch("app.services.storage_service.storage_service", self.storage):
            _, events = self._run_turn(bot["id"], stream=stream, gateway=gateway)
        completed = [e for e in events if e["type"] == "tool.completed"]
        skipped = [e for e in events if e["type"] == "tool.failed"]
        self.assertEqual(len(completed), 1)
        self.assertEqual(len(skipped), 1)
        self.assertIn("same call as n1", skipped[0]["error"])
        tool_replies = [m for m in recorder["rounds"][1]["messages"] if m.get("role") == "tool"]
        self.assertEqual([m["tool_call_id"] for m in tool_replies], ["n1", "n2"])

    def test_a_former_name_runs_as_the_real_tool_and_an_unknown_one_gets_a_hint(self):
        from app.services.bot_memory import register_memory_actions

        bot = self._bot_with_message("write it down")
        gateway, _ = self._gateway("allow")
        register_memory_actions(gateway)
        recorder = {}
        stream = _scripted_stream(recorder, [
            [{"type": "tool_calls", "calls": [
                {"id": "w1", "name": "computer_write_file", "arguments": "{\"path\": \"notes.md\", \"content\": \"x\"}"},
                {"id": "w2", "name": "memory_writ", "arguments": "{\"text\": \"x\"}"},
            ]}, {"type": "turn.completed", "ok": True}],
            [{"type": "content.delta", "delta": "ok"}, {"type": "turn.completed", "ok": True}],
        ])
        with mock.patch("app.services.storage_service.storage_service", self.storage), mock.patch.object(chat_router.settings, "COMPUTER_PROVIDER", "docker"):
            _, events = self._run_turn(bot["id"], stream=stream, gateway=gateway)
        names = [e.get("callName") for e in events if e["type"] in ("tool.started", "tool.completed", "tool.failed", "approval.requested")]
        self.assertIn("write_file", names)
        self.assertNotIn("computer_write_file", names)
        # The model's next round sees its call under the real name.
        assistant = next(m for m in recorder["rounds"][1]["messages"] if m.get("tool_calls"))
        self.assertEqual(assistant["tool_calls"][0]["name"], "write_file")
        hint = next(e for e in events if e["type"] == "tool.failed" and e.get("callName") == "memory_writ")
        self.assertIn("did you mean memory_write", hint["error"])


# Only the TurnTests above run under this name, not the whole inherited suite again.
for _name in [n for n in dir(chat_harness.ChatStreamRouteTests) if n.startswith("test_")]:
    if _name not in TurnTests.__dict__:
        setattr(TurnTests, _name, None)


if __name__ == "__main__":
    unittest.main()
