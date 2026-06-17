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
                        lambda *a, **k: SimpleNamespace(reader=None, writer=None, llm=object(),
                                                        critic_llm=object(), close=lambda: None))
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
                        lambda *a, **k: SimpleNamespace(reader=None, writer=None, llm=object(),
                                                        critic_llm=object(),
                                                        close=lambda: closed.append(1)))
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


def test_total_email_budget_stops_run(monkeypatch):
    users = [{"user_id": f"u{i}", "enabled": True} for i in range(5)]
    cc = _FakeConfigClient(users, {f"u{i}": _cfg() for i in range(5)})
    import agent.cloud as cloudmod
    monkeypatch.setattr(cloudmod, "build_user_components",
                        lambda *a, **k: SimpleNamespace(reader=None, writer=None, llm=object(),
                                                        critic_llm=object(), close=lambda: None))
    s = cloud_run(cc, base_url="https://x/api", internal_token="t", prompts=None,
                  max_total_emails=2, run_once_fn=lambda **kw: _res(processed=1))
    assert s["stopped"] and "budget" in s["stopped"]
    assert s["processed"] == 2
