import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi import HTTPException

from app.routers import bots as bots_router
from app.services.computer_provider import ComputerProviderError
from app.services.llm_config import LLMConfig
from app.services.storage_service import StorageService
from app.services.turn_manager import TurnManager
from app.services import storage_service as storage_module


class FakeComputers:
    def __init__(self, running=True):
        self.running = running
        self.stopped = []

    async def stop(self, computer_id):
        self.stopped.append(computer_id)
        if not self.running:
            raise ComputerProviderError("No computer")
        return {"state": "stopped"}


class BotsRouterTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.storage = StorageService(Path(self.temp_dir.name))
        self.computers = FakeComputers()
        self.turns = TurnManager()
        config = LLMConfig(provider="openai_compatible", api_key="", base_url="http://x/v1", reasoning_effort="", default_model="local-default")
        self.patches = [
            mock.patch.object(storage_module, "storage_service", self.storage),
            mock.patch.object(bots_router, "computer_provider", self.computers),
            mock.patch.object(bots_router, "turn_manager", self.turns),
            mock.patch.object(bots_router.llm_service, "current_llm_config", return_value=config),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        try:
            self.temp_dir.cleanup()
        except PermissionError:
            pass  # Windows keeps the SQLite file open a moment longer.

    def test_create_fills_defaults_and_requires_a_name(self):
        bot = asyncio.run(bots_router.create_bot(bots_router.BotInput(name="  Helper  ")))
        self.assertEqual(bot["name"], "Helper")
        self.assertEqual(bot["model"], "local-default")
        self.assertEqual(bot["role"], "AI Assistant")
        self.assertIn("Helper", bot["system_prompt"])
        self.assertFalse(bot["archived"])
        self.assertIn(bot["id"], [b["id"] for b in self.storage.get_bots()])

        with self.assertRaises(HTTPException) as caught:
            asyncio.run(bots_router.create_bot(bots_router.BotInput(name="   ")))
        self.assertEqual(caught.exception.status_code, 422)

    def test_a_new_bot_keeps_the_tool_and_approval_settings_it_was_created_with(self):
        bot = asyncio.run(bots_router.create_bot(bots_router.BotInput(
            name="Careful", tool_settings={"groups": {"todo": False}}, auto_approval="shadow", gate_threshold=0.456,
        )))
        self.assertEqual((bot["tool_settings"]["groups"], bot["auto_approval"], bot["gate_threshold"]), ({"todo": False}, "shadow", 0.46))
        with self.assertRaises(HTTPException) as caught:
            asyncio.run(bots_router.create_bot(bots_router.BotInput(name="Bad", gate_threshold=2)))
        self.assertEqual(caught.exception.status_code, 422)

    def test_update_only_touches_editable_fields_and_archives(self):
        bot = asyncio.run(bots_router.create_bot(bots_router.BotInput(name="Helper", model="m1")))
        updated = asyncio.run(bots_router.update_bot(bot["id"], bots_router.BotUpdate(name="Renamed", system_prompt="Be terse.", model="")))
        self.assertEqual(updated["name"], "Renamed")
        self.assertEqual(updated["system_prompt"], "Be terse.")
        self.assertEqual(updated["model"], "m1")  # a blank model keeps the old one
        self.assertEqual(updated["id"], bot["id"])

        archived = asyncio.run(bots_router.update_bot(bot["id"], bots_router.BotUpdate(archived=True)))
        self.assertTrue(archived["archived"])
        self.assertTrue(archived["archived_at"])
        restored = asyncio.run(bots_router.update_bot(bot["id"], bots_router.BotUpdate(archived=False)))
        self.assertFalse(restored["archived"])
        self.assertIsNone(restored["archived_at"])

        with self.assertRaises(HTTPException):
            asyncio.run(bots_router.update_bot("missing", bots_router.BotUpdate(name="x")))

    def test_the_risk_threshold_is_a_number_or_inherit(self):
        bot = asyncio.run(bots_router.create_bot(bots_router.BotInput(name="Helper")))
        self.assertIsNone(bot["gate_threshold"])
        updated = asyncio.run(bots_router.update_bot(bot["id"], bots_router.BotUpdate(gate_threshold=0.456)))
        self.assertEqual(updated["gate_threshold"], 0.46)
        updated = asyncio.run(bots_router.update_bot(bot["id"], bots_router.BotUpdate(gate_threshold="inherit")))
        self.assertIsNone(updated["gate_threshold"])
        for bad in (0, 1.5, "soon"):
            with self.assertRaises(HTTPException) as caught:
                asyncio.run(bots_router.update_bot(bot["id"], bots_router.BotUpdate(gate_threshold=bad)))
            self.assertEqual(caught.exception.status_code, 422)

    def test_editing_the_todo_list_records_a_note_in_the_thread(self):
        bot = asyncio.run(bots_router.create_bot(bots_router.BotInput(name="Helper")))
        before = len(self.storage.get_messages(bot["id"]))
        items = [{"id": "t1", "text": "  write   tests ", "state": "doing"}, {"text": "ship", "state": "open"}]
        asyncio.run(bots_router.update_bot(bot["id"], bots_router.BotUpdate(todo=items)))
        # The list given with the bot is its first conversation's.
        stored = self.storage.get_conversation(bot["id"])
        self.assertEqual(stored["todo"], [{"id": "t1", "text": "write tests", "state": "doing"}, {"id": "t2", "text": "ship", "state": "open"}])
        self.assertTrue(stored["todo_updated_at"])
        messages = self.storage.get_messages(bot["id"])
        self.assertEqual(len(messages), before + 1)
        self.assertEqual((messages[-1]["sender"], messages[-1]["item_type"]), ("system", "todo"))
        self.assertEqual(messages[-1]["text"], "- [~] t1: write tests\n- [ ] t2: ship")
        self.assertEqual(messages[-1]["raw_payload"]["by"], "user")
        # The same list again records nothing; a bad state is refused.
        asyncio.run(bots_router.update_bot(bot["id"], bots_router.BotUpdate(todo=stored["todo"])))
        self.assertEqual(len(self.storage.get_messages(bot["id"])), before + 1)
        with self.assertRaises(HTTPException):
            asyncio.run(bots_router.update_bot(bot["id"], bots_router.BotUpdate(todo=[{"text": "x", "state": "later"}])))

    def test_editing_the_notes_records_a_note_in_the_thread(self):
        bot = asyncio.run(bots_router.create_bot(bots_router.BotInput(name="Helper")))
        before = len(self.storage.get_messages(bot["id"]))
        updated = asyncio.run(bots_router.update_bot(bot["id"], bots_router.BotUpdate(memory="  Goal: ship.\r\n")))
        self.assertEqual(updated["memory"], "Goal: ship.")
        self.assertTrue(updated["memory_updated_at"])
        messages = self.storage.get_messages(bot["id"])
        self.assertEqual(len(messages), before + 1)
        self.assertEqual((messages[-1]["sender"], messages[-1]["item_type"], messages[-1]["text"]), ("system", "memory", "Goal: ship."))
        self.assertEqual(messages[-1]["raw_payload"]["by"], "user")

        # Saving the same notes again (the editor sends them with every save) records nothing.
        asyncio.run(bots_router.update_bot(bot["id"], bots_router.BotUpdate(memory="Goal: ship.", name="Helper 2")))
        self.assertEqual(len(self.storage.get_messages(bot["id"])), before + 1)
        # Clearing them is an edit too.
        cleared = asyncio.run(bots_router.update_bot(bot["id"], bots_router.BotUpdate(memory="")))
        self.assertEqual(cleared["memory"], "")
        self.assertEqual(self.storage.get_messages(bot["id"])[-1]["text"], "")

    def test_auto_approval_override_is_stored_and_validated(self):
        bot = asyncio.run(bots_router.create_bot(bots_router.BotInput(name="Gated")))
        self.assertEqual(bot["auto_approval"], "inherit")
        updated = asyncio.run(bots_router.update_bot(bot["id"], bots_router.BotUpdate(auto_approval="shadow")))
        self.assertEqual(updated["auto_approval"], "shadow")
        self.assertEqual(self.storage.get_bots()[-1]["auto_approval"], "shadow")
        # A change to something else leaves the override alone.
        renamed = asyncio.run(bots_router.update_bot(bot["id"], bots_router.BotUpdate(name="Still gated")))
        self.assertEqual(renamed["auto_approval"], "shadow")
        with self.assertRaises(Exception):
            bots_router.BotUpdate(auto_approval="always")

    def test_tool_settings_are_validated_and_only_store_switch_offs(self):
        bot = asyncio.run(bots_router.create_bot(bots_router.BotInput(name="Toolsy")))
        updated = asyncio.run(bots_router.update_bot(bot["id"], bots_router.BotUpdate(tool_settings={
            "groups": {"computer": False, "workspace": True},
            "toolkits": {"gmail": False},
            "tools": {"gmail_send_email": False, "exa_search": True},
            "junk": {"x": 1},
        })))
        self.assertEqual(updated["tool_settings"], {"groups": {"computer": False}, "toolkits": {"gmail": False}, "tools": {"gmail_send_email": False}})
        # An app can be switched on too, over an app that is off by default.
        updated = asyncio.run(bots_router.update_bot(bot["id"], bots_router.BotUpdate(tool_settings={"toolkits": {"slack": True, "gmail": False}})))
        self.assertEqual(updated["tool_settings"], {"toolkits": {"slack": True, "gmail": False}})
        with self.assertRaises(HTTPException):
            asyncio.run(bots_router.update_bot(bot["id"], bots_router.BotUpdate(tool_settings={"tools": {"x": "no"}})))
        with self.assertRaises(HTTPException):
            asyncio.run(bots_router.update_bot(bot["id"], bots_router.BotUpdate(tool_settings={"tools": ["x"]})))

    def test_suggestions_are_cleaned_and_bounded(self):
        bot = asyncio.run(bots_router.create_bot(bots_router.BotInput(name="Helpful", suggestions=[" Hi there "])))
        self.assertEqual(bot["suggestions"], ["Hi there"])
        updated = asyncio.run(bots_router.update_bot(bot["id"], bots_router.BotUpdate(suggestions=["  Check   the disks ", "", "Check the disks", "Update Nextcloud"])))
        self.assertEqual(updated["suggestions"], ["Check the disks", "Update Nextcloud"])
        with self.assertRaises(HTTPException):
            asyncio.run(bots_router.update_bot(bot["id"], bots_router.BotUpdate(suggestions=[f"q{i}" for i in range(7)])))
        with self.assertRaises(HTTPException):
            asyncio.run(bots_router.update_bot(bot["id"], bots_router.BotUpdate(suggestions=["x" * 301])))

    def test_drafted_suggestions_are_read_from_the_reply(self):
        self.assertEqual(bots_router.parse_drafted('Sure:\n["Is Nextcloud healthy?", "Update Nextcloud", "Is Nextcloud healthy?"]', 4),
                         ["Is Nextcloud healthy?", "Update Nextcloud"])
        self.assertEqual(bots_router.parse_drafted('1. Check disk space\n- "Restart the web app"\n\n', 4), ["Check disk space", "Restart the web app"])
        self.assertEqual(bots_router.parse_drafted('["a", "b", "c"]', 2), ["a", "b"])

        async def reply(**kwargs):
            yield {"type": "content.delta", "delta": '["Check the backups", '}
            yield {"type": "content.delta", "delta": '"Show the update status"]'}
            yield {"type": "turn.completed", "ok": True}

        bot = asyncio.run(bots_router.create_bot(bots_router.BotInput(name="Drafty", system_prompt="You look after Nextcloud.")))
        with mock.patch.object(bots_router.llm_service, "stream_chat_completion", side_effect=lambda **kw: reply(**kw)) as called:
            result = asyncio.run(bots_router.draft_suggestions(bot["id"]))
        self.assertEqual(result, {"suggestions": ["Check the backups", "Show the update status"]})
        self.assertIn("You look after Nextcloud.", called.call_args.kwargs["messages"][0]["content"])

    def test_one_bot_can_be_fetched_by_id(self):
        bot = asyncio.run(bots_router.create_bot(bots_router.BotInput(name="Solo")))
        asyncio.run(bots_router.update_bot(bot["id"], bots_router.BotUpdate(memory="Remember the milk.")))
        fetched = asyncio.run(bots_router.get_bot(bot["id"]))
        self.assertEqual((fetched["id"], fetched["memory"]), (bot["id"], "Remember the milk."))
        with self.assertRaises(HTTPException):
            asyncio.run(bots_router.get_bot("bot-nope"))

    def test_archiving_cancels_a_running_turn(self):
        bot = asyncio.run(bots_router.create_bot(bots_router.BotInput(name="Busy")))

        async def scenario():
            gate = asyncio.Event()

            async def forever(turn):
                await gate.wait()

            turn = self.turns.start(bot["id"], bot["id"], "m", forever)
            await asyncio.sleep(0.01)
            await bots_router.update_bot(bot["id"], bots_router.BotUpdate(archived=True))
            await asyncio.wait_for(turn.task, 2)
            self.assertEqual(turn.status, "cancelled")

        asyncio.run(scenario())

    def test_delete_removes_transcript_and_stops_the_computer(self):
        bot = asyncio.run(bots_router.create_bot(bots_router.BotInput(name="Gone")))
        self.storage.add_message({"id": "m1", "thread_id": bot["id"], "bot_id": bot["id"], "sender": "user", "text": "hi"})
        self.storage.add_message({"id": "m2", "thread_id": "other", "bot_id": "other", "sender": "user", "text": "keep"})

        result = asyncio.run(bots_router.delete_bot(bot["id"]))
        self.assertEqual(result["deleted_messages"], 1)
        self.assertNotIn(bot["id"], [b["id"] for b in self.storage.get_bots()])
        self.assertEqual(self.storage.get_messages(bot["id"]), [])
        self.assertEqual(len(self.storage.get_messages("other")), 1)
        self.assertEqual(len(self.computers.stopped), 1)

        with self.assertRaises(HTTPException) as caught:
            asyncio.run(bots_router.delete_bot(bot["id"]))
        self.assertEqual(caught.exception.status_code, 404)

    def test_delete_succeeds_when_there_is_no_computer(self):
        self.computers.running = False
        bot = asyncio.run(bots_router.create_bot(bots_router.BotInput(name="Plain")))
        result = asyncio.run(bots_router.delete_bot(bot["id"]))
        self.assertEqual(result["status"], "ok")


if __name__ == "__main__":
    unittest.main()
