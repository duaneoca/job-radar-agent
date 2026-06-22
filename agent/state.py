"""
LangGraph state for processing a single email.

The graph processes one email per state (the top-level run fans out over unread mail). Keeping
state single-email keeps the Langfuse trace and the HITL checkpoint scoped to one message.

Constants here encode hard-coded guardrails (NOT user-configurable):
  CONFIDENCE_THRESHOLD, MAX_ATTEMPTS, MAX_POSTINGS_PER_EMAIL.
"""

from __future__ import annotations

from typing import Any, Literal, Optional, TypedDict

from .schemas import Classification, Critique

CONFIDENCE_THRESHOLD = 0.75      # below this ⇒ treat as invalid, retry/escalate [design]
MAX_ATTEMPTS = 2                 # classifier attempts before escalation [D3]
MAX_POSTINGS_PER_EMAIL = 30      # truncate beyond this + log warning [D11]

# Logical move destinations exposed by the Email Reader MCP.
Destination = Literal["interaction", "postings", "social", "unprocessed"]

# Terminal disposition of an email after the graph runs.
Outcome = Literal["processed", "needs_review", "discarded", "awaiting_hitl"]


class EmailRef(TypedDict):
    """Metadata from the EmailReader — the source of truth for identity, never the LLM."""

    message_id: str            # RFC 822 Message-ID — idempotency key [L1]
    subject: str
    sender: str
    received_at: Optional[str]
    body_text: str
    has_attachments: bool


class AgentState(TypedDict, total=False):
    # input
    email: EmailRef

    # classify ⇄ critic loop
    attempts: int
    classification: Optional[Classification]
    critique: Optional[Critique]
    feedback: list[str]                 # accumulated critic issues fed back to the classifier

    # routing / outcome
    destination: Optional[Destination]
    outcome: Optional[Outcome]
    escalation_reason: Optional[str]
    inbox_email_id: Optional[str]       # returned by create_inbox_entry; for notification deep links
    recruiter: Optional[dict[str, Any]] # recruiter card (recruiter_outreach only); cleaned dict [§3.5]

    # HITL (ambiguous interaction match)
    hitl_id: Optional[str]
    hitl_candidates: list[dict[str, Any]]   # [{review_id, label}]
    matched_review_id: Optional[str]

    # observability
    langfuse_trace_id: Optional[str]
