import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.routers import chat as chat_router
from app.routers.commands import get_commands
from app.services.commands import COMMANDS, CommandError, command_name, get_command, list_commands, parse_action_command
from app.services.storage_service import StorageService
from app.services.turn_manager import TurnManager


class CommandRegistryTests(unittest.TestCase):
    def test_names_are_unique_and_kinds_are_known(self):
        names = [spec.name for spec in COMMANDS]
        self.assertEqual(len(names), len(set(names)))
        self.assertTrue(all(spec.kind in ("client", "action") for spec in COMMANDS))
        self.assertTrue(all(spec.usage.startswith(f"/{spec.name}") for spec in COMMANDS))

    def test_listing_is_plain_json(self):
        listed = list_commands()
        self.assertEqual(len(listed), len(COMMANDS))
        self.assertEqual(set(listed[0]), {"name", "usage", "description", "kind", "args", "while_streaming"})
        self.assertEqual(asyncio.run(get_commands()), {"commands": listed})

    def test_only_a_leading_slash_names_a_command(self):
        self.assertEqual(command_name("/Clear"), "clear")
        self.assertEqual(command_name("  /model gpt"), "model")
        self.assertEqual(command_name("/shared write a.txt\nhello"), "shared")
        self.assertIsNone(command_name("look in /etc/hosts"))
        self.assertIsNone(command_name("type /clear to reset"))
        self.assertIsNone(command_name("/"))
        self.assertIsNone(command_name(""))
        self.assertIsNone(get_command(None))


class ActionCommandParsingTests(unittest.TestCase):
    def test_ordinary_text_is_not_a_command(self):
        self.assertIsNone(parse_action_command("please read /etc/hosts for me"))
        self.assertIsNone(parse_action_command("hello"))

    def test_shared_folder_commands_parse_to_a_call(self):
        call = parse_action_command("/shared read notes.md")
        self.assertEqual(call.name, "workspace.read")
        self.assertEqual(call.path, "notes.md")
        delete = parse_action_command("/shared delete old/report.md")
        self.assertEqual((delete.name, delete.path), ("workspace.delete", "old/report.md"))
        self.assertIn("Delete from the shared folder", delete.summary)
        with self.assertRaisesRegex(CommandError, "needs a path"):
            parse_action_command("/shared delete")
        # The old name is gone, not silently accepted.
        with self.assertRaises(CommandError):
            parse_action_command("/workspace read notes.md")

    def test_unknown_and_client_commands_are_errors(self):
        with self.assertRaises(CommandError) as unknown:
            parse_action_command("/frobnicate now")
        self.assertIn("Unknown command /frobnicate", str(unknown.exception))
        self.assertEqual(unknown.exception.tool, "command")
        with self.assertRaises(CommandError) as client_side:
            parse_action_command("/clear")
        self.assertIn("handled by the app", str(client_side.exception))

    def test_malformed_action_commands_keep_their_tool(self):
        with self.assertRaises(CommandError) as workspace:
            parse_action_command("/shared burn everything")
        self.assertEqual(workspace.exception.tool, "workspace")
        self.assertIn("list, read, write and delete", str(workspace.exception))
        with self.assertRaises(CommandError) as connector:
            parse_action_command("/connector gitlab issues a/b")
        self.assertEqual(connector.exception.tool, "connector")


class ClearHistoryRouteTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.storage = StorageService(Path(self.temp_dir.name))
        self.turns = TurnManager()
        self.patches = [
            mock.patch.object(chat_router, "storage_service", self.storage),
            mock.patch.object(chat_router, "turn_manager", self.turns),
        ]
        for patch in self.patches:
            patch.start()
        for index in range(3):
            self.storage.add_message({
                "id": f"msg-{index}", "thread_id": "bot-a", "bot_id": "bot-a", "sender": "user",
                "text": f"hello {index}", "created_at": "2026-10-11T00:00:00", "item_type": "user_text",
            })
        self.storage.add_message({
            "id": "msg-other", "thread_id": "bot-b", "bot_id": "bot-b", "sender": "user",
            "text": "keep me", "created_at": "2026-10-11T00:00:00", "item_type": "user_text",
        })

    def tearDown(self):
        for patch in self.patches:
            patch.stop()
        try:
            self.temp_dir.cleanup()
        except PermissionError:
            pass  # Windows keeps the SQLite file open until the process exits.

    def test_clearing_a_thread_removes_only_that_thread(self):
        result = asyncio.run(chat_router.clear_history("bot-a"))
        self.assertEqual(result, {"status": "ok", "deleted": 3})
        self.assertEqual(self.storage.get_messages(thread_id="bot-a"), [])
        self.assertEqual(len(self.storage.get_messages(thread_id="bot-b")), 1)

    def test_a_thread_with_a_running_turn_is_not_cleared(self):
        async def runner(turn):
            await asyncio.sleep(10)

        async def attempt():
            self.turns.start("bot-a", "bot-a", "m", runner)
            try:
                await chat_router.clear_history("bot-a")
            finally:
                self.turns.cancel("bot-a")

        with self.assertRaises(chat_router.HTTPException) as raised:
            asyncio.run(attempt())
        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(len(self.storage.get_messages(thread_id="bot-a")), 3)


if __name__ == "__main__":
    unittest.main()
