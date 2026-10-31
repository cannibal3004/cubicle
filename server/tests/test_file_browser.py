"""Browsing the computer workspace and the shared folder from the UI: list, upload, folder, delete, confinement."""

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi import HTTPException

from app.routers import files as files_router
from app.services import file_browser, file_share
from app.services import storage_service as storage_module
from app.services.computer_provider import computer_id_for_bot
from app.services.file_share import FileShareError
from app.services.storage_service import StorageService


class _Upload:
    def __init__(self, name, data):
        self.filename = name
        self._data = data

    async def read(self):
        return self._data


class FileBrowserTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.shared = root / "shared"
        self.computers = root / "computers"
        (self.shared / "reports").mkdir(parents=True)
        (self.shared / "reports" / "q3.json").write_bytes(b'{"a": 1}')
        self.bot_ws = self.computers / computer_id_for_bot("bot-1")
        (self.bot_ws / "project").mkdir(parents=True)
        (self.bot_ws / "project" / "app.py").write_bytes(b"print(1)\n")
        (self.bot_ws / "notes.txt").write_bytes(b"hi")
        (root / "secret.txt").write_text("nope", encoding="utf-8")
        self.storage = StorageService(root / "data")
        self.storage.save_bots(self.storage.get_bots() + [{"id": "bot-1", "name": "One", "role": "x", "system_prompt": "x", "model": "m", "tools": []}])
        self.patches = [
            mock.patch.object(file_share.settings, "WORKSPACE_ROOT", self.shared),
            mock.patch.object(file_share.settings, "COMPUTER_DOCKER_WORKSPACE_ROOT", self.computers),
            mock.patch.object(file_share.settings, "COMPUTER_PROVIDER", "docker"),
            mock.patch.object(file_share.settings, "COMPUTER_DOCKER_WORKSPACE_MODE", "bind"),
            mock.patch.object(file_browser.settings, "ATTACHMENT_MAX_BYTES", 1024),
            mock.patch.object(storage_module, "storage_service", self.storage),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        try:
            self.temp.cleanup()
        except PermissionError:
            pass

    def test_listing_is_folders_first_and_confined(self):
        top = file_browser.list_directory("computer", "", "bot-1")
        self.assertEqual([(e["name"], e["kind"]) for e in top["entries"]], [("project", "dir"), ("notes.txt", "file")])
        self.assertEqual((top["path"], top["parent"], top["exists"]), ("", None, True))
        inner = file_browser.list_directory("computer", "/workspace/project", "bot-1")
        self.assertEqual((inner["path"], inner["parent"], inner["entries"][0]["size"]), ("project", "", 9))
        self.assertEqual(file_browser.list_directory("shared", "reports", None)["entries"][0]["name"], "q3.json")
        self.assertEqual(file_browser.list_directory("computer", "nope", "bot-1")["exists"], False)
        with self.assertRaisesRegex(FileShareError, "outside"):
            file_browser.list_directory("computer", "../../secret.txt", "bot-1")
        with self.assertRaisesRegex(FileShareError, "not a folder"):
            file_browser.list_directory("computer", "notes.txt", "bot-1")
        with mock.patch.object(file_share.settings, "COMPUTER_DOCKER_WORKSPACE_MODE", "volume"):
            with self.assertRaisesRegex(FileShareError, "bind"):
                file_browser.list_directory("computer", "", "bot-1")

    def test_a_folder_the_computer_keeps_private_is_marked_not_a_500(self):
        """A bot's .ssh (mode 700, owned by the computer's user): the API cannot read it, and says so."""
        (self.bot_ws / ".ssh").mkdir()
        (self.bot_ws / ".ssh" / "id_ed25519").write_bytes(b"key")
        real_access = file_browser.os.access
        private = {str(self.bot_ws / ".ssh"), str(self.bot_ws / ".ssh" / "id_ed25519")}
        real_iterdir, real_is_file = Path.iterdir, Path.is_file

        def iterdir(path):
            if str(path) in private:
                raise PermissionError(13, "Permission denied", str(path))
            return real_iterdir(path)

        def is_file(path):
            if str(path) in private:
                raise PermissionError(13, "Permission denied", str(path))
            return real_is_file(path)

        with mock.patch.object(file_browser.os, "access", lambda path, mode: str(path) not in private and real_access(path, mode)), mock.patch.object(Path, "iterdir", iterdir):
            top = file_browser.list_directory("computer", "", "bot-1")
            self.assertEqual({e["name"]: e["private"] for e in top["entries"]}, {".ssh": True, "project": False, "notes.txt": False})
            with self.assertRaisesRegex(FileShareError, "private to the computer's own user"):
                file_browser.list_directory("computer", ".ssh", "bot-1")
            # Through the router: a 400 with that message, not a 500.
            with self.assertRaises(HTTPException) as refused:
                asyncio.run(files_router.list_files(source="computer", path=".ssh", bot_id="bot-1"))
            self.assertEqual(refused.exception.status_code, 400)
        with mock.patch.object(file_share.os, "access", lambda path, mode: str(path) not in private):
            with self.assertRaisesRegex(FileShareError, "id_ed25519 is private"):
                file_share.resolve_shared_file("computer", ".ssh/id_ed25519", "bot-1")
        with mock.patch.object(Path, "is_file", is_file):
            with self.assertRaisesRegex(FileShareError, "private"):
                file_share.resolve_shared_file("computer", ".ssh/id_ed25519", "bot-1")

    def test_upload_folder_and_delete(self):
        stored = file_browser.store_files("computer", "incoming/data", "bot-1", [("../evil.csv", b"a,b"), ("evil.csv", b"c,d")])
        self.assertEqual([e["path"] for e in stored], ["incoming/data/evil.csv", "incoming/data/evil (2).csv"])
        self.assertEqual((self.bot_ws / "incoming" / "data" / "evil.csv").read_bytes(), b"a,b")
        with self.assertRaisesRegex(FileShareError, "upload limit"):
            file_browser.store_files("computer", "", "bot-1", [("big.bin", b"x" * 2048)])
        with self.assertRaisesRegex(FileShareError, "empty"):
            file_browser.store_files("computer", "", "bot-1", [("nothing.txt", b"")])
        made = file_browser.make_directory("computer", "incoming", "bot-1", "more")
        self.assertEqual((made["path"], made["kind"]), ("incoming/more", "dir"))
        with self.assertRaisesRegex(FileShareError, "not empty"):
            file_browser.delete_entry("computer", "incoming/data", "bot-1")
        self.assertEqual(file_browser.delete_entry("computer", "incoming/more", "bot-1"), {"deleted": "incoming/more"})
        self.assertEqual(file_browser.delete_entry("computer", "incoming/data/evil.csv", "bot-1")["deleted"], "incoming/data/evil.csv")
        self.assertFalse((self.bot_ws / "incoming" / "data" / "evil.csv").exists())
        with self.assertRaisesRegex(FileShareError, "root folder"):
            file_browser.delete_entry("computer", "", "bot-1")
        with self.assertRaisesRegex(FileShareError, "outside"):
            file_browser.delete_entry("computer", "../../secret.txt", "bot-1")

    def test_the_router_only_serves_the_callers_bots(self):
        async def scenario():
            listed = await files_router.list_files(source="computer", path="", bot_id="bot-1")
            self.assertEqual(len(listed["entries"]), 2)
            with self.assertRaises(HTTPException) as ctx:
                await files_router.list_files(source="computer", path="", bot_id="bot-someone-else")
            self.assertEqual(ctx.exception.status_code, 404)
            with self.assertRaises(HTTPException) as ctx:
                await files_router.download_file(source="computer", path="notes.txt", bot_id="bot-someone-else", inline=False)
            self.assertEqual(ctx.exception.status_code, 404)
            response = await files_router.download_file(source="computer", path="notes.txt", bot_id="bot-1", inline=False)
            self.assertEqual(response.media_type, "text/plain")
            uploaded = await files_router.upload_files(source="computer", path="drop", bot_id="bot-1", files=[_Upload("a.txt", b"A")])
            self.assertEqual(uploaded["stored"][0]["path"], "drop/a.txt")
            with self.assertRaises(HTTPException) as ctx:
                await files_router.delete_file(source="computer", path="", bot_id="bot-1")
            self.assertEqual(ctx.exception.status_code, 400)
            # The shared folder needs no bot.
            self.assertEqual((await files_router.list_files(source="shared", path="", bot_id=""))["entries"][0]["name"], "reports")

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
