import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.services import bot_todo
from app.services.action_gateway import ActionGateway, ActionInvocation
from app.services.bot_todo import TODO_MAX_ITEMS, TodoError, add_todo, clean_todo, get_todo, register_todo_actions, render_todo, set_todo, update_todo
from app.services.llm_tools import TODO_TOOLS, ToolCallError, available_tools, build_invocation, describe_tools
from app.services.storage_service import StorageService
from app.services.workspace_service import WorkspaceService
from app.services import storage_service as storage_module


class _Audit:
    def add_audit_event(self, event):
        pass


class _Broker:
    def open(self, request):
        return {"request_id": request.request_id}

    async def wait(self, request_id):
        return "allow"


class BotTodoTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.storage = StorageService(Path(self.temp_dir.name))
        self.bot = self.storage.get_bots()[0]["id"]

    def tearDown(self):
        try:
            self.temp_dir.cleanup()
        except PermissionError:
            pass  # Windows keeps the SQLite file open a moment longer.

    def test_items_are_cleaned_numbered_and_bounded(self):
        items = clean_todo(["  write   tests ", {"text": "ship", "state": "DOING"}, {"id": "t9", "text": "docs"}, {"id": "t9", "text": "dup id"}])
        self.assertEqual(
            items,
            [
                {"id": "t1", "text": "write tests", "state": "open"},
                {"id": "t2", "text": "ship", "state": "doing"},
                {"id": "t9", "text": "docs", "state": "open"},
                {"id": "t4", "text": "dup id", "state": "open"},
            ],
        )
        self.assertEqual(clean_todo(None), [])
        with self.assertRaisesRegex(TodoError, "cannot be empty"):
            clean_todo(["   "])
        with self.assertRaisesRegex(TodoError, "limited to 300"):
            clean_todo(["x" * 301])
        with self.assertRaisesRegex(TodoError, "open, doing or done"):
            clean_todo([{"text": "x", "state": "later"}])
        with self.assertRaisesRegex(TodoError, "must be a list"):
            clean_todo("not a list")
        with self.assertRaisesRegex(TodoError, f"limited to {TODO_MAX_ITEMS} items"):
            clean_todo(["x"] * (TODO_MAX_ITEMS + 1))
        self.assertEqual(render_todo([]), "(empty)")
        self.assertEqual(render_todo(items[:2]), "- [ ] t1: write tests\n- [~] t2: ship")

    def test_add_update_and_remove_keep_ids_stable(self):
        self.assertEqual(get_todo(self.bot, self.storage), [])
        result = add_todo(self.bot, ["read the code", "write the fix"], self.storage)
        self.assertEqual(result["added"], ["t1", "t2"])
        self.assertEqual((result["open"], result["done"], result["limit"]), (2, 0, TODO_MAX_ITEMS))

        result = update_todo(self.bot, "t1", state="done", storage=self.storage)
        self.assertEqual(result["updated"], "t1")
        self.assertEqual([i["state"] for i in result["items"]], ["done", "open"])
        self.assertEqual((result["open"], result["done"]), (1, 1))

        result = update_todo(self.bot, "t2", text="write the fix and a test", storage=self.storage)
        self.assertEqual(result["items"][1]["text"], "write the fix and a test")

        result = update_todo(self.bot, "t1", remove=True, storage=self.storage)
        self.assertEqual(result["removed"], "t1")
        self.assertEqual([i["id"] for i in result["items"]], ["t2"])
        # A new item does not reuse a live id; it takes the next free number.
        result = add_todo(self.bot, "run the suite", self.storage)
        self.assertEqual([i["id"] for i in result["items"]], ["t2", "t3"])
        self.assertTrue(next(b for b in self.storage.get_bots() if b["id"] == self.bot)["todo_updated_at"])

        with self.assertRaisesRegex(TodoError, "no to-do item 't7'"):
            update_todo(self.bot, "t7", state="done", storage=self.storage)
        with self.assertRaisesRegex(TodoError, "Give a new state"):
            update_todo(self.bot, "t2", storage=self.storage)
        with self.assertRaisesRegex(TodoError, "one or more item texts"):
            add_todo(self.bot, [], self.storage)
        with self.assertRaisesRegex(TodoError, "remove finished ones"):
            add_todo(self.bot, ["x"] * (TODO_MAX_ITEMS - 1), self.storage)
        with self.assertRaisesRegex(TodoError, "does not exist"):
            add_todo("nobody", ["x"], self.storage)

        replaced = set_todo(self.bot, [{"text": "only this", "state": "doing"}], self.storage)
        self.assertEqual(replaced["items"], [{"id": "t1", "text": "only this", "state": "doing"}])

    def test_gateway_actions_run_without_approval_and_return_the_whole_list(self):
        gateway = ActionGateway(workspace=WorkspaceService(Path(self.temp_dir.name)), approvals=_Broker(), audit=_Audit())
        register_todo_actions(gateway)
        with mock.patch.object(storage_module, "storage_service", self.storage):
            add = build_invocation("todo_add", {"items": ["a", " b "]}, self.bot)
            self.assertEqual(add.name, "todo.add")
            self.assertEqual(add.arguments, {"bot_id": self.bot, "items": ["a", "b"]})
            request, approval = gateway.open("t", self.bot, add)
            self.assertIsNone(approval)
            result = asyncio.run(gateway.execute(request))
            self.assertEqual(result.status, "completed")
            self.assertEqual(result.result["added"], ["t1", "t2"])
            self.assertEqual(len(result.result["items"]), 2)

            done = build_invocation("todo_update", {"id": "t1", "state": "Done"}, self.bot)
            self.assertEqual(done.arguments, {"bot_id": self.bot, "id": "t1", "remove": False, "state": "done"})
            request, approval = gateway.open("t", self.bot, done)
            self.assertIsNone(approval)
            result = asyncio.run(gateway.execute(request))
            self.assertEqual(result.result["items"][0]["state"], "done")

            listing = build_invocation("todo_list", {}, self.bot)
            request, _ = gateway.open("t", self.bot, listing)
            self.assertEqual(asyncio.run(gateway.execute(request)).result["open"], 1)

            gone = build_invocation("todo_update", {"id": "t2", "remove": True}, self.bot)
            self.assertIn("Remove to-do item t2", gone.preview)
            request, _ = gateway.open("t", self.bot, gone)
            self.assertEqual(asyncio.run(gateway.execute(request)).result["removed"], "t2")

            missing = build_invocation("todo_update", {"id": "t9", "state": "done"}, self.bot)
            request, _ = gateway.open("t", self.bot, missing)
            failed = asyncio.run(gateway.execute(request))
            self.assertNotEqual(failed.status, "completed")
            self.assertIn("no to-do item", str(failed.error))

            bad = ActionInvocation(name="todo.list", arguments={}, target={}, preview="no bot")
            request, _ = gateway.open("t", self.bot, bad)
            self.assertNotEqual(asyncio.run(gateway.execute(request)).status, "completed")

    def test_todo_tools_are_offered_described_and_validated(self):
        specs = available_tools(computer=False)
        names = [s.name for s in specs]
        for name in ("todo_list", "todo_add", "todo_update"):
            self.assertIn(name, names)
        self.assertTrue(all(s.group == "todo" and not s.needs_approval for s in TODO_TOOLS))
        described = describe_tools(specs)
        self.assertIn("Your to-do list", described)
        self.assertIn("todo_add the steps first", described)
        self.assertNotIn("Your to-do list", describe_tools([s for s in specs if s.group != "todo"]))

        with self.assertRaisesRegex(ToolCallError, "one or more item texts"):
            build_invocation("todo_add", {"items": []}, "bot-9")
        with self.assertRaisesRegex(ToolCallError, "one or more item texts"):
            build_invocation("todo_add", {"items": ["ok", "  "]}, "bot-9")
        with self.assertRaisesRegex(ToolCallError, "open, doing or done"):
            build_invocation("todo_update", {"id": "t1", "state": "later"}, "bot-9")
        with self.assertRaisesRegex(ToolCallError, "Give a new state"):
            build_invocation("todo_update", {"id": "t1"}, "bot-9")
        with self.assertRaises(ToolCallError):
            build_invocation("todo_update", {"state": "done"}, "bot-9")
        single = build_invocation("todo_add", {"items": "just one"}, "bot-9")
        self.assertEqual(single.arguments["items"], ["just one"])
        self.assertIn("Add 1 item(s)", single.preview)


if __name__ == "__main__":
    unittest.main()
