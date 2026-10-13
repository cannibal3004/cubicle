import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from app.routers import chat as chat_router
from app.services.turn_manager import TurnManager
from app.services.action_gateway import ActionGateway
from app.services.llm_config import LLMConfig
from app.services.storage_service import StorageService
from app.services.workspace_service import WorkspaceService


class NoComposio:
    def get_api_key(self):
        return ""


class DecidingApprovalBroker:
    """Approval broker stand-in that answers every request the same way."""

    def __init__(self, decision="allow"):
        self.decision = decision
        self.opened = []

    def open(self, thread_id, bot_id, call, request_id=None):
        self.opened.append(request_id)
        return {
            "request_id": request_id,
            "thread_id": thread_id,
            "bot_id": bot_id,
            "tool": call.name,
            "summary": call.summary,
            "arguments": call.arguments_for_display,
            "status": "pending",
        }

    async def wait(self, request_id):
        return self.decision


def _scripted_stream(recorder, rounds):
    """Yield a different scripted event list on each call, recording inputs."""
    state = {"calls": 0}

    async def stream_chat_completion(model, messages, system_prompt="", config=None, tools=None):
        index = min(state["calls"], len(rounds) - 1)
        state["calls"] += 1
        recorder.setdefault("rounds", []).append(
            {"model": model, "messages": [dict(m) for m in messages], "system_prompt": system_prompt, "tools": tools}
        )
        for event in rounds[index]:
            yield event

    return stream_chat_completion


def _fake_stream(recorder):
    async def stream_chat_completion(model, messages, system_prompt="", config=None, tools=None):
        recorder["model"] = model
        recorder["messages"] = messages
        recorder["system_prompt"] = system_prompt
        recorder["tools"] = tools
        yield {"type": "reasoning.delta", "delta": "Considering "}
        yield {"type": "reasoning.delta", "delta": "the question."}
        yield {"type": "content.delta", "delta": "Hello "}
        yield {"type": "content.delta", "delta": "there"}
        yield {"type": "turn.completed", "ok": True}

    return stream_chat_completion


async def _collect_sse(response):
    # sse-starlette keeps the raw generator as `body_iterator` and encodes each
    # {"event", "data"} dict only when the response is actually sent.
    events = []
    async for chunk in response.body_iterator:
        if isinstance(chunk, dict):
            events.append(json.loads(chunk["data"]))
            continue
        text = chunk.decode("utf-8") if isinstance(chunk, bytes) else str(chunk)
        for line in text.splitlines():
            if line.startswith("data:"):
                events.append(json.loads(line[len("data:"):].strip()))
    return events


async def _fake_context_window(config, model):
    return 32000


def _fake_request(last_event_id=None):
    headers = {"last-event-id": str(last_event_id)} if last_event_id is not None else {}
    return SimpleNamespace(headers=headers)


async def _start_and_stream(thread_id, model=None, after=0):
    """Start a server-side turn, then follow it to the end like the browser does."""
    started = await chat_router.start_turn(thread_id, chat_router.StartTurnRequest(model=model))
    response = await chat_router.stream_turn(thread_id, _fake_request(), turn=None, after=after)
    events = await _collect_sse(response)
    return started, events


class ChatStreamRouteTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.storage = StorageService(Path(self.temp_dir.name))
        self.config = LLMConfig(
            provider="openai_compatible",
            api_key="",
            base_url="http://localhost:11434/v1",
            reasoning_effort="",
            default_model="default-model",
        )

    def tearDown(self):
        try:
            self.temp_dir.cleanup()
        except PermissionError:
            # Windows keeps the SQLite file open briefly after the last connection.
            pass

    def _patches(self, recorder, stream=None, gateway=None, config=None, manager=None):
        patches = [
            mock.patch.object(chat_router, "storage_service", self.storage),
            mock.patch.object(chat_router.llm_service, "current_llm_config", return_value=config or self.config),
            mock.patch.object(chat_router.llm_service, "stream_chat_completion", stream or _fake_stream(recorder)),
            mock.patch.object(chat_router, "composio_service", NoComposio()),
            mock.patch.object(chat_router, "turn_manager", manager or TurnManager()),
            mock.patch.object(chat_router, "_context_window_for", _fake_context_window),
        ]
        if gateway is not None:
            patches.append(mock.patch.object(chat_router, "action_gateway", gateway))
        return patches

    def _run_turn(self, thread_id, model=None, stream=None, gateway=None, config=None):
        recorder = {}
        patches = self._patches(recorder, stream=stream, gateway=gateway, config=config)
        for p in patches:
            p.start()
        try:
            _, events = asyncio.run(_start_and_stream(thread_id, model=model))
        finally:
            for p in reversed(patches):
                p.stop()
        return recorder, events

    def _gateway(self, decision="allow"):
        workspace_root = Path(self.temp_dir.name) / "workspace"
        workspace_root.mkdir(exist_ok=True)
        (workspace_root / "hello.txt").write_text("hello", encoding="utf-8")
        broker = DecidingApprovalBroker(decision)
        return ActionGateway(workspace=WorkspaceService(workspace_root), approvals=broker, audit=self.storage), broker

    def test_plain_turn_streams_deltas_and_persists_the_reply(self):
        bot = self.storage.get_bots()[0]
        self.storage.add_message(
            {"id": "m1", "thread_id": bot["id"], "bot_id": bot["id"], "sender": "user", "text": "hi"}
        )

        recorder, events = self._run_turn(bot["id"])

        self.assertEqual(events[0]["type"], "turn.started")
        self.assertEqual(events[0]["model"], bot["model"])
        self.assertEqual([e["delta"] for e in events if e["type"] == "content.delta"], ["Hello ", "there"])
        self.assertEqual(
            [e["delta"] for e in events if e["type"] == "reasoning.delta"],
            ["Considering ", "the question."],
        )
        self.assertEqual(events[-1]["type"], "turn.completed")

        self.assertEqual(recorder["model"], bot["model"])
        self.assertIn(bot["system_prompt"], recorder["system_prompt"])
        self.assertIn("## Tools", recorder["system_prompt"])
        self.assertEqual([t["function"]["name"] for t in recorder["tools"]], ["workspace_list", "workspace_read", "workspace_write", "share_file", "memory_read", "memory_write"])
        self.assertEqual(recorder["messages"][-1], {"role": "user", "content": "hi", "image_url": None})

        saved = self.storage.get_messages(bot["id"])
        self.assertEqual(saved[-1]["sender"], "bot")
        self.assertEqual(saved[-1]["text"], "Hello there")
        # Thinking is kept for display but stays out of `text`, which is what
        # gets replayed to the model on the next turn.
        self.assertEqual(saved[-1]["raw_payload"], {"reasoning": "Considering the question."})
        self.assertNotIn("Considering", saved[-1]["text"])

    def test_model_falls_back_to_bot_then_configured_default(self):
        self.storage.add_message(
            {"id": "m1", "thread_id": "no-such-bot", "bot_id": "no-such-bot", "sender": "user", "text": "hi"}
        )
        recorder, events = self._run_turn("no-such-bot")
        self.assertEqual(recorder["model"], "default-model")
        self.assertEqual(events[0]["model"], "default-model")

        recorder, _ = self._run_turn("no-such-bot", model="override-model")
        self.assertEqual(recorder["model"], "override-model")

    def test_model_tool_call_runs_through_the_gateway_and_feeds_back(self):
        bot = self.storage.get_bots()[0]
        self.storage.add_message(
            {"id": "m1", "thread_id": bot["id"], "bot_id": bot["id"], "sender": "user", "text": "what is in the workspace?"}
        )
        gateway, broker = self._gateway("allow")
        recorder = {}
        stream = _scripted_stream(
            recorder,
            [
                [
                    {"type": "content.delta", "delta": "Let me look."},
                    {"type": "tool_calls", "calls": [{"id": "call_1", "name": "workspace_list", "arguments": "{}"}]},
                    {"type": "turn.completed", "ok": True},
                ],
                [
                    {"type": "content.delta", "delta": "There is hello.txt."},
                    {"type": "turn.completed", "ok": True},
                ],
            ],
        )

        _, events = self._run_turn(bot["id"], stream=stream, gateway=gateway)

        types = [e["type"] for e in events]
        self.assertEqual(types.count("request.opened"), 1)
        self.assertIn("tool.started", types)
        completed = next(e for e in events if e["type"] == "tool.completed")
        self.assertEqual(completed["callName"], "workspace_list")
        self.assertEqual(completed["tool"], "workspace.list")
        self.assertEqual([entry["name"] for entry in completed["result"]["entries"]], ["hello.txt"])
        self.assertEqual(len(broker.opened), 1)

        self.assertEqual(len(recorder["rounds"]), 2)
        second = recorder["rounds"][1]["messages"]
        self.assertEqual(second[-2]["role"], "assistant")
        self.assertEqual(second[-2]["tool_calls"][0]["name"], "workspace_list")
        self.assertEqual(second[-1]["role"], "tool")
        self.assertEqual(second[-1]["tool_call_id"], "call_1")
        self.assertIn("hello.txt", second[-1]["content"])

        saved = self.storage.get_messages(bot["id"])[-1]
        self.assertEqual(saved["text"], "Let me look.\n\nThere is hello.txt.")
        self.assertEqual(saved["raw_payload"]["tool_calls"][0]["status"], "completed")
        self.assertEqual(saved["raw_payload"]["tool_calls"][0]["name"], "workspace_list")

    def test_denied_tool_call_is_reported_to_the_model_and_not_executed(self):
        bot = self.storage.get_bots()[0]
        self.storage.add_message(
            {"id": "m1", "thread_id": bot["id"], "bot_id": bot["id"], "sender": "user", "text": "write a file"}
        )
        gateway, _ = self._gateway("deny")
        recorder = {}
        stream = _scripted_stream(
            recorder,
            [
                [
                    {"type": "tool_calls", "calls": [{"id": "call_1", "name": "workspace_write", "arguments": "{\"path\": \"new.txt\", \"content\": \"x\"}"}]},
                    {"type": "turn.completed", "ok": True},
                ],
                [{"type": "content.delta", "delta": "Understood, I will not write it."}, {"type": "turn.completed", "ok": True}],
            ],
        )

        _, events = self._run_turn(bot["id"], stream=stream, gateway=gateway)

        self.assertIn("tool.denied", [e["type"] for e in events])
        self.assertNotIn("tool.started", [e["type"] for e in events])
        self.assertFalse((Path(self.temp_dir.name) / "workspace" / "new.txt").exists())
        tool_message = recorder["rounds"][1]["messages"][-1]
        self.assertEqual(tool_message["role"], "tool")
        self.assertIn("denied", tool_message["content"])
        saved = self.storage.get_messages(bot["id"])[-1]
        self.assertEqual(saved["raw_payload"]["tool_calls"][0]["status"], "denied")
        self.assertEqual(saved["text"], "Understood, I will not write it.")

    def test_invalid_tool_arguments_never_reach_the_gateway(self):
        bot = self.storage.get_bots()[0]
        self.storage.add_message(
            {"id": "m1", "thread_id": bot["id"], "bot_id": bot["id"], "sender": "user", "text": "read it"}
        )
        gateway, broker = self._gateway("allow")
        recorder = {}
        stream = _scripted_stream(
            recorder,
            [
                [
                    {"type": "tool_calls", "calls": [{"id": "call_1", "name": "workspace_read", "arguments": "{}"}]},
                    {"type": "turn.completed", "ok": True},
                ],
                [{"type": "content.delta", "delta": "Which file?"}, {"type": "turn.completed", "ok": True}],
            ],
        )

        _, events = self._run_turn(bot["id"], stream=stream, gateway=gateway)

        failed = next(e for e in events if e["type"] == "tool.failed")
        self.assertIn("'path' is required", failed["error"])
        self.assertEqual(broker.opened, [])
        self.assertIn("'path' is required", recorder["rounds"][1]["messages"][-1]["content"])

    def test_tool_rounds_are_capped(self):
        bot = self.storage.get_bots()[0]
        self.storage.add_message(
            {"id": "m1", "thread_id": bot["id"], "bot_id": bot["id"], "sender": "user", "text": "loop"}
        )
        gateway, _ = self._gateway("allow")
        recorder = {}
        forever = [
            {"type": "tool_calls", "calls": [{"id": "c", "name": "workspace_list", "arguments": "{}"}]},
            {"type": "turn.completed", "ok": True},
        ]
        with mock.patch.object(chat_router.settings, "LLM_MAX_TOOL_ROUNDS", 2):
            _, events = self._run_turn(bot["id"], stream=_scripted_stream(recorder, [forever]), gateway=gateway)

        # Two tool rounds, then one forced text-only round without tools.
        self.assertEqual(len(recorder["rounds"]), 3)
        self.assertIsNotNone(recorder["rounds"][1]["tools"])
        self.assertIsNone(recorder["rounds"][2]["tools"])
        self.assertIn("tool budget for this turn is used up", recorder["rounds"][2]["system_prompt"])
        # The second round's calls were answered as skipped, never executed.
        last_tool_message = recorder["rounds"][2]["messages"][-1]
        self.assertEqual(last_tool_message["role"], "tool")
        self.assertIn("skipped", last_tool_message["content"])
        self.assertEqual([e["type"] for e in events].count("tool.started"), 1)
        # The scripted model still produced no text, so the fallback note is shown.
        self.assertIn("Stopped after 2 tool rounds", "".join(e.get("delta", "") for e in events if e["type"] == "content.delta"))
        saved = self.storage.get_messages(bot["id"])[-1]
        self.assertEqual([r["status"] for r in saved["raw_payload"]["tool_calls"]], ["completed", "skipped"])

    def test_rounds_that_only_repeat_earlier_calls_end_the_tool_loop(self):
        bot = self.storage.get_bots()[0]
        self.storage.add_message(
            {"id": "m1", "thread_id": bot["id"], "bot_id": bot["id"], "sender": "user", "text": "loop"}
        )
        gateway, _ = self._gateway("allow")
        recorder = {}
        same_call = [
            {"type": "tool_calls", "calls": [{"id": "c", "name": "workspace_list", "arguments": "{\"path\": \".\"}"}]},
            {"type": "turn.completed", "ok": True},
        ]
        patches = [
            mock.patch.object(chat_router.settings, "LLM_MAX_TOOL_ROUNDS", 50),
            mock.patch.object(chat_router.settings, "LLM_STALLED_ROUNDS", 2),
        ]
        for p in patches:
            p.start()
        try:
            _, events = self._run_turn(bot["id"], stream=_scripted_stream(recorder, [same_call]), gateway=gateway)
        finally:
            for p in reversed(patches):
                p.stop()

        # Round 1 is new; rounds 2 and 3 repeat it with the same listing; then the final call.
        self.assertEqual(len(recorder["rounds"]), 4)
        self.assertIsNotNone(recorder["rounds"][2]["tools"])
        self.assertIsNone(recorder["rounds"][3]["tools"])
        self.assertIn("not making progress", recorder["rounds"][3]["system_prompt"])
        self.assertEqual([e["type"] for e in events].count("tool.started"), 3)
        limit = next(e for e in events if e["type"] == "turn.limit")
        self.assertEqual((limit["reason"], limit["rounds"]), ("stalled", 3))
        self.assertIn("only repeated earlier calls", "".join(e.get("delta", "") for e in events if e["type"] == "content.delta"))

    def test_calls_whose_results_change_are_progress(self):
        bot = self.storage.get_bots()[0]
        self.storage.add_message(
            {"id": "m1", "thread_id": bot["id"], "bot_id": bot["id"], "sender": "user", "text": "loop"}
        )
        gateway, _ = self._gateway("allow")
        recorder = {}
        write = [
            {"type": "tool_calls", "calls": [{"id": "w", "name": "workspace_write", "arguments": "{\"path\": \"n.txt\", \"content\": \"1\"}"}]},
            {"type": "turn.completed", "ok": True},
        ]
        look = [
            {"type": "tool_calls", "calls": [{"id": "r", "name": "workspace_read", "arguments": "{\"path\": \"n.txt\"}"}]},
            {"type": "turn.completed", "ok": True},
        ]
        append = [
            {"type": "tool_calls", "calls": [{"id": "w2", "name": "workspace_write", "arguments": "{\"path\": \"n.txt\", \"content\": \"12\"}"}]},
            {"type": "turn.completed", "ok": True},
        ]
        done = [{"type": "content.delta", "delta": "Done."}, {"type": "turn.completed", "ok": True}]
        with mock.patch.object(chat_router.settings, "LLM_STALLED_ROUNDS", 1):
            _, events = self._run_turn(bot["id"], stream=_scripted_stream(recorder, [write, look, append, look, done]), gateway=gateway)
        # The second read returns different content, so nothing stalled.
        self.assertEqual(len(recorder["rounds"]), 5)
        self.assertNotIn("turn.limit", [e["type"] for e in events])
        self.assertEqual("".join(e.get("delta", "") for e in events if e["type"] == "content.delta").strip(), "Done.")

    def test_a_full_context_that_cannot_be_compacted_ends_the_tool_loop(self):
        bot = self.storage.get_bots()[0]
        # Only this turn's own user message: there is nothing older to fold.
        self.storage.add_message(
            {"id": "m1", "thread_id": bot["id"], "bot_id": bot["id"], "sender": "user", "text": "dig"}
        )
        gateway, _ = self._gateway("allow")
        recorder = {}
        stream = _scripted_stream(
            recorder,
            [
                [{"type": "tool_calls", "calls": [{"id": "c1", "name": "workspace_list", "arguments": "{}"}]},
                 {"type": "usage", "prompt_tokens": 29_500, "completion_tokens": 20},
                 {"type": "turn.completed", "ok": True}],
                [{"type": "content.delta", "delta": "Here is what I have."},
                 {"type": "usage", "prompt_tokens": 29_600, "completion_tokens": 5},
                 {"type": "turn.completed", "ok": True}],
            ],
        )
        _, events = self._run_turn(bot["id"], stream=stream, gateway=gateway)
        kinds = [e["type"] for e in events]
        self.assertIn("context.compacting", kinds)
        self.assertNotIn("context.compacted", kinds)
        limit = next(e for e in events if e["type"] == "turn.limit")
        self.assertEqual((limit["reason"], limit["rounds"]), ("context", 1))
        self.assertEqual(len(recorder["rounds"]), 2)
        self.assertIsNone(recorder["rounds"][1]["tools"])
        self.assertIn("context window is nearly full", recorder["rounds"][1]["system_prompt"])
        # The last round's tool result was still fed back before the answer.
        self.assertEqual(recorder["rounds"][1]["messages"][-1]["tool_call_id"], "c1")
        self.assertEqual("".join(e.get("delta", "") for e in events if e["type"] == "content.delta").strip(), "Here is what I have.")

    def test_the_bot_can_write_its_notes_during_a_turn(self):
        from app.services.bot_memory import register_memory_actions

        bot = self.storage.get_bots()[0]
        self.storage.add_message(
            {"id": "m1", "thread_id": bot["id"], "bot_id": bot["id"], "sender": "user", "text": "remember the repo"}
        )
        gateway, _ = self._gateway("allow")
        register_memory_actions(gateway)
        recorder = {}
        stream = _scripted_stream(
            recorder,
            [
                [{"type": "tool_calls", "calls": [{"id": "n1", "name": "memory_write", "arguments": "{\"text\": \"Repo: ~/proj\"}"}]},
                 {"type": "turn.completed", "ok": True}],
                [{"type": "content.delta", "delta": "Noted the repo."}, {"type": "turn.completed", "ok": True}],
            ],
        )
        with mock.patch("app.services.bot_memory.storage_service", self.storage):
            _, events = self._run_turn(bot["id"], stream=stream, gateway=gateway)
        self.assertEqual(next(b for b in self.storage.get_bots() if b["id"] == bot["id"])["memory"], "Repo: ~/proj")
        # No approval was needed, and the result echoed the notes.
        self.assertNotIn("approval.requested", [e["type"] for e in events])
        self.assertIn("Repo: ~/proj", recorder["rounds"][1]["messages"][-1]["content"])
        self.assertIn("Your notes", recorder["rounds"][0]["system_prompt"])

    def test_secret_values_are_redacted_from_tool_results(self):
        from app.services.secrets_service import Redactor

        bot = self.storage.get_bots()[0]
        self.storage.add_message(
            {"id": "m1", "thread_id": bot["id"], "bot_id": bot["id"], "sender": "user", "text": "read it"}
        )
        gateway, _ = self._gateway("allow")  # hello.txt contains "hello"
        recorder = {}
        stream = _scripted_stream(
            recorder,
            [
                [{"type": "tool_calls", "calls": [{"id": "r1", "name": "workspace_read", "arguments": "{\"path\": \"hello.txt\"}"}]},
                 {"type": "turn.completed", "ok": True}],
                [{"type": "content.delta", "delta": "Read it."}, {"type": "turn.completed", "ok": True}],
            ],
        )
        patches = self._patches(recorder, stream=stream, gateway=gateway)
        patches.append(mock.patch.object(chat_router, "redactor_for", lambda thread_id: Redactor({"MY_KEY": "hello"})))
        patches.append(mock.patch.object(chat_router, "names_for", lambda thread_id: ["MY_KEY"]))
        for p in patches:
            p.start()
        try:
            _, events = asyncio.run(_start_and_stream(bot["id"]))
        finally:
            for p in reversed(patches):
                p.stop()
        tool_reply = recorder["rounds"][1]["messages"][-1]["content"]
        self.assertIn("[secret MY_KEY]", tool_reply)
        self.assertNotIn("hello", tool_reply)
        completed = next(e for e in events if e["type"] == "tool.completed")
        self.assertNotIn("hello", json.dumps(completed))
        saved = self.storage.get_messages(bot["id"])[-1]
        self.assertNotIn("hello", saved["raw_payload"]["tool_calls"][0]["result"])
        # The names are only announced when the bot has a computer (the fake provider has none here).
        self.assertNotIn("MY_KEY", recorder["rounds"][0]["system_prompt"])

    def test_forced_final_round_answer_is_used_when_the_model_gives_one(self):
        bot = self.storage.get_bots()[0]
        self.storage.add_message(
            {"id": "m1", "thread_id": bot["id"], "bot_id": bot["id"], "sender": "user", "text": "loop"}
        )
        gateway, _ = self._gateway("allow")
        recorder = {}
        tool_round = [
            {"type": "tool_calls", "calls": [{"id": "c", "name": "workspace_list", "arguments": "{}"}]},
            {"type": "turn.completed", "ok": True},
        ]
        final_round = [{"type": "content.delta", "delta": "Here is what I found so far."}, {"type": "turn.completed", "ok": True}]
        with mock.patch.object(chat_router.settings, "LLM_MAX_TOOL_ROUNDS", 1):
            _, events = self._run_turn(bot["id"], stream=_scripted_stream(recorder, [tool_round, final_round]), gateway=gateway)

        self.assertEqual(len(recorder["rounds"]), 2)
        text = "".join(e.get("delta", "") for e in events if e["type"] == "content.delta")
        self.assertEqual(text, "Here is what I found so far.")
        self.assertNotIn("Stopped after", text)

    def test_muapi_provider_gets_no_tools(self):
        bot = self.storage.get_bots()[0]
        self.storage.add_message(
            {"id": "m1", "thread_id": bot["id"], "bot_id": bot["id"], "sender": "user", "text": "hi"}
        )
        muapi = LLMConfig(provider="muapi", api_key="k", base_url="https://api.muapi.ai/api/v1", reasoning_effort="", default_model="grok-4-5")
        recorder, _ = self._run_turn(bot["id"], config=muapi)
        self.assertIsNone(recorder["tools"])
        self.assertNotIn("## Tools", recorder["system_prompt"])

    def test_turn_runs_without_a_subscriber_and_can_be_replayed_by_a_late_one(self):
        bot = self.storage.get_bots()[0]
        self.storage.add_message(
            {"id": "m1", "thread_id": bot["id"], "bot_id": bot["id"], "sender": "user", "text": "hi"}
        )
        recorder = {}
        manager = TurnManager()
        patches = self._patches(recorder, manager=manager)
        for p in patches:
            p.start()
        try:
            async def scenario():
                started = await chat_router.start_turn(bot["id"], chat_router.StartTurnRequest())
                turn_id = started["turn"]["turn_id"]
                # Nobody is listening; the turn still runs to completion.
                await asyncio.wait_for(manager.get(turn_id).task, 2)
                status = await chat_router.turn_status(bot["id"])
                self.assertEqual(status["turn"]["status"], "completed")
                # A browser arriving afterwards (reload, other machine) replays everything.
                response = await chat_router.stream_turn(bot["id"], _fake_request(), turn=None, after=0)
                events = await _collect_sse(response)
                self.assertEqual(events[0]["type"], "turn.started")
                self.assertEqual(events[-1]["type"], "turn.completed")
                self.assertEqual("".join(e.get("delta", "") for e in events if e["type"] == "content.delta"), "Hello there")
                # Resuming from Last-Event-ID skips what was already seen.
                response = await chat_router.stream_turn(bot["id"], _fake_request(last_event_id=events[-2]["seq"]), turn=None, after=0)
                tail = await _collect_sse(response)
                self.assertEqual([e["type"] for e in tail], ["turn.completed"])
                # Starting again while one is running is refused with the running turn.
                gate = asyncio.Event()

                async def slow_stream(model, messages, system_prompt="", config=None, tools=None):
                    await gate.wait()
                    yield {"type": "content.delta", "delta": "late"}
                    yield {"type": "turn.completed", "ok": True}

                with mock.patch.object(chat_router.llm_service, "stream_chat_completion", slow_stream):
                    first = await chat_router.start_turn(bot["id"], chat_router.StartTurnRequest())
                    await asyncio.sleep(0.01)
                    second = await chat_router.start_turn(bot["id"], chat_router.StartTurnRequest())
                    self.assertEqual(second.status_code, 409)
                    self.assertEqual(json.loads(second.body)["turn"]["turn_id"], first["turn"]["turn_id"])
                    self.assertEqual((await chat_router.list_turns())["turns"][0]["turn_id"], first["turn"]["turn_id"])
                    gate.set()
                    await asyncio.wait_for(manager.get(first["turn"]["turn_id"]).task, 2)

            asyncio.run(scenario())
        finally:
            for p in reversed(patches):
                p.stop()

        saved = self.storage.get_messages(bot["id"])
        # The seeded bot starts with a greeting; both turns were persisted after it.
        self.assertEqual([m["text"] for m in saved if m["sender"] == "bot"][-2:], ["Hello there", "late"])

    def test_tool_results_persist_and_replay_on_the_next_turn(self):
        bot = self.storage.get_bots()[0]
        self.storage.add_message({"id": "m1", "thread_id": bot["id"], "bot_id": bot["id"], "sender": "user", "text": "what is in the workspace?"})
        workspace_root = Path(self.temp_dir.name) / "workspace"
        workspace_root.mkdir(exist_ok=True)
        (workspace_root / "notes.txt").write_text("hi", encoding="utf-8")
        gateway = ActionGateway(workspace=WorkspaceService(workspace_root), approvals=DecidingApprovalBroker("allow"), audit=self.storage)
        recorder = {}
        stream = _scripted_stream(
            recorder,
            [
                [{"type": "tool_calls", "calls": [{"id": "c1", "name": "workspace_list", "arguments": "{\"path\": \".\"}"}]},
                 {"type": "usage", "prompt_tokens": 300, "completion_tokens": 20},
                 {"type": "turn.completed", "ok": True}],
                [{"type": "content.delta", "delta": "There is notes.txt."},
                 {"type": "usage", "prompt_tokens": 420, "completion_tokens": 9},
                 {"type": "turn.completed", "ok": True}],
                [{"type": "content.delta", "delta": "Still just notes.txt."},
                 {"type": "usage", "prompt_tokens": 500, "completion_tokens": 8},
                 {"type": "turn.completed", "ok": True}],
            ],
        )
        patches = self._patches(recorder, stream=stream, gateway=gateway)
        for p in patches:
            p.start()
        try:
            _, events = asyncio.run(_start_and_stream(bot["id"]))
            usage_events = [e["usage"] for e in events if e["type"] == "turn.usage"]
            self.assertEqual([u["rounds"] for u in usage_events], [1, 2])
            self.assertEqual(usage_events[-1], {"prompt_tokens": 420, "completion_tokens": 29, "rounds": 2, "context_window": 32000})

            saved = [m for m in self.storage.get_messages(bot["id"]) if m["sender"] == "bot"][-1]
            record = saved["raw_payload"]["tool_calls"][0]
            self.assertEqual(record["raw_arguments"], "{\"path\": \".\"}")
            self.assertIn("notes.txt", record["result"])
            self.assertEqual(saved["raw_payload"]["usage"]["prompt_tokens"], 420)

            # Next turn: the model is shown the earlier call and its result.
            self.storage.add_message({"id": "m2", "thread_id": bot["id"], "bot_id": bot["id"], "sender": "user", "text": "anything new?"})
            asyncio.run(_start_and_stream(bot["id"]))
            replayed = recorder["rounds"][-1]["messages"]
            # The seeded bot opens with a greeting; look at the tail.
            tail = replayed[-5:]
            self.assertEqual([m["role"] for m in tail], ["user", "assistant", "tool", "assistant", "user"])
            self.assertEqual(tail[1]["tool_calls"], [{"id": "c1", "name": "workspace_list", "arguments": "{\"path\": \".\"}"}])
            self.assertEqual(tail[2]["tool_call_id"], "c1")
            self.assertIn("notes.txt", tail[2]["content"])
            self.assertEqual(tail[3]["content"], "There is notes.txt.")
        finally:
            for p in reversed(patches):
                p.stop()

    def test_a_turn_that_outgrows_the_window_is_compacted_between_tool_rounds(self):
        from app.config import settings
        from app.services.context_builder import SUMMARY_LEAD
        from app.services.context_compactor import SUMMARY_INSTRUCTION

        bot = self.storage.get_bots()[0]
        thread = bot["id"]
        for message in [
            {"id": "u1", "thread_id": thread, "bot_id": thread, "sender": "user", "text": "first"},
            {"id": "b1", "thread_id": thread, "bot_id": thread, "sender": "bot", "text": "A", "raw_payload": {"usage": {"prompt_tokens": 1000}}},
            {"id": "u2", "thread_id": thread, "bot_id": thread, "sender": "user", "text": "second"},
            {"id": "b2", "thread_id": thread, "bot_id": thread, "sender": "bot", "text": "B", "raw_payload": {"usage": {"prompt_tokens": 2000}}},
            {"id": "u3", "thread_id": thread, "bot_id": thread, "sender": "user", "text": "third: list the workspace"},
        ]:
            self.storage.add_message(message)
        gateway, _ = self._gateway("allow")
        recorder = {}
        stream = _scripted_stream(
            recorder,
            [
                # Round 1 calls a tool and reports a prompt past 70% of the 32k window.
                [{"type": "tool_calls", "calls": [{"id": "c1", "name": "workspace_list", "arguments": "{\"path\": \".\"}"}]},
                 {"type": "usage", "prompt_tokens": 30_000, "completion_tokens": 20},
                 {"type": "turn.completed", "ok": True}],
                # The summary request.
                [{"type": "content.delta", "delta": "SUMMARY OF THE OLD TURNS"},
                 {"type": "usage", "prompt_tokens": 2_500, "completion_tokens": 30},
                 {"type": "turn.completed", "ok": True}],
                # Round 2 answers on the rebuilt context.
                [{"type": "content.delta", "delta": "Done."},
                 {"type": "usage", "prompt_tokens": 900, "completion_tokens": 5},
                 {"type": "turn.completed", "ok": True}],
            ],
        )
        patches = self._patches(recorder, stream=stream, gateway=gateway)
        patches.append(mock.patch.object(settings, "CONTEXT_KEEP_RECENT_TOKENS", 10))
        for p in patches:
            p.start()
        try:
            _, events = asyncio.run(_start_and_stream(thread))
        finally:
            for p in reversed(patches):
                p.stop()

        kinds = [e["type"] for e in events]
        self.assertIn("context.compacting", kinds)
        self.assertIn("context.compacted", kinds)
        self.assertEqual(events[-1]["type"], "turn.completed")

        rounds = recorder["rounds"]
        self.assertEqual(len(rounds), 3)
        # The summary request: the old turns, then the instruction, no tools.
        summary_request = rounds[1]
        self.assertIsNone(summary_request["tools"])
        # The bot's stored greeting precedes the first user turn, so the old part is greeting, first, A.
        self.assertEqual([m["content"] for m in summary_request["messages"]][-3:-1], ["first", "A"])
        self.assertEqual(summary_request["messages"][-1]["content"], SUMMARY_INSTRUCTION)
        # Round 2: summary, the kept turns, then this turn's own tool round verbatim.
        second = rounds[2]["messages"]
        self.assertTrue(second[0]["content"].startswith(SUMMARY_LEAD))
        self.assertIn("SUMMARY OF THE OLD TURNS", second[0]["content"])
        self.assertEqual([m["content"] for m in second[2:5]], ["second", "B", "third: list the workspace"])
        self.assertEqual(second[-2]["tool_calls"][0]["id"], "c1")
        self.assertEqual(second[-1]["role"], "tool")
        self.assertEqual(second[-1]["tool_call_id"], "c1")

        stored = self.storage.get_messages(thread)
        compaction = [m for m in stored if m.get("item_type") == "compaction"]
        self.assertEqual(len(compaction), 1)
        self.assertEqual(compaction[0]["raw_payload"]["reason"], "auto")
        self.assertEqual(compaction[0]["raw_payload"]["covers_through"], "b1")
        self.assertEqual(stored[-1]["sender"], "bot")
        self.assertEqual(stored[-1]["text"], "Done.")

    def test_a_failed_mid_turn_compaction_lets_the_turn_continue(self):
        bot = self.storage.get_bots()[0]
        thread = bot["id"]
        for message in [
            {"id": "u1", "thread_id": thread, "bot_id": thread, "sender": "user", "text": "first"},
            {"id": "b1", "thread_id": thread, "bot_id": thread, "sender": "bot", "text": "A", "raw_payload": {"usage": {"prompt_tokens": 1000}}},
            {"id": "u2", "thread_id": thread, "bot_id": thread, "sender": "user", "text": "second"},
        ]:
            self.storage.add_message(message)
        gateway, _ = self._gateway("allow")
        recorder = {}
        stream = _scripted_stream(
            recorder,
            [
                [{"type": "tool_calls", "calls": [{"id": "c1", "name": "workspace_list", "arguments": "{\"path\": \".\"}"}]},
                 {"type": "usage", "prompt_tokens": 30_000, "completion_tokens": 20},
                 {"type": "turn.completed", "ok": True}],
                [{"type": "turn.completed", "ok": False, "error": "summary model down"}],
                [{"type": "content.delta", "delta": "Carried on."},
                 {"type": "usage", "prompt_tokens": 31_000, "completion_tokens": 5},
                 {"type": "turn.completed", "ok": True}],
            ],
        )
        patches = self._patches(recorder, stream=stream, gateway=gateway)
        patches.append(mock.patch.object(chat_router.settings, "CONTEXT_KEEP_RECENT_TOKENS", 10))
        for p in patches:
            p.start()
        try:
            _, events = asyncio.run(_start_and_stream(thread))
        finally:
            for p in reversed(patches):
                p.stop()
        kinds = [e["type"] for e in events]
        self.assertIn("context.compaction_failed", kinds)
        self.assertNotIn("context.compacted", kinds)
        self.assertEqual((events[-1]["type"], events[-1]["ok"]), ("turn.completed", True))
        # Round 2 still carried the full history and the tool round.
        second = recorder["rounds"][2]["messages"]
        self.assertEqual([m["content"] for m in second[1:4]], ["first", "A", "second"])
        self.assertEqual(second[-1]["tool_call_id"], "c1")
        self.assertFalse(any(m.get("item_type") == "compaction" for m in self.storage.get_messages(thread)))

    def test_streaming_an_unknown_thread_reports_no_turn(self):
        with mock.patch.object(chat_router, "turn_manager", TurnManager()):
            response = asyncio.run(chat_router.stream_turn("nope", _fake_request(), turn=None, after=0))
            events = asyncio.run(_collect_sse(response))
        self.assertEqual(events, [{"type": "turn.none"}])

    def test_pending_approval_is_visible_in_turn_status(self):
        bot = self.storage.get_bots()[0]
        self.storage.add_message(
            {"id": "m1", "thread_id": bot["id"], "bot_id": bot["id"], "sender": "user", "text": "write"}
        )

        class HoldingBroker(DecidingApprovalBroker):
            def __init__(self):
                super().__init__("allow")
                self.release = asyncio.Event()

            async def wait(self, request_id):
                await self.release.wait()
                return "allow"

        workspace_root = Path(self.temp_dir.name) / "workspace"
        workspace_root.mkdir(exist_ok=True)
        broker = HoldingBroker()
        gateway = ActionGateway(workspace=WorkspaceService(workspace_root), approvals=broker, audit=self.storage)
        recorder = {}
        stream = _scripted_stream(
            recorder,
            [
                [{"type": "tool_calls", "calls": [{"id": "c1", "name": "workspace_write", "arguments": "{\"path\": \"a.txt\", \"content\": \"x\"}"}]},
                 {"type": "turn.completed", "ok": True}],
                [{"type": "content.delta", "delta": "Written."}, {"type": "turn.completed", "ok": True}],
            ],
        )
        manager = TurnManager()
        patches = self._patches(recorder, stream=stream, gateway=gateway, manager=manager)
        for p in patches:
            p.start()
        try:
            async def scenario():
                started = await chat_router.start_turn(bot["id"], chat_router.StartTurnRequest())
                await asyncio.sleep(0.05)
                status = (await chat_router.turn_status(bot["id"]))["turn"]
                self.assertEqual(status["status"], "running")
                self.assertEqual(status["pending_approval"]["call_name"], "workspace_write")
                self.assertIn("a.txt", status["pending_approval"]["summary"])
                overview = (await chat_router.list_turns())["turns"]
                self.assertEqual(overview[0]["pending_approval"]["request_id"], status["pending_approval"]["request_id"])
                broker.release.set()
                await asyncio.wait_for(manager.get(started["turn"]["turn_id"]).task, 2)
                self.assertIsNone((await chat_router.turn_status(bot["id"]))["turn"]["pending_approval"])

            asyncio.run(scenario())
        finally:
            for p in reversed(patches):
                p.stop()
        self.assertTrue((workspace_root / "a.txt").exists())

    def test_rejected_tool_command_is_reported_and_added_to_the_prompt(self):
        bot = self.storage.get_bots()[0]
        self.storage.add_message(
            {
                "id": "m1",
                "thread_id": bot["id"],
                "bot_id": bot["id"],
                "sender": "user",
                "text": "/workspace delete everything",
            }
        )

        recorder, events = self._run_turn(bot["id"])

        failed = [e for e in events if e["type"] == "tool.failed"]
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0]["tool"], "workspace")
        self.assertIn("rejected before execution", recorder["system_prompt"])
        self.assertEqual(events[-1]["type"], "turn.completed")


if __name__ == "__main__":
    unittest.main()
