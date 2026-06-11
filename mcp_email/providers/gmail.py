"""
Gmail provider — Gmail API via OAuth2 (cloud multi-user path).

STATUS: stub. To be implemented in the cloud-path phase. The Email Reader interface and the
Proton provider are the local-path priority; Gmail follows.

Design notes for the implementation (so the contract stays honest):
  • Scope MUST be minimal — `gmail.modify` is sufficient for label add/remove + mark-read, but it
    also permits trashing; never request full `https://mail.google.com/`. [H5]
  • "Folders" map to Gmail LABELS. The root/subfolder config names become label names.
  • `move_and_mark` = remove the root label, add the destination label, remove UNREAD — Gmail has
    no destructive move; we relabel. Never call messages.delete / messages.trash. [guardrail]
  • `message_id` = the RFC 822 Message-ID header (stable); `native_id` = the Gmail message id.
  • Parse text/plain or sanitized text/html only; never fetch attachments / remote content. [M1]
  • Credentials (OAuth refresh token) come from job-radar `email_credentials`, fetched IN-CLUSTER
    via GET /agent/config — never over the Cloudflare edge. [H6a]
"""

from __future__ import annotations

from .base import EmailMessage, EmailProvider


class GmailProvider(EmailProvider):
    def __init__(self, *_, **__):
        raise NotImplementedError(
            "GmailProvider is a stub — implemented in the cloud-path phase. "
            "Use EMAIL_PROVIDER=proton for the local self-host path."
        )

    def list_folders(self) -> list[str]:  # pragma: no cover - stub
        raise NotImplementedError

    def get_unread(  # pragma: no cover - stub
        self, folder: str, since_days: int | None = None, limit: int | None = None
    ) -> list[EmailMessage]:
        # Impl note: map to a Gmail query, e.g. `label:<folder> is:unread newer_than:{since_days}d`,
        # ordered newest-first, capped at `limit`.
        raise NotImplementedError

    def get_email(self, message_id: str) -> EmailMessage | None:  # pragma: no cover - stub
        raise NotImplementedError

    def move_and_mark(self, message_id: str, dest_folder: str) -> None:  # pragma: no cover - stub
        raise NotImplementedError
