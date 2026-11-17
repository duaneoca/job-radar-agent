"""
Run-record spool: a run that cannot be reported must not vanish.

Four behaviours, per docs/run-record-spool.md: spool on report failure, flush oldest-first,
enforce both bounds, and never fail a run. Plus the design's hard line — cloud does not spool.
"""

from __future__ import annotations

import datetime
import json

import pytest

from agent.run_spool import MAX_AGE_DAYS, MAX_RECORDS, RunSpool


def _rec(n: int, *, days_ago: float = 0.0) -> dict:
    ts = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days_ago)
    return {"status": "failed", "emails_processed": n,
            "started_at": ts.strftime("%Y-%m-%dT%H:%M:%SZ")}


@pytest.fixture
def spool(tmp_path):
    return RunSpool(path=str(tmp_path / "pending_runs.jsonl"))


# ── 1. spool on report failure ────────────────────────────────

def test_record_survives_and_round_trips(spool):
    spool.add(_rec(1))
    assert [r["emails_processed"] for r in spool.pending()] == [1]


def test_records_accumulate_oldest_first(spool):
    for i in range(3):
        spool.add(_rec(i))
    assert [r["emails_processed"] for r in spool.pending()] == [0, 1, 2]


def test_empty_spool_reads_as_empty(spool):
    assert spool.pending() == []


# ── 2. flush oldest-first, marked late ────────────────────────

def test_flush_sends_oldest_first_and_empties(spool):
    for i in range(3):
        spool.add(_rec(i))
    sent_records: list[dict] = []
    sent, remaining = spool.flush(sent_records.append)
    assert (sent, remaining) == (3, 0)
    assert [r["emails_processed"] for r in sent_records] == [0, 1, 2]
    assert spool.pending() == []


def test_every_flushed_record_is_marked_late(spool):
    spool.add(_rec(1))
    sent_records: list[dict] = []
    spool.flush(sent_records.append)
    assert sent_records[0]["late"] is True


def test_flush_stops_at_the_first_failure_and_keeps_the_rest(spool):
    """A still-unreachable server costs one attempt, not one per record."""
    for i in range(4):
        spool.add(_rec(i))
    attempts: list[dict] = []

    def report(rec):
        attempts.append(rec)
        if len(attempts) == 2:          # second record fails
            raise RuntimeError("still unreachable")

    sent, remaining = spool.flush(report)
    assert (sent, remaining) == (1, 3)
    assert len(attempts) == 2           # stopped, did not try records 3 and 4
    assert [r["emails_processed"] for r in spool.pending()] == [1, 2, 3]


def test_flush_on_empty_spool_is_a_noop(spool):
    calls = []
    assert spool.flush(calls.append) == (0, 0)
    assert calls == []


# ── 3. both bounds ───────────────────────────────────────────

def test_records_older_than_the_age_bound_are_dropped(spool):
    spool.add(_rec(1, days_ago=MAX_AGE_DAYS + 1))
    spool.add(_rec(2, days_ago=0))
    assert [r["emails_processed"] for r in spool.pending()] == [2]


def test_records_inside_the_age_bound_are_kept(spool):
    spool.add(_rec(1, days_ago=MAX_AGE_DAYS - 1))
    assert len(spool.pending()) == 1


def test_cap_keeps_the_newest(spool):
    for i in range(MAX_RECORDS + 5):
        spool.add(_rec(i))
    pending = spool.pending()
    assert len(pending) == MAX_RECORDS
    assert pending[0]["emails_processed"] == 5          # oldest five dropped
    assert pending[-1]["emails_processed"] == MAX_RECORDS + 4


def test_dropped_count_is_reported_once_then_reset(spool):
    spool.add(_rec(1, days_ago=MAX_AGE_DAYS + 1))
    spool.add(_rec(2))
    assert spool.take_dropped() == 1
    assert spool.take_dropped() == 0                   # reported once, not every run


def test_record_without_a_usable_timestamp_is_kept(spool):
    """Unknown age is not evidence of staleness; the cap still bounds the file."""
    spool.add({"status": "failed"})
    spool.add({"status": "failed", "started_at": "not a date"})
    assert len(spool.pending()) == 2


# ── 4. never fails a run ─────────────────────────────────────

def test_add_to_an_unwritable_path_does_not_raise(tmp_path):
    bad = RunSpool(path=str(tmp_path / "nope" / "\0" / "pending.jsonl"))
    assert bad.add(_rec(1)) == 0        # degrades to today's behaviour: the record is lost


def test_flush_from_an_unreadable_path_does_not_raise(tmp_path):
    bad = RunSpool(path=str(tmp_path / "nope" / "\0" / "pending.jsonl"))
    assert bad.flush(lambda rec: None) == (0, 0)


def test_a_corrupt_line_does_not_block_the_records_behind_it(spool, tmp_path):
    spool.add(_rec(1))
    with open(spool._path, "a", encoding="utf-8") as f:
        f.write("{half written\n")
    spool.add(_rec(2))
    assert [r["emails_processed"] for r in spool.pending()] == [1, 2]


def test_a_reporter_that_always_raises_leaves_the_spool_intact(spool):
    spool.add(_rec(1))

    def boom(rec):
        raise RuntimeError("down")

    assert spool.flush(boom) == (0, 1)
    assert len(spool.pending()) == 1


def test_spool_file_is_newline_delimited_json(spool):
    spool.add(_rec(1))
    spool.add(_rec(2))
    lines = open(spool._path, encoding="utf-8").read().splitlines()
    assert len(lines) == 2
    assert all(isinstance(json.loads(ln), dict) for ln in lines)
