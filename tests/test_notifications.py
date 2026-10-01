"""
Notification tests — run-summary dispatch (FakeNotifier) + Slack message building (mocked httpx).
No live Slack/Telegram/Discord needed.
"""

import sys
import types

from notifications import dispatch
from notifications.base import FakeNotifier, NullNotifier, Notification


# ── dispatch ──────────────────────────────────────────────────

def test_run_summary_user_review_count_and_admin_failure():
    n = FakeNotifier()

    class R:
        status = "partial"; emails_processed = 3; escalations = 2; retries = 1
        errors = ["<x>: boom"]
    dispatch.run_summary(n, result=R())
    auds = [s.audience for s in n.sent]
    assert "user" in auds and "admin" in auds
    assert "Unprocessed" in n.sent[0].body


def test_clean_run_sends_nothing():
    n = FakeNotifier()

    class R:
        status = "success"; emails_processed = 5; escalations = 0; retries = 0; errors = []
    dispatch.run_summary(n, result=R())
    assert n.sent == []   # postings land in the Job Radar inbox; no per-email pings


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


def test_routing_notifier_splits_user_and_admin():
    from notifications.base import RoutingNotifier, FakeNotifier as FN, Notification as N
    user, admin = FN(), FN()
    r = RoutingNotifier(user, admin)
    r.send(N("user", "ping"))
    r.send(N("admin", "alert"))
    assert [m.title for m in user.sent] == ["ping"]
    assert [m.title for m in admin.sent] == ["alert"]
