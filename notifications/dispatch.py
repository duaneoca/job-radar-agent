"""
Dispatch — maps agent outcomes to Notifications (D16 priority tiers).

User channel (actionable job activity):
  • offer            → 🎉 high-signal
  • interaction status change (applied/interviewing/rejected) with a match
  • recruiter outreach
  • ambiguous interaction → HITL prompt (buttons) until the interactive resume lands
Per-run user summary: count of items needing review.
Admin channel: run failure/partial + per-email errors, with the Langfuse trace link.

`per_email` is called per processed email with its final graph state; `run_summary` once at the end.
"""

from __future__ import annotations

from typing import Any

from .base import Notification, Notifier


def _trace_url(lf_host: str | None, trace_id: str | None) -> str | None:
    if not (lf_host and trace_id):
        return None
    return f"{lf_host.rstrip('/')}/trace/{trace_id}"


def per_email(notifier: Notifier, *, email: dict, final: dict,
              inbox_base_url: str | None = None) -> None:
    c = final.get("classification")
    if c is None:
        return
    cat = c.category.value
    company = None
    if c.interaction is not None:
        company = c.interaction.company
    elif c.postings:
        company = c.postings[0].company
    link = None
    if inbox_base_url and final.get("inbox_email_id"):
        link = f"{inbox_base_url.rstrip('/')}/inbox/{final['inbox_email_id']}"

    sig = c.interaction
    if sig is not None and sig.new_status and sig.new_status.value == "offer":
        notifier.send(Notification("user", f"🎉 Offer — {company}",
                                   body=sig.summary or email.get("subject", ""),
                                   link=link, link_text="View in Job Radar"))
    elif cat == "application_confirmation" and final.get("outcome") == "processed" and sig and sig.new_status:
        notifier.send(Notification("user", f"{company}: → {sig.new_status.value}",
                                   body=sig.summary or email.get("subject", ""), link=link,
                                   link_text="View in Job Radar"))
    elif cat == "recruiter_outreach":
        role = c.postings[0].role if c.postings else ""
        notifier.send(Notification("user", f"Recruiter outreach — {company}",
                                   body=f"{role}".strip() or email.get("subject", ""),
                                   link=(c.postings[0].link if c.postings else link) or link,
                                   link_text="View posting"))


def hitl_prompt(notifier: Notifier, *, company: str, hitl_id: str,
                candidates: list[dict[str, Any]]) -> None:
    """Ambiguous interaction — ask the user which tracked job this belongs to."""
    buttons = [{"text": c["label"][:75], "value": f"{hitl_id}|{c['review_id']}"} for c in candidates]
    buttons.append({"text": "None of these", "value": f"{hitl_id}|none"})
    notifier.send(Notification("user", f"Which job is this update from {company}?",
                               body="An interview/application update matched more than one tracked job.",
                               buttons=buttons))


def run_summary(notifier: Notifier, *, result, lf_host: str | None = None) -> None:
    if result.escalations:
        notifier.send(Notification("user", f"{result.escalations} item(s) need your review",
                                   body="Filed to Unprocessed / flagged in Interaction.",))
    if result.status in ("failed", "partial") or result.errors:
        notifier.send(Notification(
            "admin", f"Agent run {result.status}",
            body="\n".join(result.errors[:10]) or "see logs",
            fields={"processed": str(result.emails_processed),
                    "escalations": str(result.escalations),
                    "retries": str(result.retries)},
        ))
