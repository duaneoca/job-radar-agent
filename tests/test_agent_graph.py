"""
Tests for the LangGraph agent — node logic + full end-to-end graph runs, all offline via fakes.
Proves: classify→critic→retry→escalate loop, category routing, dedup, truncation, interaction
matching (unique / ambiguous→HITL / none), and the single move point.
"""

import pytest

from agent.graph import build_graph
from agent.llm import FakeLLM
from agent.nodes import Nodes
from agent.prompts import SeedPromptProvider
from agent.reader import FakeReader
from agent.nodes import _clean_recruiter
from agent.schemas import (
    Category,
    Classification,
    Critique,
    InteractionSignal,
    Posting,
    RecruiterContact,
    WritableStatus,
)
from agent.state import AgentState
from agent.writer import FakeWriter

PROMPTS = SeedPromptProvider()


def email(**kw) -> dict:
    base = {
        "message_id": "<m1@x>", "subject": "s", "sender": "a@b.com",
        "received_at": None, "body_text": "body", "has_attachments": False,
    }
    base.update(kw)
    return base


def make_nodes(classifier_responses, critic_responses, reviews=None):
    writer = FakeWriter(reviews=reviews or [])
    reader = FakeReader()
    nodes = Nodes(
        llm=FakeLLM(classifier_responses),
        critic_llm=FakeLLM(critic_responses),
        writer=writer,
        reader=reader,
        prompts=PROMPTS,
    )
    return nodes, writer, reader


# ── node-level ────────────────────────────────────────────────

def test_gate_valid_routes():
    state: AgentState = {
        "classification": Classification(category=Category.job_alert, confidence=0.9, reasoning="x"),
        "critique": Critique(valid=True), "attempts": 1,
    }
    assert Nodes.gate(state) == "route"


def test_gate_low_confidence_retries_then_escalates():
    cls = Classification(category=Category.job_alert, confidence=0.4, reasoning="x")
    assert Nodes.gate({"classification": cls, "critique": Critique(valid=True), "attempts": 1}) == "retry"
    assert Nodes.gate({"classification": cls, "critique": Critique(valid=True), "attempts": 2}) == "escalate"


def test_classify_increments_attempts_and_uses_feedback():
    cls = Classification(category=Category.job_alert, confidence=0.9, reasoning="x")
    nodes, *_ = make_nodes([cls], [])
    out = nodes.classify({"email": email(), "attempts": 1, "feedback": ["fix the role"]})
    assert out["attempts"] == 2
    assert "fix the role" in nodes.llm.calls[0]["user"]


def test_classify_handles_malformed_output():
    def boom(*_):
        raise ValueError("not json")
    nodes, *_ = make_nodes([boom], [])
    out = nodes.classify({"email": email(), "attempts": 0})
    assert out["classification"] is None
    assert out["feedback"] and "not valid structured data" in out["feedback"][0]


def test_critic_unparseable_output_soft_fails_not_crash():
    # A malformed critic response must retry (valid=False), not abort the email.
    cls = Classification(category=Category.job_alert, confidence=0.9, reasoning="x")
    def boom(*_):
        raise ValueError("trailing characters")
    nodes, *_ = make_nodes([cls], [boom])
    out = nodes.critic({"classification": cls, "email": email(), "attempts": 1})
    assert out["critique"].valid is False
    assert "unparseable" in out["critique"].issues[0]


def test_critic_propagates_infra_errors():
    cls = Classification(category=Category.job_alert, confidence=0.9, reasoning="x")
    class RateLimitError(Exception):
        pass
    def rate_limited(*_):
        raise RateLimitError("429")
    nodes, *_ = make_nodes([cls], [rate_limited])
    with pytest.raises(RateLimitError):
        nodes.critic({"classification": cls, "email": email(), "attempts": 1})


def test_classify_propagates_infra_errors():
    # A rate-limit/timeout/API error is NOT a ValueError — it must propagate (so the runner leaves the
    # email in place to retry) rather than being swallowed into a misfile-to-Unprocessed escalation.
    class RateLimitError(Exception):
        pass

    def rate_limited(*_):
        raise RateLimitError("429 quota exceeded")
    nodes, *_ = make_nodes([rate_limited], [])
    with pytest.raises(RateLimitError):
        nodes.classify({"email": email(), "attempts": 0})


