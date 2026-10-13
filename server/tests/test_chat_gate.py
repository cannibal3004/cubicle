"""The auto-approval gate inside a chat turn: the real ActionGateway, a broker
whose `resolve` releases the wait, and a scripted decision server."""

import asyncio
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, Optional
from unittest import mock

from app.routers import chat as chat_router
from app.services.action_gateway import ActionGateway
from app.services.approval_gate import QUESTIONS, ApprovalGate, DeciderError
from app.services.llm_config import LLMConfig
from app.services.storage_service import StorageService
from app.services.turn_manager import TurnManager
from app.services.workspace_service import WorkspaceService

from test_chat_router import NoComposio, _collect_sse, _fake_request, _scripted_stream


class ReleasingBroker:
    """Holds every approval open until `resolve` is called, like the real broker.

    If nobody resolves within `user_answer_after` seconds the stand-in user
    answers with `user_answer`, so a test can tell "the gate approved it" from
    "the user eventually did".
    """

    def __init__(self, user_answer="allow", user_answer_after=0.3):
        self.user_answer = user_answer
        self.user_answer_after = user_answer_after
        self.futures: Dict[str, asyncio.Future] = {}
        self.resolved = []   # (request_id, decision, decided_by)

    def open(self, thread_id, bot_id, call, request_id=None):
        self.futures[request_id] = asyncio.get_running_loop().create_future()
        return {
            "request_id": request_id, "thread_id": thread_id, "bot_id": bot_id,
            "tool": call.name, "summary": call.summary, "arguments": call.arguments_for_display, "status": "pending",
        }

    async def wait(self, request_id):
        future = self.futures[request_id]
        try:
            return await asyncio.wait_for(asyncio.shield(future), self.user_answer_after)
        except asyncio.TimeoutError:
            self.resolve(request_id, self.user_answer, decided_by="user")
            return await future

    def resolve(self, request_id, action, decided_by="user"):
        future = self.futures.get(request_id)
        if future is None or future.done():
            return False
        future.set_result(action)
        self.resolved.append((request_id, action, decided_by))
        return True


class ScriptedDecider:
    scores: Optional[Dict[str, float]] = None
    error: Optional[str] = None
    calls = []

    def __init__(self, base_url, timeout):
        pass

    async def score(self, state, questions):
        ScriptedDecider.calls.append(state)
        if ScriptedDecider.error:
            raise DeciderError(ScriptedDecider.error)
        return dict(ScriptedDecider.scores or {key: 0.01 for key in questions})


WRITE_CALL = {"id": "c1", "name": "shared_folder_write", "arguments": "{\"path\": \"a.txt\", \"content\": \"hello\"}"}


class ChatGateTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.storage = StorageService(root / "data")
        self.workspace_root = root / "workspace"
        self.workspace_root.mkdir()
        self.storage.save_settings({"auto_approval": "on", "decider_url": "http://decider.test:8731", "decider_threshold": 0.2})
        self.gate = ApprovalGate(storage=self.storage, workspace=WorkspaceService(self.workspace_root), client_factory=ScriptedDecider)
        self.broker = ReleasingBroker()
        self.gateway = ActionGateway(workspace=WorkspaceService(self.workspace_root), approvals=self.broker, audit=self.storage)
        self.config = LLMConfig(provider="openai_compatible", api_key="", base_url="http://x/v1", reasoning_effort="", default_model="m")
        ScriptedDecider.scores = None
        ScriptedDecider.error = None
        ScriptedDecider.calls = []
        self.bot = self.storage.get_bots()[0]
        self.storage.add_message({"id": "m1", "thread_id": self.bot["id"], "bot_id": self.bot["id"], "sender": "user", "text": "save hello to a.txt"})

    def tearDown(self):
        try:
            self.temp_dir.cleanup()
        except PermissionError:
            pass  # Windows keeps the SQLite file open until the process exits.

    def _run(self, calls=(WRITE_CALL,)):
        recorder: Dict[str, Any] = {}
        stream = _scripted_stream(recorder, [
            [{"type": "tool_calls", "calls": list(calls)}, {"type": "turn.completed", "ok": True}],
            [{"type": "content.delta", "delta": "Done."}, {"type": "turn.completed", "ok": True}],
        ])
        patches = [
            mock.patch.object(chat_router, "storage_service", self.storage),
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
        return events

    def _set_bot_mode(self, mode):
        bots = self.storage.get_bots()
        bots[0]["auto_approval"] = mode
        self.storage.save_bots(bots)

    def test_low_risk_action_is_approved_by_the_decider_and_runs(self):
        ScriptedDecider.scores = {key: 0.03 for key in QUESTIONS}
        events = self._run()
        types = [e["type"] for e in events]

        self.assertIn("request.opened", types)            # the card still opens
        scored = next(e for e in events if e["type"] == "gate.scored")
        self.assertEqual(scored["verdict"]["outcome"], "auto_approved")
        self.assertEqual(scored["verdict"]["mode"], "on")
        self.assertIn("tool.completed", types)
        self.assertEqual(self.broker.resolved, [(scored["requestId"], "allow", "decider")])
        self.assertEqual((self.workspace_root / "a.txt").read_text(encoding="utf-8"), "hello")

        # The model saw the user's request and the action, never the content.
        state = ScriptedDecider.calls[0]
        self.assertEqual(state["user_request"], "save hello to a.txt")
        self.assertEqual(state["tool"], "workspace.write")
        self.assertEqual(state["arguments"], {"path": "a.txt", "bytes": 5})
        self.assertFalse(state["file_exists"])
        self.assertNotIn("hello", str(state["arguments"]))

        row = self.storage.get_gate_decisions()[0]
        self.assertEqual((row["outcome"], row["final_decision"], row["decided_by"]), ("auto_approved", "allow", "decider"))
        saved = self.storage.get_messages(self.bot["id"])[-1]
        self.assertEqual(saved["raw_payload"]["tool_calls"][0]["gate"]["outcome"], "auto_approved")

    def test_risky_action_waits_for_the_user(self):
        ScriptedDecider.scores = {**{key: 0.02 for key in QUESTIONS}, "destroy": 0.8}
        events = self._run()
        scored = next(e for e in events if e["type"] == "gate.scored")
        self.assertEqual(scored["verdict"]["outcome"], "ask")
        self.assertIn("destroy", scored["verdict"]["reason"])
        self.assertEqual(self.broker.resolved[0][2], "user")
        row = self.storage.get_gate_decisions()[0]
        self.assertEqual((row["outcome"], row["final_decision"], row["decided_by"]), ("ask", "allow", "user"))

    def test_user_denial_is_logged_against_the_gate_verdict(self):
        self.broker.user_answer = "deny"
        ScriptedDecider.scores = {**{key: 0.02 for key in QUESTIONS}, "exfiltrate": 0.9}
        events = self._run()
        self.assertIn("tool.denied", [e["type"] for e in events])
        self.assertFalse((self.workspace_root / "a.txt").exists())
        row = self.storage.get_gate_decisions()[0]
        self.assertEqual((row["final_decision"], row["decided_by"]), ("deny", "user"))

    def test_shadow_mode_scores_but_never_approves(self):
        self._set_bot_mode("shadow")
        ScriptedDecider.scores = {key: 0.01 for key in QUESTIONS}
        events = self._run()
        scored = next(e for e in events if e["type"] == "gate.scored")
        self.assertEqual(scored["verdict"]["mode"], "shadow")
        self.assertEqual(scored["verdict"]["outcome"], "ask")
        self.assertEqual(self.broker.resolved[0][2], "user")
        self.assertEqual(self.storage.get_gate_decisions()[0]["decided_by"], "user")

    def test_bot_override_off_skips_the_decider_entirely(self):
        self._set_bot_mode("off")
        events = self._run()
        self.assertNotIn("gate.scored", [e["type"] for e in events])
        self.assertEqual(ScriptedDecider.calls, [])
        self.assertEqual(self.storage.get_gate_decisions(), [])
        self.assertEqual(self.broker.resolved[0][2], "user")

    def test_decider_outage_falls_back_to_the_user(self):
        ScriptedDecider.error = "decision server unreachable: ConnectError"
        events = self._run()
        scored = next(e for e in events if e["type"] == "gate.scored")
        self.assertEqual(scored["verdict"]["outcome"], "error")
        self.assertEqual(self.broker.resolved[0][2], "user")
        self.assertIn("tool.completed", [e["type"] for e in events])
        self.assertEqual(self.storage.get_gate_decisions()[0]["outcome"], "error")

    def test_actions_that_need_no_approval_are_not_scored(self):
        # share_file registers itself on the app-wide gateway; give this test's
        # gateway the same definition with a stand-in executor.
        from app.services.action_gateway import ActionDefinition
        self.gateway.register_action(
            ActionDefinition(name="files.share", tool="files", action="share", intent="share", risk="read", requires_approval=False),
            lambda invocation: {"attachment": {"source": "workspace", "path": "a.txt"}},
        )
        share = {"id": "c2", "name": "share_file", "arguments": "{\"source\": \"workspace\", \"path\": \"a.txt\"}"}
        events = self._run(calls=(share,))
        self.assertIn("tool.completed", [e["type"] for e in events])
        self.assertNotIn("request.opened", [e["type"] for e in events])
        self.assertNotIn("gate.scored", [e["type"] for e in events])
        self.assertEqual(ScriptedDecider.calls, [])
        self.assertEqual(self.storage.get_gate_decisions(), [])


if __name__ == "__main__":
    unittest.main()
