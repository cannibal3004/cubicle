"""The orchestrator bot: seeding, what it sees, and delegating through the chat router."""

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.routers import bots as bots_router
from app.routers import chat as chat_router
from app.services import orchestrator
from app.services import storage_service as storage_module
from app.services.action_gateway import ActionGateway, ActionInvocation
from app.services.context_builder import DELEGATED_LEAD, build_history_messages
from app.services.llm_config import LLMConfig
from app.services.llm_tools import ToolCallError, available_tools, build_invocation, describe_tools
from app.services.orchestrator import OrchestratorError, delegated_message, ensure_orchestrator, is_orchestrator
from app.services.storage_service import StorageService
from app.services.turn_manager import TurnManager
from app.services.workspace_service import WorkspaceService

from tests.test_chat_router import NoComposio, _fake_context_window, _scripted_stream


def _call(action, me, **arguments):
    return ActionInvocation(name=action, arguments=arguments, target={"bot_id": me}, preview="")


class OrchestratorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.storage = StorageService(Path(self.temp.name))
        self.patch = mock.patch.object(storage_module, "storage_service", self.storage)
        self.patch.start()
        # A fresh store already holds Cubie; ensure_orchestrator only fills a gap.
        self.cubie = ensure_orchestrator(self.storage) or next(b for b in self.storage.get_bots() if is_orchestrator(b))
        self.worker = {"id": "bot-worker", "name": "Worker", "role": "Helper", "system_prompt": "You help.", "model": "m", "tools": []}
        self.storage.save_bots(self.storage.get_bots() + [self.worker])

    def tearDown(self):
        self.patch.stop()
        try:
            self.temp.cleanup()
        except PermissionError:
            pass

    def test_every_account_gets_one_orchestrator_first_in_the_list(self):
        self.assertEqual((self.cubie["name"], self.cubie["orchestrator"], self.cubie["pinned"]), ("Cubie", True, True))
        self.assertEqual(self.storage.get_bots()[0]["id"], self.cubie["id"])
        self.assertIsNone(ensure_orchestrator(self.storage))  # idempotent
        # Delete it and the next listing brings one back.
        self.storage.save_bots([b for b in self.storage.get_bots() if not is_orchestrator(b)])
        self.assertEqual(ensure_orchestrator(self.storage)["name"], "Cubie")
        listed = asyncio.run(bots_router.get_bots())
        self.assertEqual(sum(1 for b in listed if b.get("orchestrator")), 1)

    def test_only_orchestrators_get_the_tools_and_the_prompt_says_how(self):
        plain = [s.name for s in available_tools(computer=False)]
        self.assertNotIn("bots_send", plain)
        mine = available_tools(computer=False, orchestrator=True)
        self.assertEqual([s.name for s in mine if s.group == "orchestration"], ["bots_list", "bots_status", "bots_create", "bots_send", "bots_reply"])
        self.assertEqual([s.needs_approval for s in mine if s.group == "orchestration"], [False, False, True, True, False])
        self.assertIn("you are an orchestrator", describe_tools(mine))

    def test_invocations_are_validated(self):
        send = build_invocation("bots_send", {"bot_id": self.worker["id"], "message": "Do the thing", "wait_seconds": 5000}, self.cubie["id"])
        self.assertEqual(send.name, "bots.send")
        self.assertEqual(send.arguments["wait_seconds"], 1800.0)  # capped
        self.assertEqual(send.target, {"bot_id": self.cubie["id"], "to": self.worker["id"]})
        with self.assertRaises(ToolCallError):
            build_invocation("bots_send", {"bot_id": self.worker["id"], "message": "x", "wait_seconds": "soon"}, self.cubie["id"])
        with self.assertRaises(ToolCallError):
            build_invocation("bots_create", {"name": "New", "role": "x"}, self.cubie["id"])  # no prompt
        create = build_invocation("bots_create", {"name": "Scout", "role": "Researcher", "system_prompt": "Find things."}, self.cubie["id"])
        self.assertEqual(create.preview, "Create a bot named Scout (Researcher)")

    def test_list_status_create_and_who_may_call(self):
        async def scenario():
            # A plain bot cannot use these even if the model tries.
            with self.assertRaises(OrchestratorError):
                await orchestrator.execute_list(_call("bots.list", self.worker["id"]))
            listed = await orchestrator.execute_list(_call("bots.list", self.cubie["id"]))
            self.assertNotIn(self.cubie["id"], [b["id"] for b in listed["bots"]])
            self.assertEqual(listed["bots"][0]["state"], "idle")
            self.storage.add_message({"id": "m1", "thread_id": self.worker["id"], "bot_id": self.worker["id"], "sender": "user", "text": "hello"})
            status = await orchestrator.execute_status(_call("bots.status", self.cubie["id"], bot_id=self.worker["id"]))
            self.assertEqual(status["name"], self.worker["name"])
            self.assertEqual(status["recent"][-1]["text"], "hello")
            self.assertEqual(status["todo"], [])
            with self.assertRaisesRegex(OrchestratorError, "no bot"):
                await orchestrator.execute_status(_call("bots.status", self.cubie["id"], bot_id="bot-nope"))
            with self.assertRaisesRegex(OrchestratorError, "That is you"):
                await orchestrator.execute_status(_call("bots.status", self.cubie["id"], bot_id=self.cubie["id"]))
            with mock.patch("app.services.llm_service.current_llm_config", return_value=mock.Mock(default_model="default-model")):
                created = await orchestrator.execute_create(_call("bots.create", self.cubie["id"], name="Scout", role="Researcher", system_prompt="Find things."))
            self.assertEqual((created["bot"]["name"], created["bot"]["role"], created["bot"]["model"]), ("Scout", "Researcher", "default-model"))
            self.assertIn("Scout", [b["name"] for b in self.storage.get_bots()])
            with self.assertRaisesRegex(OrchestratorError, "already exists"):
                await orchestrator.execute_create(_call("bots.create", self.cubie["id"], name="scout", role="x", system_prompt="y"))

        asyncio.run(scenario())

    def test_delegated_messages_tell_the_bot_who_asked(self):
        message = delegated_message(self.worker["id"], "Check the logs.", self.cubie)
        self.assertEqual(message["raw_payload"]["delegated"]["from_name"], "Cubie")
        content = build_history_messages([message], replay_tools=True)[0]["content"]
        self.assertTrue(content.startswith(DELEGATED_LEAD.format(name="Cubie")))
        self.assertTrue(content.endswith("Check the logs."))


