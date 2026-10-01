"""
Cloud runner tests — fakes only. Covers: enumerate→fetch→process→discard per user, skipping
disabled users, per-user isolation, circuit breaker, total-email budget, and RestWriter cloud auth.
"""

from types import SimpleNamespace

from agent.cloud import cloud_run
from agent.writer_rest import RestWriter
from notifications.base import FakeNotifier


class _FakeConfigClient:
    def __init__(self, users, configs, fail_config=()):
        self._users = users
        self._configs = configs
        self._fail = set(fail_config)
        self.fetched: list[str] = []

    def users(self):
        return self._users

    def config(self, uid):
        self.fetched.append(uid)
        if uid in self._fail:
            raise RuntimeError("config fetch failed")
        return self._configs[uid]


def _stub(close=lambda: None):
    return SimpleNamespace(reader=None, writer=None, llm=object(), policy=None, dedup=None,
                           zero_postings_action="mark_read", user_notifier=FakeNotifier(),
                           close=close)


def _res(processed=1, escalations=0):
    return SimpleNamespace(emails_processed=processed, escalations=escalations,
                           status="success", errors=[])


# ── RestWriter cloud auth ─────────────────────────────────────

def test_restwriter_cloud_auth_headers():
    w = RestWriter("https://x/api", internal_token="tok", user_id="u1")
    h = w._client.headers
    assert h["X-Internal-Token"] == "tok" and h["X-User-Id"] == "u1"
    assert "X-Agent-Key" not in h


def test_restwriter_requires_some_auth():
    import pytest
    with pytest.raises(ValueError):
        RestWriter("https://x/api")


# ── cloud_run ─────────────────────────────────────────────────

def _cfg():
    return {"folders": {"root": "R", "interaction": "R/I", "postings": "R/P",
                        "social": "R/S", "unprocessed": "R/U"},
            "email_credentials": {"provider": "gmail", "refresh_token": "rt"},
            "llm": {"provider": "anthropic", "preferred_model": "claude-haiku-4-5", "api_key": "k"}}


def test_skips_disabled_and_processes_enabled(monkeypatch):
    users = [{"user_id": "u1", "enabled": True}, {"user_id": "u2", "enabled": False}]
    cc = _FakeConfigClient(users, {"u1": _cfg()})
    calls = []

    def fake_run_once(**kw):
        calls.append(kw["environment"]); return _res()

    # avoid building real providers/llm: stub build_user_components
    import agent.cloud as cloudmod
    monkeypatch.setattr(cloudmod, "build_user_components",
                        lambda *a, **k: _stub())
    s = cloud_run(cc, base_url="https://x/api", internal_token="t", prompts=None,
                  run_once_fn=fake_run_once)
    assert s["users"] == 1                       # only u1 (enabled)
    assert cc.fetched == ["u1"]                  # u2 never fetched
    assert s["processed"] == 1 and calls == ["cloud"]


def test_per_user_isolation_and_creds_discarded(monkeypatch):
    users = [{"user_id": "u1", "enabled": True}, {"user_id": "u2", "enabled": True}]
    cc = _FakeConfigClient(users, {"u1": _cfg(), "u2": _cfg()}, fail_config=("u1",))
    closed = []
    import agent.cloud as cloudmod
    monkeypatch.setattr(cloudmod, "build_user_components",
                        lambda *a, **k: _stub(close=lambda: closed.append(1)))
    s = cloud_run(cc, base_url="https://x/api", internal_token="t", prompts=None,
                  run_once_fn=lambda **kw: _res())
    assert s["users"] == 2
    assert any("u1" in e for e in s["errors"])   # u1 isolated
    assert s["processed"] == 1                    # u2 still processed
    assert closed == [1]                          # u2's creds discarded (u1 failed before build)


def test_circuit_breaker_stops_on_consecutive_failures(monkeypatch):
    users = [{"user_id": f"u{i}", "enabled": True} for i in range(6)]
    cc = _FakeConfigClient(users, {}, fail_config={f"u{i}" for i in range(6)})
    s = cloud_run(cc, base_url="https://x/api", internal_token="t", prompts=None,
                  run_once_fn=lambda **kw: _res())
    assert s["stopped"] and "circuit breaker" in s["stopped"]
    assert len(cc.fetched) == 3                   # CIRCUIT_BREAK


def test_build_user_components_uses_the_users_own_llm():
    # Regression: a Gemini user must NOT get an Anthropic default (the V1 cloud bug).
    from agent.cloud import build_user_components
    cfg = {"folders": {"root": "Hire Duane", "postings": "Hire Duane/Postings",
                       "unprocessed": "Hire Duane/Unprocessed"},
           "email_credentials": {"provider": "gmail", "refresh_token": "rt",
                                 "client_id": "c", "client_secret": "s"},
           "llm": {"provider": "google", "preferred_model": "gemini/gemini-1.5-flash", "api_key": "k"}}
    comp = build_user_components(cfg, "u1", base_url="https://x/api", internal_token="t",
                                 since_days=14, limit=100)
    try:
        assert comp.llm._model == "gemini/gemini-1.5-flash" and comp.llm._api_key == "k"
    finally:
        comp.close()