def test_clean_recruiter_caps_strips_and_filters():
    rc = RecruiterContact(
        name="<b>Jane Smith</b>", email="jane@agency.com", phone="+1 555-1212",
        employer="Best Recruiting", title="x" * 300,
        linkedin_url="javascript:alert(1)", is_agency=True,
        represents=["Acme", "  ", "<i>Beta</i>"], recruiter_confidence=0.9,
    )
    out = _clean_recruiter(rc)
    assert out["name"] == "Jane Smith"               # markup stripped
    assert len(out["title"]) == 200                   # capped
    assert "linkedin_url" not in out                  # non-http dropped (C2 allowlist)
    assert out["represents"] == ["Acme", "Beta"]      # blanks dropped, markup stripped
    assert out["is_agency"] is True and out["recruiter_confidence"] == 0.9


def test_clean_recruiter_requires_name():
    assert _clean_recruiter(RecruiterContact(name="   ", email="x@y.com")) is None


def test_extract_recruiter_best_effort_never_blocks():
    # success path (direct node call pops one llm response — the recruiter card)
    rc = RecruiterContact(name="Jane Smith", employer="Best Recruiting", is_agency=True)
    nodes, *_ = make_nodes([rc], [])
    assert nodes.extract_recruiter({"email": email()})["recruiter"]["name"] == "Jane Smith"
    # failure path: any extraction error yields no card, does not raise
    def boom(*_):
        raise RuntimeError("rate limited")
    nodes2, *_ = make_nodes([boom], [])
    assert nodes2.extract_recruiter({"email": email()}) == {"recruiter": None}


def test_recruiter_outreach_attaches_card_both_phases():
    cls = Classification(category=Category.recruiter_outreach, confidence=0.95, reasoning="x",
                         postings=[Posting(company="Acme", role="Staff Eng", link="https://acme/job")])
    rc = RecruiterContact(name="Jane Smith", email="jane@agency.com", employer="Best Recruiting",
                          is_agency=True, represents=["Acme"])
    nodes, writer, _ = make_nodes([cls, rc], [Critique(valid=True)])
    app = build_graph(nodes)
    final = app.invoke({"email": email(), "attempts": 0})
    assert final["outcome"] == "processed" and final["destination"] == "interaction"
    payload = writer.inbox_entries[0]
    assert payload["recruiter"]["name"] == "Jane Smith"                       # Phase 2 (typed)
    assert payload["raw_extracted_json"]["recruiter_contact"]["name"] == "Jane Smith"  # Phase 1


def test_write_postings_dedup_and_truncation():
    postings = [Posting(company=f"Co{i}", role="Eng", link="https://x") for i in range(35)]
    postings[0] = Posting(company="Acme", role="FDE", link="https://acme")
    cls = Classification(category=Category.job_alert, confidence=0.95, reasoning="x", postings=postings)
    reviews = [{"review_id": "r1", "company": "Acme", "title": "FDE", "status": "applied", "url": "u"}]
    nodes, writer, _ = make_nodes([], [], reviews=reviews)
    out = nodes.write_postings({"email": email(), "classification": cls})
    entry = writer.inbox_entries[0]
    assert len(entry["postings"]) == 30                      # truncated [D11]
    assert entry["truncated"] is True
    assert entry["postings"][0]["possible_duplicate"] is True
    assert entry["postings"][0]["matched_review_id"] == "r1"
    assert out["destination"] == "postings"


def test_write_interaction_unique_match_updates_status():
    cls = Classification(
        category=Category.application_confirmation, confidence=0.9, reasoning="x",
        interaction=InteractionSignal(company="Acme", role="FDE",
                                      new_status=WritableStatus.interviewing, summary="call set"),
    )
    reviews = [{"review_id": "r1", "company": "Acme Corp", "title": "Forward Deployed Engineer",
                "status": "applied", "url": "u"}]
    nodes, writer, _ = make_nodes([], [], reviews=reviews)
    out = nodes.write_interaction({"email": email(), "classification": cls})
    assert out["outcome"] == "processed"
    assert out["destination"] == "interaction"
    assert writer.interactions[0]["matched_review_id"] == "r1"
    assert writer.interactions[0]["new_status"] == "interviewing"


