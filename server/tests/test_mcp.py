import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi import HTTPException

from app.routers import mcp as mcp_router
from app.services import mcp_service, mcp_tools
from app.services.action_gateway import ActionGateway, ActionInvocation
from app.services.llm_tools import MCP_TOOLS, ToolCallError, available_tools, build_invocation, connector_invocation, describe_tools
from app.services.mcp_service import McpError, McpManager, McpStore, clean_server, public, resolve_values, secret_references
from app.services.mcp_tools import mcp_tool_specs, register_mcp_actions
from app.services.storage_service import StorageService
from app.services.workspace_service import WorkspaceService

PROBE = str(Path(__file__).parent / "fixtures" / "mcp_probe_server.py")


def probe_definition(**extra):
    return {"name": "Probe", "transport": "stdio", "command": sys.executable, "args": [PROBE], **extra}


class _Audit:
    def add_audit_event(self, event):
        pass


class _Broker:
    def __init__(self, decision="allow"):
        self.decision = decision

    def open(self, thread_id, bot_id, call, request_id=None):
        return {"request_id": request_id, "thread_id": thread_id, "bot_id": bot_id, "tool": call.name, "status": "pending"}

    async def wait(self, request_id):
        return self.decision


class ServerDefinitionTests(unittest.TestCase):
    def test_definitions_are_cleaned_and_secret_references_found(self):
        record = clean_server({"name": "  Context7 ", "command": "npx", "args": "-y @upstash/context7-mcp", "env": "CONTEXT7_API_KEY=$CONTEXT7_API_KEY\nMODE=fast"})
        self.assertTrue(record["id"].startswith("mcp-"))
        self.assertEqual((record["name"], record["transport"], record["command"], record["args"]), ("Context7", "stdio", "npx", ["-y", "@upstash/context7-mcp"]))
        self.assertEqual(record["env"], {"CONTEXT7_API_KEY": "$CONTEXT7_API_KEY", "MODE": "fast"})
        self.assertEqual(secret_references(record), ["CONTEXT7_API_KEY"])
        self.assertEqual(resolve_values(record["env"], {"CONTEXT7_API_KEY": "ctx-123"}), {"CONTEXT7_API_KEY": "ctx-123", "MODE": "fast"})
        self.assertEqual(resolve_values({"Authorization": "Bearer ${EXA_API_KEY}"}, {"EXA_API_KEY": "exa-1"}), {"Authorization": "Bearer exa-1"})
        with self.assertRaisesRegex(McpError, "not set"):
            resolve_values(record["env"], {})
        http = clean_server({"name": "Exa", "url": "https://mcp.exa.ai/mcp", "headers": {"Authorization": "Bearer ${EXA_API_KEY}"}})
        self.assertEqual((http["transport"], http["url"]), ("http", "https://mcp.exa.ai/mcp"))
        self.assertEqual(secret_references(http), ["EXA_API_KEY"])
        self.assertEqual(public(http)["secret_references"], ["EXA_API_KEY"])
        for bad, message in (
            ({"name": "", "command": "npx"}, "needs a name"),
            ({"name": "x", "transport": "ssh"}, "stdio .*http"),
            ({"name": "x", "transport": "stdio"}, "needs the command"),
            ({"name": "x", "transport": "http", "url": "ftp://x"}, "http\\(s\\) URL"),
            ({"name": "x", "command": "npx", "env": {"bad name": "1"}}, "not a valid name"),
        ):
            with self.assertRaisesRegex(McpError, message):
                clean_server(bad)

    def test_a_value_typed_in_is_never_shown_and_an_edit_keeps_it(self):
        """A key pasted into env or headers instead of a $NAME reference: the bot's tools and the
        settings page see it hidden; sending the hidden value back keeps the stored one."""
        with tempfile.TemporaryDirectory() as tmp:
            store = McpStore(StorageService(Path(tmp)))
            record = store.add({"name": "DigitalOcean", "command": "npx", "args": ["@digitalocean/mcp"],
                                "env": {"DIGITALOCEAN_API_TOKEN": "dop_v1_abc123", "REGION": "${DO_REGION}", "EMPTY": ""}})
            shown = public(record)
            self.assertEqual(shown["env"], {"DIGITALOCEAN_API_TOKEN": mcp_service.HIDDEN, "REGION": "${DO_REGION}", "EMPTY": ""})
            self.assertNotIn("dop_v1_abc123", repr(shown))
            # The bot's list tool shows the same.
            with mock.patch.object(mcp_tools.mcp_manager, "records", lambda bot_id=None: [record]):
                listed = asyncio.run(mcp_tools.list_servers(ActionInvocation(name="mcp.servers_list", arguments={}, target={}, preview="")))
            self.assertNotIn("dop_v1_abc123", repr(listed))
            # An edit that sends the hidden value back changes the rest and keeps the token.
            edited = store.update(record["id"], {"args": ["-y", "@digitalocean/mcp"], "env": shown["env"]})
            self.assertEqual(edited["env"]["DIGITALOCEAN_API_TOKEN"], "dop_v1_abc123")
            self.assertEqual(edited["args"], ["-y", "@digitalocean/mcp"])
            # A new value replaces it; a hidden value under a new name has nothing to keep.
            self.assertEqual(store.update(record["id"], {"env": {"DIGITALOCEAN_API_TOKEN": "$DO_TOKEN"}})["env"], {"DIGITALOCEAN_API_TOKEN": "$DO_TOKEN"})
            with self.assertRaisesRegex(McpError, "enter the value for OTHER"):
                store.update(record["id"], {"env": {"OTHER": mcp_service.HIDDEN}})
            http = store.add({"name": "Exa", "url": "https://mcp.exa.ai/mcp", "headers": {"Authorization": "Bearer exa-secret"}})
            self.assertEqual(public(http)["headers"], {"Authorization": mcp_service.HIDDEN})
            self.assertEqual(store.update(http["id"], {"headers": public(http)["headers"]})["headers"], {"Authorization": "Bearer exa-secret"})

    def test_store_adds_updates_and_removes(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage = StorageService(Path(tmp))
            store = McpStore(storage)
            record = store.add({"name": "Probe", "command": "python", "args": ["x.py"]}, created_by="bot")
            self.assertEqual([s["name"] for s in store.list()], ["Probe"])
            self.assertEqual(record["created_by"], "bot")
            with self.assertRaisesRegex(McpError, "already exists"):
                store.add({"name": "probe", "command": "python"})
            changed = store.update(record["id"], {"enabled": False, "trust_read_only": True})
            self.assertEqual((changed["enabled"], changed["trust_read_only"], changed["command"]), (False, True, "python"))
            self.assertEqual(store.get(record["id"])["enabled"], False)
            store.remove(record["id"])
            self.assertEqual(store.list(), [])
            with self.assertRaisesRegex(McpError, "no MCP server"):
                store.get(record["id"])


def _admin_request():
    """What the auth middleware leaves on a request from the first (admin) account."""
    from types import SimpleNamespace

    return SimpleNamespace(state=SimpleNamespace(user={"id": "local-user", "username": "owner", "role": "owner"}), headers={}, cookies={})


class LiveProbeServerTests(unittest.IsolatedAsyncioTestCase):
    """Runs the fixture server as a real subprocess over stdio."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.storage = StorageService(Path(self.temp_dir.name))
        self.store = McpStore(self.storage)
        self.manager = McpManager(self.store, secrets=lambda: {"PROBE_TOKEN": "from-secrets"})

    async def asyncTearDown(self):
        await self.manager.stop_all()
        try:
            self.temp_dir.cleanup()
        except PermissionError:
            pass

    async def test_tools_are_listed_called_and_gated_by_their_hints(self):
        record = self.store.add(probe_definition(env={"PROBE_TOKEN": "$PROBE_TOKEN"}))
        connection = self.manager.connection(record["id"])
        await connection.wait_ready()
        self.assertEqual(connection.status, "connected")
        self.assertEqual(sorted(t.name for t in connection.tools), ["add", "secret_name", "shout"])

        specs = mcp_tool_specs(self.manager)
        by_name = {s.name: s for s in specs}
        self.assertEqual(sorted(by_name), ["probe_add", "probe_secret_name", "probe_shout"])
        self.assertFalse(by_name["probe_add"].needs_approval)
        self.assertTrue(by_name["probe_shout"].needs_approval)  # no hint: ask
        self.assertEqual(by_name["probe_add"].connector["toolkit"], f"mcp:{record['id']}")
        self.assertEqual(by_name["probe_add"].parameters["properties"].keys(), {"a", "b"})
        self.assertIn("[Probe] Add two numbers", by_name["probe_add"].description)
        # The prompt lists it with the connected apps.
        self.assertIn("Probe", describe_tools(available_tools(computer=False, connectors=specs)))

        # Through the gateway: read tools need no approval, the rest do; the secret arrived.
        gateway = ActionGateway(workspace=WorkspaceService(Path(self.temp_dir.name)), approvals=_Broker(), audit=_Audit())
        register_mcp_actions(gateway)
        with mock.patch.object(mcp_tools, "mcp_manager", self.manager):
            add = connector_invocation(by_name["probe_add"], {"a": 2, "b": 3})
            self.assertEqual((add.name, add.arguments["tool"], add.arguments["server"]), ("mcp.read", "add", record["id"]))
            request, approval = gateway.open("t", "bot-1", add)
            self.assertIsNone(approval)
            result = await gateway.execute(request)
            self.assertEqual((result.status, result.result["text"]), ("completed", "5"))
            self.assertEqual(result.result["structured"], {"result": 5})

            shout = connector_invocation(by_name["probe_shout"], {"text": "hi"})
            self.assertEqual(shout.name, "mcp.call")
            request, approval = gateway.open("t", "bot-1", shout)
            self.assertIsNotNone(approval)
            await gateway.wait_for_decision(request)
            self.assertEqual((await gateway.execute(request)).result["text"], "HI")

            secret = connector_invocation(by_name["probe_secret_name"], {})
            request, _ = gateway.open("t", "bot-1", secret)
            self.assertEqual((await gateway.execute(request)).result["text"], "from-secrets")

            # A tool error is surfaced, and the session survives it.
            bad = connector_invocation(by_name["probe_add"], {"a": "x", "b": 1})
            request, _ = gateway.open("t", "bot-1", bad)
            failed = await gateway.execute(request)
            self.assertNotEqual(failed.status, "completed")
            self.assertIn("validation", str(failed.error).lower())
            request, _ = gateway.open("t", "bot-1", connector_invocation(by_name["probe_add"], {"a": 1, "b": 1}))
            self.assertEqual((await gateway.execute(request)).result["text"], "2")

        # Trusted read-only makes every tool approval-free; a disabled server offers nothing.
        self.store.update(record["id"], {"trust_read_only": True})
        connection = self.manager.connection(record["id"])
        await connection.wait_ready()
        self.assertFalse(any(s.needs_approval for s in mcp_tool_specs(self.manager)))
        self.store.update(record["id"], {"enabled": False})
        self.assertEqual(mcp_tool_specs(self.manager), [])

    async def test_a_server_that_cannot_start_reports_its_error(self):
        record = self.store.add({"name": "Broken", "command": sys.executable, "args": ["-c", "import sys; sys.exit(3)"]})
        connection = self.manager.connection(record["id"])
        with self.assertRaises(McpError):
            await connection.wait_ready(timeout=20)
        self.assertEqual(connection.status, "error")
        self.assertEqual(mcp_tool_specs(self.manager), [])
        outcome = await self.manager.test(record)
        self.assertFalse(outcome["ok"])
        self.assertTrue(outcome["error"])
        # A missing secret is a clear message, not a crash.
        needs = self.store.add(probe_definition(name="Needs", env={"PROBE_TOKEN": "$MISSING"}))
        outcome = await self.manager.test(needs)
        self.assertIn("MISSING is not set", outcome["error"])

    async def test_router_and_bot_tools_manage_servers(self):
        with mock.patch.object(mcp_router, "mcp_store", self.store), mock.patch.object(mcp_router, "mcp_manager", self.manager), mock.patch.object(
            mcp_tools, "mcp_store", self.store
        ), mock.patch.object(mcp_tools, "mcp_manager", self.manager):
            created = (await mcp_router.mcp_add_server(mcp_router.ServerInput(**probe_definition()), _admin_request()))["server"]
            self.assertEqual(created["name"], "Probe")
            tested = await mcp_router.mcp_test_server(created["id"], _admin_request())
            self.assertTrue(tested["ok"])
            self.assertEqual(sorted(t["name"] for t in tested["tools"]), ["add", "secret_name", "shout"])
            listed = (await mcp_router.mcp_list_servers())["servers"]
            self.assertEqual(listed[0]["status"], "connected")
            self.assertEqual(listed[0]["tool_count"], 3)
            with self.assertRaises(HTTPException) as caught:
                await mcp_router.mcp_add_server(mcp_router.ServerInput(name="Probe", command="x"), _admin_request())
            self.assertEqual(caught.exception.status_code, 422)

            # The bot's tools: list is free, add and remove ask.
            offered = available_tools(computer=False)
            self.assertEqual([s.name for s in offered if s.group == "mcp"], ["mcp_list_servers", "mcp_add_server", "mcp_test_server", "mcp_remove_server"])
            self.assertEqual([s.needs_approval for s in MCP_TOOLS], [False, True, False, True])
            self.assertIn("mcp_add_server with the command", describe_tools(offered))
            add = build_invocation("mcp_add_server", {"name": "Probe 2", "scope": "user", "transport": "stdio", "command": sys.executable, "args": [PROBE], "trust_read_only": True}, "bot-1")
            self.assertEqual(add.name, "mcp.servers_add")
            self.assertIn("Add MCP server 'Probe 2' for every bot (stdio)", add.preview)
            self.assertIn("(trusted read-only)", add.preview)
            with self.assertRaisesRegex(ToolCallError, "needs the command"):
                build_invocation("mcp_add_server", {"name": "x", "transport": "stdio"}, "bot-1")
            added = await mcp_tools.add_server(add)
            self.assertTrue(added["connected"])
            self.assertEqual(sorted(t["name"] for t in added["tools"]), ["add", "secret_name", "shout"])
            self.assertEqual(added["server"]["created_by"], "bot")
            names = [s["name"] for s in (await mcp_tools.list_servers(ActionInvocation(name="mcp.servers_list", arguments={}, target={}, preview="")))["servers"]]
            self.assertEqual(names, ["Probe", "Probe 2"])
            # A server that does not connect is kept but switched off.
            broken = build_invocation("mcp_add_server", {"name": "Broken", "scope": "user", "transport": "stdio", "command": sys.executable, "args": ["-c", "raise SystemExit(2)"]}, "bot-1")
            outcome = await mcp_tools.add_server(broken)
            self.assertFalse(outcome["connected"])
            self.assertFalse(outcome["server"]["enabled"])
            removed = await mcp_tools.remove_server(build_invocation("mcp_remove_server", {"id": outcome["server"]["id"]}, "bot-1"))
            self.assertEqual(removed["removed"]["name"], "Broken")
            self.assertEqual(len(self.manager.records()), 2)


class SseServerTests(unittest.IsolatedAsyncioTestCase):
    """A server that speaks only the older SSE protocol (mem0's OpenMemory, for one), on a real port."""

    def _serve(self, sse_path):
        import socket
        import threading

        import uvicorn
        from mcp.server.mcpserver import MCPServer

        server = MCPServer("sse-probe")

        @server.tool()
        def remember(text: str) -> str:
            """Keep a note."""
            return f"kept: {text}"

        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        web = uvicorn.Server(uvicorn.Config(server.sse_app(sse_path=sse_path), host="127.0.0.1", port=port, log_level="error"))
        thread = threading.Thread(target=web.run, daemon=True)
        thread.start()
        for _ in range(100):
            if web.started:
                break
            threading.Event().wait(0.05)

        def stop():
            web.should_exit = True
            thread.join(5)

        self.addCleanup(stop)
        return f"http://127.0.0.1:{port}{sse_path}"

    async def _connect(self, record):
        connection = mcp_service.McpConnection(record=record)
        self.addAsyncCleanup(connection.stop)
        await connection.wait_ready(timeout=30)
        return connection

    async def test_a_url_falls_back_to_sse_and_sse_can_be_chosen(self):
        # A path that does not say sse: streamable HTTP is tried first, then SSE.
        url = self._serve("/events")
        connection = await self._connect(clean_server({"name": "Memory", "url": url}))
        self.assertEqual((connection.record["transport"], connection.status, connection.via), ("http", "connected", "sse"))
        self.assertEqual([t.name for t in connection.tools], ["remember"])
        result = await connection.call("remember", {"text": "the NAS is nas.lan"})
        self.assertIn("kept: the NAS is nas.lan", result.content[0].text)
        # Chosen outright.
        forced = await self._connect(clean_server({"name": "Memory", "transport": "sse", "url": url}))
        self.assertEqual((forced.status, forced.via), ("connected", "sse"))

    def test_a_path_that_says_sse_tries_sse_first(self):
        protocols = lambda url, transport="http": mcp_service.McpConnection(record={"name": "x", "transport": transport, "url": url})._protocols()
        self.assertEqual(protocols("http://nas:8765/mcp/openmemory/sse/default_user"), ["sse", "http"])
        self.assertEqual(protocols("https://mcp.example.com/sse"), ["sse", "http"])
        self.assertEqual(protocols("https://mcp.exa.ai/mcp"), ["http", "sse"])
        self.assertEqual(protocols("https://example.com/sseverything/mcp"), ["http", "sse"])
        self.assertEqual(protocols("https://mcp.example.com/mcp", "sse"), ["sse"])


if __name__ == "__main__":
    unittest.main()
