import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.services import bot_memory
from app.services.action_gateway import ActionGateway, ActionInvocation
from app.services.bot_memory import MEMORY_MAX_CHARS, MemoryError, clean_memory, get_memory, register_memory_actions, set_memory
from app.services.llm_tools import MEMORY_TOOLS, available_tools, build_invocation, describe_tools
from app.services.storage_service import StorageService
from app.services.workspace_service import WorkspaceService


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

    def test_gateway_actions_read_and_write_without_approval(self):
        gateway = ActionGateway(workspace=WorkspaceService(Path(self.temp_dir.name)), approvals=_Broker(), audit=_Audit())
        register_memory_actions(gateway)
        with mock.patch.object(bot_memory, "storage_service", self.storage):
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

            bad = ActionInvocation(name="memory.write", arguments={"text": "x"}, target={}, preview="Write notes with no bot id")
            request, _ = gateway.open("t", self.bot["id"], bad)
            failed = asyncio.run(gateway.execute(request))
            self.assertNotEqual(failed.status, "completed")

    def test_memory_tools_are_offered_and_described(self):
        specs = available_tools(computer=False)
        names = [s.name for s in specs]
        self.assertIn("memory_read", names)
        self.assertIn("memory_write", names)
        self.assertTrue(all(s.group == "memory" and not s.needs_approval for s in MEMORY_TOOLS))
        self.assertIn("Your notes", describe_tools(specs))
        invocation = build_invocation("memory_write", {"text": "n" * 20}, "bot-9")
        self.assertEqual(invocation.arguments, {"bot_id": "bot-9", "text": "n" * 20})
        self.assertIn("20 characters", invocation.preview)


if __name__ == "__main__":
    unittest.main()
