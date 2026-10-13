import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi import HTTPException

from app.routers import files as files_router
from app.services import file_share
from app.services.action_gateway import ActionInvocation
from app.services.computer_provider import computer_id_for_bot
from app.services.file_share import FileShareError, resolve_shared_file
from app.services.llm_tools import ToolCallError, build_invocation, describe_tools, available_tools


class FileShareTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.workspace = root / "workspace"
        self.computers = root / "computers"
        (self.workspace / "reports").mkdir(parents=True)
        (self.workspace / "reports" / "q3.json").write_bytes(b'{"a": 1}')
        (root / "secret.txt").write_text("nope", encoding="utf-8")
        bot_ws = self.computers / computer_id_for_bot("bot-1")
        bot_ws.mkdir(parents=True)
        (bot_ws / "chart.png").write_bytes(b"\x89PNG fake")
        self.patches = [
            mock.patch.object(file_share.settings, "WORKSPACE_ROOT", self.workspace),
            mock.patch.object(file_share.settings, "COMPUTER_DOCKER_WORKSPACE_ROOT", self.computers),
            mock.patch.object(file_share.settings, "COMPUTER_PROVIDER", "docker"),
            mock.patch.object(file_share.settings, "COMPUTER_DOCKER_WORKSPACE_MODE", "bind"),
            mock.patch.object(file_share.settings, "FILE_SHARE_MAX_BYTES", 1024),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.temp.cleanup()

    def test_workspace_files_resolve_with_metadata(self):
        shared = resolve_shared_file("workspace", "reports/q3.json", None)
        self.assertEqual((shared.name, shared.path, shared.size, shared.mime), ("q3.json", "reports/q3.json", 8, "application/json"))
        # A leading slash or backslashes are tolerated.
        self.assertEqual(resolve_shared_file("workspace", "/reports\\q3.json", None).path, "reports/q3.json")

    def test_computer_files_accept_the_sandbox_spelling(self):
        shared = resolve_shared_file("computer", "/workspace/chart.png", "bot-1")
        self.assertEqual((shared.path, shared.mime), ("chart.png", "image/png"))
        self.assertEqual(resolve_shared_file("computer", "chart.png", "bot-1").path, "chart.png")
        # /shared inside the computer is the shared folder; the card says so.
        via_mount = resolve_shared_file("computer", "/shared/reports/q3.json", "bot-1")
        self.assertEqual((via_mount.source, via_mount.path), ("workspace", "reports/q3.json"))
        with self.assertRaisesRegex(FileShareError, "bot id"):
            resolve_shared_file("computer", "chart.png", None)
        with mock.patch.object(file_share.settings, "COMPUTER_DOCKER_WORKSPACE_MODE", "volume"):
            with self.assertRaisesRegex(FileShareError, "bind"):
                resolve_shared_file("computer", "chart.png", "bot-1")

    def test_escapes_directories_and_oversize_files_are_refused(self):
        with self.assertRaisesRegex(FileShareError, "outside"):
            resolve_shared_file("workspace", "../secret.txt", None)
        with self.assertRaisesRegex(FileShareError, "outside"):
            resolve_shared_file("computer", "../../secret.txt", "bot-1")
        with self.assertRaisesRegex(FileShareError, "Only files"):
            resolve_shared_file("workspace", "reports", None)
        with self.assertRaisesRegex(FileShareError, "No such file"):
            resolve_shared_file("workspace", "reports/missing.csv", None)
        (self.workspace / "big.bin").write_bytes(b"x" * 2048)
        with self.assertRaisesRegex(FileShareError, "larger"):
            resolve_shared_file("workspace", "big.bin", None)
        with self.assertRaisesRegex(FileShareError, "source"):
            resolve_shared_file("usb", "x", None)

    def test_model_tool_becomes_a_gateway_share_and_returns_an_attachment(self):
        call = build_invocation("share_file", {"source": "computer", "path": "/workspace/chart.png"}, "bot-1")
        self.assertEqual(call.name, "files.share")
        self.assertEqual(call.target, {"bot_id": "bot-1", "source": "computer"})
        self.assertIn("chart.png", call.preview)
        result = asyncio.run(file_share.execute_share(call))
        self.assertTrue(result["shared"])
        self.assertEqual(result["attachment"], {"source": "computer", "path": "chart.png", "name": "chart.png", "size": 9, "mime": "image/png"})

        with self.assertRaisesRegex(ToolCallError, "workspace or computer"):
            build_invocation("share_file", {"source": "usb", "path": "x"}, "bot-1")
        with self.assertRaisesRegex(FileShareError, "No such file"):
            asyncio.run(file_share.execute_share(ActionInvocation(
                name="files.share", arguments={"source": "workspace", "path": "nope.txt"}, target={"bot_id": "bot-1"}, preview="x",
            )))

    def test_share_tool_is_always_offered_and_described(self):
        specs = available_tools(computer=False)
        self.assertIn("share_file", [s.name for s in specs])
        self.assertIn("share_file puts a download card", describe_tools(specs))

    def test_download_route_serves_the_file_or_404s(self):
        response = asyncio.run(files_router.download_file(source="workspace", path="reports/q3.json", bot_id="", inline=False))
        self.assertEqual(Path(response.path), self.workspace / "reports" / "q3.json")
        self.assertEqual(response.media_type, "application/json")
        self.assertIn('filename="q3.json"', response.headers["content-disposition"])
        self.assertTrue(response.headers["content-disposition"].startswith("attachment"))
        inline = asyncio.run(files_router.download_file(source="computer", path="chart.png", bot_id="bot-1", inline=True))
        self.assertTrue(inline.headers["content-disposition"].startswith("inline"))
        with self.assertRaises(HTTPException) as caught:
            asyncio.run(files_router.download_file(source="workspace", path="../secret.txt", bot_id="", inline=False))
        self.assertEqual(caught.exception.status_code, 404)


if __name__ == "__main__":
    unittest.main()
