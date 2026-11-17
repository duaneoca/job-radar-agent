"""
The user-tier review notification lists each email moved to Unprocessed on its own line.

The invariant under test throughout: the headline count and the lines beneath it always account
for the same number of emails. `result.escalations` sums TWO sources — the sort stage's
`moved["unprocessed"]` and the link picker's per-email `destination == "unprocessed"` — so a body
built from one source alone would contradict its own title.
"""

from __future__ import annotations

import pytest

from notifications import dispatch
from notifications.base import FakeNotifier


def _result(*, escalations, details=None, sort_details=None, status="success", errors=None):
    class R:
        pass
    r = R()
    r.status = status
    r.emails_processed = 0
    r.escalations = escalations
    r.retries = 0
    r.errors = errors or []
    r.details = details if details is not None else []
    r.sort_details = sort_details if sort_details is not None else []
    return r


def _picked(subject, reason=None, destination="unprocessed"):
    return {"message_id": "<m>", "subject": subject, "outcome": "needs_review",
            "destination": destination, "postings": 0, "duplicates": 0,
            "attempts": 1, "reason": reason}


def _sorted_(subject, reason=None, folder="unprocessed"):
    return {"message_id": "<s>", "subject": subject, "sender": "x@y.com",
            "category": "unknown", "folder": folder, "confidence": 0.5,
            "rule": "low_confidence", "reason": reason}


# ── listing ───────────────────────────────────────────────────

def test_single_email_is_listed_by_subject():
    r = _result(escalations=1, details=[_picked("Senior Platform Engineer at Acme")])
    body = dispatch.review_body(r)
    assert "Moved to the Unprocessed folder." in body
    assert "• Senior Platform Engineer at Acme" in body


def test_each_email_on_its_own_line():
    r = _result(escalations=3, details=[_picked("One"), _picked("Two"), _picked("Three")])
    bullets = [ln for ln in dispatch.review_body(r).splitlines() if ln.startswith("• ")]
    assert bullets == ["• One", "• Two", "• Three"]


def test_reason_is_appended_when_present():
    r = _result(escalations=1, details=[_picked("Weekly digest", reason="no job postings found")])
    assert "• Weekly digest — no job postings found" in dispatch.review_body(r)


def test_both_sources_are_listed_sorter_first():
    """The sort stage runs before the link picker, so its escalations are listed first."""
    r = _result(escalations=2,
                details=[_picked("From the picker")],
                sort_details=[_sorted_("From the sorter")])
    bullets = [ln for ln in dispatch.review_body(r).splitlines() if ln.startswith("• ")]
    assert bullets == ["• From the sorter", "• From the picker"]


def test_non_unprocessed_entries_are_ignored():
    r = _result(escalations=1,
                details=[_picked("Processed fine", destination=None), _picked("Needs review")],
                sort_details=[_sorted_("Filed to postings", folder="postings")])
    bullets = [ln for ln in dispatch.review_body(r).splitlines() if ln.startswith("• ")]
    assert bullets == ["• Needs review"]


# ── the count must always match the lines ─────────────────────

@pytest.mark.parametrize("n", [1, 5, 19, 20])
def test_every_email_listed_up_to_the_cap(n):
    r = _result(escalations=n, details=[_picked(f"Email {i}") for i in range(n)])
    body = dispatch.review_body(r)
    assert len([ln for ln in body.splitlines() if ln.startswith("• ")]) == n
    assert "more" not in body


def test_cap_lists_twenty_and_summarises_the_rest():
    r = _result(escalations=25, details=[_picked(f"Email {i}") for i in range(25)])
    body = dispatch.review_body(r)
    assert len([ln for ln in body.splitlines() if ln.startswith("• ")]) == dispatch.MAX_LISTED
    assert "… and 5 more" in body


def test_listed_plus_hidden_equals_the_headline_count():
    n = 57
    r = _result(escalations=n, details=[_picked(f"Email {i}") for i in range(n)])
    body = dispatch.review_body(r)
    shown = len([ln for ln in body.splitlines() if ln.startswith("• ")])
    hidden = int(body.rsplit("and ", 1)[1].split(" more")[0])
    assert shown + hidden == n


def test_shortfall_is_stated_when_details_are_missing_some_escalations():
    """Belt and braces: if the sources ever under-report, say so rather than contradict the title."""
    r = _result(escalations=4, details=[_picked("Only one recorded")])
    body = dispatch.review_body(r)
    assert "• Only one recorded" in body
    assert "… and 3 more" in body


# ── hostile and degenerate input ──────────────────────────────

def test_newline_in_subject_cannot_forge_extra_lines():
    """A crafted subject must not be able to fake additional bullets."""
    r = _result(escalations=1, details=[_picked("Real subject\n• Fake entry\nmore text")])
    body = dispatch.review_body(r)
    assert len([ln for ln in body.splitlines() if ln.startswith("• ")]) == 1
    assert "Real subject • Fake entry more text" in body


def test_missing_subject_gets_a_placeholder():
    r = _result(escalations=1, details=[_picked("")])
    assert "• (no subject)" in dispatch.review_body(r)


def test_result_without_detail_fields_still_works():
    """Cloud results and older callers carry no details; the body degrades to the plain note."""
    class Bare:
        status = "success"; emails_processed = 2; escalations = 2; retries = 0; errors = []
    assert dispatch.review_body(Bare()) == "Moved to the Unprocessed folder."


# ── end to end through run_summary ────────────────────────────

def test_run_summary_sends_the_listing_to_the_user_tier():
    n = FakeNotifier()
    r = _result(escalations=2, details=[_picked("Alpha", reason="verification failed")],
                sort_details=[_sorted_("Beta", reason="low confidence 0.62")])
    dispatch.run_summary(n, result=r)
    user = [s for s in n.sent if s.audience == "user"]
    assert len(user) == 1
    assert user[0].title == "2 item(s) need your review"
    assert "• Beta — low confidence 0.62" in user[0].body
    assert "• Alpha — verification failed" in user[0].body


def test_clean_run_still_sends_nothing():
    n = FakeNotifier()
    dispatch.run_summary(n, result=_result(escalations=0))
    assert n.sent == []
