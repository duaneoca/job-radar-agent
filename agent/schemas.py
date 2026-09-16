"""
Structured output of the V2 pipeline's single LLM call (the link picker).

The model receives a numbered list of links extracted from the email and answers with the numbers
of the links that are individual job postings, plus the job title and company it read for each.
It never returns a URL — the URL is looked up from the extracted list by number — and every value
it does return is checked against the email by the deterministic verifier (`agent/verify.py`).

Email metadata (message_id, subject, sender, received_at) comes from the mailbox, never the model.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class PickedPosting(BaseModel):
    """One job posting the model identified in the link list."""

    link_id: int = Field(description="The number in brackets of the job-title link, e.g. 7 for [7].")
    title: str = Field(description="The job title, copied exactly from that link's text.")
    company: str = Field(description="The hiring company, copied exactly from the text near that link.")


class LinkPicks(BaseModel):
    """All job postings found in the email (empty if there are none)."""

    postings: list[PickedPosting] = Field(default_factory=list)
