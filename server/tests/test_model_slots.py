"""The gate in front of the model server: a fair limit on requests in flight."""

import asyncio
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List
from unittest import mock

from app.routers import chat as chat_router
from app.services import llm_service
from app.services import storage_service as storage_module
from app.services.action_gateway import ActionGateway
from app.services.llm_config import LLMConfig
from app.services.model_slots import ModelSlots
from app.services.storage_service import StorageService
from app.services.turn_manager import TurnManager
from app.services.workspace_service import WorkspaceService

from tests.test_chat_router import NoComposio, _collect_sse, _fake_request


async def _stream(gate: asyncio.Event, text: str):
    """A model reply that holds its slot until `gate` is set."""
    yield {"type": "content.delta", "delta": text}
    await gate.wait()
    yield {"type": "turn.completed", "ok": True}


class ModelSlotsTests(unittest.IsolatedAsyncioTestCase):
    async def _run(self, slots: ModelSlots, owner: str, gate: asyncio.Event, text: str, log: List[Dict[str, Any]]):
        async for event in slots.gated(owner, _stream(gate, text)):
            log.append({**event, "owner": owner, "text": text})

    async def test_requests_beyond_the_limit_wait_their_turn_and_say_so(self):
        slots = ModelSlots(limit=lambda: 2)
        gates = [asyncio.Event() for _ in range(3)]
        log: List[Dict[str, Any]] = []
        tasks = [asyncio.create_task(self._run(slots, "jamie", gates[i], f"r{i}", log)) for i in range(3)]
        await asyncio.sleep(0.05)
        self.assertEqual(slots.snapshot()["active"], 2)
        self.assertEqual(slots.snapshot()["waiting"], 1)
        queued = [e for e in log if e["type"] == "model.queued"]
        self.assertEqual([(e["text"], e["ahead"], e["limit"]) for e in queued], [("r2", 0, 2)])
        self.assertEqual([e["text"] for e in log if e["type"] == "content.delta"], ["r0", "r1"])
        gates[0].set()
        await asyncio.sleep(0.05)
        started = [e for e in log if e["type"] == "model.started"]
        self.assertEqual([e["text"] for e in started], ["r2"])
        self.assertIn("r2", [e["text"] for e in log if e["type"] == "content.delta"])
        for gate in gates:
            gate.set()
        await asyncio.gather(*tasks)
        self.assertEqual((slots.snapshot()["active"], slots.snapshot()["waiting"]), (0, 0))

    async def test_slots_rotate_between_accounts(self):
        slots = ModelSlots(limit=lambda: 1)
        first = asyncio.Event()
        log: List[Dict[str, Any]] = []
        holder = asyncio.create_task(self._run(slots, "jamie", first, "hold", log))
        await asyncio.sleep(0.01)
        # Jamie queues three more, then a co-worker queues one: the co-worker is served second.
        gates = {name: asyncio.Event() for name in ("j1", "j2", "j3", "c1")}
        tasks = [asyncio.create_task(self._run(slots, "jamie", gates[n], n, log)) for n in ("j1", "j2", "j3")]
        await asyncio.sleep(0.01)
        tasks.append(asyncio.create_task(self._run(slots, "coworker", gates["c1"], "c1", log)))
        await asyncio.sleep(0.01)
        ahead = {e["text"]: e["ahead"] for e in log if e["type"] == "model.queued"}
        self.assertEqual(ahead, {"j1": 0, "j2": 2, "j3": 3, "c1": 1})
        order = []
        for name in ("hold", "j1", "c1", "j2", "j3"):
            (first if name == "hold" else gates[name]).set()
            await asyncio.sleep(0.02)
            order = [e["text"] for e in log if e["type"] == "content.delta"]
        self.assertEqual(order, ["hold", "j1", "c1", "j2", "j3"])
        await asyncio.gather(holder, *tasks)

    async def test_a_cancelled_wait_leaves_the_queue_and_no_limit_means_no_queue(self):
        slots = ModelSlots(limit=lambda: 1)
        first = asyncio.Event()
        log: List[Dict[str, Any]] = []
        holder = asyncio.create_task(self._run(slots, "jamie", first, "hold", log))
        await asyncio.sleep(0.01)
        waiter = asyncio.create_task(self._run(slots, "jamie", asyncio.Event(), "gone", log))
        await asyncio.sleep(0.01)
        self.assertEqual(slots.snapshot()["waiting"], 1)
        waiter.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiter
        self.assertEqual(slots.snapshot()["waiting"], 0)
        first.set()
        await holder
        self.assertEqual(slots.snapshot()["active"], 0)

        free = ModelSlots(limit=lambda: 0)
        gates = [asyncio.Event() for _ in range(5)]
        log = []
        tasks = [asyncio.create_task(self._run(free, "jamie", g, f"f{i}", log)) for i, g in enumerate(gates)]
        await asyncio.sleep(0.02)
        self.assertEqual(free.snapshot()["active"], 5)
        self.assertEqual([e for e in log if e["type"] == "model.queued"], [])
        for g in gates:
            g.set()
        await asyncio.gather(*tasks)


