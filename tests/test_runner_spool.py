"""
The runner's use of the spool: keep an unreportable record, flush it when the server returns,
state what the bounds discarded, and never let any of that break a run.

Cloud passes no spool — see docs/run-record-spool.md for why (pod disk is the wrong lifetime and a
cross-user boundary; cloud reachability is inferred server-side instead).
"""

from __future__ import annotations

import pytest

from agent.llm import FakeLLM
from agent.prompts import SeedPromptProvider
from agent.reader import FakeReader
from agent.run_spool import RunSpool
from agent.runner import run_once
from agent.writer import FakeWriter

PROMPTS = SeedPromptProvider()


class _UnreachableWriter(FakeWriter):
    """Reporting fails the way it does in the field: the network that the run needed is gone."""

    def __init__(self, fail_times: int = 10**6):
        super().__init__()
        self._left = fail_times
        self.attempts = 0

    def report_run(self, record):
        self.attempts += 1
        if self._left > 0:
            self._left -= 1
            raise ConnectionError("[Errno 8] nodename nor servname provided, or not known")
        return super().report_run(record)


def _run(writer, spool, tmp_path, **kw):
    return run_once(reader=FakeReader([]), writer=writer, llm=FakeLLM([]), prompts=PROMPTS,
                    run_spool=spool, lock_path=str(tmp_path / "a.lock"), **kw)


@pytest.fixture
def spool(tmp_path):
    return RunSpool(path=str(tmp_path / "pending_runs.jsonl"))


# ── spool on report failure ───────────────────────────────────

def test_unreportable_record_is_spooled(spool, tmp_path):
    res = _run(_UnreachableWriter(), spool, tmp_path)
    assert any("report_run failed" in e for e in res.errors)
    assert len(spool.pending()) == 1
    assert res.spool_pending == 1


def test_the_spooled_record_is_the_real_run_record(spool, tmp_path):
    _run(_UnreachableWriter(), spool, tmp_path, environment="local", agent_version="2.0.0")
    rec = spool.pending()[0]
    assert rec["environment"] == "local" and rec["agent_version"] == "2.0.0"
    assert rec["started_at"] and rec["finished_at"]


def test_successful_report_spools_nothing(spool, tmp_path):
    res = _run(FakeWriter(), spool, tmp_path)
    assert spool.pending() == []
    assert res.spool_pending == 0 and res.spool_flushed == 0


# ── flush when the server comes back ──────────────────────────

def test_next_reachable_run_flushes_what_earlier_runs_spooled(spool, tmp_path):
    for _ in range(3):
        _run(_UnreachableWriter(), spool, tmp_path)
    assert len(spool.pending()) == 3

    good = FakeWriter()
    res = _run(good, spool, tmp_path)
    assert res.spool_flushed == 3 and res.spool_pending == 0
    assert spool.pending() == []
    # this run's own record plus the three backfilled ones
    assert len(good.runs) == 4
    assert sum(1 for r in good.runs if r.get("late")) == 3


def test_a_still_unreachable_run_does_not_flush(spool, tmp_path):
    _run(_UnreachableWriter(), spool, tmp_path)
    res = _run(_UnreachableWriter(), spool, tmp_path)
    assert res.spool_flushed == 0
    assert len(spool.pending()) == 2


def test_dropped_records_are_stated_in_the_next_report(spool, tmp_path):
    spool._bump_dropped(4)
    good = FakeWriter()
    res = _run(good, spool, tmp_path)
    assert any("4 unreported run record(s) dropped" in e for e in res.errors)
    assert "4 unreported run record(s) dropped" in (good.runs[0]["error_summary"] or "")


# ── never fails a run ─────────────────────────────────────────

def test_a_broken_spool_does_not_fail_the_run(tmp_path):
    class _Broken:
        def take_dropped(self): raise OSError("disk gone")
        def add(self, rec): raise OSError("disk gone")
        def flush(self, report): raise OSError("disk gone")
        def pending(self): raise OSError("disk gone")

    with pytest.raises(OSError):
        _run(FakeWriter(), _Broken(), tmp_path)


def test_no_spool_behaves_exactly_as_before(tmp_path):
    """run_spool defaults to None, so every existing caller is untouched."""
    res = run_once(reader=FakeReader([]), writer=_UnreachableWriter(), llm=FakeLLM([]),
                   prompts=PROMPTS, lock_path=str(tmp_path / "a.lock"))
    assert any("report_run failed" in e for e in res.errors)
    assert res.spool_flushed == 0 and res.spool_pending == 0


def test_dry_run_neither_reports_nor_spools(spool, tmp_path):
    _run(_UnreachableWriter(), spool, tmp_path, dry_run=True)
    assert spool.pending() == []


# ── cloud must not spool ──────────────────────────────────────

def test_cloud_run_passes_no_spool():
    """Local-only by design; a pod disk is the wrong place for per-user state."""
    import inspect

    from agent import cloud
    src = inspect.getsource(cloud)
    assert "run_spool" not in src, "cloud must not pass a run spool"
