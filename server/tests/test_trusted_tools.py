"""Per-bot trusted tools: the user's standing approval for named tools on one bot."""

import asyncio
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict
from unittest import mock

from fastapi import HTTPException

from app.routers import bots as bots_router
from app.routers import chat as chat_router
from app.services import storage_service as storage_module
from app.services.action_gateway import ActionGateway, ActionInvocation, ActionPolicyError
from app.services.approval_gate import ApprovalGate
from app.services.llm_config import LLMConfig
from app.services.llm_tools import available_tools, describe_tools, trust_tools
from app.services.storage_service import StorageService
from app.services.turn_manager import TurnManager
from app.services.workspace_service import WorkspaceService, parse_workspace_command

from tests.test_action_gateway import ImmediateApprovalBroker, RecordingAudit
from tests.test_chat_gate import ReleasingBroker, ScriptedDecider
from tests.test_chat_router import NoComposio, _collect_sse, _fake_request, _scripted_stream

WRITE_CALL = {"id": "c1", "name": "shared_folder_write", "arguments": "{\"path\": \"a.txt\", \"content\": \"hello\"}"}


class TrustedSpecTests(unittest.TestCase):
    def test_trusting_a_tool_drops_its_approval_and_the_prompt_says_so(self):
        specs = available_tools(computer=False)
        write = next(s for s in specs if s.name == "shared_folder_write")
        self.assertTrue(write.needs_approval)
        trusted = trust_tools(specs, ["shared_folder_write", "memory_read", "no_such_tool"])
        by_name = {s.name: s for s in trusted}
        self.assertEqual((by_name["shared_folder_write"].needs_approval, by_name["shared_folder_write"].trusted), (False, True))
        # A tool that never asked is left alone, and unknown names change nothing.
        self.assertFalse(by_name["memory_read"].trusted)
        self.assertEqual(len(trusted), len(specs))
        self.assertIs(trust_tools(specs, []), specs)
        text = describe_tools(trusted)
        self.assertIn("trusted for you", text)
        self.assertIn("shared_folder_write", text.split("trusted for you", 1)[1])
        self.assertNotIn("shared_folder_write", text.split("Tools that need approval:", 1)[1].split("\n", 1)[0])

    def test_the_list_is_validated_and_stored_on_the_bot(self):
        temp = tempfile.TemporaryDirectory()
        storage = StorageService(Path(temp.name))
        with mock.patch.object(storage_module, "storage_service", storage):
            bot = asyncio.run(bots_router.create_bot(bots_router.BotInput(name="Trusty", trusted_tools=["computer_terminal", "computer_terminal"])))
            self.assertEqual(bot["trusted_tools"], ["computer_terminal"])
            updated = asyncio.run(bots_router.update_bot(bot["id"], bots_router.BotUpdate(trusted_tools=["shared_folder_write", "ctx7_resolve-library-id"])))
            self.assertEqual(updated["trusted_tools"], ["shared_folder_write", "ctx7_resolve-library-id"])
            self.assertEqual(asyncio.run(bots_router.update_bot(bot["id"], bots_router.BotUpdate(trusted_tools=[])))["trusted_tools"], [])
            for bad in (["has space"], [""], [1], "computer_terminal", ["x" * 200]):
                with self.assertRaises((HTTPException, ValueError)):  # pydantic's ValidationError is a ValueError
                    asyncio.run(bots_router.update_bot(bot["id"], bots_router.BotUpdate(trusted_tools=bad)))
        try:
            temp.cleanup()
        except PermissionError:
            pass


class TrustedGatewayTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_trusted_call_is_approved_on_record_without_a_card(self):
        temp = tempfile.TemporaryDirectory()
        root = Path(temp.name)
        audit = RecordingAudit()
        broker = ImmediateApprovalBroker()
        gateway = ActionGateway(workspace=WorkspaceService(root), approvals=broker, audit=audit)
        call = parse_workspace_command("/shared write notes.txt\nhello")
        request, approval = gateway.open("thread", "bot", call, trusted=True)
        self.assertIsNone(approval)
        self.assertEqual(broker.opened, [])
        self.assertEqual((request.requires_approval, request.state), (False, "approved"))
        self.assertEqual(await gateway.wait_for_decision(request), "allow")
        approved = next(e for e in audit.events if e["event"] == "action.approved")
        self.assertEqual((approved["decision"], approved["decided_by"]), ("allow", "trusted"))
        await gateway.execute(request)
        self.assertEqual((root / "notes.txt").read_text(encoding="utf-8"), "hello")
        # Policy still applies: an unregistered action is denied before trust is considered.
        with self.assertRaises(ActionPolicyError):
            gateway.open("thread", "bot", ActionInvocation(name="nope.thing", arguments={}, target={}, preview="x"), trusted=True)
        try:
            temp.cleanup()
        except PermissionError:
            pass


