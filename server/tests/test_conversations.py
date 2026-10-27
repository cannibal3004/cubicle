"""A bot's conversations: separate histories and to-do lists, shared notes, announced changes."""

import asyncio
import unittest
from unittest import mock

from fastapi import HTTPException

from app.routers import chat as chat_router
from app.routers import conversations as conversations_router
from app.schemas.contracts import TurnRequest
from app.services import bot_memory, conversations
from app.services import storage_service as storage_module
from app.services.action_gateway import ActionInvocation
from app.services.bot_todo import get_todo
from app.services.context_builder import MEMORY_ELSEWHERE_NOTE, MEMORY_START_LEAD

import test_chat_router as chat_harness
from test_chat_router import _scripted_stream


def _notes_call(name, bot_id, thread_id, **arguments):
    return ActionInvocation(name=name, arguments={"bot_id": bot_id, "thread_id": thread_id, **arguments}, target={"bot_id": bot_id}, preview="")


class ConversationTests(chat_harness.ChatStreamRouteTests):
    def setUp(self):
        super().setUp()
        self.bot = self.storage.get_bots()[0]["id"]
        self.storage.delete_messages(self.bot)
        self.patch = mock.patch.object(storage_module, "storage_service", self.storage)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        super().tearDown()

    def _reply(self, thread_id, text):
        recorder = {}
        stream = _scripted_stream(recorder, [[{"type": "content.delta", "delta": text}, {"type": "turn.completed", "ok": True}]])
        self._run_turn(thread_id, stream=stream)
        return recorder

    def test_each_conversation_has_its_own_history_and_to_do_list_and_starts_with_the_notes(self):
        bots = self.storage.get_bots()
        bots[0]["memory"] = "Goal: keep the NAS healthy."
        self.storage.save_bots(bots)
        self.storage.add_message({"id": "u1", "thread_id": self.bot, "bot_id": self.bot, "sender": "user", "text": "first conversation question"})
        second = conversations.create(self.bot)
        self.storage.add_message({"id": "u2", "thread_id": second["id"], "bot_id": self.bot, "sender": "user", "text": "second conversation question"})

        recorder = self._reply(second["id"], "Second answer.")
        sent = "\n".join(str(m.get("content")) for m in recorder["rounds"][0]["messages"])
        self.assertIn("second conversation question", sent)
        self.assertNotIn("first conversation question", sent)
        self.assertIn(MEMORY_START_LEAD, sent)  # a new conversation starts with the bot's notes
        self.assertIn("Goal: keep the NAS healthy.", sent)
        reply = self.storage.get_messages(second["id"])[-1]
        self.assertEqual((reply["sender"], reply["thread_id"], reply["bot_id"], reply["text"]), ("bot", second["id"], self.bot, "Second answer."))
        self.assertEqual([m["id"] for m in self.storage.get_messages(self.bot)], ["u1"])

        # The to-do list is the conversation's.
        asyncio.run(conversations_router.update_conversation(second["id"], conversations_router.ConversationUpdate(todo=[{"text": "check the disks"}])))
        self.assertEqual([i["text"] for i in get_todo(second["id"])], ["check the disks"])
        self.assertEqual(get_todo(self.bot), [])
        self.assertEqual(self.storage.get_messages(second["id"])[-1]["item_type"], "todo")

    def test_a_message_goes_to_the_conversations_bot_and_names_the_conversation(self):
        record = conversations.create(self.bot)
        request = TurnRequest(thread_id=record["id"], bot_id="someone-else", user_text="Check the backup jobs on the NAS tonight and tell me which ones failed")
        stored = asyncio.run(chat_router.send_message(request))["message"]
        self.assertEqual(stored["bot_id"], self.bot)
        self.assertEqual(conversations.get(record["id"])["title"], "Check the backup jobs on the NAS tonight and tell me which…")
        # A later message does not rename it.
        asyncio.run(chat_router.send_message(TurnRequest(thread_id=record["id"], bot_id=self.bot, user_text="And the disks")))
        self.assertTrue(conversations.get(record["id"])["title"].startswith("Check the backup jobs"))
        with self.assertRaises(HTTPException):
            asyncio.run(chat_router.send_message(TurnRequest(thread_id="cv-nope", bot_id=self.bot, user_text="hi")))

    def test_a_notes_change_is_announced_once_in_the_other_conversations(self):
        self.storage.add_message({"id": "u1", "thread_id": self.bot, "bot_id": self.bot, "sender": "user", "text": "hi"})
        other = conversations.create(self.bot)
        self.storage.add_message({"id": "u2", "thread_id": other["id"], "bot_id": self.bot, "sender": "user", "text": "hello"})
        idle = conversations.create(self.bot)  # nothing said there yet: it will start with the notes anyway

        def told(thread_id):
            return [m for m in self.storage.get_messages(thread_id) if (m.get("raw_payload") or {}).get("by") == "elsewhere"]

        asyncio.run(bot_memory.execute_write(_notes_call("memory.write", self.bot, self.bot, text="v1")))
        self.assertEqual(len(told(other["id"])), 1)
        self.assertEqual((told(self.bot), told(idle["id"])), ([], []))
        asyncio.run(bot_memory.execute_edit(_notes_call("memory.edit", self.bot, self.bot, old_text="v1", new_text="v2")))
        self.assertEqual(len(told(other["id"])), 1)  # not again until it has read them
        asyncio.run(bot_memory.execute_read(_notes_call("memory.read", self.bot, other["id"])))
        asyncio.run(bot_memory.execute_write(_notes_call("memory.write", self.bot, self.bot, text="v3")))
        self.assertEqual(len(told(other["id"])), 2)
        rendered = chat_harness.build_history_messages(self.storage.get_messages(other["id"]), replay_tools=True)
        self.assertTrue(any(MEMORY_ELSEWHERE_NOTE in str(m.get("content")) for m in rendered))

    def test_conversations_are_listed_started_renamed_archived_and_deleted(self):
        self.storage.add_message({"id": "u1", "thread_id": self.bot, "bot_id": self.bot, "sender": "user", "text": "How full is the array?"})
        listed = asyncio.run(conversations_router.list_conversations(self.bot))
        self.assertEqual([c["id"] for c in listed], [self.bot])
        self.assertEqual(listed[0]["title"], "How full is the array?")  # the first one is named after its first message

        started = asyncio.run(conversations_router.start_conversation(self.bot, conversations_router.ConversationInput(title="Nextcloud")))
        self.assertTrue(started["id"].startswith("cv-"))
        renamed = asyncio.run(conversations_router.update_conversation(started["id"], conversations_router.ConversationUpdate(title="  Nextcloud   updates ")))
        self.assertEqual(renamed["title"], "Nextcloud updates")
        archived = asyncio.run(conversations_router.update_conversation(started["id"], conversations_router.ConversationUpdate(archived=True)))
        self.assertTrue(archived["archived"])
        self.assertEqual(len(asyncio.run(conversations_router.list_conversations(self.bot))), 2)
        self.assertEqual([m["id"] for m in asyncio.run(chat_router.get_history(started["id"]))], [])

        asyncio.run(conversations_router.delete_conversation(started["id"]))
        with self.assertRaises(HTTPException):
            asyncio.run(conversations_router.get_conversation(started["id"]))
        # The first conversation is emptied, not removed: it is where the bot starts.
        asyncio.run(conversations_router.delete_conversation(self.bot))
        self.assertEqual(self.storage.get_messages(self.bot), [])
        self.assertEqual([c["id"] for c in asyncio.run(conversations_router.list_conversations(self.bot))], [self.bot])
        with self.assertRaises(HTTPException):
            asyncio.run(conversations_router.list_conversations("bot-nope"))

    def test_scheduled_and_orchestrated_work_get_conversations_of_their_own(self):
        scheduled = conversations.for_purpose(self.bot, "scheduled")
        self.assertEqual((scheduled["title"], conversations.for_purpose(self.bot, "scheduled")["id"]), ("Scheduled", scheduled["id"]))
        self.assertEqual(conversations.bot_id_of(scheduled["id"]), self.bot)
        self.assertIsNone(conversations.bot_id_of("cv-unknown"))


