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

import re

from . import matching
from .links import clean_link
from .llm import LLMClient
from .prompts import PromptProvider, wrap_email
from .reader import EmailReaderClient
from .schemas import Category, Classification, Critique, RecruiterContact
from .state import (
    CONFIDENCE_THRESHOLD,
    MAX_ATTEMPTS,
    MAX_POSTINGS_PER_EMAIL,
    AgentState,
)
from .writer import JobRadarWriter

_TAG = re.compile(r"<[^>]+>")
# Field length caps from INTEGRATION_SPEC §3.5 — we truncate (not reject) so a long signature line
# never fails best-effort enrichment; job-radar caps again on its side [C2r].
_RECRUITER_CAPS = {"name": 200, "email": 255, "phone": 50, "employer": 200,
                   "title": 200, "linkedin_url": 500}


def _clean_recruiter(rc: RecruiterContact) -> dict[str, Any] | None:
    """Cap lengths, strip markup, drop empties/non-http linkedin — the C2r agent-side contract.

    Returns a plain dict (omitting unknowns) ready for the wire, or None if there's no usable name."""
    out: dict[str, Any] = {}
    for k, v in rc.model_dump(exclude_none=True).items():
        if k == "represents":
            reps = [_TAG.sub("", s).strip()[:200] for s in (v or []) if s and s.strip()]
            if reps:
                out["represents"] = reps
        elif isinstance(v, str):
            s = _TAG.sub("", v).strip()[: _RECRUITER_CAPS.get(k, 500)]
            if s:
                out[k] = s
        else:                                    # is_agency (bool/None), recruiter_confidence (float)
            out[k] = v
    lu = clean_link(out.get("linkedin_url"))     # http/https allowlist, same as posting links [C2]
    if lu:
        out["linkedin_url"] = lu
    else:
        out.pop("linkedin_url", None)
    return out if out.get("name") else None


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
        except ValueError as exc:
            # Malformed/unparseable model output (incl. LLMParseError, pydantic ValidationError) is a
            # recoverable validation failure → in-loop retry. Infrastructure errors (RateLimitError,
            # Timeout, API/5xx) are NOT ValueErrors: they propagate so the runner leaves the email in
            # place to retry next run, instead of misfiling a perfectly good email to Unprocessed.
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
        try:
            critique = self.critic_llm.structured(
                system=self.prompts.get("critic"), user=user, schema=Critique
            )
        except ValueError as exc:
            # Unparseable critic output (transient JSON glitch) → don't crash the email. Treat as a
            # soft fail so the gate retries (re-running classify+critic); after MAX_ATTEMPTS it
            # escalates to human review. Infra errors (RateLimitError/Timeout) are not ValueErrors and
            # still propagate (runner leaves the email to retry next run). Mirrors classify().
            return {"critique": Critique(valid=False, issues=[f"critic output unparseable: {exc}"])}
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
        if cat == Category.recruiter_outreach:
            return "extract_recruiter"   # recruiter card first, then write_postings
        return "write_postings"          # job_alert

    def extract_recruiter(self, state: AgentState) -> dict[str, Any]:
        """Dedicated, best-effort recruiter-card extraction (recruiter_outreach only). [§3.5]

        Runs ONLY on recruiter emails (kept off the hot path + out of the classify/critic loop). It is
        enrichment, never the main action: ANY failure (unparseable, rate-limit, timeout) just yields
        no card and proceeds to write_postings — it must not block or escalate the email."""
        email = state["email"]
        user = wrap_email(email["subject"], email["sender"], email["body_text"])
        try:
            rc = self.llm.structured(
                system=self.prompts.get("recruiter"), user=user, schema=RecruiterContact
            )
            return {"recruiter": _clean_recruiter(rc)}
        except Exception:
            return {"recruiter": None}

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
                "company": p.company, "role": p.role, "link": clean_link(p.link),
                "action_required": p.action_required,
                "possible_duplicate": m.best is not None,
                "matched_review_id": (m.best or {}).get("review_id"),
            })
        # Recruiter card (recruiter_outreach only) — emit BOTH Phase 1 (nested in raw_extracted_json)
        # and Phase 2 (typed top-level `recruiter`) so job-radar can adopt either. [§3.5]
        recruiter = state.get("recruiter")
        raw = c.model_dump(mode="json")
        if recruiter:
            raw["recruiter_contact"] = recruiter
        payload = {
            "message_id": email["message_id"], "subject": email["subject"],
            "sender": email["sender"], "received_at": email["received_at"],
            "category": c.category.value, "confidence": c.confidence,
            "langfuse_trace_id": state.get("langfuse_trace_id"),
            "raw_extracted_json": raw,
            "postings": payload_postings,
            "truncated": truncated,
        }
        if recruiter:
            payload["recruiter"] = recruiter
        resp = self.writer.create_inbox_entry(payload)
        return {"destination": _CATEGORY_FOLDER[c.category], "outcome": "processed",
                "inbox_email_id": (resp or {}).get("inbox_email_id")}

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
        # No tracked-job match — but interaction mail (interview invites, recruiter replies) is
        # high-value and must stay VISIBLE. File to Interaction (not Unprocessed); flag for review
        # so the human can match it. Missing an interview invite is the worst-case failure.
        return {"destination": "interaction", "outcome": "needs_review",
                "escalation_reason": "no confident job match — filed to Interaction for review"}

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
        """The single mutation point: move the email if a destination was chosen.

        Interactions are left UNREAD so high-value items (interview invites, recruiter replies) stay
        visibly unread in the Interaction folder; everything else is marked read.
        """
        dest = state.get("destination")
        if dest:
            self.reader.move_and_mark(state["email"]["message_id"], dest,
                                      mark_read=(dest != "interaction"))
        return {}
