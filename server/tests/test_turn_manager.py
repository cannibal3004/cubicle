import asyncio
import unittest

from app.services.turn_manager import TurnBusyError, TurnManager


async def _collect(turn, after=0, limit=50):
    events = []
    async for event in turn.subscribe(after):
        events.append(event)
        if len(events) >= limit:
            break
    return events


class TurnManagerTests(unittest.TestCase):
    def test_events_are_numbered_and_replayed_to_late_subscribers(self):
        async def scenario():
            manager = TurnManager()
            gate = asyncio.Event()

            async def runner(turn):
                turn.emit({"type": "turn.started"})
                turn.emit({"type": "content.delta", "delta": "Hel"})
                await gate.wait()
                turn.emit({"type": "content.delta", "delta": "lo"})
                turn.emit({"type": "turn.completed"})

            turn = manager.start("thread-1", "bot-1", "model-x", runner)
            await asyncio.sleep(0.01)
            self.assertEqual(turn.status, "running")
            self.assertEqual(turn.seq, 2)

            # A subscriber arriving mid-turn gets the buffer, then live events.
            collector = asyncio.create_task(_collect(turn))
            await asyncio.sleep(0.01)
            gate.set()
            events = await asyncio.wait_for(collector, 2)
            self.assertEqual([e["seq"] for e in events], [1, 2, 3, 4])
            self.assertEqual("".join(e.get("delta", "") for e in events), "Hello")
            self.assertTrue(all(e["turnId"] == turn.turn_id for e in events))

            await asyncio.wait_for(turn.task, 2)
            self.assertEqual(turn.status, "completed")
            # After completion, replay from a sequence number resumes exactly there.
            late = await _collect(turn, after=2)
            self.assertEqual([e["seq"] for e in late], [3, 4])
            self.assertEqual(manager.overview(), [])
            self.assertEqual(manager.current("thread-1").to_dict()["status"], "completed")

        asyncio.run(scenario())

    def test_a_replay_sends_each_run_of_tokens_as_one_event(self):
        async def scenario():
            manager = TurnManager()

            async def runner(turn):
                turn.emit({"type": "turn.started", "botMsgId": "m"})
                for word in ("Let ", "me ", "check."):
                    turn.emit({"type": "reasoning.delta", "botMsgId": "m", "delta": word})
                turn.emit({"type": "tool.started", "botMsgId": "m", "callId": "c1"})
                for word in ("All ", "good."):
                    turn.emit({"type": "content.delta", "botMsgId": "m", "delta": word})
                turn.emit({"type": "reasoning.delta", "botMsgId": "m", "delta": "Done."})
                turn.emit({"type": "turn.completed", "botMsgId": "m"})

            turn = manager.start("thread-1", "bot-1", "model-x", runner)
            await asyncio.wait_for(turn.task, 2)
            events = await _collect(turn)
            self.assertEqual(
                [(e["type"], e.get("delta"), e["seq"]) for e in events],
                [
                    ("turn.started", None, 1),
                    ("reasoning.delta", "Let me check.", 4),
                    ("tool.started", None, 5),
                    ("content.delta", "All good.", 7),
                    ("reasoning.delta", "Done.", 8),
                    ("turn.completed", None, 9),
                ],
            )
            # Resuming after a merged run continues with what followed it.
            self.assertEqual([e["seq"] for e in await _collect(turn, after=4)], [5, 7, 8, 9])
            # Mid-run: the rest of the run, merged.
            self.assertEqual([(e.get("delta"), e["seq"]) for e in (await _collect(turn, after=2))[:1]], [("me check.", 4)])

        asyncio.run(scenario())

    def test_one_running_turn_per_thread(self):
        async def scenario():
            manager = TurnManager()
            gate = asyncio.Event()

            async def runner(turn):
                await gate.wait()

            first = manager.start("thread-1", "bot-1", "m", runner)
            with self.assertRaises(TurnBusyError) as caught:
                manager.start("thread-1", "bot-1", "m", runner)
            self.assertIs(caught.exception.turn, first)
            # Other threads are independent.
            other = manager.start("thread-2", "bot-2", "m", runner)
            self.assertEqual({t["thread_id"] for t in manager.overview()}, {"thread-1", "thread-2"})
            gate.set()
            await asyncio.gather(first.task, other.task)
            manager.start("thread-1", "bot-1", "m", runner)  # allowed once finished

        asyncio.run(scenario())

    def test_failures_and_cancellation_finish_the_turn_with_an_event(self):
        async def scenario():
            manager = TurnManager()

            async def boom(turn):
                turn.emit({"type": "turn.started"})
                raise RuntimeError("model exploded")

            failed = manager.start("t-fail", "b", "m", boom)
            await asyncio.wait_for(failed.task, 2)
            self.assertEqual(failed.status, "failed")
            self.assertEqual(failed.events[-1]["type"], "turn.failed")
            self.assertIn("model exploded", failed.events[-1]["error"])

            async def forever(turn):
                turn.set_pending_approval({"request_id": "req-1", "summary": "Do a thing"})
                await asyncio.sleep(60)

            waiting = manager.start("t-cancel", "b", "m", forever)
            await asyncio.sleep(0.01)
            self.assertEqual(manager.overview()[0]["pending_approval"]["request_id"], "req-1")
            self.assertIsNotNone(manager.cancel("t-cancel"))
            await asyncio.wait_for(waiting.task, 2)
            self.assertEqual(waiting.status, "cancelled")
            self.assertIsNone(waiting.pending_approval)
            self.assertEqual(waiting.events[-1]["type"], "turn.cancelled")
            self.assertIsNone(manager.cancel("t-cancel"))

        asyncio.run(scenario())

    def test_finished_turns_are_forgotten_after_retention(self):
        async def scenario():
            manager = TurnManager(retention_seconds=0)

            async def quick(turn):
                turn.emit({"type": "turn.completed"})

            turn = manager.start("t", "b", "m", quick)
            await asyncio.wait_for(turn.task, 2)
            await asyncio.sleep(0.01)
            manager.overview()  # triggers cleanup
            self.assertIsNone(manager.current("t"))
            self.assertIsNone(manager.get(turn.turn_id))

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
