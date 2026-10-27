import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.services import bot_memory
from app.services.action_gateway import ActionGateway, ActionInvocation
from app.services.bot_memory import MEMORY_MAX_CHARS, MemoryError, clean_memory, edit_memory, get_memory, register_memory_actions, set_memory
from app.services.llm_tools import MEMORY_TOOLS, ToolCallError, available_tools, build_invocation, describe_tools
from app.services.storage_service import StorageService
from app.services.workspace_service import WorkspaceService
from app.services import storage_service as storage_module


class _Audit:
    def add_audit_event(self, event):
        pass


class _Broker:
    def __init__(self):
        self.opened = []

    def open(self, request):
        self.opened.append(request)
        return {"request_id": request.request_id}

    async def wait(self, request_id):
        return "allow"


class BotMemoryTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.storage = StorageService(Path(self.temp_dir.name))
        self.bot = self.storage.get_bots()[0]

    def tearDown(self):
        try:
            self.temp_dir.cleanup()
        except PermissionError:
            pass  # Windows keeps the SQLite file open a moment longer.

    def test_notes_are_stored_on_the_bot_and_bounded(self):
        self.assertEqual(get_memory(self.bot["id"], self.storage), "")
        result = set_memory(self.bot["id"], "  Goal: ship it.\r\nRepo: ~/proj  ", self.storage)
        self.assertEqual(result["memory"], "Goal: ship it.\nRepo: ~/proj")
        self.assertEqual(result["chars"], len(result["memory"]))
        self.assertEqual(get_memory(self.bot["id"], self.storage), "Goal: ship it.\nRepo: ~/proj")
        stored = next(b for b in self.storage.get_bots() if b["id"] == self.bot["id"])
        self.assertTrue(stored["memory_updated_at"])

        with self.assertRaisesRegex(MemoryError, "limited to"):
            clean_memory("x" * (MEMORY_MAX_CHARS + 1))
        with self.assertRaisesRegex(MemoryError, "must be text"):
            clean_memory({"not": "text"})
        with self.assertRaisesRegex(MemoryError, "does not exist"):
            set_memory("nobody", "x", self.storage)
        self.assertEqual(clean_memory(None), "")

    def test_edit_replaces_one_unique_passage_and_reports_only_that_passage(self):
        bot = self.bot["id"]
        with self.assertRaisesRegex(MemoryError, "empty"):
            edit_memory(bot, "a", "b", self.storage)
        notes = "# Project\nGoal: ship it.\nRepo: ~/proj\n\n# Decisions\n- sqlite for storage\n- no ORM\n\n# How to run\nmake dev"
        set_memory(bot, notes, self.storage)

        result = edit_memory(bot, "Goal: ship it.", "Goal: ship it by Friday.", self.storage)
        self.assertEqual(get_memory(bot, self.storage), notes.replace("Goal: ship it.", "Goal: ship it by Friday."))
        self.assertEqual(result["chars"], len(get_memory(bot, self.storage)))
        self.assertNotIn("memory", result)  # the whole document is not echoed
        self.assertIn("Goal: ship it by Friday.", result["excerpt"])
        self.assertNotIn("make dev", result["excerpt"])  # far away from the change
        self.assertTrue(result["excerpt"].endswith("..."))
        self.assertTrue(result["updated_at"])

        # Removing a passage, with Windows line endings in the arguments.
        result = edit_memory(bot, "- no ORM\r\n", "", self.storage)
        self.assertNotIn("no ORM", get_memory(bot, self.storage))
        self.assertIn("sqlite for storage", result["excerpt"])

        with self.assertRaisesRegex(MemoryError, "not in the notes"):
            edit_memory(bot, "Goal: ship it.", "x", self.storage)
        with self.assertRaisesRegex(MemoryError, "appears 2 times"):
            edit_memory(bot, "\n# ", "\n## ", self.storage)
        with self.assertRaisesRegex(MemoryError, "exact passage"):
            edit_memory(bot, "   ", "x", self.storage)
        with self.assertRaisesRegex(MemoryError, "must be text"):
            edit_memory(bot, "make dev", 3, self.storage)
        with self.assertRaisesRegex(MemoryError, "limited to"):
            edit_memory(bot, "make dev", "x" * MEMORY_MAX_CHARS, self.storage)
        # Failed edits leave the notes alone.
        self.assertIn("make dev", get_memory(bot, self.storage))

        # An edit at the very start keeps the excerpt anchored after the strip.
        edit_memory(bot, "# Project", "   # Project (renamed)", self.storage)
        self.assertTrue(get_memory(bot, self.storage).startswith("# Project (renamed)"))

    def test_gateway_actions_read_and_write_without_approval(self):
        gateway = ActionGateway(workspace=WorkspaceService(Path(self.temp_dir.name)), approvals=_Broker(), audit=_Audit())
        register_memory_actions(gateway)
        with mock.patch.object(storage_module, "storage_service", self.storage):
            write = build_invocation("memory_write", {"text": "Decisions: use sqlite."}, self.bot["id"])
            self.assertEqual(write.name, "memory.write")
            request, approval = gateway.open("t", self.bot["id"], write)
            self.assertIsNone(approval)
            result = asyncio.run(gateway.execute(request))
            self.assertEqual(result.status, "completed")
            self.assertEqual(result.result["memory"], "Decisions: use sqlite.")
            self.assertEqual(result.result["limit"], MEMORY_MAX_CHARS)

            read = build_invocation("memory_read", {}, self.bot["id"])
            request, approval = gateway.open("t", self.bot["id"], read)
            self.assertIsNone(approval)
            self.assertEqual(asyncio.run(gateway.execute(request)).result["memory"], "Decisions: use sqlite.")

            edit = build_invocation("memory_edit", {"old_text": "sqlite", "new_text": "sqlite (WAL mode)"}, self.bot["id"])
            self.assertEqual(edit.name, "memory.edit")
            self.assertIn("6 characters become 17", edit.preview)
            request, approval = gateway.open("t", self.bot["id"], edit)
            self.assertIsNone(approval)
            result = asyncio.run(gateway.execute(request))
            self.assertEqual(result.status, "completed")
            self.assertEqual(result.result["excerpt"], "Decisions: use sqlite (WAL mode).")
            self.assertEqual(result.result["limit"], MEMORY_MAX_CHARS)
            self.assertEqual(get_memory(self.bot["id"], self.storage), "Decisions: use sqlite (WAL mode).")

            missing = build_invocation("memory_edit", {"old_text": "postgres", "new_text": ""}, self.bot["id"])
            request, _ = gateway.open("t", self.bot["id"], missing)
            failed = asyncio.run(gateway.execute(request))
            self.assertNotEqual(failed.status, "completed")
            self.assertIn("not in the notes", str(failed.error))

            bad = ActionInvocation(name="memory.write", arguments={"text": "x"}, target={}, preview="Write notes with no bot id")
            request, _ = gateway.open("t", self.bot["id"], bad)
            failed = asyncio.run(gateway.execute(request))
            self.assertNotEqual(failed.status, "completed")

    def test_memory_tools_are_offered_and_described(self):
        specs = available_tools(computer=False)
        names = [s.name for s in specs]
        self.assertIn("memory_read", names)
        self.assertIn("memory_write", names)
        self.assertIn("memory_edit", names)
        self.assertTrue(all(s.group == "memory" and not s.needs_approval for s in MEMORY_TOOLS))
        described = describe_tools(specs)
        self.assertIn("Your notes", described)
        self.assertIn("memory_edit rather than rewriting", described)
        write_spec = next(s for s in MEMORY_TOOLS if s.name == "memory_write")
        self.assertIn(f"at most {MEMORY_MAX_CHARS} characters", write_spec.description)
        with self.assertRaises(ToolCallError):
            build_invocation("memory_edit", {"old_text": "  ", "new_text": "x"}, "bot-9")
        with self.assertRaises(ToolCallError):
            build_invocation("memory_write", {"text": "x" * (MEMORY_MAX_CHARS + 1)}, "bot-9")
        invocation = build_invocation("memory_write", {"text": "n" * 20}, "bot-9")
        self.assertEqual(invocation.arguments, {"bot_id": "bot-9", "text": "n" * 20, "thread_id": "bot-9"})
        self.assertIn("20 characters", invocation.preview)


if __name__ == "__main__":
    unittest.main()
