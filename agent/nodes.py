"""
Graph nodes + routing functions.

Flow:  classify → critic → gate →  retry (→ classify)  |  route  |  escalate
       route → {write_postings | write_interaction | social_discard} → finalize → END
       escalate → finalize → END

A single `finalize` node performs the one mutation (move_and_mark), so moving mail lives in exactly
one place. The HITL path (ambiguous interaction) sets no destination and ends awaiting resolution.

`Nodes` holds the injected dependencies (LLM, Critic LLM, Writer, Reader, Prompts) so the whole
graph is unit-testable with fakes. Node methods return partial-state dicts (LangGraph merges them).
"""

from __future__ import annotations

import uuid
from typing import Any

from . import matching
from .llm import LLMClient
from .prompts import PromptProvider, wrap_email
from .reader import EmailReaderClient
from .schemas import Category, Classification, Critique
from .state import (
    CONFIDENCE_THRESHOLD,
    MAX_ATTEMPTS,
    MAX_POSTINGS_PER_EMAIL,
    AgentState,
)
from .writer import JobRadarWriter

# category → (writes-postings?, move destination). application_confirmation handled separately.
_CATEGORY_FOLDER = {
    Category.recruiter_outreach: "interaction",
    Category.job_alert: "postings",
    Category.application_confirmation: "interaction",
    Category.network_notification: "social",
}


