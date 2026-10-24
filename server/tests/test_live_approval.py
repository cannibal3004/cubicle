"""Approval settings changed during a turn take effect in that turn: later calls and a card already waiting."""

import asyncio
import unittest
from typing import Any, Dict
from unittest import mock

from app.routers import bots as bots_router
from app.routers import chat as chat_router
from app.services import storage_service as storage_module
from app.services.llm_tools import available_tools
from app.services.turn_manager import TurnManager

from tests.test_chat_gate import ReleasingBroker, ScriptedDecider
from tests.test_chat_router import NoComposio, _collect_sse, _fake_request, _scripted_stream
import tests.test_trusted_tools as trusted_harness


def write_call(call_id, path):
    return {"id": call_id, "name": "shared_folder_write", "arguments": "{\"path\": \"%s\", \"content\": \"hello\"}" % path}


class LiveApprovalTests(trusted_harness.TrustedTurnTests):
    def setUp(self):
        super().setUp()
        # The user would deny if asked: an action that runs was allowed by the settings.
        self.broker = ReleasingBroker(user_answer="deny", user_answer_after=3.0)
        self.gateway.approvals = self.broker
        self.storage.save_settings({"auto_approval": "off", "decider_url": "http://decider.test:8731", "decider_threshold": 0.2})

    def _turn(self, rounds, while_waiting):
        """Run a turn; when the first card opens, `while_waiting()` changes a setting the way the user would."""
        recorder: Dict[str, Any] = {}
        stream = _scripted_stream(recorder, rounds)
        patches = [
            mock.patch.object(storage_module, "storage_service", self.storage),
            mock.patch.object(chat_router.llm_service, "current_llm_config", return_value=self.config),
            mock.patch.object(chat_router.llm_service, "stream_chat_completion", stream),
            mock.patch.object(chat_router, "composio_service", NoComposio()),
            mock.patch.object(chat_router, "turn_manager", TurnManager()),
            mock.patch.object(chat_router, "action_gateway", self.gateway),
            mock.patch.object(chat_router, "approval_gate", self.gate),
        ]
        for p in patches:
            p.start()
        try:
            async def scenario():
                await chat_router.start_turn(self.bot["id"], chat_router.StartTurnRequest())

                async def user():
                    for _ in range(200):
                        if self.broker.futures:
                            break
                        await asyncio.sleep(0.01)
                    await while_waiting()

                watcher = asyncio.get_running_loop().create_task(user())
                response = await chat_router.stream_turn(self.bot["id"], _fake_request(), turn=None, after=0)
                events = await _collect_sse(response)
                await watcher
                return events

            events = asyncio.run(scenario())
        finally:
            for p in reversed(patches):
                p.stop()
        return events, recorder

    def test_trusting_the_tool_allows_the_open_card_and_the_next_call_does_not_ask(self):
        async def trust():
            await bots_router.update_bot(self.bot["id"], bots_router.BotUpdate(trusted_tools=["shared_folder_write"]))

        events, _ = self._turn(
            [
                [{"type": "tool_calls", "calls": [write_call("c1", "a.txt")]}, {"type": "turn.completed", "ok": True}],
                [{"type": "tool_calls", "calls": [write_call("c2", "b.txt")]}, {"type": "turn.completed", "ok": True}],
                [{"type": "content.delta", "delta": "Done."}, {"type": "turn.completed", "ok": True}],
            ],
            trust,
        )
        opened = [e for e in events if e["type"] == "request.opened"]
        completed = [e for e in events if e["type"] == "tool.completed"]
        self.assertEqual(len(opened), 1)  # only the first call ever showed a card
        self.assertEqual(len(completed), 2)
        self.assertTrue((self.workspace_root / "a.txt").exists())
        self.assertTrue((self.workspace_root / "b.txt").exists())
        self.assertEqual([(d, by) for _, d, by in self.broker.resolved], [("allow", "trusted")])
        records = self.storage.get_messages(self.bot["id"])[-1]["raw_payload"]["tool_calls"]
        self.assertEqual([(r["status"], r.get("trusted_by")) for r in records], [("completed", "trusted"), ("completed", "trusted")])

    def test_switching_auto_approval_on_scores_the_open_card(self):
        ScriptedDecider.scores = None  # every question scores low: safe

        async def switch_on():
            await bots_router.update_bot(self.bot["id"], bots_router.BotUpdate(auto_approval="on"))

        events, _ = self._turn(
            [
                [{"type": "tool_calls", "calls": [write_call("c1", "a.txt")]}, {"type": "turn.completed", "ok": True}],
                [{"type": "content.delta", "delta": "Done."}, {"type": "turn.completed", "ok": True}],
            ],
            switch_on,
        )
        types = [e["type"] for e in events]
        self.assertIn("gate.scored", types)
        self.assertIn("tool.completed", types)
        self.assertTrue((self.workspace_root / "a.txt").exists())
        self.assertEqual(len(self.broker.resolved), 1)
        self.assertNotEqual(self.broker.resolved[0][2], "user")
        self.assertEqual(len(ScriptedDecider.calls), 1)

    def test_an_unrelated_change_leaves_the_card_to_the_user(self):
        async def rename():
            await bots_router.update_bot(self.bot["id"], bots_router.BotUpdate(trusted_tools=["memory_write"]))

        events, _ = self._turn(
            [
                [{"type": "tool_calls", "calls": [write_call("c1", "a.txt")]}, {"type": "turn.completed", "ok": True}],
                [{"type": "content.delta", "delta": "ok"}, {"type": "turn.completed", "ok": True}],
            ],
            rename,
        )
        self.assertIn("tool.denied", [e["type"] for e in events])
        self.assertFalse((self.workspace_root / "a.txt").exists())
        self.assertEqual(chat_router._open_cards, {})  # nothing left behind


class TrustedForTests(unittest.TestCase):
    def test_current_settings_decide(self):
        specs = available_tools(computer=True)
        bot = {"trusted_tools": ["computer_write_file"], "allowed_commands": ["git status"]}
        self.assertEqual(chat_router.trusted_for(bot, "write_file", {}, specs), "trusted")
        self.assertEqual(chat_router.trusted_for(bot, "computer_terminal_execute", {"command": "git status"}, specs), "allowed_command")
        self.assertFalse(chat_router.trusted_for(bot, "computer_terminal_execute", {"command": "rm -rf x"}, specs))
        self.assertFalse(chat_router.trusted_for(bot, "edit_file", {}, specs))
        self.assertFalse(chat_router.trusted_for(None, "write_file", {}, specs))


# Only the tests above run under this module, not the inherited trusted-tool tests again.
for _name in [n for n in dir(trusted_harness.TrustedTurnTests) if n.startswith("test_")]:
    if _name not in LiveApprovalTests.__dict__:
        setattr(LiveApprovalTests, _name, None)


if __name__ == "__main__":
    unittest.main()
