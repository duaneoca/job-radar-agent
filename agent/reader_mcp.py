"""
McpReaderClient — consume the Email Reader MCP (Server 1) over stdio.

This is the "agent genuinely consumes an MCP server at runtime" path. It spawns
`python -m mcp_email.server` as a subprocess and calls its tools (`get_unread_emails`,
`move_and_mark`) over the MCP stdio transport, exposing the same `EmailReaderClient` interface as the
direct `ProviderReader`. Selectable via `EMAIL_READER_TRANSPORT=mcp`.

The MCP client is async; the agent runner is sync, so we hold a dedicated event loop and an
`AsyncExitStack` keeping the stdio_client + ClientSession open across calls (closed on `.close()`).
"""

from __future__ import annotations

import asyncio
import json
import os
from contextlib import AsyncExitStack
from typing import Any

from .state import Destination, EmailRef


def _parse_tool_result(result: Any) -> Any:
    """FastMCP returns tool output as TextContent JSON; structuredContent when available."""
    sc = getattr(result, "structuredContent", None)
    if sc is not None:
        # FastMCP wraps non-dict returns under {"result": ...}
        return sc.get("result", sc) if isinstance(sc, dict) else sc
    for block in getattr(result, "content", []) or []:
        text = getattr(block, "text", None)
        if text:
            try:
                return json.loads(text)
            except ValueError:
                return text
    return None


class McpReaderClient:
    def __init__(self, command: str = "python", args: list[str] | None = None,
                 env: dict[str, str] | None = None):
        self._command = command
        self._args = args or ["-m", "mcp_email.server"]
        self._env = {**os.environ, **(env or {})}
        self._loop = asyncio.new_event_loop()
        self._stack: AsyncExitStack | None = None
        self._session = None

    # ── async plumbing ────────────────────────────────────────
    async def _ensure(self):
        if self._session is not None:
            return
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        self._stack = AsyncExitStack()
        params = StdioServerParameters(command=self._command, args=self._args, env=self._env)
        read, write = await self._stack.enter_async_context(stdio_client(params))
        self._session = await self._stack.enter_async_context(ClientSession(read, write))
        await self._session.initialize()

    async def _acall(self, tool: str, args: dict) -> Any:
        await self._ensure()
        return _parse_tool_result(await self._session.call_tool(tool, args))

    def _call(self, tool: str, args: dict) -> Any:
        return self._loop.run_until_complete(self._acall(tool, args))

    # ── EmailReaderClient interface ───────────────────────────
    def get_unread(self) -> list[EmailRef]:
        data = self._call("get_unread_emails", {})
        return data or []

    def move_and_mark(self, message_id: str, destination: Destination,
                      mark_read: bool = True) -> None:
        self._call("move_and_mark",
                   {"message_id": message_id, "destination": destination, "mark_read": mark_read})

    def close(self) -> None:
        if self._stack is not None:
            try:
                self._loop.run_until_complete(self._stack.aclose())
            except Exception:
                pass
            self._stack = None
            self._session = None
        try:
            self._loop.close()
        except Exception:
            pass