class Nodes:
    def __init__(
        self,
        llm: LLMClient,
        critic_llm: LLMClient,
        writer: JobRadarWriter,
        reader: EmailReaderClient,
        prompts: PromptProvider,
    ):
        self.llm = llm
        self.critic_llm = critic_llm
        self.writer = writer
        self.reader = reader
        self.prompts = prompts

    # ── classify ⇄ critic loop ────────────────────────────────
    def classify(self, state: AgentState) -> dict[str, Any]:
        email = state["email"]
        user = wrap_email(email["subject"], email["sender"], email["body_text"])
        feedback = state.get("feedback") or []
        if feedback:
            tmpl = self.prompts.get("retry_feedback")
            user = tmpl.format(issues="\n".join(f"- {i}" for i in feedback)) + "\n\n" + user
        attempts = state.get("attempts", 0) + 1
        try:
            classification = self.llm.structured(
                system=self.prompts.get("classifier"), user=user, schema=Classification
            )
        except Exception as exc:  # malformed / unparseable output is a validation failure, not a crash
            return {
                "attempts": attempts,
                "classification": None,
                "feedback": feedback + [f"previous output was not valid structured data: {exc}"],
            }
        return {"attempts": attempts, "classification": classification}

    def critic(self, state: AgentState) -> dict[str, Any]:
        classification = state.get("classification")
        if classification is None:
            return {"critique": Critique(valid=False, issues=["no parseable classification"])}
        email = state["email"]
        user = (
            wrap_email(email["subject"], email["sender"], email["body_text"])
            + "\n\nProposed classification:\n"
            + classification.model_dump_json(indent=2)
        )
        critique = self.critic_llm.structured(
            system=self.prompts.get("critic"), user=user, schema=Critique
        )
        return {"critique": critique}

    # ── routing functions (for conditional edges) ────────────
    @staticmethod
    def gate(state: AgentState) -> str:
        """valid → 'route'; recoverable → 'retry'; exhausted → 'escalate'."""
        classification = state.get("classification")
        critique = state.get("critique")
        confident = classification is not None and classification.confidence >= CONFIDENCE_THRESHOLD
        valid = bool(critique and critique.valid) and confident
        if valid:
            return "route"
        if state.get("attempts", 0) < MAX_ATTEMPTS:
            return "retry"
        return "escalate"

    @staticmethod
    def route_by_category(state: AgentState) -> str:
        cat = state["classification"].category
        if cat == Category.application_confirmation:
            return "write_interaction"
        if cat == Category.network_notification:
            return "social_discard"
        return "write_postings"  # recruiter_outreach | job_alert

    def prepare_retry(self, state: AgentState) -> dict[str, Any]:
        issues = (state.get("critique").issues if state.get("critique") else []) or []
        return {"feedback": (state.get("feedback") or []) + issues}

    # ── terminal actions ──────────────────────────────────────
    def write_postings(self, state: AgentState) -> dict[str, Any]:
        email = state["email"]
        c: Classification = state["classification"]
        reviews = self.writer.get_reviews()
        postings = c.postings[:MAX_POSTINGS_PER_EMAIL]
        truncated = len(c.postings) > MAX_POSTINGS_PER_EMAIL
        payload_postings = []
        for p in postings:
            m = matching.match(p.company, p.role, reviews)
            payload_postings.append({
                "company": p.company, "role": p.role, "link": p.link,
                "action_required": p.action_required,
                "possible_duplicate": m.best is not None,
                "matched_review_id": (m.best or {}).get("review_id"),
            })
        self.writer.create_inbox_entry({
            "message_id": email["message_id"], "subject": email["subject"],
            "sender": email["sender"], "received_at": email["received_at"],
            "category": c.category.value, "confidence": c.confidence,
            "langfuse_trace_id": state.get("langfuse_trace_id"),
            "raw_extracted_json": c.model_dump(mode="json"),
            "postings": payload_postings,
            "truncated": truncated,
        })
        return {"destination": _CATEGORY_FOLDER[c.category], "outcome": "processed"}

    def write_interaction(self, state: AgentState) -> dict[str, Any]:
        email = state["email"]
        c: Classification = state["classification"]
        sig = c.interaction
        reviews = self.writer.get_reviews()
        result = matching.match(sig.company if sig else "", sig.role if sig else None, reviews)

        if result.ambiguous:
            # Pause for human disambiguation: register, post buttons (A-5), exit without moving.
            hitl_id = str(uuid.uuid4())
            candidates = [
                {"review_id": r["review_id"], "label": f'{r.get("company")} — {r.get("title")}'}
                for r in result.candidates if r["_score"] >= matching.STRONG
            ]
            self.writer.register_hitl(hitl_id, candidates)
            return {
                "hitl_id": hitl_id, "hitl_candidates": candidates,
                "destination": None, "outcome": "awaiting_hitl",
            }

        matched = result.best if result.unique_strong else None
        self.writer.record_interaction({
            "message_id": email["message_id"], "subject": email["subject"],
            "sender": email["sender"], "received_at": email["received_at"],
            "category": c.category.value, "confidence": c.confidence,
            "langfuse_trace_id": state.get("langfuse_trace_id"),
            "matched_review_id": (matched or {}).get("review_id"),
            "match_confidence": (matched or {}).get("_score"),
            "new_status": sig.new_status.value if (sig and sig.new_status) else None,
            "timeline_note": sig.summary if sig else "",
        })
        if matched:
            return {"matched_review_id": matched["review_id"],
                    "destination": "interaction", "outcome": "processed"}
        return {"destination": "unprocessed", "outcome": "needs_review",
                "escalation_reason": "no confident job match for status update"}

    def social_discard(self, state: AgentState) -> dict[str, Any]:
        email = state["email"]
        c: Classification = state["classification"]
        self.writer.create_inbox_entry({
            "message_id": email["message_id"], "subject": email["subject"],
            "sender": email["sender"], "received_at": email["received_at"],
            "category": c.category.value, "confidence": c.confidence,
            "langfuse_trace_id": state.get("langfuse_trace_id"),
            "raw_extracted_json": c.model_dump(mode="json"), "postings": [],
        })
        return {"destination": "social", "outcome": "discarded"}

    def escalate(self, state: AgentState) -> dict[str, Any]:
        critique = state.get("critique")
        issues = (critique.issues if critique else []) or ["validation failed"]
        return {
            "destination": "unprocessed", "outcome": "needs_review",
            "escalation_reason": "; ".join(issues),
        }

    def finalize(self, state: AgentState) -> dict[str, Any]:
        """The single mutation point: move the email if a destination was chosen."""
        dest = state.get("destination")
        if dest:
            self.reader.move_and_mark(state["email"]["message_id"], dest)
        return {}
