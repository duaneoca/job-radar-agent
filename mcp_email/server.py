"""
Email Reader MCP server (Server 1) — stdio transport.

Exposes a deliberately small tool surface to the agent (V2):
    list_folders()                         — all folders + the resolved layout
    get_unread_emails()                    — UNREAD mail in the Postings (source) folder only
    get_email_by_id(message_id)            — fetch one message by RFC 822 Message-ID
    mark_read(message_id)                  — mark read in place (processed mail)
    move_and_mark(message_id, destination) — move to Unprocessed (the only move)

There is no delete/archive tool, by design (guardrail by absence). `destination` is validated so the
agent cannot move mail anywhere except the configured Unprocessed folder.

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

            _provider = GmailProvider(
                token_file=settings.gmail_token_file,
                credentials_file=settings.gmail_credentials_file,
                root_folder=folders.source,
            )
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
    missing = [f for f in folders.v2_folders() if f not in existing]
    return {
        "folders": existing,
        "layout": {
            "source": folders.source,
            "unprocessed": folders.unprocessed,
        },
        # The agent does NOT create folders — the user does. Surface missing ones as a warning. [D6]
        "missing_folders": missing,
    }


@mcp.tool()
def get_unread_emails(limit: int | None = None) -> list[dict]:
    """
    Return UNREAD emails in the Postings (source) folder only, NEWEST-FIRST. Read mail is the human's;
    the agent leaves it alone. To hand an already-read email to the agent, mark it unread.

    Honors MAX_EMAIL_AGE_DAYS (old backlog is never fetched) and caps the result at `limit` or
    MAX_EMAILS_PER_RUN — so a full folder on the first run stays bounded and cheap.
    """
    provider = _get_provider()
    since = settings.max_email_age_days if settings.max_email_age_days > 0 else None
    cap = limit or settings.max_emails_per_run
    return [_serialize(m) for m in provider.get_unread(folders.source, since_days=since, limit=cap)]


@mcp.tool()
def get_email_by_id(message_id: str) -> dict | None:
    """Fetch a single email by its RFC 822 Message-ID (the stable idempotency key)."""
    provider = _get_provider()
    msg = provider.get_email(message_id)
    return _serialize(msg) if msg else None


@mcp.tool()
def mark_read(message_id: str) -> dict:
    """Mark an email read without moving it (V2: processed mail stays in the Postings folder)."""
    _get_provider().mark_read(message_id)
    return {"marked_read": message_id}


@mcp.tool()
def move_and_mark(message_id: str, destination: str, mark_read: bool = True) -> dict:
    """
    Move an email to the Unprocessed folder (the only destination V2 uses), optionally marking it
    read. `destination` is a logical name, validated against the configured layout. Never deletes.
    """
    dest_map = {"unprocessed": folders.unprocessed}
    if destination not in dest_map:
        raise ValueError(
            f"invalid destination '{destination}'; expected one of {sorted(dest_map)}"
        )
    provider = _get_provider()
    provider.move_and_mark(message_id, dest_map[destination], mark_read=mark_read)
    return {"moved": message_id, "to": dest_map[destination], "marked_read": mark_read}


def main() -> None:
    mcp.run()  # stdio transport


if __name__ == "__main__":
    main()
