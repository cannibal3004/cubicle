import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi import HTTPException

from app.routers import chat as chat_router
from app.routers import questions as questions_router
from app.schemas.contracts import QuestionAnswer
from app.services import user_questions
from app.services.action_gateway import ActionGateway
from app.services.llm_config import LLMConfig
from app.services.llm_tools import ToolCallError, build_invocation
from app.services.storage_service import StorageService
from app.services.turn_manager import Turn, TurnManager
from app.services.user_questions import QuestionBroker, QuestionError, execute_ask, register_question_actions
from app.services.workspace_service import WorkspaceService
from app.services import storage_service as storage_module

from tests.test_chat_router import NoComposio, _fake_context_window, _scripted_stream


class AskUserInvocationTests(unittest.TestCase):
    def test_question_with_options(self):
        call = build_invocation("ask_user", {"question": "Which region?", "options": ["North", "South"]}, "bot-1")
        self.assertEqual(call.name, "user.ask")
        self.assertEqual(call.arguments, {"bot_id": "bot-1", "question": "Which region?", "options": ["North", "South"], "allow_other": True})
        self.assertEqual(call.preview, "Ask the user: Which region?")

    def test_free_text_question_always_allows_typing(self):
        call = build_invocation("ask_user", {"question": "What is the budget?", "allow_other": False}, "bot-1")
        self.assertEqual(call.arguments["options"], [])
        self.assertTrue(call.arguments["allow_other"])

    def test_bad_arguments(self):
        with self.assertRaises(ToolCallError):
            build_invocation("ask_user", {"question": ""}, "bot-1")
        with self.assertRaises(ToolCallError):
            build_invocation("ask_user", {"question": "x", "options": ["only one"]}, "bot-1")
        with self.assertRaises(ToolCallError):
            build_invocation("ask_user", {"question": "x", "options": [str(i) for i in range(7)]}, "bot-1")
        with self.assertRaises(ToolCallError):
            build_invocation("ask_user", {"question": "x", "options": ["a", ""]}, "bot-1")
        with self.assertRaises(ToolCallError):
            build_invocation("ask_user", {"question": "x", "allow_other": "yes"}, "bot-1")


