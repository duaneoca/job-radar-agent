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

    def move_and_mark(self, message_id: str, destination: Destination,
                      mark_read: bool = True) -> None:
        """Move to a logical subfolder, optionally marking read. The only mutation."""
        ...


class ProviderReader:
    """
    Adapts an EmailProvider (e.g. ProtonProvider) to EmailReaderClient for direct local use.

    (The MCP-consuming path — connecting to the Email Reader stdio server as an MCP client — is a
    thin follow-up wrapper; this exercises the same provider code the MCP server publishes.)
    """

    def __init__(self, provider, *, root: str, dest_folders: dict, since_days=None, limit=None):
        self._p = provider
        self._root = root
        self._dest = dest_folders          # {"interaction": "<path>", ...}
        self._since_days = since_days
        self._limit = limit

    def get_unread(self) -> list[EmailRef]:
        out: list[EmailRef] = []
        for m in self._p.get_unread(self._root, since_days=self._since_days, limit=self._limit):
            out.append({
                "message_id": m.message_id, "subject": m.subject, "sender": m.sender,
                "received_at": m.received_at.isoformat() if m.received_at else None,
                "body_text": m.body_text, "has_attachments": m.has_attachments,
            })
        return out

    def move_and_mark(self, message_id: str, destination: Destination,
                      mark_read: bool = True) -> None:
        self._p.move_and_mark(message_id, self._dest[destination], mark_read=mark_read)


class FakeReader:
    def __init__(self, unread: list[EmailRef] | None = None):
        self._unread = unread or []
        self.moves: list[tuple[str, str]] = []
        self.mark_reads: list[bool] = []

    def get_unread(self) -> list[EmailRef]:
        return list(self._unread)

    def move_and_mark(self, message_id: str, destination: Destination,
                      mark_read: bool = True) -> None:
        self.moves.append((message_id, destination))
        self.mark_reads.append(mark_read)
