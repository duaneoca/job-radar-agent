"""
Dispatch — maps run outcomes to Notifications (D16 tiers).

V2 processes job-alert postings only, which are deliberately NOT pinged individually (they land in the
Job Radar inbox). What remains is the run summary:
  • user  — "N email(s) need your review" when anything went to Unprocessed
  • admin — run failure / partial, with the per-email errors
"""

from __future__ import annotations

from .base import Notification, Notifier


def run_summary(notifier: Notifier, *, result, lf_host: str | None = None) -> None:
    if result.escalations:
        notifier.send(Notification("user", f"{result.escalations} item(s) need your review",
                                   body="Moved to the Unprocessed folder.",))
    if result.status in ("failed", "partial") or result.errors:
        notifier.send(Notification(
            "admin", f"Agent run {result.status}",
            body="\n".join(result.errors[:10]) or "see logs",
            fields={"processed": str(result.emails_processed),
                    "escalations": str(result.escalations),
                    "retries": str(result.retries)},
        ))
