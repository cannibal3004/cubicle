"""Undo and redo: rewinding to before a user message, the redo stack, and what is put right."""

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi import HTTPException

from app.routers import chat as chat_router
from app.schemas.contracts import TurnRequest
from app.services import retrieval, rewind
from app.services import storage_service as storage_module
from app.services.context_builder import build_history_messages
from app.services.retrieval import RetrievalConfig, toy_embedding
from app.services.storage_service import StorageService
from app.services.turn_manager import TurnManager

CONFIG = RetrievalConfig(enabled=True, embedding_model="toy", rerank_model="", base_url="http://model.test/v1", top_k=6, chunk_chars=2000)


async def fake_embed(texts, config=None):
    return [toy_embedding(text) for text in texts]


class RewindTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.storage = StorageService(Path(self.temp.name))
        self.patch = mock.patch.object(storage_module, "storage_service", self.storage)
        self.patch.start()
        self.bot = self.storage.get_bots()[0]["id"]
        self.storage.delete_messages(self.bot)
        self.n = 0

    def tearDown(self):
        self.patch.stop()
        try:
            self.temp.cleanup()
        except PermissionError:
            pass

    def add(self, sender, text, **extra):
        self.n += 1
        message = {"id": f"m{self.n}", "thread_id": self.bot, "bot_id": self.bot, "sender": sender, "text": text, "created_at": f"2026-10-23T10:{self.n:02d}:00", **extra}
        self.storage.add_message(message)
        return message

    def ids(self):
        return [m["id"] for m in self.storage.get_messages(thread_id=self.bot)]

    def exchange(self, question, answer, tool_calls=None):
        self.add("user", question, item_type="user_text")
        self.add("bot", answer, raw_payload={"tool_calls": tool_calls} if tool_calls else {})

    def test_undo_takes_back_the_last_exchange_and_redo_puts_it_back(self):
        self.exchange("What is the capital of France?", "Paris.")
        self.exchange("What did you just say?", "I said Paris.")
        result = rewind.rewind(self.bot)
        self.assertEqual(result["message"]["text"], "What did you just say?")
        self.assertEqual(result["removed"], 2)
        self.assertEqual(self.ids(), ["m1", "m2"])
        self.assertEqual(result["redo"], {"available": True, "messages": 2, "depth": 1})
        restored = rewind.redo(self.bot)
        self.assertEqual(restored["restored"], 2)
        self.assertEqual(self.ids(), ["m1", "m2", "m3", "m4"])
        self.assertFalse(rewind.redo_state(self.bot)["available"])
        with self.assertRaises(rewind.RewindError):
            rewind.redo(self.bot)

    def test_edit_rewinds_to_before_an_earlier_message_and_undos_stack(self):
        self.exchange("one", "1")
        self.exchange("two", "2")
        self.exchange("three", "3")
        rewind.rewind(self.bot, "m3")  # edit "two": it and everything after go
        self.assertEqual(self.ids(), ["m1", "m2"])
        rewind.rewind(self.bot)  # then undo "one" as well
        self.assertEqual(self.ids(), [])
        self.assertEqual(rewind.redo_state(self.bot)["depth"], 2)
        rewind.redo(self.bot)
        self.assertEqual(self.ids(), ["m1", "m2"])
        rewind.redo(self.bot)
        self.assertEqual(self.ids(), ["m1", "m2", "m3", "m4", "m5", "m6"])

    def test_a_new_message_ends_the_redo_history(self):
        self.exchange("one", "1")
        self.exchange("two", "2")
        rewind.rewind(self.bot)
        self.add("user", "something else", item_type="user_text")  # e.g. a schedule posted, or another device sent
        self.assertFalse(rewind.redo_state(self.bot)["available"])
        with self.assertRaisesRegex(rewind.RewindError, "nothing to redo"):
            rewind.redo(self.bot)
        # Through the send route the stack is cleared outright.
        rewind.rewind(self.bot)
        self.assertTrue(rewind.redo_state(self.bot)["available"])
        asyncio.run(chat_router.send_message(TurnRequest(thread_id=self.bot, bot_id=self.bot, user_text="a new branch")))
        self.assertEqual(self.storage.count_rewinds(self.bot), 0)

    def test_only_the_users_own_messages_after_the_latest_compaction_can_be_rewound(self):
        self.exchange("old question", "old answer")
        self.add("system", "Summary of the earlier conversation.", item_type="compaction", raw_payload={"covers": "m2"})
        self.exchange("new question", "new answer")
        self.add("user", "Daily digest", item_type="user_text", raw_payload={"scheduled": {"name": "digest", "at": "09:00"}})
        self.add("user", "Research this", item_type="user_text", raw_payload={"delegated": {"from_name": "Cubie"}})
        history = self.storage.get_messages(thread_id=self.bot)
        self.assertEqual(rewind.editable_ids(history), ["m4"])
        with self.assertRaisesRegex(rewind.RewindError, "compaction"):
            rewind.rewind(self.bot, "m1")
        with self.assertRaisesRegex(rewind.RewindError, "your own messages"):
            rewind.rewind(self.bot, "m5")
        with self.assertRaisesRegex(rewind.RewindError, "your own messages"):
            rewind.rewind(self.bot, "m6")
        with self.assertRaisesRegex(rewind.RewindError, "not in this conversation"):
            rewind.rewind(self.bot, "m99")
        # /undo picks the newest message the user typed, skipping the schedule's and Cubie's.
        result = rewind.rewind(self.bot)
        self.assertEqual(result["message"]["id"], "m4")
        self.assertEqual(self.ids(), ["m1", "m2", "m3"])

    def test_the_preview_lists_what_stays_done_and_leaves_out_reads(self):
        calls = [
            {"id": "c1", "name": "computer_screenshot", "status": "completed"},
            {"id": "c2", "name": "computer_terminal_execute", "status": "completed", "arguments": {"command": "npm install left-pad"}},
            {"id": "c3", "name": "computer_write_file", "status": "completed", "raw_arguments": "{\"path\": \"/workspace/report.md\", \"content\": \"x\"}"},
            {"id": "c4", "name": "gmail_fetch_emails", "status": "completed"},
            {"id": "c5", "name": "gmail_send_email", "status": "completed", "arguments": {"recipient_email": "boss@example.com"}},
            {"id": "c6", "name": "computer_terminal_execute", "status": "rejected", "arguments": {"command": "rm -rf /"}},
            {"id": "c7", "name": "memory_edit", "status": "completed"},
        ]
        self.exchange("Do the thing", "Done.", calls)
        info = rewind.preview(self.bot)
        self.assertEqual(info["message_id"], "m1")
        self.assertEqual(info["messages"], 2)
        self.assertEqual(
            info["actions"],
            [
                {"tool": "computer_terminal_execute", "summary": "npm install left-pad"},
                {"tool": "computer_write_file", "summary": "/workspace/report.md"},
                {"tool": "gmail_send_email", "summary": "boss@example.com"},
            ],
        )
        self.assertTrue(info["notes_changed"])
        self.assertFalse(info["todo_changed"])

    def test_notes_and_to_do_changed_in_the_span_are_shown_again_after_the_rewind(self):
        bots = self.storage.get_bots()
        bots[0]["memory"] = "Goal: ship v2."
        bots[0]["todo"] = [{"id": "t1", "text": "write tests", "state": "doing"}]
        self.storage.save_bots(bots)
        self.exchange("hello", "hi")
        self.exchange("plan it", "Planned.", [
            {"id": "c1", "name": "memory_write", "status": "completed"},
            {"id": "c2", "name": "todo_add", "status": "completed"},
        ])
        result = rewind.rewind(self.bot)
        self.assertEqual(result["resynced"], {"notes": True, "todo": True})
        rows = self.storage.get_messages(thread_id=self.bot)
        self.assertEqual([m["item_type"] for m in rows[2:]], ["memory", "todo"])
        self.assertEqual(rows[2]["raw_payload"]["by"], "rewind")
        model = build_history_messages(rows, replay_tools=True)
        texts = [m["content"] for m in model if m["role"] == "user"]
        self.assertTrue(texts[1].startswith("The user rewound the conversation"))
        self.assertIn("Goal: ship v2.", texts[1])
        self.assertIn("write tests", texts[2])
        # Redo takes the resync rows away again and puts the span back.
        rewind.redo(self.bot)
        self.assertEqual(self.ids(), ["m1", "m2", "m3", "m4"])
        # A span without changes adds nothing.
        rewind.rewind(self.bot)
        rewind.redo(self.bot)
        rewind.rewind(self.bot, "m1")
        self.assertEqual([m["item_type"] for m in self.storage.get_messages(thread_id=self.bot)], ["memory", "todo"])

    def test_recall_forgets_rewound_messages(self):
        self.exchange("The launch code word is pineapple", "Noted.")
        for message in self.storage.get_messages(thread_id=self.bot):
            asyncio.run(retrieval.index_text("message", f"{self.bot}:{message['id']}", message["text"], config=CONFIG, storage=self.storage, embed=fake_embed))
        index = retrieval.index_for(self.storage)
        self.assertEqual(index.count(kind="message", ref_prefix=f"{self.bot}:"), 2)
        with mock.patch.object(retrieval, "index_for", return_value=index):
            rewind.rewind(self.bot)
        self.assertEqual(index.count(kind="message", ref_prefix=f"{self.bot}:"), 0)


class RewindRouteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.storage = StorageService(Path(self.temp.name))
        self.manager = TurnManager()
        self.patches = [
            mock.patch.object(storage_module, "storage_service", self.storage),
            mock.patch.object(chat_router, "turn_manager", self.manager),
        ]
        for p in self.patches:
            p.start()
        self.bot = self.storage.get_bots()[0]["id"]
        self.storage.delete_messages(self.bot)

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        try:
            self.temp.cleanup()
        except PermissionError:
            pass

    def test_editing_mid_reply_stops_the_reply_first_and_takes_its_partial_text_too(self):
        storage = self.storage
        bot = self.bot

        async def scenario():
            await chat_router.send_message(TurnRequest(thread_id=bot, bot_id=bot, user_text="Summarise the report"))

            async def runner(turn):
                try:
                    await asyncio.sleep(30)
                except asyncio.CancelledError:
                    storage.add_message({"id": "m-partial", "thread_id": bot, "bot_id": bot, "sender": "bot", "text": "The report says", "created_at": "2026-10-23T10:00:05"})
                    raise

            self.manager.start(bot, bot, "m", runner)
            await asyncio.sleep(0.05)
            preview = await chat_router.rewind_preview(bot)
            result = await chat_router.rewind_thread(bot, chat_router.RewindRequest())
            return preview, result

        preview, result = asyncio.run(scenario())
        self.assertEqual(preview["messages"], 1)  # the partial reply is not stored until the stop
        self.assertEqual(result["message"]["text"], "Summarise the report")
        self.assertEqual(result["removed"], 2)
        self.assertEqual(storage.get_messages(thread_id=bot), [])
        self.assertTrue(asyncio.run(chat_router.redo_status(bot))["available"])
        self.assertEqual(asyncio.run(chat_router.redo_thread(bot))["restored"], 2)
        with self.assertRaises(HTTPException) as caught:
            asyncio.run(chat_router.redo_thread(bot))
        self.assertEqual(caught.exception.status_code, 409)
        with self.assertRaises(HTTPException):
            asyncio.run(chat_router.rewind_thread("bot-nobody", chat_router.RewindRequest()))

    def test_clear_also_forgets_the_redo_history(self):
        self.storage.add_message({"id": "m1", "thread_id": self.bot, "bot_id": self.bot, "sender": "user", "text": "hi", "created_at": "2026-10-23T10:00:00"})
        asyncio.run(chat_router.rewind_thread(self.bot, chat_router.RewindRequest(message_id="m1")))
        self.assertEqual(self.storage.count_rewinds(self.bot), 1)
        asyncio.run(chat_router.clear_history(self.bot))
        self.assertEqual(self.storage.count_rewinds(self.bot), 0)


if __name__ == "__main__":
    unittest.main()
