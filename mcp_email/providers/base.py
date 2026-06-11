"""
Email provider interface — the contract every backend (Proton, Gmail, …) implements.

GUARDRAILS (enforced by ABSENCE — see INTEGRATION_SPEC §1 / CLAUDE.md):
  The interface exposes ONLY read / mark-read / move. There is deliberately no delete or
  archive method. A provider MUST NOT implement destructive operations. `move_and_mark` is the
  single mutating call and is atomic (mark \\Seen + move to a sibling folder).

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


class EmailProvider(ABC):
    """Read / mark-read / move only. No delete, no archive — by design."""

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

    @abstractmethod
    def get_email(self, message_id: str) -> EmailMessage | None:
        """Fetch one message by its RFC 822 Message-ID. None if not found."""

    @abstractmethod
    def move_and_mark(self, message_id: str, dest_folder: str) -> None:
        """
        Atomically mark the message \\Seen AND move it to `dest_folder`.
        This is the ONLY mutating operation. It never deletes the message — moving relocates it.
        """

    def close(self) -> None:  # optional cleanup hook
        """Release any open connection. Default no-op."""