class DelegationTests(unittest.TestCase):
    """bots_send through the real chat router: the worker's turn runs and the reply comes back."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.storage = StorageService(Path(self.temp.name))
        self.config = LLMConfig(provider="openai_compatible", api_key="", base_url="http://localhost:11434/v1", reasoning_effort="", default_model="m")
        # A fresh store already holds Cubie; ensure_orchestrator only fills a gap.
        self.cubie = ensure_orchestrator(self.storage) or next(b for b in self.storage.get_bots() if is_orchestrator(b))
        self.worker = {"id": "bot-worker", "name": "Worker", "role": "Helper", "system_prompt": "You help.", "model": "m", "tools": []}
        self.storage.save_bots(self.storage.get_bots() + [self.worker])
        workspace = Path(self.temp.name) / "workspace"
        workspace.mkdir()
        self.manager = TurnManager()
        self.patches = [
            mock.patch.object(storage_module, "storage_service", self.storage),
            mock.patch.object(chat_router.llm_service, "current_llm_config", return_value=self.config),
            mock.patch.object(chat_router, "composio_service", NoComposio()),
            mock.patch.object(chat_router, "turn_manager", self.manager),
            mock.patch.object(chat_router, "_context_window_for", _fake_context_window),
            mock.patch.object(chat_router, "action_gateway", ActionGateway(workspace=WorkspaceService(workspace), approvals=mock.Mock(), audit=self.storage)),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        try:
            self.temp.cleanup()
        except PermissionError:
            pass

    def test_send_waits_for_the_reply_and_records_the_exchange(self):
        recorder = {}
        stream = _scripted_stream(recorder, [[{"type": "content.delta", "delta": "Logs look clean."}, {"type": "turn.completed", "ok": True}]])
        with mock.patch.object(chat_router.llm_service, "stream_chat_completion", stream):
            result = asyncio.run(orchestrator.execute_send(_call("bots.send", self.cubie["id"], bot_id=self.worker["id"], message="Check the logs.", wait_seconds=10)))
        self.assertEqual((result["status"], result["reply"], result["name"]), ("completed", "Logs look clean.", self.worker["name"]))
        # The worker saw the delegation lead, and its chat holds both sides.
        first = recorder["rounds"][0]["messages"][-1]["content"]
        self.assertTrue(first.startswith(DELEGATED_LEAD.format(name="Cubie")))
        # The task went to the worker's "From Cubie" conversation, not its first one.
        task_thread = orchestrator.task_thread(self.cubie, self.worker, self.storage)
        self.assertEqual(self.storage.get_conversation(task_thread)["title"], f"From {self.cubie['name']}")
        texts = [(m["sender"], m["text"]) for m in self.storage.get_messages(task_thread)][-2:]
        self.assertEqual(texts, [("user", "Check the logs."), ("bot", "Logs look clean.")])
        self.assertEqual(self.storage.get_messages(task_thread)[-2]["raw_payload"]["delegated"]["from"], self.cubie["id"])
        self.assertFalse(any(m.get("sender") == "user" for m in self.storage.get_messages(self.worker["id"])))
        # bots_reply finds the same reply by message id, and the latest one without it.
        by_id = asyncio.run(orchestrator.execute_reply(_call("bots.reply", self.cubie["id"], bot_id=self.worker["id"], message_id=result["message_id"])))
        self.assertEqual((by_id["reply"], by_id["state"]), ("Logs look clean.", "idle"))
        latest = asyncio.run(orchestrator.execute_reply(_call("bots.reply", self.cubie["id"], bot_id=self.worker["id"])))
        self.assertEqual(latest["reply"], "Logs look clean.")

    def test_send_returns_early_when_the_bot_waits_for_the_user_or_is_busy(self):
        gate = asyncio.Event()

        async def slow_stream(model, messages, system_prompt="", config=None, tools=None):
            yield {"type": "content.delta", "delta": "Working..."}
            await gate.wait()
            yield {"type": "turn.completed", "ok": True}

        async def scenario():
            with mock.patch.object(chat_router.llm_service, "stream_chat_completion", slow_stream):
                started = await orchestrator.execute_send(_call("bots.send", self.cubie["id"], bot_id=self.worker["id"], message="Long job.", wait_seconds=0))
                self.assertEqual(started["status"], "started")
                turn = self.manager.get(started["turn_id"])
                # While it runs, another send is refused as busy; a pending card ends the wait early.
                busy = await orchestrator.execute_send(_call("bots.send", self.cubie["id"], bot_id=self.worker["id"], message="Another.", wait_seconds=0))
                self.assertEqual(busy["status"], "busy")
                turn.set_pending_approval({"request_id": "req-x", "kind": "question", "summary": "Which log?"})
                reply = await orchestrator.execute_reply(_call("bots.reply", self.cubie["id"], bot_id=self.worker["id"]))
                self.assertEqual((reply["state"], reply["waiting_on"], reply["about"]), ("waiting_for_user", "a question", "Which log?"))
                turn.set_pending_approval(None)
                gate.set()
                await asyncio.wait_for(turn.task, 2)
                # Orchestrators do not direct each other.
                with self.assertRaisesRegex(OrchestratorError, "do not direct each other"):
                    other = {**self.cubie, "id": "bot-other", "name": "Other"}
                    self.storage.save_bots(self.storage.get_bots() + [other])
                    await orchestrator.execute_send(_call("bots.send", self.cubie["id"], bot_id="bot-other", message="x"))

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
