"""Sort stage: write-then-move, one mutation per email, dry-run safety, stale-view tolerance."""

import pytest

from agent.runner import run_once
from agent.sort_stage import SortConfig, SortStage
from agent.writer import FakeWriter


class FakeJev:
    """Answers the category question from a per-subject table; the card question gets no picks."""

    def __init__(self, table):
        self.table = table
        self.run_cost = 0.0

    def reset_cost(self):
        self.run_cost = 0.0

    def ask(self, state, questions):
        self.run_cost += 0.001
        if "category" in questions:
            cat, p = self.table[state["subject"]]
            return {"category": {"type": "choice", "choice": cat, "confidence": p,
                                 "probabilities": {cat: p, "other": round(1 - p, 4)}}}
        return {"is_agency": {"type": "noul", "noul": 0.9}}

    def ask_choice(self, state, name, question):
        from agent.jev import ChoiceAnswer
        a = self.ask(state, {name: question})[name]
        return ChoiceAnswer(a["choice"], a["confidence"], a["probabilities"])


class RootReader:
    def __init__(self, emails, missing=()):
        self.emails, self.missing, self.moves = emails, set(missing), []

    def get_unread(self):
        return list(self.emails)

    def move_and_mark(self, message_id, destination, mark_read=True):
        if message_id in self.missing:
            raise LookupError(message_id)
        self.moves.append((message_id, destination, mark_read))


class BoomWriter(FakeWriter):
    def create_inbox_entry(self, payload):
        raise RuntimeError("Job Radar down")


def _email(mid, subject, sender="Pat Example <pat@examplestaffing.com>"):
    return {"message_id": mid, "subject": subject, "sender": sender,
            "received_at": "2026-09-29T10:00:00+00:00", "body_text": "Hi\nPat Example\nRecruiter"}


TABLE = {"recruiter": ("recruiter_outreach", 0.97), "update": ("application_update", 0.95),
         "alert": ("job_alert", 0.99), "social": ("network_social", 0.96),
         "unsure": ("job_alert", 0.55), "dice": ("recruiter_outreach", 0.99),
         "inmail": ("recruiter_outreach", 0.95)}

EMAILS = [_email("<r>", "recruiter"), _email("<u>", "update"), _email("<a>", "alert"),
          _email("<s>", "social"), _email("<q>", "unsure"),
          _email("<d>", "dice", sender='"Kajal Saini" <x-y@user.dice.com>')]


def _stage(emails=EMAILS, missing=()):
    reader = RootReader(emails, missing)
    return SortStage(reader, FakeJev(TABLE), SortConfig()), reader


def test_routes_writes_interaction_and_moves_once_each():
    stage, reader = _stage()
    w = FakeWriter()
    res = stage.run(w)
    assert reader.moves == [("<r>", "interaction", False), ("<u>", "interaction", False),
                            ("<a>", "postings", False), ("<s>", "social", True),
                            ("<q>", "unprocessed", True), ("<d>", "interaction", False)]
    assert dict(res.moved) == {"interaction": 3, "postings": 1, "social": 1, "unprocessed": 1}
    # only Interaction mail is written; postings are left to the link-picker stage
    assert [p["message_id"] for p in w.inbox_entries] == ["<r>", "<u>", "<d>"]
    r, u, d = w.inbox_entries
    assert r["category"] == "recruiter_outreach" and r["postings"] == []
    assert r["raw_extracted_json"]["recruiter_contact"]["email"] == "pat@examplestaffing.com"
    assert r["recruiter"] == r["raw_extracted_json"]["recruiter_contact"]
    assert u["category"] == "application_confirmation" and "recruiter_contact" not in u["raw_extracted_json"]
    # Dice's per-recruiter relay is a real recruiter conversation → a suggestion keyed by that address
    assert d["category"] == "recruiter_outreach"
    assert d["raw_extracted_json"]["recruiter_contact"]["email"] == "x-y@user.dice.com"
    assert res.inbox_written == 3 and res.recruiter_cards == 1 + 0 + 1 and not res.errors


