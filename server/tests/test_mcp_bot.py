"""MCP servers that belong to one bot and run inside its computer."""

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi import HTTPException

from app.routers import mcp as mcp_router
from app.services import computer_provider as provider_module
from app.services import mcp_service, mcp_tools
from app.services import storage_service as storage_module
from app.services.action_gateway import ActionInvocation
from app.services.computer_provider import FakeComputerProvider
from app.services.llm_tools import ToolCallError, build_invocation
from app.services.mcp_service import BotMcpStore, McpError, McpManager, McpStore, clean_server, computer_endpoint
from app.services.storage_service import StorageService


class BotScopeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.storage = StorageService(Path(self.temp.name))
        self.bot = self.storage.get_bots()[0]["id"]
        self.provider = FakeComputerProvider()
        self.manager = McpManager(McpStore(self.storage), secrets=lambda: {})
        self.patches = [
            mock.patch.object(storage_module, "storage_service", self.storage),
            mock.patch.object(provider_module, "computer_provider", self.provider),
            mock.patch.object(mcp_router, "mcp_manager", self.manager),
            mock.patch.object(mcp_tools, "mcp_manager", self.manager),
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

    def test_computer_transport_is_a_command(self):
        record = clean_server({"name": "Scanner", "transport": "computer", "command": "uvx", "args": ["nmap-mcp"], "env": {"API_KEY": "$SHODAN"}})
        self.assertEqual((record["transport"], record["command"], record["args"]), ("computer", "uvx", ["nmap-mcp"]))
        with self.assertRaisesRegex(McpError, "needs the command"):
            clean_server({"name": "x", "transport": "computer"})

    def test_bot_store_lives_on_the_bot_record_and_is_scoped(self):
        store = BotMcpStore(self.bot, storage=self.storage)
        self.assertEqual(store.list(), [])
        record = store.add({"name": "Scanner", "command": "uvx", "args": ["nmap-mcp"]}, created_by="bot")
        self.assertEqual((record["scope"], record["bot_id"], record["transport"], record["created_by"]), ("bot", self.bot, "computer", "bot"))
        saved = next(b for b in self.storage.get_bots() if b["id"] == self.bot)["mcp_servers"]
        self.assertEqual([s["name"] for s in saved], ["Scanner"])
        self.assertNotIn("scope", saved[0])  # the record itself is plain; scope is implied
        with self.assertRaisesRegex(McpError, "already exists"):
            store.add({"name": "scanner", "command": "x"})
        changed = store.update(record["id"], {"enabled": False, "trust_read_only": True})
        self.assertEqual((changed["enabled"], changed["trust_read_only"], changed["transport"]), (False, True, "computer"))
        # The manager sees it only when asked for this bot; other bots and the user scope do not.
        self.assertEqual([r["name"] for r in self.manager.records()], [])
        self.assertEqual([r["name"] for r in self.manager.records(self.bot)], ["Scanner"])
        self.assertEqual(self.manager.find(record["id"], self.bot)["scope"], "bot")
        self.assertEqual(self.manager.find(record["id"])["scope"], "bot")  # a tool call without the bot still finds it
        self.storage.save_bots(self.storage.get_bots() + [{"id": "bot-other", "name": "Other"}])
        other = "bot-other"
        self.assertEqual(self.manager.records(other), [])
        self.assertIs(type(self.manager.store_for(record)), BotMcpStore)
        store.remove(record["id"])
        self.assertEqual(store.list(), [])
        with self.assertRaisesRegex(McpError, "no MCP server"):
            store.remove(record["id"])

    async def test_endpoint_starts_the_computer_and_the_server(self):
        record = BotMcpStore(self.bot, storage=self.storage).add({"name": "Scanner", "command": "uvx", "args": ["nmap-mcp"], "env": {"KEY": "$SHODAN"}})
        self.assertNotEqual(self.provider.get_or_create(self.bot).state, "running")
        endpoint = await computer_endpoint(record)
        status = self.provider.describe(self.bot)
        self.assertEqual(status.state, "running")
        self.assertEqual(endpoint["url"], f"http://fake-computer/{status.computer_id}/mcp/{record['id']}")
        started = self.provider.mcp_servers[status.computer_id][record["id"]]
        self.assertEqual((started["command"], started["args"], started["env"]), ("uvx", ["nmap-mcp"], {"KEY": "$SHODAN"}))  # $NAME is for the computer to resolve
        await self.provider.mcp_stop(status.computer_id, record["id"])
        self.assertIsNone(self.provider.mcp_endpoint(status.computer_id, record["id"]))
        with self.assertRaisesRegex(McpError, "needs the bot"):
            await computer_endpoint({"id": "mcp-x", "command": "x"})

    async def test_the_bots_add_tool_defaults_to_its_computer(self):
        call = build_invocation("mcp_add_server", {"name": "Scanner", "command": "uvx", "args": ["nmap-mcp"]}, self.bot)
        self.assertEqual(call.arguments["scope"], "computer")
        self.assertEqual(call.arguments["server"]["transport"], "computer")
        self.assertEqual(call.target, {"mcp_server": "Scanner", "bot_id": self.bot})
        self.assertIn("in your computer (computer)", call.preview)
        with self.assertRaisesRegex(ToolCallError, "needs 'transport'"):
            build_invocation("mcp_add_server", {"name": "Docs", "scope": "user", "url": "https://x"}, self.bot)
        with self.assertRaisesRegex(ToolCallError, "scope"):
            build_invocation("mcp_add_server", {"name": "Docs", "scope": "global", "command": "x"}, self.bot)
        # The executor stores it on the bot; the fake computer has no real endpoint, so it is kept switched off.
        result = await mcp_tools.add_server(call)
        self.assertFalse(result["connected"])
        saved = next(b for b in self.storage.get_bots() if b["id"] == self.bot)["mcp_servers"]
        self.assertEqual((saved[0]["name"], saved[0]["enabled"], saved[0]["created_by"]), ("Scanner", False, "bot"))
        listed = await mcp_tools.list_servers(ActionInvocation(name="mcp.servers_list", arguments={}, target={"bot_id": self.bot}, preview=""))
        self.assertEqual([(s["name"], s["scope"]) for s in listed["servers"]], [("Scanner", "bot")])
        # Removing goes through the bot's store; a system server is refused.
        self.manager.store.add({"name": "Shared", "transport": "http", "url": "https://mcp.example.com/mcp"})
        shared_id = self.manager.store.list()[0]["id"]
        with self.assertRaisesRegex(McpError, "admin"):
            await mcp_tools.remove_server(ActionInvocation(name="mcp.servers_remove", arguments={"id": shared_id}, target={"bot_id": self.bot}, preview=""))
        removed = await mcp_tools.remove_server(ActionInvocation(name="mcp.servers_remove", arguments={"id": saved[0]["id"]}, target={"bot_id": self.bot}, preview=""))
        self.assertEqual(removed["removed"]["name"], "Scanner")
        self.assertEqual(BotMcpStore(self.bot, storage=self.storage).list(), [])

    async def test_router_manages_a_bots_servers_for_its_owner_only(self):
        created = (await mcp_router.bot_mcp_add(self.bot, mcp_router.ServerInput(name="Scanner", command="uvx", args=["nmap-mcp"], enabled=False)))["server"]
        self.assertEqual((created["scope"], created["bot_id"], created["transport"]), ("bot", self.bot, "computer"))
        listed = (await mcp_router.bot_mcp_list(self.bot))["servers"]
        self.assertEqual([s["name"] for s in listed], ["Scanner"])
        self.assertEqual([s["scope"] for s in (await mcp_router.mcp_list_servers())["servers"]], [])  # not in the settings list
        with self.assertRaises(HTTPException) as ctx:
            await mcp_router.bot_mcp_list("bot-not-mine")
        self.assertEqual(ctx.exception.status_code, 404)
        changed = (await mcp_router.bot_mcp_update(self.bot, created["id"], mcp_router.ServerUpdate(trust_read_only=True)))["server"]
        self.assertTrue(changed["trust_read_only"])
        removed = await mcp_router.bot_mcp_remove(self.bot, created["id"])
        self.assertEqual(removed["removed"]["name"], "Scanner")
        with self.assertRaises(HTTPException):
            await mcp_router.bot_mcp_test(self.bot, created["id"])

    async def test_a_bot_can_have_a_url_server_of_its_own(self):
        """Reached from the API like the account's URL servers, with the bot's secrets, offered to that bot alone."""
        import socket
        import subprocess
        import sys
        import urllib.error
        import urllib.request

        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        process = subprocess.Popen([sys.executable, str(Path(__file__).parent / "fixtures" / "mcp_sse_server.py"), str(port), "/sse"])
        self.addCleanup(lambda: (process.kill(), process.wait(10)))
        for _ in range(200):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/nothing", timeout=1)
            except urllib.error.HTTPError:
                break
            except OSError:
                await asyncio.sleep(0.05)

        created = (await mcp_router.bot_mcp_add(self.bot, mcp_router.ServerInput(name="Memory", url=f"http://127.0.0.1:{port}/sse", headers={"X-Client": "cubicle"})))["server"]
        self.assertEqual((created["scope"], created["transport"], created["url"]), ("bot", "http", f"http://127.0.0.1:{port}/sse"))
        changed = (await mcp_router.bot_mcp_update(self.bot, created["id"], mcp_router.ServerUpdate(trust_read_only=True)))["server"]
        self.assertEqual(changed["transport"], "http")  # an edit keeps it a URL server
        connection = self.manager.connection(created["id"], self.bot)
        await connection.wait_ready(timeout=30)
        self.addAsyncCleanup(connection.stop)
        self.assertEqual((connection.status, connection.via), ("connected", "sse"))
        self.assertEqual([s.name for s in mcp_tools.mcp_tool_specs(self.manager, self.bot)], ["memory_remember"])
        self.assertEqual(mcp_tools.mcp_tool_specs(self.manager, "bot-other"), [])
        # Its $NAME values are the bot's secrets over the account's.
        resolving = McpManager(McpStore(self.storage))
        with mock.patch.object(self.storage, "secret_values", lambda bot_id="": {"MEM_TOKEN": f"for {bot_id or 'app'}"}):
            self.assertEqual(resolving._secrets_for(changed), {"MEM_TOKEN": f"for {self.bot}"})
        # The bot's own add tool takes scope bot for a URL.
        call = build_invocation("mcp_add_server", {"name": "Docs", "scope": "bot", "url": "https://mcp.example.com/sse"}, self.bot)
        self.assertEqual((call.arguments["scope"], call.arguments["server"]["transport"]), ("bot", "http"))
        self.assertIn("for you", call.preview)
        with self.assertRaises(ToolCallError):
            build_invocation("mcp_add_server", {"name": "Docs", "scope": "bot", "command": "npx"}, self.bot)
        removed = await mcp_router.bot_mcp_remove(self.bot, created["id"])
        self.assertEqual(removed["removed"]["transport"], "http")


if __name__ == "__main__":
    unittest.main()