class SlotsInATurnTests(unittest.TestCase):
    """Two bots, one slot: the second turn shows its place in line, then runs."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.storage = StorageService(root / "data")
        (root / "workspace").mkdir()
        self.config = LLMConfig(provider="openai_compatible", api_key="", base_url="http://x/v1", reasoning_effort="", default_model="m")
        bots = self.storage.get_bots()
        self.first = bots[0]
        self.second = {"id": "bot-two", "name": "Two", "role": "x", "system_prompt": "You help.", "model": "m", "tools": []}
        self.storage.save_bots(bots + [self.second])
        for bot in (self.first, self.second):
            self.storage.add_message({"id": f"m-{bot['id']}", "thread_id": bot["id"], "bot_id": bot["id"], "sender": "user", "text": "hi"})
        self.gate = asyncio.Event
        self.patches = [
            mock.patch.object(storage_module, "storage_service", self.storage),
            mock.patch.object(llm_service, "current_llm_config", return_value=self.config),
            mock.patch.object(chat_router, "composio_service", NoComposio()),
            mock.patch.object(chat_router, "turn_manager", TurnManager()),
            mock.patch.object(chat_router, "action_gateway", ActionGateway(workspace=WorkspaceService(root / "workspace"), approvals=mock.Mock(), audit=self.storage)),
            mock.patch.object(llm_service.model_slots, "_limit", lambda: 1),
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

    def test_the_second_turn_waits_for_the_first_and_shows_it(self):
        async def scenario():
            release = asyncio.Event()

            async def provider(model, messages, system_prompt="", config=None, tools=None, one_shot=False):
                async for event in _stream(release, "done"):
                    yield event

            with mock.patch.object(llm_service.openai_compatible_service, "stream_chat_completion", provider):
                await chat_router.start_turn(self.first["id"], chat_router.StartTurnRequest())
                await asyncio.sleep(0.05)
                await chat_router.start_turn(self.second["id"], chat_router.StartTurnRequest())
                await asyncio.sleep(0.05)
                self.assertEqual(llm_service.model_slots.snapshot()["waiting"], 1)
                waiting = chat_router.turn_manager.current(self.second["id"])
                queued = [e for e in waiting.events if e["type"] == "model.queued"]
                self.assertEqual([(e["ahead"], e["limit"]) for e in queued], [(0, 1)])
                self.assertEqual([e["type"] for e in waiting.events if e["type"] == "content.delta"], [])
                release.set()
                response = await chat_router.stream_turn(self.second["id"], _fake_request(), turn=None, after=0)
                return await _collect_sse(response)

        events = asyncio.run(scenario())
        types = [e["type"] for e in events]
        self.assertLess(types.index("model.queued"), types.index("model.started"))
        self.assertLess(types.index("model.started"), types.index("content.delta"))
        self.assertEqual(types[-1], "turn.completed")
        self.assertEqual(llm_service.model_slots.snapshot(), {"limit": 1, "active": 0, "waiting": 0, "active_by_owner": {}, "waiting_by_owner": {}})


if __name__ == "__main__":
    unittest.main()
