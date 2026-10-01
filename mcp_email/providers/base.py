"""
Email provider interface — the contract every backend (Proton, Gmail, …) implements.

GUARDRAILS (see INTEGRATION_SPEC §5 [R1] / CLAUDE.md):
  Per-message mutations are read / mark-read / move only — `move_and_mark` is atomic (mark \\Seen +
  move to a sibling folder). There is NO permanent delete and NO archive, anywhere.
  The one exception is `trash_expired`: a RETENTION sweep that moves mail older than N days (and not
  starred) from one folder to the provider's Trash, recoverable until the provider empties Trash. It
  takes no message ids — eligibility is decided by the mail server's own date and flags, so nothing a
  model outputs can select a message for it — and it is never exposed as an MCP tool.

IDEMPOTENCY:
  `EmailMessage.message_id` is the RFC 822 `Message-ID` header — stable across folder moves and
  globally unique. It is the idempotency key everywhere downstream. The provider-native handle
  (IMAP UID / Gmail id) is `native_id` and is NOT stable across moves; never use it as a key.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class EmailMessage:
    """A single email, normalized across providers. Body is text only (no attachments)."""

    message_id: str                      # RFC 822 Message-ID — idempotency key
    native_id: str                       # IMAP UID / Gmail id — per-folder, NOT stable across moves
    folder: str                          # folder the message currently lives in
    subject: str
    sender: str                          # From header (display + addr as received)
    received_at: datetime | None
    body_text: str                       # text/plain, or sanitized text/html → text fallback
    has_attachments: bool = False        # flagged, never parsed (M1)
    headers: dict[str, str] = field(default_factory=dict)
    body_html: str = ""                  # raw text/html part (V2 link extraction); never rendered
    # Every Authentication-Results header, TOP-DOWN. A dict would keep only the last (bottom-most)
    # one — the one a forger controls — so the sender check needs the ordered list.
    auth_results: list[str] = field(default_factory=list)


class EmailProvider(ABC):
    """Read / mark-read / move, plus the retention sweep to Trash. No permanent delete — by design."""

    @abstractmethod
    def list_folders(self) -> list[str]:
        """Return all folder paths visible to this account."""

    @abstractmethod
    def get_unread(
        self, folder: str, since_days: int | None = None, limit: int | None = None
    ) -> list[EmailMessage]:
        """
        Return UNREAD messages in `folder`, newest-first. Read = the human owns it; agent skips it.

        `since_days` — ignore messages received more than this many days ago. Applied at the server
        (IMAP SINCE / Gmail query) so an old backlog is never fetched or classified. This is the
        first-run cost control: an ancient unread pile is invisible to the agent. None ⇒ no cutoff.
        `limit` — cap the number returned (newest-first), e.g. MAX_EMAILS_PER_RUN. None ⇒ no cap.
        """

    def get_recent(self, folder: str, limit: int) -> list[EmailMessage]:
        """
        The `limit` most recent messages in `folder`, READ OR UNREAD, newest-first. Read-only and
        offline-evaluation only (scripts/eval_sorter.py samples already-sorted folders as ground
        truth); the agent's runtime path never calls it. Must not change any flag.
        """
        raise NotImplementedError

    @abstractmethod
    def get_email(self, message_id: str) -> EmailMessage | None:
        """Fetch one message by its RFC 822 Message-ID. None if not found."""

    @abstractmethod
    def move_and_mark(self, message_id: str, dest_folder: str, mark_read: bool = True) -> None:
        """
        Move the message to `dest_folder`, optionally marking it read.
        `mark_read=True` (default) marks \\Seen + moves; `mark_read=False` leaves it UNREAD (used for
        the Interaction folder, so high-value interactions stay visibly unread). The only mutating
        operation; never deletes — moving relocates. (Safe: moved mail leaves the scanned root folder,
        so unread-after-move is never reprocessed.)
        """

    @abstractmethod
    def mark_read(self, message_id: str) -> None:
        """Mark the message read WITHOUT moving it (V2: processed mail stays in the Postings folder)."""

    def trash_expired(self, folder: str, older_than_days: int, limit: int,
                      dry_run: bool = False) -> int:
        """
        RETENTION: move up to `limit` messages in `folder` received more than `older_than_days` days
        ago (server date, read OR unread) and NOT starred/flagged to the provider's Trash. Returns the
        count moved (or, with `dry_run`, that would be). Never permanently deletes; refuses to run on
        the Trash or the Inbox. Only the agent's retention stage calls this, for an allow-listed folder.
        """
        raise NotImplementedError

    def close(self) -> None:  # optional cleanup hook
        """Release any open connection. Default no-op."""