class TrustedTurnTests(unittest.TestCase):
    """A chat turn: the trusted tool runs with no card and no gate, even with auto-approval off."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.storage = StorageService(root / "data")
        self.workspace_root = root / "workspace"
        self.workspace_root.mkdir()
        self.storage.save_settings({"auto_approval": "off"})
        self.gate = ApprovalGate(storage=self.storage, workspace=WorkspaceService(self.workspace_root), client_factory=ScriptedDecider)
        self.broker = ReleasingBroker(user_answer="deny")
        self.gateway = ActionGateway(workspace=WorkspaceService(self.workspace_root), approvals=self.broker, audit=self.storage)
        self.config = LLMConfig(provider="openai_compatible", api_key="", base_url="http://x/v1", reasoning_effort="", default_model="m")
        ScriptedDecider.calls = []
        self.bot = self.storage.get_bots()[0]
        self.storage.add_message({"id": "m1", "thread_id": self.bot["id"], "bot_id": self.bot["id"], "sender": "user", "text": "save hello to a.txt"})

    def tearDown(self):
        try:
            self.temp_dir.cleanup()
        except PermissionError:
            pass

    def _trust(self, names):
        bots = self.storage.get_bots()
        bots[0]["trusted_tools"] = names
        self.storage.save_bots(bots)

    def _run(self):
        recorder: Dict[str, Any] = {}
        stream = _scripted_stream(recorder, [
            [{"type": "tool_calls", "calls": [WRITE_CALL]}, {"type": "turn.completed", "ok": True}],
            [{"type": "content.delta", "delta": "Done."}, {"type": "turn.completed", "ok": True}],
        ])
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
                response = await chat_router.stream_turn(self.bot["id"], _fake_request(), turn=None, after=0)
                return await _collect_sse(response)
            events = asyncio.run(scenario())
        finally:
            for p in reversed(patches):
                p.stop()
        return events, recorder

    def test_trusted_tool_runs_without_asking(self):
        self._trust(["shared_folder_write"])
        events, recorder = self._run()
        types = [e["type"] for e in events]
        self.assertNotIn("request.opened", types)
        self.assertNotIn("gate.scored", types)
        self.assertIn("tool.completed", types)
        self.assertEqual(self.broker.resolved, [])
        self.assertEqual(ScriptedDecider.calls, [])
        self.assertEqual((self.workspace_root / "a.txt").read_text(encoding="utf-8"), "hello")
        started = next(e for e in events if e["type"] == "tool.started")
        self.assertTrue(started.get("trusted"))
        saved = self.storage.get_messages(self.bot["id"])[-1]
        self.assertTrue(saved["raw_payload"]["tool_calls"][0]["trusted"])
        # The model was told which tools are trusted.
        system = recorder["rounds"][0]["system_prompt"]
        self.assertIn("trusted for you", system)
        approved = [e for e in self.storage.get_audit_events() if e.get("event") == "action.approved"]
        self.assertEqual(approved[-1].get("decided_by"), "trusted")

    def test_an_untrusted_tool_still_asks(self):
        self._trust(["computer_terminal"])
        events, _ = self._run()
        types = [e["type"] for e in events]
        self.assertIn("request.opened", types)
        self.assertIn("tool.denied", types)
        self.assertFalse((self.workspace_root / "a.txt").exists())


if __name__ == "__main__":
    unittest.main()


class AllowedCommandTests(unittest.TestCase):
    """A bot's allowed-command rules make a matching terminal call trusted; anything else asks."""

    def test_rules_are_stored_and_decide_the_call(self):
        temp = tempfile.TemporaryDirectory()
        storage = StorageService(Path(temp.name))
        with mock.patch.object(storage_module, "storage_service", storage):
            bot = asyncio.run(bots_router.create_bot(bots_router.BotInput(name="Shelly")))
            updated = asyncio.run(bots_router.update_bot(bot["id"], bots_router.BotUpdate(allowed_commands=["git status", " npm   test ", "cat", "git status"])))
            self.assertEqual(updated["allowed_commands"], ["git status", "npm test", "cat"])
            with self.assertRaises(HTTPException):
                asyncio.run(bots_router.update_bot(bot["id"], bots_router.BotUpdate(allowed_commands=[""])))
        self.assertEqual(chat_router.command_trusted(updated, "computer_terminal_execute", {"command": "cd x && npm test | cat"}), "allowed_command")  # cd needs no rule
        self.assertEqual(chat_router.command_trusted(updated, "computer_terminal_execute", {"command": "cd x && rm -rf y"}), False)  # rm does
        self.assertEqual(chat_router.command_trusted(updated, "computer_terminal_execute", {"command": "npm test | cat"}), "allowed_command")
        self.assertEqual(chat_router.command_trusted(updated, "computer_terminal_execute", {"command": 'bash -c "git status -sb"'}), "allowed_command")
        self.assertFalse(chat_router.command_trusted(updated, "computer_terminal_execute", {"command": "git push"}))
        self.assertFalse(chat_router.command_trusted(updated, "computer_terminal_execute", {"command": "cat <<EOF\nx\nEOF"}))
        self.assertFalse(chat_router.command_trusted(updated, "computer_write_file", {"path": "x"}))
        self.assertFalse(chat_router.command_trusted({**updated, "allowed_commands": []}, "computer_terminal_execute", {"command": "cat x"}))
        try:
            temp.cleanup()
        except PermissionError:
            pass

    def test_the_gateway_records_who_allowed_it(self):
        async def scenario():
            temp = tempfile.TemporaryDirectory()
            audit = RecordingAudit()
            gateway = ActionGateway(workspace=WorkspaceService(Path(temp.name)), approvals=ImmediateApprovalBroker(), audit=audit)
            request, approval = gateway.open("thread", "bot", parse_workspace_command("/shared write notes.txt\nhello"), trusted="allowed_command")
            self.assertIsNone(approval)
            approved = next(e for e in audit.events if e["event"] == "action.approved")
            self.assertEqual(approved["decided_by"], "allowed_command")
            try:
                temp.cleanup()
            except PermissionError:
                pass

        asyncio.run(scenario())