class AskUserExecutorTests(unittest.TestCase):
    """The executor against a bare Turn: events, the waiting flag, the result."""

    def setUp(self):
        self.manager = TurnManager()
        self.broker = QuestionBroker()
        self.patches = [
            mock.patch.object(user_questions, "turn_manager", self.manager),
            mock.patch.object(user_questions, "question_broker", self.broker),
            mock.patch.object(user_questions.settings, "QUESTION_TIMEOUT_SECONDS", 1),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()

    def _turn(self):
        async def runner(turn):
            await asyncio.Event().wait()

        async def make():
            return self.manager.start("bot-1", "bot-1", "m", runner)

        return make

    def _call(self, **arguments):
        from app.services.action_gateway import ActionInvocation

        return ActionInvocation(name="user.ask", arguments={"bot_id": "bot-1", **arguments}, target={"bot_id": "bot-1"}, preview="ask")

    def test_answer_by_option_and_by_typing(self):
        async def scenario():
            turn = await self._turn()()
            task = asyncio.create_task(execute_ask(self._call(question="Which?", options=["A", "B"])))
            await asyncio.sleep(0.01)
            opened = turn.events[-1]
            self.assertEqual((opened["type"], opened["requestType"], opened["options"], opened["allowOther"]), ("request.opened", "question", ["A", "B"], True))
            self.assertEqual(turn.pending_approval["kind"], "question")
            self.assertEqual(turn.pending_approval["summary"], "Which?")
            # The router answers it; an unknown id is refused.
            with self.assertRaises(HTTPException):
                await questions_router.answer_question(QuestionAnswer(request_id="q-nope", answer="x"))
            with self.assertRaises(HTTPException):
                await questions_router.answer_question(QuestionAnswer(request_id=opened["requestId"], answer=""))
            await questions_router.answer_question(QuestionAnswer(request_id=opened["requestId"], answer="B", option_index=1))
            result = await task
            self.assertEqual(result, {"answered": True, "answer": "B", "selected_option": "B", "option_index": 1})
            self.assertIsNone(turn.pending_approval)
            self.assertEqual(turn.events[-1]["type"], "request.answered")
            self.assertEqual(turn.events[-1]["optionIndex"], 1)

            task = asyncio.create_task(execute_ask(self._call(question="Budget?")))
            await asyncio.sleep(0.01)
            request_id = turn.events[-1]["requestId"]
            await questions_router.answer_question(QuestionAnswer(request_id=request_id, answer="  about 5k  "))
            self.assertEqual(await task, {"answered": True, "answer": "about 5k", "typed": True})
            turn.task.cancel()

        asyncio.run(scenario())

    def test_timeout_tells_the_model_to_carry_on(self):
        async def scenario():
            turn = await self._turn()()
            with mock.patch.object(user_questions.settings, "QUESTION_TIMEOUT_SECONDS", 0.05):
                result = await execute_ask(self._call(question="Which?", options=["A", "B"]))
            self.assertFalse(result["answered"])
            self.assertIn("did not answer", result["note"])
            self.assertEqual(turn.events[-1]["type"], "request.expired")
            self.assertIsNone(turn.pending_approval)
            turn.task.cancel()

        asyncio.run(scenario())

    def test_needs_a_running_turn_and_sane_options(self):
        async def scenario():
            with self.assertRaises(QuestionError):
                await execute_ask(self._call(question="Which?"))
            turn = await self._turn()()
            with self.assertRaises(QuestionError):
                await execute_ask(self._call(question="Which?", options=["only"]))
            with self.assertRaises(QuestionError):
                await execute_ask(self._call(question="Which?", options="not a list"))
            turn.task.cancel()

        asyncio.run(scenario())


class AskUserTurnTests(unittest.TestCase):
    """Through the chat router: the call pauses the turn, the answer reaches the model."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.storage = StorageService(Path(self.temp_dir.name))
        self.config = LLMConfig(provider="openai_compatible", api_key="", base_url="http://localhost:11434/v1", reasoning_effort="", default_model="m")

    def tearDown(self):
        try:
            self.temp_dir.cleanup()
        except PermissionError:
            pass

    def test_question_pauses_the_turn_and_the_answer_is_the_tool_result(self):
        bot = self.storage.get_bots()[0]
        self.storage.add_message({"id": "m1", "thread_id": bot["id"], "bot_id": bot["id"], "sender": "user", "text": "Deploy it"})
        manager = TurnManager()
        broker = QuestionBroker()
        workspace = Path(self.temp_dir.name) / "workspace"
        workspace.mkdir()
        gateway = ActionGateway(workspace=WorkspaceService(workspace), approvals=mock.Mock(), audit=self.storage)
        register_question_actions(gateway)
        recorder = {}
        stream = _scripted_stream(
            recorder,
            [
                [
                    {"type": "content.delta", "delta": "One question first."},
                    {"type": "tool_calls", "calls": [{"id": "call_q", "name": "ask_user", "arguments": json.dumps({"question": "Which environment?", "options": ["staging", "production"]})}]},
                    {"type": "turn.completed", "ok": True},
                ],
                [
                    {"type": "content.delta", "delta": "Deploying to staging."},
                    {"type": "turn.completed", "ok": True},
                ],
            ],
        )
        patches = [
            mock.patch.object(storage_module, "storage_service", self.storage),
            mock.patch.object(chat_router.llm_service, "current_llm_config", return_value=self.config),
            mock.patch.object(chat_router.llm_service, "stream_chat_completion", stream),
            mock.patch.object(chat_router, "composio_service", NoComposio()),
            mock.patch.object(chat_router, "turn_manager", manager),
            mock.patch.object(chat_router, "_context_window_for", _fake_context_window),
            mock.patch.object(chat_router, "action_gateway", gateway),
            mock.patch.object(user_questions, "turn_manager", manager),
            mock.patch.object(user_questions, "question_broker", broker),
        ]
        for p in patches:
            p.start()
        try:
            async def scenario():
                started = await chat_router.start_turn(bot["id"], chat_router.StartTurnRequest())
                turn = manager.get(started["turn"]["turn_id"])
                for _ in range(100):
                    await asyncio.sleep(0.01)
                    if turn.pending_approval:
                        break
                self.assertEqual(turn.pending_approval["kind"], "question")
                status = await chat_router.turn_status(bot["id"])
                self.assertEqual(status["turn"]["pending_approval"]["summary"], "Which environment?")
                opened = next(e for e in turn.events if e["type"] == "request.opened")
                self.assertEqual(opened["requestType"], "question")
                await questions_router.answer_question(QuestionAnswer(request_id=opened["requestId"], answer="staging", option_index=0))
                await asyncio.wait_for(turn.task, 2)
                types = [e["type"] for e in turn.events]
                self.assertIn("request.answered", types)
                self.assertEqual(types[-1], "turn.completed")

            asyncio.run(scenario())
        finally:
            for p in reversed(patches):
                p.stop()

        # The model got the answer as the tool result of its own call.
        second = recorder["rounds"][1]["messages"]
        self.assertEqual(second[-1]["role"], "tool")
        self.assertEqual(second[-1]["tool_call_id"], "call_q")
        self.assertEqual(json.loads(second[-1]["content"])["selected_option"], "staging")
        saved = self.storage.get_messages(bot["id"])[-1]
        record = saved["raw_payload"]["tool_calls"][0]
        self.assertEqual((record["name"], record["status"]), ("ask_user", "completed"))
        self.assertIn("staging", record["result"])
        self.assertEqual(saved["text"], "One question first.\n\nDeploying to staging.")


if __name__ == "__main__":
    unittest.main()
