"""Disposable external MCP process for stdio integration tests."""

import asyncio
import sys

from mcp.server.mcpserver import MCPServer

server = MCPServer("echo")


@server.tool()
async def echo(text: str) -> str:
    return text.upper()


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--http":
        asyncio.run(server.run_streamable_http_async(port=int(sys.argv[2])))
    else:
        asyncio.run(server.run_stdio_async())
