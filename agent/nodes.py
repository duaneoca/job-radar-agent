"""
Graph nodes + routing functions for the V2 link-picker pipeline.

Flow (one email):
  screen ─┬─ (sender rejected) ───────────────────────────────────────────────┐
          ├─ (no links) ─→ no_postings ───────────────────────────────────────┤
          └─ pick → verify → gate ─┬─ write ──────────────────────────────────┤→ finalize → END
                                   ├─ retry → prepare_retry → pick (max 3)     │
                                   └─ escalate (→ Unprocessed) ────────────────┘

Exactly one LLM call per attempt (`pick`); everything else is deterministic. `finalize` is the single
place that changes the mailbox: mark the email read in place, or move it to Unprocessed.

LLM infrastructure errors (rate limit / timeout / 5xx) are NOT caught here — they propagate so the
runner leaves the email unread and it is retried on the next run instead of being misfiled.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any

from . import matching
from .dedup import DedupStore, NullDedupStore, dedup_key
from .extract import LinkCandidate, extract_links, render_candidates
from .llm import LLMClient
from .prompts import PromptProvider
from .reader import EmailReaderClient
from .schemas import LinkPicks
from .senders import SenderPolicy
from .state import MAX_ATTEMPTS, MAX_POSTINGS_PER_EMAIL, AgentState
from .verify import verify_picks
from .writer import JobRadarWriter

ZERO_POSTINGS_ACTIONS = ("mark_read", "unprocessed")
PROMPT_NAME = "link_picker"


def _safe(s: str) -> str:
    return (s or "").replace("<", "").replace(">", "")


def build_user_message(subject: str, candidates: list[LinkCandidate], feedback: list[str]) -> str:
    """The model's input: subject + numbered links, plus corrective notes on a retry.

    Feedback lines come only from the verifier and reference our own link numbers — the model's
    previous (wrong) answer is never sent back.
    """
    lines = [
        f"Email subject (data): {_safe(subject)}",
        "",
        "<links>",
        render_candidates(candidates),
        "</links>",
    ]
    if feedback:
        lines += ["", "Your previous answer had these problems. Answer again from the list above, "
                      "fixing them:"]
        lines += [f"- {f}" for f in feedback]
    return "\n".join(lines)


class Nodes:
    def __init__(
        self,
        llm: LLMClient,
        writer: JobRadarWriter,
        reader: EmailReaderClient,
        prompts: PromptProvider,
        *,
        policy: SenderPolicy | None = None,
        dedup: DedupStore | None = None,
        zero_postings_action: str = "mark_read",
        max_postings: int = MAX_POSTINGS_PER_EMAIL,
    ):
        if zero_postings_action not in ZERO_POSTINGS_ACTIONS:
            raise ValueError(f"zero_postings_action must be one of {ZERO_POSTINGS_ACTIONS}")
        self.llm = llm
        self.writer = writer
        self.reader = reader
        self.prompts = prompts
        self.policy = policy or SenderPolicy()
        self.dedup = dedup or NullDedupStore()
        self.zero_postings_action = zero_postings_action
        self.max_postings = max_postings

    # ── deterministic screening ───────────────────────────────
    def screen(self, state: AgentState) -> dict[str, Any]:
        email = state["email"]
        reason = self.policy.check(email.get("sender", ""), email.get("auth_results") or [])
        if reason:
            return {"candidates": [], "outcome": "needs_review", "destination": "unprocessed",
                    "reason": reason}
        candidates = extract_links(email.get("body_html", ""), email.get("body_text", ""))
        return {"candidates": candidates, "attempts": 0, "feedback": [], "issues": []}

    @staticmethod
    def after_screen(state: AgentState) -> str:
        if state.get("outcome") == "needs_review":
            return "finalize"
        return "pick" if state.get("candidates") else "no_postings"

    # ── the one LLM call ──────────────────────────────────────
    def pick(self, state: AgentState) -> dict[str, Any]:
        attempts = state.get("attempts", 0) + 1
        user = build_user_message(state["email"].get("subject", ""), state["candidates"],
                                  state.get("feedback") or [])
        try:
            picks = self.llm.structured(system=self.prompts.get(PROMPT_NAME), user=user,
                                        schema=LinkPicks)
        except ValueError:
            # Unparseable / wrong-shape output is a retryable mistake (infra errors propagate).
            return {"attempts": attempts, "picks": None, "verified": [],
                    "issues": ["Your reply was not a single JSON object in the required format."]}
        return {"attempts": attempts, "picks": picks}

    # ── deterministic verification ────────────────────────────
    def verify(self, state: AgentState) -> dict[str, Any]:
        picks = state.get("picks")
        if picks is None:
            return {}                                  # pick() already recorded the issue
        verdict = verify_picks(picks, state["candidates"], max_postings=self.max_postings)
        return {"issues": verdict.issues, "truncated": verdict.truncated,
                "verified": [asdict(p) for p in verdict.postings]}

    @staticmethod
    def gate(state: AgentState) -> str:
        if not state.get("issues"):
            return "write"
        return "retry" if state.get("attempts", 0) < MAX_ATTEMPTS else "escalate"

    @staticmethod
    def prepare_retry(state: AgentState) -> dict[str, Any]:
        return {"feedback": list(state.get("issues") or [])}

    # ── terminal actions ──────────────────────────────────────
    def no_postings(self, state: AgentState) -> dict[str, Any]:
        if self.zero_postings_action == "unprocessed":
            return {"outcome": "no_postings", "destination": "unprocessed",
                    "reason": "no job postings found in the email"}
        return {"outcome": "no_postings", "mark_read": True}

    def write(self, state: AgentState) -> dict[str, Any]:
        verified = state.get("verified") or []
        if not verified:
            return self.no_postings(state)

        email = state["email"]
        reviews = self.writer.get_reviews()
        new: list[dict[str, Any]] = []
        dup_keys: list[str] = []
        for v in verified:
            key = dedup_key(v["url"], v["company"], v["title"])
            if self.dedup.seen(key):
                dup_keys.append(key)
                continue
            m = matching.match(v["company"], v["title"], reviews)
            new.append({
                "company": v["company"], "role": v["title"], "link": v["url"],
                "action_required": False,
                "possible_duplicate": m.best is not None,
                "matched_review_id": (m.best or {}).get("review_id"),
                "dedup_key": key,
            })

        if not new:
            self.dedup.add(dup_keys)                   # refresh the window for re-sent jobs
            return {"outcome": "duplicates_only", "mark_read": True, "postings_written": 0,
                    "duplicates_skipped": len(dup_keys)}

        resp = self.writer.create_inbox_entry({
            "message_id": email["message_id"], "subject": email.get("subject", ""),
            "sender": email.get("sender", ""),
            # Job Radar requires received_at; an email with no Date header falls back to "now".
            "received_at": email.get("received_at") or datetime.now(timezone.utc).isoformat(),
            "category": "job_alert",
            "confidence": 1.0,                         # every posting passed deterministic checks
            "langfuse_trace_id": state.get("langfuse_trace_id"),
            "raw_extracted_json": {
                "pipeline": "v2-link-picker",
                "attempts": state.get("attempts"),
                "links_considered": len(state.get("candidates") or []),
                "postings": [{"link_id": v["link_id"], "title": v["title"],
                              "company": v["company"]} for v in verified],
                "duplicates_skipped": len(dup_keys),
            },
            "postings": new,
            "truncated": bool(state.get("truncated")),
        })
        # Only after Job Radar accepted the write — a failed write must not hide these postings.
        self.dedup.add([p["dedup_key"] for p in new] + dup_keys)
        return {"outcome": "processed", "mark_read": True,
                "inbox_email_id": (resp or {}).get("inbox_email_id"),
                "postings_written": len(new), "duplicates_skipped": len(dup_keys)}

    def escalate(self, state: AgentState) -> dict[str, Any]:
        return {"outcome": "needs_review", "destination": "unprocessed",
                "reason": (f"link picks failed verification after {state.get('attempts', 0)} "
                           f"attempts ({len(state.get('issues') or [])} open issue(s))")}

    def finalize(self, state: AgentState) -> dict[str, Any]:
        """The single mailbox mutation: move to Unprocessed, or mark read in place."""
        message_id = state["email"]["message_id"]
        if state.get("destination"):
            self.reader.move_and_mark(message_id, state["destination"], mark_read=True)
        elif state.get("mark_read"):
            self.reader.mark_read(message_id)
        return {}
