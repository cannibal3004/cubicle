"""A tiny MCP server over stdio for the tests: one read-only tool, one unmarked."""

import os

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

server = MCPServer("probe")


@server.tool(annotations=ToolAnnotations(readOnlyHint=True))
def add(a: int, b: int) -> int:
    """Add two numbers."""
    return a + b


@server.tool()
def shout(text: str) -> str:
    """Upper-case text (no hints)."""
    return text.upper()


@server.tool(annotations=ToolAnnotations(readOnlyHint=True))
def secret_name() -> str:
    """The value of the PROBE_TOKEN environment variable, to prove secrets arrive."""
    return os.environ.get("PROBE_TOKEN", "(unset)")


if __name__ == "__main__":
    server.run(transport="stdio")
