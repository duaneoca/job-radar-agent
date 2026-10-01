"""
Top-level runner — one pass: [sort the root folder] → the unread Postings folder (V2).

Acquire lock → preflight → optional SORT stage (agent/sort_stage.py: Jev routes root-folder mail into
Interaction / Postings / Social / Unprocessed) → fetch unread Postings (newest-first; age + count caps
applied by the reader) → run the per-email graph with per-email error isolation → optional RETENTION
sweep (agent/retention.py: expired Social/Postings mail → Trash) → report a run record (ALWAYS, even on
crash). Sorting first means mail it files into Postings is picked up in the same run.

Dry-run wraps the reader, writer and duplicate store so NOTHING changes — no mail marked or moved, no
Job Radar writes, no duplicate keys recorded — while the LLM pick + verification still run.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from .dedup import DedupStore, NullDedupStore, ReadOnlyDedupStore
from .graph import build_graph
from .llm import LLMClient
from .lock import LockHeld, run_lock
from .nodes import Nodes
from .observability import email_trace, get_langfuse
from .prompts import PromptProvider
from .reader import EmailReaderClient
from .senders import SenderPolicy
from .state import Destination
from .writer import JobRadarWriter


@dataclass
class RunResult:
    status: str = "success"                 # success | partial | failed
    emails_processed: int = 0
    postings_created: int = 0
    duplicates_skipped: int = 0
    escalations: int = 0                    # emails moved to Unprocessed
    retries: int = 0                        # extra pick attempts beyond the first
    interactions_recorded: int = 0          # Interaction mail written to the inbox by the sorter
    sorted: dict = field(default_factory=dict)          # sorter: destination folder → count
    sort_details: list[dict] = field(default_factory=list)
    trashed: dict = field(default_factory=dict)          # retention: logical folder → count
    errors: list[str] = field(default_factory=list)
    skipped: bool = False                   # lock held
    details: list[dict] = field(default_factory=list)

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


class _CachedReviews:
    """Serves the reviews fetched once at run start (instead of one API call per email)."""

    def __init__(self, inner: JobRadarWriter, reviews: list):
        self._inner = inner
        self._reviews = reviews

    def get_reviews(self):
        return list(self._reviews)

    def create_inbox_entry(self, payload):
        return self._inner.create_inbox_entry(payload)

    def report_run(self, record):
        return self._inner.report_run(record)


class _NoMoveReader:
    """Dry-run reader: reads for real, records intended changes instead of making them."""

    def __init__(self, inner: EmailReaderClient):
        self._inner = inner
        self.intended: list[tuple[str, str]] = []

    def get_unread(self):
        return self._inner.get_unread()

    def mark_read(self, message_id: str) -> None:
        self.intended.append((message_id, "mark_read"))

    def move_and_mark(self, message_id: str, destination: Destination,
                      mark_read: bool = True) -> None:
        self.intended.append((message_id, destination))


class _NoWriteWriter:
    """Dry-run writer: serves real reviews (for duplicate flags) but swallows all writes."""

    def __init__(self, inner: JobRadarWriter):
        self._inner = inner
        self.intended: list[dict] = []

    def get_reviews(self):
        return self._inner.get_reviews()

    def create_inbox_entry(self, payload):
        self.intended.append(payload)
        return {"inbox_email_id": "dry", "posting_ids": []}

    def report_run(self, record):
        return {"run_id": "dry"}


def run_once(
    *,
    reader: EmailReaderClient,
    writer: JobRadarWriter,
    llm: LLMClient,
    prompts: PromptProvider,
    policy: SenderPolicy | None = None,
    dedup: DedupStore | None = None,
    zero_postings_action: str = "mark_read",
    notifier=None,
    inbox_base_url: str | None = None,
    environment: str = "local",
    agent_version: str = "2.0.0",
    dry_run: bool = False,
    use_lock: bool = True,
    lock_path: str | None = None,
    spend_key: str = "local",
    daily_ceiling: float | None = None,
    spend_store=None,
    sort_stage=None,
    retention_stage=None,
) -> RunResult:
    from notifications import dispatch as _dispatch
    from notifications.base import NullNotifier
    notifier = notifier or NullNotifier()
    dedup = dedup or NullDedupStore()
    started = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    lf = get_langfuse()

    jev = getattr(sort_stage, "jev", None)

    def _run_cost() -> float:
        return getattr(llm, "run_cost", 0.0) + getattr(jev, "run_cost", 0.0)

    def _go() -> RunResult:
        result = RunResult()
        # Preflight: one tracked-jobs fetch per run. If Job Radar refuses (agent disabled in the
        # user's settings, revoked key, outage), stop BEFORE reading mail or paying for LLM calls.
        try:
            reviews = writer.get_reviews()
        except Exception as exc:
            result.status = "failed"
            result.errors.append(f"Job Radar preflight failed — nothing processed: "
                                 f"{type(exc).__name__}: {exc}")
            return result
        cached = _CachedReviews(writer, reviews)
        nodes = Nodes(
            llm=llm,
            writer=_NoWriteWriter(cached) if dry_run else cached,
            reader=_NoMoveReader(reader) if dry_run else reader,
            prompts=prompts, policy=policy,
            dedup=ReadOnlyDedupStore(dedup) if dry_run else dedup,
            zero_postings_action=zero_postings_action,
        )
        app = build_graph(nodes)

        # H4 daily spend ceiling — refuse the run if already over, then enforce per email.
        # ceiling <= 0 means DISABLED (a misconfig must not silently skip everything).
        enforce_budget = bool(daily_ceiling and daily_ceiling > 0 and spend_store is not None)
        already = spend_store.spent_today(spend_key) if enforce_budget else 0.0
        if enforce_budget and already >= daily_ceiling:
            result.status = "partial"
            result.errors.append(f"daily spend ceiling reached (${already:.2f} ≥ ${daily_ceiling:.2f}) — skipped")
            return result
        if hasattr(llm, "reset_cost"):
            llm.reset_cost()
        if hasattr(jev, "reset_cost"):
            jev.reset_cost()

        if sort_stage is not None:
            s = sort_stage.run(cached, dry_run=dry_run, over_budget=lambda: bool(
                enforce_budget and already + _run_cost() >= daily_ceiling))
            result.sorted = dict(s.moved)
            result.sort_details = s.details
            result.interactions_recorded = s.inbox_written
            result.escalations += s.moved.get("unprocessed", 0)
            if s.gone:
                result.sorted["gone"] = s.gone
            if s.errors or s.halted:
                result.status = "partial"
                result.errors.extend(s.errors + ([s.halted] if s.halted else []))

        for email in reader.get_unread():
            if enforce_budget and already + _run_cost() >= daily_ceiling:
                result.status = "partial"
                result.errors.append(f"daily spend ceiling reached mid-run (${already + _run_cost():.2f}) — halting")
                break
            try:
                with email_trace(lf, message_id=email.get("message_id", "?"),
                                 subject=email.get("subject", "")) as span:
                    final = app.invoke({"email": email, "langfuse_trace_id": span.trace_id})
                    span.update(output={
                        "outcome": final.get("outcome"), "destination": final.get("destination"),
                        "attempts": final.get("attempts", 0),
                        "postings_written": final.get("postings_written", 0),
                        "reason": final.get("reason"),
                    })
            except Exception as exc:  # one poison email must not abort the whole run [L3]
                # The email is left untouched (still unread) and is retried next run.
                result.status = "partial"
                result.errors.append(f'{email.get("message_id", "?")}: {type(exc).__name__}: {exc}')
                continue

            result.emails_processed += 1
            result.retries += max(0, final.get("attempts", 0) - 1)
            result.postings_created += final.get("postings_written", 0)
            result.duplicates_skipped += final.get("duplicates_skipped", 0)
            if final.get("destination") == "unprocessed":
                result.escalations += 1
            result.details.append({
                "message_id": email.get("message_id"),
                "subject": (email.get("subject") or "")[:60],
                "outcome": final.get("outcome"),
                "postings": final.get("postings_written", 0),
                "duplicates": final.get("duplicates_skipped", 0),
                "attempts": final.get("attempts", 0),
                "reason": final.get("reason"),
            })

        if retention_stage is not None:
            r = retention_stage.run(dry_run=dry_run)
            result.trashed = r.trashed
            if r.errors:
                result.status = "partial"
                result.errors.extend(r.errors)

        if enforce_budget and not dry_run:
            try:
                spend_store.add(spend_key, _run_cost())
            except Exception as exc:
                result.errors.append(f"spend persist failed: {exc}")
        try:
            _dispatch.run_summary(notifier, result=result)
        except Exception as exc:
            result.errors.append(f"run_summary notify failed: {exc}")
        return result

    result: RunResult | None = None
    try:
        if use_lock:
            if lock_path is None:
                from .paths import data_dir
                lock_path = str(data_dir() / "agent.lock")
            with run_lock(lock_path):
                result = _go()
        else:
            result = _go()
    except LockHeld:
        result = RunResult(skipped=True)
    except Exception as exc:
        result = RunResult(status="failed", errors=[f"{type(exc).__name__}: {exc}"])
    finally:
        # ALWAYS finalize so a run never dangles — including SIGTERM (SystemExit) mid-run.
        if result is None:
            result = RunResult(status="failed", errors=["interrupted before completion"])
        if not result.skipped and not dry_run:
            finished = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
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
