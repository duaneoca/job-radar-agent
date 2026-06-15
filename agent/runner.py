"""
Top-level runner — one pass over the unread queue.

Orchestrates: acquire lock → fetch unread (newest-first, age/cap applied by the reader) → for each
email, run the graph with per-email error isolation → report a run heartbeat. The graph performs its
own single mutation (move_and_mark in finalize); the runner just drives the fan-out and tallies.

Dry-run: wraps the reader+writer so NOTHING mutates — no moves, no staging writes — but classification
still runs and the intended actions are tallied. Use it for the first live pass over a real mailbox.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from .graph import build_graph
from .lock import LockHeld, run_lock
from .llm import LLMClient
from .nodes import Nodes
from .observability import email_trace, get_langfuse
from .prompts import PromptProvider
from .reader import EmailReaderClient
from .state import Destination
from .writer import JobRadarWriter


@dataclass
class RunResult:
    status: str = "success"                 # success | partial | failed
    emails_processed: int = 0
    postings_created: int = 0
    interactions_recorded: int = 0
    escalations: int = 0
    retries: int = 0
    errors: list[str] = field(default_factory=list)
    skipped: bool = False                   # lock held
    details: list[dict] = field(default_factory=list)   # per-email {message_id, category, outcome, destination}

    def as_run_record(self, environment: str, agent_version: str,
                      started_at: str, finished_at: str) -> dict[str, Any]:
        return {
            "environment": environment, "agent_version": agent_version,
            "status": self.status, "started_at": started_at, "finished_at": finished_at,
            "emails_processed": self.emails_processed, "postings_created": self.postings_created,
            "interactions_recorded": self.interactions_recorded,
            "escalations": self.escalations, "retries": self.retries,
            "error_summary": "; ".join(self.errors)[:2000] or None,
        }


class _NoMoveReader:
    """Dry-run reader: reads for real, swallows moves (logs intent)."""

    def __init__(self, inner: EmailReaderClient):
        self._inner = inner
        self.intended_moves: list[tuple[str, str]] = []

    def get_unread(self):  # type: ignore[override]
        return self._inner.get_unread()

    def move_and_mark(self, message_id: str, destination: Destination) -> None:
        self.intended_moves.append((message_id, destination))


class _NoWriteWriter:
    """Dry-run writer: serves real reviews (for matching) but swallows all writes."""

    def __init__(self, inner: JobRadarWriter):
        self._inner = inner
        self.intended: list[tuple[str, dict]] = []

    def get_reviews(self):
        return self._inner.get_reviews()

    def create_inbox_entry(self, payload):
        self.intended.append(("inbox", payload)); return {"inbox_email_id": "dry", "posting_ids": []}

    def record_interaction(self, payload):
        self.intended.append(("interaction", payload)); return {"interaction_id": "dry"}

    def register_hitl(self, hitl_id, candidates):
        self.intended.append(("hitl", {"hitl_id": hitl_id})); return {"ok": True}

    def report_run(self, record):
        return {"run_id": "dry"}


def run_once(
    *,
    reader: EmailReaderClient,
    writer: JobRadarWriter,
    llm: LLMClient,
    critic_llm: LLMClient,
    prompts: PromptProvider,
    environment: str = "local",
    agent_version: str = "0.1.0",
    dry_run: bool = False,
    use_lock: bool = True,
    lock_path: str = "/tmp/job-radar-agent.lock",
) -> RunResult:
    started = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    lf = get_langfuse()

    def _go() -> RunResult:
        result = RunResult()
        eff_reader = _NoMoveReader(reader) if dry_run else reader
        eff_writer = _NoWriteWriter(writer) if dry_run else writer
        nodes = Nodes(llm=llm, critic_llm=critic_llm, writer=eff_writer,
                      reader=eff_reader, prompts=prompts)
        app = build_graph(nodes)

        for email in reader.get_unread():
            try:
                with email_trace(lf, message_id=email.get("message_id", "?"),
                                 subject=email.get("subject", "")) as span:
                    state = {"email": email, "attempts": 0, "langfuse_trace_id": span.trace_id}
                    final = app.invoke(state)
                    span.update(output={"outcome": final.get("outcome"),
                                        "destination": final.get("destination"),
                                        "category": (final.get("classification").category.value
                                                     if final.get("classification") else None)})
            except Exception as exc:  # one poison email must not abort the whole run [L3]
                result.status = "partial"
                result.errors.append(f'{email.get("message_id","?")}: {type(exc).__name__}: {exc}')
                continue
            result.emails_processed += 1
            result.retries += max(0, final.get("attempts", 1) - 1)
            outcome = final.get("outcome")
            if outcome == "needs_review":
                result.escalations += 1
            c = final.get("classification")
            if c is not None:
                result.postings_created += len(c.postings)
                if c.interaction is not None and outcome == "processed":
                    result.interactions_recorded += 1
            result.details.append({
                "message_id": email.get("message_id"),
                "subject": email.get("subject", "")[:60],
                "category": c.category.value if c is not None else None,
                "confidence": c.confidence if c is not None else None,
                "outcome": outcome,
                "destination": final.get("destination"),
            })
        return result

    try:
        if use_lock:
            with run_lock(lock_path):
                result = _go()
        else:
            result = _go()
    except LockHeld:
        return RunResult(skipped=True)
    except Exception as exc:
        result = RunResult(status="failed", errors=[f"{type(exc).__name__}: {exc}"])

    finished = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    if not dry_run:
        try:
            writer.report_run(result.as_run_record(environment, agent_version, started, finished))
        except Exception as exc:
            result.errors.append(f"report_run failed: {exc}")
    if lf is not None:
        try:
            lf.flush()
        except Exception:
            pass
    return result
