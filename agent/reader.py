"""
Email Reader client seam — what the agent consumes from MCP Server 1.

Production wraps the stdio MCP tools (`get_unread_emails`, `move_and_mark`). Tests use `FakeReader`
which records moves and serves canned unread mail, so the graph runs without a live mailbox.
"""

from __future__ import annotations

from typing import Protocol

from .state import Destination, EmailRef


class EmailReaderClient(Protocol):
    def get_unread(self) -> list[EmailRef]:
        """Unread mail in the configured root folder."""
        ...

    def move_and_mark(self, message_id: str, destination: Destination) -> None:
        """Atomically mark read + move to a logical subfolder. The only mutation."""
        ...


class FakeReader:
    def __init__(self, unread: list[EmailRef] | None = None):
        self._unread = unread or []
        self.moves: list[tuple[str, str]] = []

    def get_unread(self) -> list[EmailRef]:
        return list(self._unread)

    def move_and_mark(self, message_id: str, destination: Destination) -> None:
        self.moves.append((message_id, destination))