def test_email_policy_comes_from_the_users_settings_with_safe_defaults():
    from agent.cloud import build_user_components
    base = {"folders": {"root": "R", "postings": "P", "unprocessed": "U"},
            "email_credentials": {"provider": "gmail", "refresh_token": "rt"},
            "llm": {"provider": "google", "preferred_model": "gemini/x", "api_key": "k"}}
    comp = build_user_components(base, "u1", base_url="https://x/api", internal_token="t",
                                 since_days=14, limit=10)
    try:   # older Job Radar deploys send no email_policy → defaults
        assert comp.policy.require_auth and "linkedin.com" in comp.policy.allowed_domains
        assert comp.zero_postings_action == "mark_read"
        assert type(comp.dedup).__name__ == "NullDedupStore"   # Job Radar enforces server-side
    finally:
        comp.close()
    custom = {**base, "email_policy": {"allowed_sender_domains": ["Example.org"],
                                       "require_sender_auth": False,
                                       "zero_postings_action": "unprocessed"}}
    comp = build_user_components(custom, "u2", base_url="https://x/api", internal_token="t",
                                 since_days=14, limit=10)
    try:
        assert comp.policy.allowed_domains == ("example.org",) and not comp.policy.require_auth
        assert comp.zero_postings_action == "unprocessed"
    finally:
        comp.close()


def test_full_label_joins_bare_leaf_and_is_idempotent():
    from agent.cloud import _full_label
    assert _full_label("Hire Duane", "Postings") == "Hire Duane/Postings"      # bare leaf → joined
    assert _full_label("Hire Duane", "Hire Duane/Postings") == "Hire Duane/Postings"  # already full
    assert _full_label("Hire Duane", "") == "Hire Duane"


def test_build_user_components_joins_sublabels_under_root():
    from agent.cloud import build_user_components
    cfg = {"folders": {"root": "Hire Duane", "interaction": "Interaction", "postings": "Postings",
                       "social": "Social", "unprocessed": "Unprocessed"},  # BARE leaves (V1 shape still ok)
           "email_credentials": {"provider": "gmail", "refresh_token": "rt"},
           "llm": {"provider": "google", "preferred_model": "gemini/x", "api_key": "k"}}
    comp = build_user_components(cfg, "u1", base_url="https://x/api", internal_token="t",
                                 since_days=14, limit=100)
    try:
        assert comp.reader._source == "Hire Duane/Postings"           # joined, not bare "Postings"
        assert comp.reader._dest == {"unprocessed": "Hire Duane/Unprocessed"}   # the only move
        assert comp.reader._p._root == "Hire Duane/Postings"          # Gmail removes this label on move
    finally:
        comp.close()


def test_build_user_components_builds_imap_provider():
    # Cloud IMAP users: build a generic ImapProvider from the email_credentials blob (no stub).
    from agent.cloud import build_user_components
    from mcp_email.providers.imap import ImapProvider
    cfg = {"folders": {"root": "Mail", "interaction": "Mail/I", "postings": "Mail/P",
                       "social": "Mail/S", "unprocessed": "Mail/U"},
           "email_credentials": {"provider": "imap", "host": "imap.example.com", "port": 993,
                                 "username": "u@example.com", "password": "pw", "use_ssl": True},
           "llm": {"provider": "google", "preferred_model": "gemini/x", "api_key": "k"}}
    comp = build_user_components(cfg, "u1", base_url="https://x/api", internal_token="t",
                                since_days=14, limit=25)
    try:
        assert isinstance(comp.reader._p, ImapProvider)
        assert comp.reader._p._host == "imap.example.com" and comp.reader._p._use_ssl is True
    finally:
        comp.close()


def test_total_email_budget_stops_run(monkeypatch):
    users = [{"user_id": f"u{i}", "enabled": True} for i in range(5)]
    cc = _FakeConfigClient(users, {f"u{i}": _cfg() for i in range(5)})
    import agent.cloud as cloudmod
    monkeypatch.setattr(cloudmod, "build_user_components",
                        lambda *a, **k: _stub())
    s = cloud_run(cc, base_url="https://x/api", internal_token="t", prompts=None,
                  max_total_emails=2, run_once_fn=lambda **kw: _res(processed=1))
    assert s["stopped"] and "budget" in s["stopped"]
    assert s["processed"] == 2


def test_per_user_slack_built_from_config_else_null():
    from agent.cloud import build_user_components
    from notifications.base import NullNotifier
    base_cfg = {"folders": {"root": "R", "interaction": "I", "postings": "P",
                            "social": "S", "unprocessed": "U"},
                "email_credentials": {"provider": "gmail", "refresh_token": "rt"},
                "llm": {"provider": "google", "preferred_model": "gemini/x", "api_key": "k"}}
    # with slack block → SlackNotifier
    with_slack = {**base_cfg, "slack": {"bot_token": "xoxb-1", "channel_id": "C123"}}
    c1 = build_user_components(with_slack, "u1", base_url="https://x/api", internal_token="t",
                               since_days=14, limit=100)
    try:
        assert type(c1.user_notifier).__name__ == "SlackNotifier"
    finally:
        c1.close()
    # no slack block → NullNotifier (no per-user pings)
    c2 = build_user_components(base_cfg, "u2", base_url="https://x/api", internal_token="t",
                               since_days=14, limit=100)
    try:
        assert isinstance(c2.user_notifier, NullNotifier)
    finally:
        c2.close()
