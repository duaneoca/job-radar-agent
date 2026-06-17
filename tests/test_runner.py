"""
Runner + lock tests — fakes only, offline. Covers fan-out, per-email error isolation, the run-record
heartbeat, dry-run (no mutations), and the lockfile overlap guard.
"""

import os

import pytest

from agent.lock import LockHeld, run_lock
from agent.llm import FakeLLM
from agent.prompts import SeedPromptProvider
from agent.reader import FakeReader
from agent.runner import run_once
from agent.schemas import Category, Classification, Critique, Posting
from agent.writer import FakeWriter

PROMPTS = SeedPromptProvider()


def _email(mid):
    return {"message_id": mid, "subject": "s", "sender": "a@b.com",
            "received_at": None, "body_text": "b", "has_attachments": False}


def _alert(n=1):
    return Classification(category=Category.job_alert, confidence=0.95, reasoning="x",
                          postings=[Posting(company=f"C{i}", role="R") for i in range(n)])


# ── lock ──────────────────────────────────────────────────────

def test_lock_blocks_second_holder(tmp_path):
    lp = str(tmp_path / "x.lock")
    with run_lock(lp):
        with pytest.raises(LockHeld):
            with run_lock(lp):
                pass


def test_lock_reclaims_stale_dead_pid(tmp_path):
    lp = tmp_path / "x.lock"
    lp.write_text("999999")  # not a live pid
    with run_lock(str(lp)):
        assert lp.read_text().strip() == str(os.getpid())


# ── runner ────────────────────────────────────────────────────

def test_run_once_fans_out_and_reports(tmp_path):
    reader = FakeReader([_email("<1>"), _email("<2>")])
    writer = FakeWriter()
    runner_kw = dict(reader=reader, writer=writer, prompts=PROMPTS,
                     use_lock=False)
    res = run_once(
        llm=FakeLLM([_alert(2), _alert(1)]),
        critic_llm=FakeLLM([Critique(valid=True), Critique(valid=True)]),
        **runner_kw,
    )
    assert res.emails_processed == 2
    assert res.postings_created == 3
    assert reader.moves == [("<1>", "postings"), ("<2>", "postings")]
    assert len(writer.runs) == 1                      # heartbeat reported
    assert writer.runs[0]["emails_processed"] == 2


def test_run_once_isolates_poison_email(tmp_path):
    reader = FakeReader([_email("<good>"), _email("<bad>")])
    writer = FakeWriter()

    def boom(*_):
        raise RuntimeError("kaboom")

    # Both emails classify fine; the SECOND email's critic raises — which propagates out of the graph
    # (classify swallows LLM errors as retries; critic does not). The runner must isolate it.
    res = run_once(
        reader=reader, writer=writer, prompts=PROMPTS, use_lock=False,
        llm=FakeLLM([_alert(1), _alert(1)]),
        critic_llm=FakeLLM([Critique(valid=True), boom]),
    )
    assert res.status == "partial"
    assert res.emails_processed == 1                  # good one still processed
    assert any("kaboom" in e for e in res.errors)


def test_dry_run_makes_no_mutations(tmp_path):
    reader = FakeReader([_email("<1>")])
    writer = FakeWriter()
    res = run_once(
        reader=reader, writer=writer, prompts=PROMPTS, use_lock=False, dry_run=True,
        llm=FakeLLM([_alert(1)]),
        critic_llm=FakeLLM([Critique(valid=True)]),
    )
    assert res.emails_processed == 1
    assert reader.moves == []                          # nothing moved
    assert writer.inbox_entries == []                  # nothing written
    assert writer.runs == []                           # no heartbeat in dry-run


def test_run_once_skips_when_locked(tmp_path):
    lp = str(tmp_path / "x.lock")
    with run_lock(lp):
        res = run_once(
            reader=FakeReader([_email("<1>")]), writer=FakeWriter(), prompts=PROMPTS,
            llm=FakeLLM([]), critic_llm=FakeLLM([]), lock_path=lp,
        )
    assert res.skipped is True


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))


def test_crash_still_records_failed_run_with_finished_at():
    # A reader that blows up mid-run must NOT leave the run dangling — a terminal record is posted.
    class _BoomReader:
        def get_unread(self):
            raise RuntimeError("scan exploded")
        def move_and_mark(self, *a, **k):  # pragma: no cover
            pass
    writer = FakeWriter()
    res = run_once(reader=_BoomReader(), writer=writer, prompts=PROMPTS, use_lock=False,
                   llm=FakeLLM([]), critic_llm=FakeLLM([]))
    assert res.status == "failed"
    assert len(writer.runs) == 1                       # a record WAS posted
    rec = writer.runs[0]
    assert rec["status"] == "failed" and rec["finished_at"]   # terminal + finished_at set
