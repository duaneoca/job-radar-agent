"""DailySpendStore + run_once daily-ceiling enforcement (H4)."""

from agent.budget import DailySpendStore
from agent.llm import FakeLLM
from agent.prompts import SeedPromptProvider
from agent.reader import FakeReader
from agent.runner import run_once
from agent.writer import FakeWriter
from samples import LINKEDIN_HTML, linkedin_picks, make_email

PROMPTS = SeedPromptProvider()


def _email(mid):
    return make_email(LINKEDIN_HTML, message_id=mid)


def _alert():
    return linkedin_picks()


# ── store ─────────────────────────────────────────────────────

def test_store_accumulates_and_persists(tmp_path):
    s = DailySpendStore(str(tmp_path / "spend.json"))
    assert s.spent_today("u1") == 0.0
    s.add("u1", 0.10)
    s.add("u1", 0.05)
    assert abs(s.spent_today("u1") - 0.15) < 1e-9
    assert s.spent_today("u2") == 0.0                 # keyed per user
    # survives a fresh instance (persisted)
    assert abs(DailySpendStore(str(tmp_path / "spend.json")).spent_today("u1") - 0.15) < 1e-9


# ── enforcement ───────────────────────────────────────────────

class _CostLLM(FakeLLM):
    """FakeLLM that also accrues run_cost like LiteLLMClient."""
    def __init__(self, responses, cost_per_call=0.0):
        super().__init__(responses)
        self.run_cost = 0.0
        self._cpc = cost_per_call
    def reset_cost(self):
        self.run_cost = 0.0
    def structured(self, **kw):
        self.run_cost += self._cpc
        return super().structured(**kw)


def test_run_skipped_when_already_over_ceiling(tmp_path):
    store = DailySpendStore(str(tmp_path / "spend.json"))
    store.add("local", 5.0)                            # already at ceiling
    reader = FakeReader([_email("<1>")])
    res = run_once(reader=reader, writer=FakeWriter(), prompts=PROMPTS, use_lock=False,
                   llm=FakeLLM([_alert()]),
                   spend_key="local", daily_ceiling=5.0, spend_store=store)
    assert res.emails_processed == 0
    assert any("ceiling reached" in e for e in res.errors)
    assert reader.moves == [] and reader.marked_read == []   # nothing processed


def test_run_halts_midway_and_persists_spend(tmp_path):
    store = DailySpendStore(str(tmp_path / "spend.json"))
    # ceiling 0.25; each pick costs 0.10 — enforcement halts at the top of an email once
    # accrued spend ≥ ceiling, so with 5 emails it processes 3 (0.30) then halts before #4.
    n = 5
    llm = _CostLLM([_alert() for _ in range(n)], cost_per_call=0.10)
    reader = FakeReader([_email(f"<{i}>") for i in range(n)])
    res = run_once(reader=reader, writer=FakeWriter(), prompts=PROMPTS, use_lock=False,
                   llm=llm,
                   spend_key="local", daily_ceiling=0.25, spend_store=store)
    assert res.emails_processed < n                    # halted before processing all
    assert any("ceiling reached mid-run" in e for e in res.errors)
    assert store.spent_today("local") > 0              # accrued spend persisted


def test_no_enforcement_without_store(tmp_path):
    # default (no spend_store) → no budget logic, processes normally
    reader = FakeReader([_email("<1>")])
    res = run_once(reader=reader, writer=FakeWriter(), prompts=PROMPTS, use_lock=False,
                   llm=FakeLLM([_alert()]))
    assert res.emails_processed == 1


def test_ceiling_zero_means_disabled_not_block_all(tmp_path):
    # a misconfigured ceiling of 0 must NOT silently skip every run (footgun guard)
    store = DailySpendStore(str(tmp_path / "spend.json"))
    reader = FakeReader([_email("<1>")])
    res = run_once(reader=reader, writer=FakeWriter(), prompts=PROMPTS, use_lock=False,
                   llm=FakeLLM([_alert()]),
                   spend_key="local", daily_ceiling=0, spend_store=store)
    assert res.emails_processed == 1                  # processed, not skipped


def test_scheduled_loop_passes_the_ceiling_to_the_runner():
    # V1 bug: `job-radar-agent run` (the launchd path) never passed the ceiling, so it was unenforced.
    from types import SimpleNamespace
    from agent.loop import run_loop
    seen = {}

    def fake_run(**kw):
        seen.update(kw)
        return SimpleNamespace(skipped=False, status="success", emails_processed=0,
                               postings_created=0, duplicates_skipped=0, escalations=0,
                               retries=0, errors=[])
    comps = SimpleNamespace(reader=None, writer=None, llm=None, policy=None, dedup=None,
                            zero_postings_action="mark_read", notifier=None, inbox_base_url="",
                            spend_store="STORE", daily_ceiling=5.0, close=lambda: None)
    run_loop(once=True, dry_run=True, interval=1, build_components=lambda: comps,
             run_once=fake_run, sleep=lambda _: None, install_signals=False)
    assert seen["daily_ceiling"] == 5.0 and seen["spend_store"] == "STORE"
