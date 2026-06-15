"""
Observability seam tests — verify graceful no-op when Langfuse isn't configured, and that the
generation handle records output/usage without raising. No Langfuse account needed.
"""

import agent.observability as obs
from agent.llm import FakeLLM
from agent.nodes import Nodes
from agent.prompts import SeedPromptProvider
from agent.reader import FakeReader
from agent.runner import run_once
from agent.schemas import Category, Classification, Critique, Posting
from agent.writer import FakeWriter


def test_get_langfuse_none_without_env(monkeypatch):
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    obs._RESOLVED = False  # reset memoization for the test
    obs._CLIENT = None
    assert obs.get_langfuse() is None


def test_email_trace_and_generation_noop_when_lf_none():
    with obs.email_trace(None, message_id="<x>", subject="s") as span:
        assert span.trace_id is None
        span.update(output={"a": 1})            # must not raise
    with obs.generation(None, name="classify", model="m", messages=[]) as gen:
        gen.finish(output="{}", usage={"input": 1, "output": 2})  # must not raise


def test_runner_works_with_tracing_disabled(monkeypatch):
    # With no Langfuse env, a full run still works and trace_id flows through as None.
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    obs._RESOLVED = False
    obs._CLIENT = None
    reader = FakeReader([{"message_id": "<1>", "subject": "s", "sender": "a@b.com",
                          "received_at": None, "body_text": "b", "has_attachments": False}])
    writer = FakeWriter()
    res = run_once(
        reader=reader, writer=writer, prompts=SeedPromptProvider(), use_lock=False,
        llm=FakeLLM([Classification(category=Category.job_alert, confidence=0.95, reasoning="x",
                                    postings=[Posting(company="C", role="R")])]),
        critic_llm=FakeLLM([Critique(valid=True)]),
    )
    assert res.emails_processed == 1
    # trace_id propagated into the write payload (None here, but the field is wired)
    assert "langfuse_trace_id" in writer.inbox_entries[0]
