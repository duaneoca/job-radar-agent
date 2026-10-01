"""Runner fan-out, isolation, dry-run, locking, and always-reported run records (V2)."""

import os

import pytest

from agent.dedup import SqliteDedupStore
from agent.lock import LockHeld, run_lock
from agent.llm import FakeLLM
from agent.prompts import SeedPromptProvider
from agent.reader import FakeReader
from agent.runner import run_once
from agent.writer import FakeWriter
from samples import LINKEDIN_HTML, linkedin_picks, make_email

PROMPTS = SeedPromptProvider()


def _email(mid):
    return make_email(LINKEDIN_HTML, message_id=mid)


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


def test_default_lock_lives_in_agent_home_not_tmp(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_HOME", str(tmp_path))
    run_once(reader=FakeReader([]), writer=FakeWriter(), llm=FakeLLM([]), prompts=PROMPTS)
    assert (tmp_path / "data").is_dir()               # lock dir created under AGENT_HOME


# ── runner ────────────────────────────────────────────────────

def test_run_once_fans_out_and_reports():
    reader = FakeReader([_email("<1>"), _email("<2>")])
    writer = FakeWriter()
    res = run_once(reader=reader, writer=writer, prompts=PROMPTS, use_lock=False,
                   llm=FakeLLM([linkedin_picks(), linkedin_picks()]))
    assert res.status == "success"
    assert res.emails_processed == 2 and res.postings_created == 4
    assert reader.marked_read == ["<1>", "<2>"] and reader.moves == []
    assert len(writer.runs) == 1 and writer.runs[0]["emails_processed"] == 2
    assert writer.runs[0]["agent_version"] == "2.0.0"


def test_duplicates_across_emails_in_one_run(tmp_path):
    reader = FakeReader([_email("<1>"), _email("<2>")])
    writer = FakeWriter()
    res = run_once(reader=reader, writer=writer, prompts=PROMPTS, use_lock=False,
                   dedup=SqliteDedupStore(tmp_path / "d.sqlite"),
                   llm=FakeLLM([linkedin_picks(), linkedin_picks()]))
    assert res.postings_created == 2 and res.duplicates_skipped == 2
    assert len(writer.inbox_entries) == 1 and reader.marked_read == ["<1>", "<2>"]


def test_run_once_isolates_poison_email_and_leaves_it_unread():
    reader = FakeReader([_email("<good>"), _email("<bad>")])

    def boom(*_):
        raise RuntimeError("kaboom")            # e.g. an LLM timeout — not a parse error
    res = run_once(reader=reader, writer=FakeWriter(), prompts=PROMPTS, use_lock=False,
                   llm=FakeLLM([linkedin_picks(), boom]))
    assert res.status == "partial" and res.emails_processed == 1
    assert any("kaboom" in e for e in res.errors)
    assert reader.marked_read == ["<good>"] and reader.moves == []    # <bad> untouched


def test_unprocessed_emails_are_counted_as_escalations():
    reader = FakeReader([make_email(LINKEDIN_HTML, auth=[])])        # fails the DMARC check
    res = run_once(reader=reader, writer=FakeWriter(), prompts=PROMPTS, use_lock=False,
                   llm=FakeLLM([]))
    assert res.escalations == 1 and "DMARC" in res.details[0]["reason"]


def test_dry_run_makes_no_mutations(tmp_path):
    reader = FakeReader([_email("<1>")])
    writer = FakeWriter()
    store = SqliteDedupStore(tmp_path / "d.sqlite")
    res = run_once(reader=reader, writer=writer, prompts=PROMPTS, use_lock=False, dry_run=True,
                   dedup=store, llm=FakeLLM([linkedin_picks()]))
    assert res.emails_processed == 1 and res.postings_created == 2   # what WOULD happen
    assert reader.marked_read == [] and reader.moves == []
    assert writer.inbox_entries == [] and writer.runs == []
    assert not store.seen("linkedin:4457198691")                     # nothing recorded


def test_run_once_skips_when_locked(tmp_path):
    lp = str(tmp_path / "x.lock")
    with run_lock(lp):
        res = run_once(reader=FakeReader([_email("<1>")]), writer=FakeWriter(), prompts=PROMPTS,
                       llm=FakeLLM([]), lock_path=lp)
    assert res.skipped is True


def test_crash_still_records_failed_run_with_finished_at():
    class _BoomReader:
        def get_unread(self):
            raise RuntimeError("scan exploded")
    writer = FakeWriter()
    res = run_once(reader=_BoomReader(), writer=writer, prompts=PROMPTS, use_lock=False,
                   llm=FakeLLM([]))
    assert res.status == "failed"
    (rec,) = writer.runs
    assert rec["status"] == "failed" and rec["finished_at"] and rec["interactions_recorded"] == 0


def test_job_radar_refusal_stops_the_run_before_any_llm_or_mail_access():
    class Disabled(FakeWriter):
        def get_reviews(self):
            raise RuntimeError('403 {"detail":"Email agent disabled"}')

    class NoTouchReader(FakeReader):
        def get_unread(self):
            raise AssertionError("mail must not be read")
    writer = Disabled()
    res = run_once(reader=NoTouchReader(), writer=writer, prompts=PROMPTS, use_lock=False,
                   llm=FakeLLM([]))                       # FakeLLM would fail if called
    assert res.status == "failed" and "Email agent disabled" in res.errors[0]
    assert writer.runs and writer.runs[0]["status"] == "failed"


def test_tracked_jobs_are_fetched_once_per_run():
    class Counting(FakeWriter):
        calls = 0

        def get_reviews(self):
            Counting.calls += 1
            return []
    run_once(reader=FakeReader([_email("<1>"), _email("<2>")]), writer=Counting(),
             prompts=PROMPTS, use_lock=False, llm=FakeLLM([linkedin_picks(), linkedin_picks()]))
    assert Counting.calls == 1
