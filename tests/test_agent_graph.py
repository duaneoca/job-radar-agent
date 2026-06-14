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
from agent.schemas import (
    Category,
    Classification,
    Critique,
    InteractionSignal,
    Posting,
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