def test_configured_bulk_channel_goes_to_postings():
    reader = RootReader([_email("<d>", "dice", sender='"Dice Recruiter" <x-y@user.dice.com>')])
    stage = SortStage(reader, FakeJev(TABLE), SortConfig(bulk_domains=("user.dice.com",)))
    w = FakeWriter()
    stage.run(w)
    assert reader.moves == [("<d>", "postings", False)] and w.inbox_entries == []


def test_relay_recruiter_without_real_address_is_still_a_recruiter():
    stage, _ = _stage([_email("<i>", "inmail", sender='"Kyle Stock" <inmail-hit-reply@linkedin.com>')])
    w = FakeWriter()
    stage.run(w)
    (p,) = w.inbox_entries
    # Job Radar groups shared relay senders by person (job-radar #150), so InMail recruiters reach
    # suggestions instead of being filed as network messages; the card carries no relay address.
    assert p["category"] == "recruiter_outreach"
    assert p["raw_extracted_json"]["recruiter_contact"]["name"] == "Kyle Stock"
    assert "email" not in p["raw_extracted_json"]["recruiter_contact"]


def test_dry_run_neither_writes_nor_moves():
    stage, reader = _stage()
    w = FakeWriter()
    res = stage.run(w, dry_run=True)
    assert reader.moves == [] and w.inbox_entries == []
    assert res.moved["interaction"] == 3 and len(res.details) == 6


def test_already_moved_message_counts_as_gone_not_error():
    stage, reader = _stage(missing={"<s>"})
    res = stage.run(FakeWriter())
    assert res.gone == 1 and not res.errors and "social" not in res.moved


def test_failed_inbox_write_leaves_email_unmoved():
    stage, reader = _stage([_email("<r>", "recruiter"), _email("<a>", "alert")])
    res = stage.run(BoomWriter())
    assert reader.moves == [("<a>", "postings", False)]       # <r> stays unread in the root
    assert len(res.errors) == 1 and "<r>" in res.errors[0]


def test_budget_halts_stage():
    stage, reader = _stage()
    res = stage.run(FakeWriter(), over_budget=lambda: True)
    assert reader.moves == [] and res.halted


# ── runner integration ────────────────────────────────────────

class EmptyPostings:
    def get_unread(self):
        return []

    def mark_read(self, mid):
        pass

    def move_and_mark(self, mid, dest, mark_read=True):
        pass


def _run(stage, **kw):
    w = FakeWriter()
    res = run_once(reader=EmptyPostings(), writer=w, llm=object(), prompts=None,
                   use_lock=False, sort_stage=stage, **kw)
    return res, w


def test_run_once_sorts_first_and_reports_counts():
    stage, _ = _stage()
    res, w = _run(stage)
    assert res.status == "success" and res.sorted["interaction"] == 3
    assert res.escalations == 1 and res.interactions_recorded == 3
    (record,) = w.runs
    assert record["interactions_recorded"] == 3 and record["escalations"] == 1


def test_run_once_charges_jev_against_the_daily_ceiling():
    class Spend:
        added = []

        def spent_today(self, key):
            return 0.0

        def add(self, key, usd):
            self.added.append(usd)

    stage, reader = _stage()
    res, _ = _run(stage, daily_ceiling=0.0025, spend_store=Spend())
    assert len(reader.moves) == 2 and res.status == "partial"      # halted after ~$0.0025 of Jev
    assert Spend.added and Spend.added[0] == pytest.approx(stage.jev.run_cost)


def test_run_once_dry_run_sorts_nothing_for_real():
    stage, reader = _stage()
    res, w = _run(stage, dry_run=True)
    assert reader.moves == [] and w.inbox_entries == [] and w.runs == []
    assert res.sorted["postings"] == 1