def test_write_interaction_ambiguous_triggers_hitl():
    cls = Classification(
        category=Category.application_confirmation, confidence=0.9, reasoning="x",
        interaction=InteractionSignal(company="Acme", role="Engineer", new_status=None, summary="update"),
    )
    reviews = [
        {"review_id": "r1", "company": "Acme", "title": "Engineer", "status": "applied", "url": "u"},
        {"review_id": "r2", "company": "Acme", "title": "Engineer", "status": "applied", "url": "u"},
    ]
    nodes, writer, _ = make_nodes([], [], reviews=reviews)
    out = nodes.write_interaction({"email": email(), "classification": cls})
    assert out["outcome"] == "awaiting_hitl"
    assert out["destination"] is None              # no move while paused
    assert writer.hitl and len(writer.hitl[0]["candidates"]) == 2


def test_write_interaction_no_match_needs_review():
    cls = Classification(
        category=Category.application_confirmation, confidence=0.9, reasoning="x",
        interaction=InteractionSignal(company="Unknown Co", role="X", new_status=WritableStatus.rejected),
    )
    nodes, writer, _ = make_nodes([], [], reviews=[
        {"review_id": "r1", "company": "Acme", "title": "FDE", "status": "applied", "url": "u"}])
    out = nodes.write_interaction({"email": email(), "classification": cls})
    assert out["outcome"] == "needs_review"
    # unmatched interactions stay VISIBLE in the Interaction folder (not hidden in Unprocessed)
    assert out["destination"] == "interaction"


def test_finalize_moves_email():
    nodes, _, reader = make_nodes([], [])
    nodes.finalize({"email": email(), "destination": "postings"})
    assert reader.moves == [("<m1@x>", "postings")]
    assert reader.mark_reads == [True]              # non-interaction → marked read


def test_finalize_leaves_interaction_unread():
    nodes, _, reader = make_nodes([], [])
    nodes.finalize({"email": email(), "destination": "interaction"})
    assert reader.moves == [("<m1@x>", "interaction")]
    assert reader.mark_reads == [False]             # interaction → left UNREAD


# ── full graph runs ───────────────────────────────────────────

def test_graph_happy_path_job_alert():
    cls = Classification(category=Category.job_alert, confidence=0.95, reasoning="clear digest",
                         postings=[Posting(company="Acme", role="FDE", link="https://acme")])
    nodes, writer, reader = make_nodes([cls], [Critique(valid=True)])
    app = build_graph(nodes)
    final = app.invoke({"email": email(), "attempts": 0})
    assert final["outcome"] == "processed"
    assert reader.moves == [("<m1@x>", "postings")]
    assert len(writer.inbox_entries) == 1


def test_graph_retry_then_success():
    bad = Classification(category=Category.job_alert, confidence=0.4, reasoning="unsure")
    good = Classification(category=Category.job_alert, confidence=0.9, reasoning="clear",
                          postings=[Posting(company="Acme", role="FDE")])
    nodes, writer, reader = make_nodes(
        [bad, good], [Critique(valid=False, issues=["confidence too low"]), Critique(valid=True)]
    )
    app = build_graph(nodes)
    final = app.invoke({"email": email(), "attempts": 0})
    assert final["attempts"] == 2
    assert final["outcome"] == "processed"


def test_graph_exhaustion_escalates():
    bad = Classification(category=Category.job_alert, confidence=0.3, reasoning="unsure")
    nodes, writer, reader = make_nodes(
        [bad, bad], [Critique(valid=False, issues=["bad"]), Critique(valid=False, issues=["still bad"])]
    )
    app = build_graph(nodes)
    final = app.invoke({"email": email(), "attempts": 0})
    assert final["outcome"] == "needs_review"
    assert reader.moves == [("<m1@x>", "unprocessed")]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
