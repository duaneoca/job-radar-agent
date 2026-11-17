"""Cloud-run configuration guards.

make_sort_stage raises SystemExit when SORTER_ENABLED=true and TYPESAFE_API_KEY is
empty. SystemExit is a BaseException, so raised inside the per-user loop it skips the
`except Exception` handler and tears down the run for EVERY user on the first user with
the sorter on. The Jev key is pod-wide, so that fault is never one user's problem.

Two guards, tested here:
  1. check_startup_config() fails the whole run up front, before any user is touched.
  2. a SystemExit while BUILDING a user's components is downgraded to that user's
     failure — while SystemExit from the run itself (SIGTERM) still stops the run.
"""

from types import SimpleNamespace

import pytest

import agent.cloud as cloudmod
from agent.cloud import UserConfigError, check_startup_config, cloud_run
from notifications.base import FakeNotifier


def _settings(*, sorter_enabled, typesafe_api_key):
    return SimpleNamespace(sorter_enabled=sorter_enabled, typesafe_api_key=typesafe_api_key)


class _FakeConfigClient:
    def __init__(self, users, configs):
        self._users, self._configs = users, configs
        self.listed = 0
        self.fetched: list[str] = []

    def users(self):
        self.listed += 1
        return self._users

    def config(self, uid):
        self.fetched.append(uid)
        return self._configs[uid]


def _stub(close=lambda: None):
    return SimpleNamespace(reader=None, writer=None, llm=object(), policy=None, dedup=None,
                           zero_postings_action="mark_read", user_notifier=FakeNotifier(),
                           close=close, sort_stage=None, retention_stage=None)


def _res(processed=1):
    return SimpleNamespace(emails_processed=processed, escalations=0, status="success", errors=[])


# ── 1. startup check ──────────────────────────────────────────

def test_startup_check_rejects_sorter_without_key():
    with pytest.raises(SystemExit) as exc:
        check_startup_config(_settings(sorter_enabled=True, typesafe_api_key=""))
    assert "TYPESAFE_API_KEY" in str(exc.value)


@pytest.mark.parametrize("sorter_enabled,key", [(False, ""), (False, "k"), (True, "k")])
def test_startup_check_passes_otherwise(sorter_enabled, key):
    assert check_startup_config(_settings(sorter_enabled=sorter_enabled,
                                          typesafe_api_key=key)) is None


def test_cloud_run_fails_before_touching_any_user(monkeypatch):
    cc = _FakeConfigClient([{"user_id": "u1", "enabled": True}], {"u1": {}})
    monkeypatch.setattr(cloudmod, "settings", _settings(sorter_enabled=True, typesafe_api_key=""))
    with pytest.raises(SystemExit):
        cloud_run(cc, base_url="https://x/api", internal_token="t", prompts=None)
    # nothing enumerated, nothing fetched, nothing spent
    assert cc.listed == 0 and cc.fetched == []


# ── 2. per-user isolation ─────────────────────────────────────

def test_one_users_systemexit_does_not_abort_the_others(monkeypatch):
    users = [{"user_id": "u1", "enabled": True}, {"user_id": "u2", "enabled": True}]
    cc = _FakeConfigClient(users, {"u1": {}, "u2": {}})
    monkeypatch.setattr(cloudmod, "settings", _settings(sorter_enabled=False, typesafe_api_key=""))

    def build(cfg, uid, **kw):
        if uid == "u1":
            raise SystemExit("✗ SORTER_ENABLED=true but TYPESAFE_API_KEY is not set")
        return _stub()

    monkeypatch.setattr(cloudmod, "build_user_components", build)
    s = cloud_run(cc, base_url="https://x/api", internal_token="t", prompts=None,
                  run_once_fn=lambda **kw: _res())

    assert s["users"] == 2                 # both considered
    assert s["processed"] == 1             # u2 still ran
    assert s["stopped"] is None
    assert any("u1" in e and "TYPESAFE_API_KEY" in e for e in s["errors"])


def test_build_failure_is_reported_as_a_normal_user_error(monkeypatch):
    cc = _FakeConfigClient([{"user_id": "u1", "enabled": True}], {"u1": {}})
    monkeypatch.setattr(cloudmod, "settings", _settings(sorter_enabled=False, typesafe_api_key=""))
    monkeypatch.setattr(cloudmod, "build_user_components",
                        lambda cfg, uid, **kw: (_ for _ in ()).throw(SystemExit("boom")))
    s = cloud_run(cc, base_url="https://x/api", internal_token="t", prompts=None)
    assert s["errors"] and "UserConfigError" in s["errors"][0]


def test_user_config_error_is_an_ordinary_exception():
    # The per-user handler catches Exception; a BaseException subclass would slip past it.
    assert issubclass(UserConfigError, Exception)


# ── 3. the guard must not swallow a real shutdown ─────────────

def test_sigterm_during_a_users_run_still_stops_the_run(monkeypatch):
    """run_once raising SystemExit means SIGTERM (see runner.run_once's finally).
    That must still tear the run down — otherwise a k8s CronJob hitting its
    activeDeadlineSeconds would march on to the next user instead of exiting."""
    users = [{"user_id": "u1", "enabled": True}, {"user_id": "u2", "enabled": True}]
    cc = _FakeConfigClient(users, {"u1": {}, "u2": {}})
    monkeypatch.setattr(cloudmod, "settings", _settings(sorter_enabled=False, typesafe_api_key=""))
    monkeypatch.setattr(cloudmod, "build_user_components", lambda cfg, uid, **kw: _stub())

    def sigterm(**kw):
        raise SystemExit(143)

    with pytest.raises(SystemExit):
        cloud_run(cc, base_url="https://x/api", internal_token="t", prompts=None,
                  run_once_fn=sigterm)
    assert cc.fetched == ["u1"]            # stopped at the first user, never reached u2
