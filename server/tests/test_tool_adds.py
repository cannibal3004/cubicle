"""Tools offered partway through a stretch: with TOOL_ADD_STYLE=message the tool list holds and they are described in the message."""

import asyncio
import json
import unittest
from unittest import mock

from app.routers import bots as bots_router
from app.services import tool_selection
from app.services.llm_tools import ToolSpec

import test_chat_router as chat_harness


def spec(name, connector=None):
    return ToolSpec(name=name, description=f"{name} does a thing", parameters={"type": "object", "properties": {}}, group="memory", needs_approval=False, connector=connector)


class HoldListTests(unittest.TestCase):
    def setUp(self):
        self.a, self.b, self.c = spec("a"), spec("b"), spec("c")

    def test_the_first_turn_of_a_stretch_sends_what_it_is_offered_and_later_ones_hold_it(self):
        listed, added, record = tool_selection.hold_list([self.a, self.b], [self.a, self.b], kept=None, since="", note="n", history=[])
        self.assertEqual(([s.name for s in listed], added, record), (["a", "b"], [], {"since": "", "names": ["a", "b"], "note": "n"}))
        # c arrives: the list holds (catalog order) and c is described in the message.
        listed, added, kept = tool_selection.hold_list([self.a, self.c, self.b], [self.a, self.c, self.b], kept=record, since="", note="other", history=[])
        self.assertEqual(([s.name for s in listed], [s.name for s in added], kept), (["a", "b"], ["c"], record))
        # Once a message describes c it is not described again; a tool that is gone leaves the list.
        history = [{"id": "u1", "sender": "user", "text": "x", "raw_payload": {"tools_added": {"names": ["c"], "text": "..."}}}]
        listed, added, _ = tool_selection.hold_list([self.c, self.b], [self.c, self.b], kept=record, since="", note="", history=history)
        self.assertEqual(([s.name for s in listed], added), (["b"], []))
        # A compaction starts a new stretch with a new list.
        listed, added, fresh = tool_selection.hold_list([self.a, self.c], [self.a, self.c], kept=record, since="cmp-1", note="", history=history)
        self.assertEqual(([s.name for s in listed], added, fresh["since"]), (["a", "c"], [], "cmp-1"))

    def test_the_block_uses_the_templates_tools_notation(self):
        definition = {"type": "function", "function": {"name": "c", "description": "d", "parameters": {}}}
        block = tool_selection.render_added([definition])
        self.assertTrue(block.startswith(tool_selection.ADDED_LEAD))
        self.assertIn("<tools>\n" + json.dumps(definition) + "\n</tools>", block)


class AddedToolTurnTests(chat_harness.ChatStreamRouteTests):
    def setUp(self):
        super().setUp()
        self.bot = self.storage.get_bots()[0]["id"]
        self.patch = mock.patch.object(chat_harness.storage_module, "storage_service", self.storage)
        self.patch.start()
        self.sent = []

    def tearDown(self):
        self.patch.stop()
        super().tearDown()

    def _turn(self, text, todo_tools_on):
        settings = {} if todo_tools_on else {"groups": {"todo": False}}
        asyncio.run(bots_router.update_bot(self.bot, bots_router.BotUpdate(tool_settings=settings)))
        self.storage.add_message({"id": f"u{len(self.sent)}", "thread_id": self.bot, "bot_id": self.bot, "sender": "user", "text": text})

        async def stream(model, messages, system_prompt="", config=None, tools=None, one_shot=False):
            self.sent.append({"tools": [t["function"]["name"] for t in tools or []], "system": system_prompt, "messages": [m["content"] for m in messages]})
            yield {"type": "content.delta", "delta": "Done."}
            yield {"type": "turn.completed", "ok": True}

        self._run_turn(self.bot, stream=stream)
        return self.sent[-1]

    def test_a_tool_switched_on_partway_is_described_in_the_message_and_the_list_holds(self):
        with mock.patch.object(tool_selection.settings, "TOOL_ADD_STYLE", "message"):
            first = self._turn("hello", todo_tools_on=False)
            self.assertNotIn("todo_add", first["tools"])
            second = self._turn("add a to-do: buy milk", todo_tools_on=True)
            third = self._turn("and another: call mum", todo_tools_on=True)
        # The list and the system prompt are the ones the stretch began with.
        self.assertEqual(second["tools"], first["tools"])
        self.assertEqual((second["system"], third["system"]), (first["system"], first["system"]))
        # The to-do tools are described at the end of the message they arrived with...
        self.assertTrue(second["messages"][-1].startswith("add a to-do: buy milk\n\n" + tool_selection.ADDED_LEAD))
        self.assertIn('"name": "todo_add"', second["messages"][-1])
        self.assertTrue(second["messages"][-1].endswith("</tools>"))
        # ...and replayed there unchanged, not described again.
        self.assertEqual(third["messages"][-3], second["messages"][-1])
        self.assertEqual(third["messages"][-1], "and another: call mum")
        stored = {m["id"]: m for m in self.storage.get_messages(self.bot)}
        self.assertEqual(stored["u1"]["text"], "add a to-do: buy milk")
        self.assertIn("todo_add", stored["u1"]["raw_payload"]["tools_added"]["names"])

    def test_the_list_style_adds_to_the_list(self):
        first = self._turn("hello", todo_tools_on=False)
        second = self._turn("add a to-do: buy milk", todo_tools_on=True)
        self.assertNotIn("todo_add", first["tools"])
        self.assertIn("todo_add", second["tools"])
        self.assertEqual(second["messages"][-1], "add a to-do: buy milk")


if __name__ == "__main__":
    unittest.main()
