"""
Email Reader MCP server (Server 1) — stdio transport.

Exposes a deliberately small, read-mostly tool surface to the LangGraph agent:
    list_folders()                         — all folders + the resolved Job Radar layout
    get_unread_emails()                    — UNREAD mail in the configured ROOT folder only
    get_email_by_id(message_id)            — fetch one message by RFC 822 Message-ID
    move_and_mark(message_id, destination) — atomic mark-read + move (the ONLY mutation)

There is no delete/archive tool, by design (guardrail by absence). `destination` is validated
against the configured subfolders so the agent cannot move mail to arbitrary locations.

Run:  python -m mcp_email.server     (stdio; launched by the agent / docker-compose)
"""

from __future__ import annotations

import dataclasses

from mcp.server.fastmcp import FastMCP

from .config import folders, settings
from .providers.base import EmailMessage, EmailProvider

mcp = FastMCP("job-radar-email-reader")

_provider: EmailProvider | None = None


def _get_provider() -> EmailProvider:
    global _provider
    if _provider is None:
        if settings.email_provider == "proton":
            from .providers.proton import ProtonProvider

            _provider = ProtonProvider(
                host=settings.proton_imap_host,
                port=settings.proton_imap_port,
                user=settings.proton_imap_user,
                password=settings.proton_imap_password,
            )
        elif settings.email_provider == "gmail":
            from .providers.gmail import GmailProvider

            _provider = GmailProvider()
        else:
            raise ValueError(f"unknown EMAIL_PROVIDER: {settings.email_provider}")
    return _provider


def _serialize(m: EmailMessage) -> dict:
    d = dataclasses.asdict(m)
    d["received_at"] = m.received_at.isoformat() if m.received_at else None
    return d


@mcp.tool()
def list_folders() -> dict:
    """List all mailbox folders and the resolved Job Radar folder layout (root + subfolders)."""
    provider = _get_provider()
    existing = provider.list_folders()
    expected = folders.all_subfolders()
    missing = [f for f in [folders.root, *expected] if f not in existing]
    return {
        "folders": existing,
        "layout": {
            "root": folders.root,
            "interaction": folders.interaction,
            "postings": folders.postings,
            "social": folders.social,
            "unprocessed": folders.unprocessed,
        },
        # The agent does NOT create folders — the user does. Surface missing ones as a warning. [D6]
        "missing_folders": missing,
    }


@mcp.tool()
def get_unread_emails() -> list[dict]:
    """
    Return UNREAD emails in the configured ROOT folder only. Read mail is the human's; the agent
    leaves it alone. To hand an already-read email to the agent, mark it unread.
    """
    provider = _get_provider()
    return [_serialize(m) for m in provider.get_unread(folders.root)]


@mcp.tool()
def get_email_by_id(message_id: str) -> dict | None:
    """Fetch a single email by its RFC 822 Message-ID (the stable idempotency key)."""
    provider = _get_provider()
    msg = provider.get_email(message_id)
    return _serialize(msg) if msg else None


@mcp.tool()
def move_and_mark(message_id: str, destination: str) -> dict:
    """
    Atomically mark an email read AND move it to one of the Job Radar subfolders
    (interaction | postings | social | unprocessed). This is the only mutating action; it never
    deletes mail. `destination` is a logical name, validated against the configured layout.
    """
    dest_map = {
        "interaction": folders.interaction,
        "postings": folders.postings,
        "social": folders.social,
        "unprocessed": folders.unprocessed,
    }
    if destination not in dest_map:
        raise ValueError(
            f"invalid destination '{destination}'; expected one of {sorted(dest_map)}"
        )
    provider = _get_provider()
    provider.move_and_mark(message_id, dest_map[destination])
    return {"moved": message_id, "to": dest_map[destination]}


def main() -> None:
    mcp.run()  # stdio transport


if __name__ == "__main__":
    main()
