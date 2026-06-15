"""
Notification tests — dispatch logic (FakeNotifier) + Slack message building (mocked httpx).
No live Slack/Telegram/Discord needed.
"""

import sys
import types

from notifications import dispatch
from notifications.base import FakeNotifier, NullNotifier, Notification
from agent.schemas import Category, Classification, InteractionSignal, Posting, WritableStatus


def _final(**kw):
    base = {"classification": None, "outcome": "processed", "inbox_email_id": "ie1"}
    base.update(kw)
    return base


def _email(subj="s"):
    return {"message_id": "<1>", "subject": subj, "sender": "a@b.com"}


# ── dispatch ──────────────────────────────────────────────────

def test_offer_notifies_user():
    n = FakeNotifier()
    cls = Classification(category=Category.application_confirmation, confidence=0.9, reasoning="x",
                         interaction=InteractionSignal(company="Acme", new_status=WritableStatus.offer,
                                                       summary="They made an offer!"))
    dispatch.per_email(n, email=_email(), final=_final(classification=cls))
    assert len(n.sent) == 1 and n.sent[0].audience == "user"
    assert "Offer" in n.sent[0].title and "Acme" in n.sent[0].title


def test_status_change_notifies_user_with_link():
    n = FakeNotifier()
    cls = Classification(category=Category.application_confirmation, confidence=0.9, reasoning="x",
                         interaction=InteractionSignal(company="Acme", new_status=WritableStatus.interviewing))
    dispatch.per_email(n, email=_email(), final=_final(classification=cls),
                       inbox_base_url="https://job-radar.net")
    assert n.sent[0].link == "https://job-radar.net/inbox/ie1"
    assert "interviewing" in n.sent[0].title


def test_recruiter_outreach_notifies_user():
    n = FakeNotifier()
    cls = Classification(category=Category.recruiter_outreach, confidence=0.9, reasoning="x",
                         postings=[Posting(company="Acme", role="FDE", link="https://acme/job")])
    dispatch.per_email(n, email=_email(), final=_final(classification=cls))
    assert n.sent[0].audience == "user" and "Recruiter" in n.sent[0].title


def test_job_alert_does_not_notify_user():
    n = FakeNotifier()
    cls = Classification(category=Category.job_alert, confidence=0.9, reasoning="x",
                         postings=[Posting(company="C", role="R")])
    dispatch.per_email(n, email=_email(), final=_final(classification=cls))
    assert n.sent == []   # bulk job alerts aren't pinged — they sit in the inbox


def test_hitl_prompt_has_candidate_buttons_plus_none():
    n = FakeNotifier()
    dispatch.hitl_prompt(n, company="Acme", hitl_id="H1",
                         candidates=[{"review_id": "r1", "label": "Acme — SWE"},
                                     {"review_id": "r2", "label": "Acme — Staff"}])
    btns = n.sent[0].buttons
    assert [b["value"] for b in btns] == ["H1|r1", "H1|r2", "H1|none"]


def test_run_summary_user_review_count_and_admin_failure():
    n = FakeNotifier()

    class R:
        status = "partial"; emails_processed = 3; escalations = 2; retries = 1
        errors = ["<x>: boom"]
    dispatch.run_summary(n, result=R())
    auds = [s.audience for s in n.sent]
    assert "user" in auds and "admin" in auds


# ── Slack message building (mocked) ───────────────────────────

def test_slack_builds_blocks_and_routes_admin(monkeypatch):
    calls = {}

    class _Resp:
        def raise_for_status(self): ...
        def json(self): return {"ok": True}

    class _Client:
        def __init__(self, **kw): ...
        def post(self, path, json=None):
            calls["path"] = path; calls["json"] = json; return _Resp()
        def close(self): ...

    fake_httpx = types.ModuleType("httpx"); fake_httpx.Client = _Client
    monkeypatch.setitem(sys.modules, "httpx", fake_httpx)
    # import after patching so SlackNotifier picks up the fake client
    import importlib
    slack = importlib.reload(importlib.import_module("notifications.slack"))

    s = slack.SlackNotifier("xoxb-test", "#user", "#admin")
    s.send(Notification("admin", "Run failed", body="oops", fields={"processed": "3"}))
    assert calls["json"]["channel"] == "#admin"
    assert calls["json"]["blocks"][0]["type"] == "header"


def test_null_notifier_is_noop():
    NullNotifier().send(Notification("user", "x"))  # must not raise
