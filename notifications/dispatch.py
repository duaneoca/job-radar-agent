"""
Dispatch — maps run outcomes to Notifications (D16 tiers).

V2 processes job-alert postings only, which are deliberately NOT pinged individually (they land in the
Job Radar inbox). What remains is the run summary:
  • user  — "N email(s) need your review" when anything went to Unprocessed, one line per email
  • admin — run failure / partial, with the per-email errors
"""

from __future__ import annotations

from .base import Notification, Notifier

# At most this many emails are listed by subject; the rest are summarised as "… and N more".
# A pathological run (a mis-sorted bulk sender, a mailbox-wide screen failure) must not turn the
# notification into a wall of text.
MAX_LISTED = 20

SUBJECT_CHARS = 60          # upstream already truncates; dispatch does not rely on it
_PLACEHOLDER = "(no subject)"


def _one_line(text: str | None, limit: int) -> str:
    """Collapse to a single line, bounded.

    Subjects are attacker-controlled: a header containing a newline would otherwise forge extra
    bullets in the notification, so one email could appear as several. Flatten every kind of line
    break before the text reaches the message body.
    """
    flat = " ".join((text or "").split())
    return flat[:limit]


def unprocessed_items(result) -> list[tuple[str, str | None]]:
    """(subject, reason) for each email moved to Unprocessed, in the order the run moved them.

    BOTH paths must be read. `result.escalations` is the sum of the sorter's `moved["unprocessed"]`
    and the link picker's per-email `destination == "unprocessed"` (agent/runner.py), so a list
    drawn from one source alone would show a count that disagrees with its own lines. The sort
    stage runs first, so its entries come first.

    `getattr` throughout: callers predating these fields, and the cloud runner's result objects,
    must keep working — a missing field means "nothing to list", never a crash in the notifier.
    """
    items: list[tuple[str, str | None]] = []
    for d in getattr(result, "sort_details", None) or []:
        if d.get("folder") == "unprocessed":
            items.append((_one_line(d.get("subject"), SUBJECT_CHARS) or _PLACEHOLDER,
                          _one_line(d.get("reason"), 120) or None))
    for d in getattr(result, "details", None) or []:
        if d.get("destination") == "unprocessed":
            items.append((_one_line(d.get("subject"), SUBJECT_CHARS) or _PLACEHOLDER,
                          _one_line(d.get("reason"), 120) or None))
    return items


def review_body(result) -> str:
    """The user-tier body: the folder note, then one line per email moved to Unprocessed.

    The lines always account for every escalation in the headline count. When fewer items are
    listed than were escalated — because of the MAX_LISTED cap, or because a result object does
    not carry the detail — the shortfall is stated rather than silently dropped, so the count in
    the title and the lines below it can never disagree.
    """
    header = "Moved to the Unprocessed folder."
    items = unprocessed_items(result)
    if not items:
        return header

    shown = items[:MAX_LISTED]
    lines = [f"• {subject}" + (f" — {reason}" if reason else "") for subject, reason in shown]
    hidden = max(0, int(getattr(result, "escalations", 0) or 0) - len(shown))
    if hidden:
        lines.append(f"… and {hidden} more")
    return "\n".join([header, *lines])


def run_summary(notifier: Notifier, *, result, lf_host: str | None = None) -> None:
    if result.escalations:
        notifier.send(Notification("user", f"{result.escalations} item(s) need your review",
                                   body=review_body(result),))
    if result.status in ("failed", "partial") or result.errors:
        notifier.send(Notification(
            "admin", f"Agent run {result.status}",
            body="\n".join(result.errors[:10]) or "see logs",
            fields={"processed": str(result.emails_processed),
                    "escalations": str(result.escalations),
                    "retries": str(result.retries)},
        ))
