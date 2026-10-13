import asyncio
import tempfile
import unittest
from pathlib import Path

from app.services.workspace_service import (
    WorkspaceService,
    WorkspaceToolError,
    parse_workspace_command,
)
from app.services.approval_broker import approval_broker

try:
    from app.main import app
except ModuleNotFoundError:
    app = None


class WorkspaceServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.service = WorkspaceService(self.root)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_read_write_and_list(self):
        (self.root / "readme.txt").write_text("hello", encoding="utf-8")

        read_call = parse_workspace_command("/shared read readme.txt")
        self.assertEqual(self.service.execute(read_call)["content"], "hello")

        write_call = parse_workspace_command("/shared write new.txt\nworld")
        self.assertTrue(self.service.execute(write_call)["written"])
        self.assertEqual((self.root / "new.txt").read_text(encoding="utf-8"), "world")

        list_call = parse_workspace_command("/shared list")
        names = {entry["name"] for entry in self.service.execute(list_call)["entries"]}
        self.assertEqual(names, {"readme.txt", "new.txt"})

    def test_write_creates_missing_folders_inside_the_workspace(self):
        call = parse_workspace_command("/shared write notes/2026/todo.txt\nremember")
        result = self.service.execute(call)
        self.assertTrue(result["written"])
        self.assertTrue(result["created_directories"])
        self.assertEqual((self.root / "notes" / "2026" / "todo.txt").read_text(encoding="utf-8"), "remember")

        # But never outside the root, even through a new folder name.
        escape = parse_workspace_command("/shared write ../escaped/x.txt\nno")
        with self.assertRaises(WorkspaceToolError):
            self.service.execute(escape)
        self.assertFalse((self.root.parent / "escaped").exists())

    def test_paths_cannot_escape_workspace(self):
        call = parse_workspace_command("/shared read ../outside")
        with self.assertRaises(WorkspaceToolError):
            self.service.execute(call)

    def test_invalid_command_is_rejected(self):
        with self.assertRaises(WorkspaceToolError):
            parse_workspace_command("/shared execute rm -rf .")

    def test_delete_removes_a_file_or_a_whole_folder_but_never_the_root(self):
        (self.root / "old.txt").write_text("x", encoding="utf-8")
        (self.root / "build" / "out").mkdir(parents=True)
        (self.root / "build" / "out" / "a.js").write_text("1", encoding="utf-8")
        (self.root / "build" / "b.js").write_text("2", encoding="utf-8")

        result = self.service.execute(parse_workspace_command("/shared delete old.txt"))
        self.assertEqual(result, {"path": "old.txt", "deleted": True, "kind": "file"})
        self.assertFalse((self.root / "old.txt").exists())

        result = self.service.execute(parse_workspace_command("/shared delete build"))
        self.assertEqual(result, {"path": "build", "deleted": True, "kind": "directory", "entries": 3})
        self.assertFalse((self.root / "build").exists())

        with self.assertRaisesRegex(WorkspaceToolError, "does not exist"):
            self.service.execute(parse_workspace_command("/shared delete build"))
        with self.assertRaisesRegex(WorkspaceToolError, "cannot be deleted"):
            self.service.execute(parse_workspace_command("/shared delete ."))
        with self.assertRaises(WorkspaceToolError):
            self.service.execute(parse_workspace_command("/shared delete ../elsewhere"))

    def test_write_replaces_an_existing_file_in_place(self):
        (self.root / "report.md").write_text("old", encoding="utf-8")
        self.service.execute(parse_workspace_command("/shared write report.md\nnew"))
        self.assertEqual((self.root / "report.md").read_text(encoding="utf-8"), "new")
        # No staging file is left beside it.
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ["report.md"])


class ApiRouteTests(unittest.TestCase):
    @unittest.skipIf(app is None, "FastAPI dependencies are not installed")
    def test_audit_route_is_registered(self):
        self.assertIn("/api/v1/audit", app.openapi()["paths"])


class ApprovalBrokerTests(unittest.IsolatedAsyncioTestCase):
    async def test_allow_resolves_pending_request(self):
        call = parse_workspace_command("/shared read README.md")
        approval = approval_broker.open("thread-test", "bot-test", call)
        waiting = asyncio.create_task(approval_broker.wait(approval["request_id"]))
        await asyncio.sleep(0)

        self.assertTrue(approval_broker.resolve(approval["request_id"], "allow"))
        self.assertEqual(await waiting, "allow")


if __name__ == "__main__":
    unittest.main()