class ComputerTurnTests(unittest.TestCase):
    def test_changes_to_one_computer_take_turns_and_reads_do_not(self):
        from app.services import computer_turns

        order, waits = [], []

        async def change(thread_id, label, hold):
            async with computer_turns.computer_turn("bot-x", thread_id, label, on_wait=waits.append):
                order.append(f"start {label}")
                await asyncio.sleep(hold)
                order.append(f"end {label}")

        async def other_bot():
            async with computer_turns.computer_turn("bot-y", "bot-y", "ls"):
                order.append("bot-y ran")

        async def scenario():
            first = asyncio.create_task(change("bot-x", "make build", 0.05))
            await asyncio.sleep(0.01)
            await asyncio.gather(change("cv-2", "rm tmp", 0), other_bot())
            await first

        asyncio.run(scenario())
        self.assertEqual(order, ["start make build", "bot-y ran", "end make build", "start rm tmp", "end rm tmp"])
        self.assertEqual(waits, [{"thread_id": "bot-x", "action": "make build"}])
        self.assertTrue(computer_turns.takes_turns("computer.terminal_execute"))
        self.assertFalse(computer_turns.takes_turns("computer.files_read"))
        self.assertFalse(computer_turns.takes_turns("computer.screenshot"))


for _name in [n for n in dir(chat_harness.ChatStreamRouteTests) if n.startswith("test_")]:
    if _name not in ConversationTests.__dict__:
        setattr(ConversationTests, _name, None)


if __name__ == "__main__":
    unittest.main()
