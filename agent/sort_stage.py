"""
Sort stage — runs BEFORE the postings stage, inside the same lock / preflight / spend ceiling.

For each unread email in the ROOT folder (newest-first, age + count caps from the reader):
  1. Jev classifies it (agent/sorter.py) → a destination folder or Unprocessed.
  2. Interaction mail is written to Job Radar's inbox first (`POST /agent/inbox`, no postings), with a
     recruiter card for `recruiter_outreach` (agent/recruiter.py → /recruiters/suggestions).
  3. Then the ONE mailbox mutation: move it (Interaction + Postings stay unread; Social + Unprocessed
     are marked read).

Write-then-move is safe to repeat: /agent/inbox is idempotent on (user, Message-ID), so if the move
fails the next run re-sends and gets the existing row back. A move that cannot find the message
(LookupError — e.g. Proton Bridge's view is stale and the human already moved it) counts as `gone`,
not as an error. Any other failure leaves the email unread in the root folder for the next run.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .recruiter import RELAY_DOMAINS, build_card
from .sorter import (DEFAULT_BULK_RECRUITER_DOMAINS, SortDecision, _on_domain, classify, decide,
                     sender_domain)

# Sorter category → Job Radar's EmailCategory enum (recruiter_outreach | application_confirmation |
# job_alert | network_notification). Only Interaction mail is written, so job_alert never appears.
JOBRADAR_CATEGORY = {
    "recruiter_outreach": "recruiter_outreach",
    "application_update": "application_confirmation",
    "connection_request": "network_notification",
    "direct_message": "network_notification",
}


@dataclass(frozen=True)
class SortConfig:
    min_confidence: float = 0.85
    min_margin: float = 0.30
    bulk_domains: tuple[str, ...] = DEFAULT_BULK_RECRUITER_DOMAINS


@dataclass
class SortResult:
    moved: Counter = field(default_factory=Counter)    # destination folder → count
    inbox_written: int = 0                              # Interaction rows sent to Job Radar
    recruiter_cards: int = 0
    gone: int = 0                                       # already moved by someone else (stale view)
    errors: list[str] = field(default_factory=list)
    details: list[dict] = field(default_factory=list)
    halted: str | None = None                           # spend ceiling hit mid-stage


def jobradar_category(decision: SortDecision, email: dict, card: dict | None) -> str:
    """
    Job Radar's /recruiters/suggestions keys a recruiter by the card's email, else the SENDER address.
    A relay sender (LinkedIn InMail, …) with no real address in the card would merge every such
    recruiter into one junk suggestion — so that mail is filed as a network message instead.
    """
    cat = JOBRADAR_CATEGORY[decision.category]
    if (cat == "recruiter_outreach" and not (card or {}).get("email")
            and _on_domain(sender_domain(email.get("sender", "")), RELAY_DOMAINS)):
        return "network_notification"
    return cat


def interaction_payload(email: dict, decision: SortDecision, card: dict | None) -> dict:
    raw: dict = {"sorter": {"model": "jev", "category": decision.category,
                            "folder_confidence": round(decision.confidence, 4)}}
    payload = {
        "message_id": email["message_id"],
        "subject": email.get("subject") or "",
        "sender": email.get("sender") or "",
        # Job Radar requires received_at; an email with no Date header falls back to "now".
        "received_at": email.get("received_at") or datetime.now(timezone.utc).isoformat(),
        "category": jobradar_category(decision, email, card),
        "confidence": round(decision.confidence, 4),
        "raw_extracted_json": raw,
        "postings": [],
    }
    if card:
        raw["recruiter_contact"] = card     # §3.5 Phase 1 (what /recruiters/suggestions reads)
        payload["recruiter"] = card         # §3.5 Phase 2 (typed; ignored until Job Radar adopts it)
    return payload


class SortStage:
    def __init__(self, reader, jev, config: SortConfig = SortConfig()):
        self.reader = reader          # EmailReaderClient over the ROOT folder, 4 destinations
        self.jev = jev
        self.config = config

    def run(self, writer, *, dry_run: bool = False, over_budget=lambda: False) -> SortResult:
        res = SortResult()
        for email in self.reader.get_unread():
            if over_budget():
                res.halted = "daily spend ceiling reached mid-sort"
                break
            mid = email.get("message_id", "?")
            try:
                d = decide(classify(self.jev, email), email.get("sender", ""),
                           min_confidence=self.config.min_confidence,
                           min_margin=self.config.min_margin, bulk_domains=self.config.bulk_domains)
                card = None
                if d.folder == "interaction":
                    if d.category == "recruiter_outreach":
                        card = build_card(self.jev, email)
                    payload = interaction_payload(email, d, card)
                    if not dry_run:
                        writer.create_inbox_entry(payload)
                    res.inbox_written += 1
                    res.recruiter_cards += bool(card)
                if not dry_run:
                    try:
                        self.reader.move_and_mark(mid, d.folder, mark_read=d.mark_read)
                    except LookupError:
                        res.gone += 1
                        continue
                res.moved[d.folder] += 1
                res.details.append({
                    "message_id": mid, "subject": (email.get("subject") or "")[:60],
                    "sender": (email.get("sender") or "")[:60], "category": d.category,
                    "folder": d.folder, "confidence": round(d.confidence, 2), "rule": d.rule,
                    "reason": d.reason, "card": card,
                })
            except Exception as exc:   # one poison email must not stop the stage [L3]
                res.errors.append(f"sort {mid}: {type(exc).__name__}: {exc}")
        return res
