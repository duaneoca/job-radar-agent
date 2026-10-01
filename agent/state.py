"""
LangGraph state for processing a single email (V2 link-picker pipeline).

The graph processes one email per state (the runner fans out over unread mail), which keeps each
Langfuse trace scoped to one message.

Hard-coded guardrails (NOT user-configurable):
  MAX_ATTEMPTS           — link-picker attempts before the email goes to Unprocessed
  MAX_POSTINGS_PER_EMAIL — postings beyond this are truncated
"""

from __future__ import annotations

from typing import Any, Literal, Optional, TypedDict

from .extract import LinkCandidate
from .schemas import LinkPicks

MAX_ATTEMPTS = 3
MAX_POSTINGS_PER_EMAIL = 30

# The only move V2 makes. Successfully processed mail is marked read and stays where it is.
Destination = Literal["unprocessed"]

# Terminal disposition of an email after the graph runs.
Outcome = Literal["processed", "no_postings", "duplicates_only", "needs_review"]


class EmailRef(TypedDict, total=False):
    """Metadata + content from the mailbox — the source of truth, never the LLM."""

    message_id: str            # RFC 822 Message-ID — idempotency key [L1]
    subject: str
    sender: str
    received_at: Optional[str]
    body_text: str
    body_html: str
    auth_results: list[str]    # Authentication-Results headers, top-down
    has_attachments: bool


class AgentState(TypedDict, total=False):
    # input
    email: EmailRef

    # screening + extraction
    candidates: list[LinkCandidate]

    # pick ⇄ verify loop
    attempts: int
    picks: Optional[LinkPicks]
    issues: list[str]          # verifier findings from the latest attempt
    feedback: list[str]        # corrective instructions for the next attempt (no model output)
    verified: list[dict[str, Any]]
    truncated: bool

    # outcome
    outcome: Optional[Outcome]
    destination: Optional[Destination]
    mark_read: bool
    reason: Optional[str]      # why the email went to Unprocessed (safe to log: no model output)
    inbox_email_id: Optional[str]
    postings_written: int
    duplicates_skipped: int

    # observability
    langfuse_trace_id: Optional[str]
