"""An MCP server that speaks only the older SSE protocol, for tests: python mcp_sse_server.py <port> <sse path>."""

import sys

import uvicorn
from mcp.server.mcpserver import MCPServer

server = MCPServer("sse-probe")


@server.tool()
def remember(text: str) -> str:
    """Keep a note."""
    return f"kept: {text}"


if __name__ == "__main__":
    uvicorn.run(server.sse_app(sse_path=sys.argv[2]), host="127.0.0.1", port=int(sys.argv[1]), log_level="error")
