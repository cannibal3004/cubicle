"""The computer driver's MCP bridge, run for real: node starts driver.mjs,
the driver spawns the probe server over stdio, and the same MCP client the
API uses talks to it over streamable HTTP.

Needs node and the runtime's node modules (`npm install` in runtime/);
skipped otherwise.
"""

import asyncio
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[2]
DRIVER = ROOT / "runtime" / "driver.mjs"
PROBE = str(Path(__file__).resolve().parent / "fixtures" / "mcp_probe_server.py")
NODE = shutil.which("node")
TOKEN = "bridge-test-token"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@unittest.skipUnless(NODE and (ROOT / "runtime" / "node_modules" / "playwright").exists(), "node and runtime/node_modules are needed")
class McpBridgeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.port = _free_port()
        env = {
            **os.environ,
            "PORT": str(self.port),
            "COMPUTER_TOKEN": TOKEN,
            "WORKSPACE": self.temp.name,
            "COMPUTER_ID": "computer-test",
            "PROBE_SECRET": "from-the-computer-env",
        }
        self.driver = subprocess.Popen([NODE, str(DRIVER)], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, cwd=str(ROOT / "runtime"))
        self.base = f"http://127.0.0.1:{self.port}"
        self.headers = {"x-computer-token": TOKEN}
        async with httpx.AsyncClient(timeout=5) as client:
            for _ in range(100):
                if self.driver.poll() is not None:
                    raise RuntimeError(self.driver.stdout.read().decode(errors="replace"))
                try:
                    if (await client.get(f"{self.base}/health", headers=self.headers)).status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(0.1)
            else:
                raise RuntimeError("the driver did not come up")

    async def asyncTearDown(self):
        self.driver.kill()
        self.driver.wait()
        self.temp.cleanup()

    async def _post(self, route, body, timeout=20):
        async with httpx.AsyncClient(timeout=timeout) as client:
            return await client.post(f"{self.base}{route}", json=body, headers=self.headers)

    async def test_start_talk_and_stop(self):
        # Start the probe server inside the "computer"; $NAME resolves from its environment.
        started = await self._post("/mcp/start", {"id": "mcp-probe", "command": sys.executable, "args": [PROBE], "env": {"PROBE_TOKEN": "$PROBE_SECRET"}})
        self.assertEqual(started.status_code, 200, started.text)
        self.assertTrue(started.json()["started"])
        # Starting it again with the same definition is a no-op; a token is required.
        self.assertFalse((await self._post("/mcp/start", {"id": "mcp-probe", "command": sys.executable, "args": [PROBE], "env": {"PROBE_TOKEN": "$PROBE_SECRET"}})).json()["started"])
        async with httpx.AsyncClient(timeout=5) as client:
            self.assertEqual((await client.post(f"{self.base}/mcp/start", json={})).status_code, 401)
            # The endpoint offers no server-to-client stream.
            self.assertEqual((await client.get(f"{self.base}/mcp/mcp-probe", headers=self.headers)).status_code, 405)

        # The API's own client over the bridge.
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client

        async with httpx.AsyncClient(headers=self.headers, timeout=httpx.Timeout(30, connect=5)) as client:
            async with streamable_http_client(f"{self.base}/mcp/mcp-probe", http_client=client) as streams:
                async with ClientSession(streams[0], streams[1]) as session:
                    init = await session.initialize()
                    self.assertEqual(init.server_info.name, "probe")
                    tools = sorted(t.name for t in (await session.list_tools()).tools)
                    self.assertEqual(tools, ["add", "secret_name", "shout"])
                    result = await session.call_tool("add", {"a": 2, "b": 3})
                    self.assertIn("5", result.content[0].text)
                    secret = await session.call_tool("secret_name", {})
                    self.assertIn("from-the-computer-env", secret.content[0].text)

        statuses = (await httpx.AsyncClient(timeout=5).get(f"{self.base}/mcp/servers", headers=self.headers)).json()["servers"]
        self.assertTrue(statuses["mcp-probe"]["running"])

        # A missing secret is refused before anything starts; a bad command dies and says so.
        missing = await self._post("/mcp/start", {"id": "mcp-missing", "command": sys.executable, "args": [PROBE], "env": {"PROBE_TOKEN": "$NOT_SET_ANYWHERE"}})
        self.assertEqual(missing.status_code, 400)
        self.assertIn("NOT_SET_ANYWHERE is not set", missing.json()["error"])
        dead = await self._post("/mcp/start", {"id": "mcp-dead", "command": sys.executable, "args": ["-c", "import sys; sys.stderr.write('boom\\n'); sys.exit(3)"]})
        self.assertEqual(dead.status_code, 200)
        await asyncio.sleep(0.8)
        reply = await self._post("/mcp/mcp-dead", {"jsonrpc": "2.0", "id": 1, "method": "ping"})
        self.assertEqual(reply.status_code, 503)
        self.assertIn("code 3", reply.json()["error"])
        self.assertIn("boom", reply.json()["error"])

        # Stop: the endpoint is gone.
        self.assertTrue((await self._post("/mcp/stop", {"id": "mcp-probe"})).json()["stopped"])
        self.assertEqual((await self._post("/mcp/mcp-probe", {"jsonrpc": "2.0", "id": 2, "method": "ping"})).status_code, 404)


if __name__ == "__main__":
    unittest.main()
