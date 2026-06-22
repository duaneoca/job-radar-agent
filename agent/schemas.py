"""
Structured outputs for the agent's LLM calls and the routing decisions built on them.

Two LLM calls produce structured data:
  • Classifier → `Classification`  (category + extracted content + self-justified confidence)
  • Critic     → `Critique`        (validity verdict + concrete issues for the retry feedback)

Email *metadata* (message_id, subject, sender, received_at) comes from the EmailReader, NOT the
LLM — the model only extracts content. Confidence is the model's own justified score; the email
text must never be able to set it. [C1]
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class Category(str, Enum):
    recruiter_outreach = "recruiter_outreach"
    application_confirmation = "application_confirmation"
    job_alert = "job_alert"
    network_notification = "network_notification"


# Agent-writable status subset (server also enforces this). [C1/D9]
class WritableStatus(str, Enum):
    applied = "applied"
    interviewing = "interviewing"
    offer = "offer"
    rejected = "rejected"


class Posting(BaseModel):
    """One job posting extracted from a job_alert / recruiter_outreach email."""

    company: str
    role: str
    link: str | None = None          # http/https only; validated at write time [C2]
    action_required: bool = False


class RecruiterContact(BaseModel):
    """One recruiter contact card extracted from a recruiter_outreach email (the sender). [§3.5]

    Email-level: the whole email is this recruiter pitching these roles, so every posting is
    attributable to them. All fields optional except name; the agent omits what it can't find rather
    than guessing, caps lengths, and strips markup before sending (see nodes._clean_recruiter)."""

    name: str
    email: str | None = None
    phone: str | None = None              # verbatim — not normalized
    employer: str | None = None           # recruiter's own firm/agency (or hiring company if in-house)
    title: str | None = None
    linkedin_url: str | None = None       # http/https only; safeHref'd by job-radar [C2]
    is_agency: bool | None = None         # agent inference; null if unsure
    represents: list[str] = Field(default_factory=list)   # client companies named
    recruiter_confidence: float | None = None             # extraction confidence (≠ classification)


class InteractionSignal(BaseModel):
    """A status update extracted from an application_confirmation email."""

    company: str
    role: str | None = None
    # None ⇒ activity with no status change (reminder / reschedule / "next round") [D10]
    new_status: WritableStatus | None = None
    summary: str = ""                # short human-readable note → TimelineEvent text


class Classification(BaseModel):
    """Classifier output. Exactly one of postings / interaction is populated per category."""

    category: Category
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str                              # justifies the category AND the confidence [C1]
    postings: list[Posting] = Field(default_factory=list)
    interaction: InteractionSignal | None = None


class Critique(BaseModel):
    """Critic verdict over a Classification given the same email."""

    valid: bool
    issues: list[str] = Field(default_factory=list)     # fed back to the Classifier on retry [D3]
    suggested_category: Category | None = None
