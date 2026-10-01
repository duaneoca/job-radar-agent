"""
Email Reader client seam — what the agent consumes from MCP Server 1.

V2 reads ONE folder (the Postings folder your mail filter sorts job alerts into) and makes at most two
kinds of change: mark a processed email read in place, or move a problem email to Unprocessed.

`ProviderReader` adapts an in-process provider; `McpReaderClient` (reader_mcp.py) speaks to the stdio
MCP server; `FakeReader` serves canned mail for tests.
"""

from __future__ import annotations

from typing import Protocol

from .state import Destination, EmailRef


class EmailReaderClient(Protocol):
    def get_unread(self) -> list[EmailRef]:
        """Unread mail in the source (Postings) folder, newest-first."""
        ...

    def mark_read(self, message_id: str) -> None:
        """Mark read in place (processed mail stays in the source folder)."""
        ...

    def move_and_mark(self, message_id: str, destination: Destination,
                      mark_read: bool = True) -> None:
        """Move to a logical destination folder (V2: only 'unprocessed')."""
        ...


def to_ref(m) -> EmailRef:
    return {
        "message_id": m.message_id, "subject": m.subject, "sender": m.sender,
        "received_at": m.received_at.isoformat() if m.received_at else None,
        "body_text": m.body_text, "body_html": m.body_html,
        "auth_results": list(m.auth_results), "has_attachments": m.has_attachments,
    }


class ProviderReader:
    """Adapts an EmailProvider (Proton / Gmail / IMAP) to EmailReaderClient."""

    def __init__(self, provider, *, source: str, dest_folders: dict, since_days=None, limit=None):
        self._p = provider
        self._source = source
        self._dest = dest_folders          # {"unprocessed": "<full folder path>"}
        self._since_days = since_days
        self._limit = limit

    def get_unread(self) -> list[EmailRef]:
        return [to_ref(m) for m in
                self._p.get_unread(self._source, since_days=self._since_days, limit=self._limit)]

    def mark_read(self, message_id: str) -> None:
        self._p.mark_read(message_id)

    def move_and_mark(self, message_id: str, destination: Destination,
                      mark_read: bool = True) -> None:
        self._p.move_and_mark(message_id, self._dest[destination], mark_read=mark_read)

    def close(self) -> None:
        if hasattr(self._p, "close"):
            self._p.close()


class FakeReader:
    def __init__(self, unread: list[EmailRef] | None = None):
        self._unread = unread or []
        self.moves: list[tuple[str, str]] = []
        self.marked_read: list[str] = []

    def get_unread(self) -> list[EmailRef]:
        return list(self._unread)

    def mark_read(self, message_id: str) -> None:
        self.marked_read.append(message_id)

    def move_and_mark(self, message_id: str, destination: Destination,
                      mark_read: bool = True) -> None:
        self.moves.append((message_id, destination))
