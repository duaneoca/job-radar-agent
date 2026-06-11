"""
Golden-set fixtures: validate shape, and run each through the real graph to assert correct routing.

The synthetic fixtures (PII-free, modeled on real mail) serve double duty:
  1. human-readable examples for prompt design (A-4),
  2. executable assertions that a correct classification flows to the right folder/outcome.

`expected` describes the ground-truth classification; here we synthesize that Classification and
verify the pipeline behaves. When the REAL LLM client lands, `test_llm_eval` (skipped by default)
will instead feed `email.body_text` through the live Classifier and compare predicted vs expected.
"""

import os

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
from agent.writer import FakeWriter
from tests.fixtures import load_synthetic

FIXTURES = load_synthetic()
PROMPTS = SeedPromptProvider()


def _ids(fx):
    return fx["name"]


def _classification_from_expected(exp: dict) -> Classification:
    postings = [Posting(company=p["company"], role=p["role"]) for p in exp.get("postings", [])]
    interaction = None
    if "interaction" in exp:
        i = exp["interaction"]
        interaction = InteractionSignal(
            company=i["company"], role=i.get("role"),
            new_status=WritableStatus(i["new_status"]) if i.get("new_status") else None,
            summary=i.get("summary", ""),
        )
    return Classification(
        category=Category(exp["category"]), confidence=0.9, reasoning="fixture",
        postings=postings, interaction=interaction,
    )


@pytest.mark.parametrize("fx", FIXTURES, ids=[f["name"] for f in FIXTURES])
def test_fixture_wellformed(fx):
    assert {"name", "email", "expected"} <= fx.keys()
    e = fx["email"]
    assert {"message_id", "subject", "sender", "body_text"} <= e.keys()
    Category(fx["expected"]["category"])  # raises if not a valid category
    assert fx["expected"]["destination"] in {"interaction", "postings", "social", "unprocessed"}


@pytest.mark.parametrize("fx", FIXTURES, ids=[f["name"] for f in FIXTURES])
def test_fixture_routes_correctly(fx):
    exp = fx["expected"]
    e = fx["email"]
    email_ref = {
        "message_id": e["message_id"], "subject": e["subject"], "sender": e["sender"],
        "received_at": e.get("received_at"), "body_text": e["body_text"],
        "has_attachments": e.get("has_attachments", False),
    }
    nodes = Nodes(
        llm=FakeLLM([_classification_from_expected(exp)]),
        critic_llm=FakeLLM([Critique(valid=True)]),
        writer=FakeWriter(reviews=exp.get("reviews", [])),
        reader=FakeReader(),
        prompts=PROMPTS,
    )
    app = build_graph(nodes)
    final = app.invoke({"email": email_ref, "attempts": 0})

    assert final["outcome"] == exp["outcome"]
    if exp.get("destination"):
        assert nodes.reader.moves[-1] == (e["message_id"], exp["destination"])

    if exp["category"] in ("job_alert", "recruiter_outreach"):
        assert nodes.writer.inbox_entries, "expected an inbox entry with postings"
    elif exp["category"] == "application_confirmation":
        rec = nodes.writer.interactions[-1]
        want = exp["interaction"].get("new_status")
        assert rec["new_status"] == want


@pytest.mark.skipif(
    not os.environ.get("RUN_LLM_EVAL"),
    reason="live LLM eval — set RUN_LLM_EVAL=1 and provider creds to run (real classifier vs expected)",
)
def test_llm_eval():  # pragma: no cover - opt-in, needs real LLM client (A-4)
    pytest.skip("real LLM client not wired yet (A-4)")
