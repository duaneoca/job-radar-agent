"""
Run-record spool — don't lose the record of a run that could not be reported.

When Job Radar is unreachable, `report_run` fails for exactly the same reason the run itself failed:
it needs the same network. Measured on the local agent, 283 of 298 lifetime failures reported
nothing, so those runs left no `agent_runs` row and the dashboard showed silence during an outage —
indistinguishable from "no mail arrived". See docs/run-record-spool.md.

The spool holds unreported records as newline-delimited JSON and is flushed oldest-first by the next
run that reaches the server. A flushed record carries `late: true` so the dashboard can tell a
backfill from an on-time report.

LOCAL ONLY, by design. Cloud does not spool: a CronJob pod's disk is the wrong lifetime for per-user
state and a cross-user boundary besides, so cloud visibility is inferred server-side from the absence
of recent `agent_runs` rows — which also covers an agent that is not running at all, something
spooling can never do, since a dead agent spools nothing.

Nothing here may turn a network failure into a crash: every operation is best-effort and a full disk
or read-only volume degrades to today's behaviour, a lost record.
"""

from __future__ import annotations

import datetime
import json
import os
import threading

MAX_RECORDS = 500          # cap, oldest dropped first
MAX_AGE_DAYS = 7           # a month-old run record has little value


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _age_days(record: dict) -> float | None:
    """Age from the record's own `started_at`; None when it is missing or unparseable."""
    raw = record.get("started_at")
    if not isinstance(raw, str) or not raw:
        return None
    try:
        ts = datetime.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=datetime.timezone.utc)
    return (_now() - ts).total_seconds() / 86400.0


class RunSpool:
    """Append-only queue of run records that could not be reported."""

    def __init__(self, path: str | None = None):
        if path is None:                       # resolved lazily, never at import
            from .paths import data_dir
            path = str(data_dir() / "pending_runs.jsonl")
        self._path = path
        # Dropped-record count, carried across runs so the loss can be reported in the next
        # record that actually reaches the server. A sidecar rather than a field in the spool
        # file, which holds records only.
        self._dropped_path = f"{path}.dropped"
        self._lock = threading.Lock()

    # ── reading ───────────────────────────────────────────────

    def _read(self) -> list[dict]:
        """Every well-formed record, oldest first. A corrupt line is skipped, not fatal: a
        half-written line from a killed process must not block every record behind it."""
        try:
            with open(self._path, encoding="utf-8") as f:
                lines = f.read().splitlines()
        except (FileNotFoundError, OSError):
            return []
        out = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if isinstance(rec, dict):
                out.append(rec)
        return out

    def _write(self, records: list[dict]) -> None:
        os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
        tmp = f"{self._path}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            for rec in records:
                f.write(json.dumps(rec) + "\n")
        os.replace(tmp, self._path)

    def pending(self) -> list[dict]:
        """The spooled records, oldest first. Read-only; does not prune."""
        return self._read()

    # ── dropped-record accounting ─────────────────────────────

    def _bump_dropped(self, n: int) -> None:
        if n <= 0:
            return
        try:
            os.makedirs(os.path.dirname(self._dropped_path) or ".", exist_ok=True)
            with open(self._dropped_path, "w", encoding="utf-8") as f:
                f.write(str(self._read_dropped() + n))
        except OSError:
            pass            # the count is diagnostic; losing it must not break a run

    def _read_dropped(self) -> int:
        try:
            with open(self._dropped_path, encoding="utf-8") as f:
                return max(0, int(f.read().strip() or 0))
        except (FileNotFoundError, OSError, ValueError):
            return 0

    def take_dropped(self) -> int:
        """How many records the bounds have discarded since this was last called; resets to 0.

        The caller folds this into the next run record it reports, so a silent loss becomes a
        stated one.
        """
        try:
            n = self._read_dropped()
            if n:
                os.unlink(self._dropped_path)
            return n
        except OSError:
            return 0

    # ── writing ───────────────────────────────────────────────

    def add(self, record: dict) -> int:
        """Spool one unreported record. Returns how many records the bounds dropped.

        Best-effort: an unwritable spool returns 0 and loses the record, which is exactly what
        happens today — it must never raise into a run that is already failing.
        """
        try:
            with self._lock:
                records, dropped = self._bounded(self._read() + [dict(record)])
                self._write(records)
            self._bump_dropped(dropped)
            return dropped
        except Exception:
            return 0

    def _bounded(self, records: list[dict]) -> tuple[list[dict], int]:
        """Apply both bounds: drop anything older than MAX_AGE_DAYS, then cap at MAX_RECORDS
        keeping the newest. A record with no usable `started_at` is kept — unknown age is not
        evidence of staleness, and the cap still bounds the file."""
        fresh = [r for r in records
                 if (_age_days(r) or 0.0) <= MAX_AGE_DAYS]
        dropped = len(records) - len(fresh)
        if len(fresh) > MAX_RECORDS:
            dropped += len(fresh) - MAX_RECORDS
            fresh = fresh[-MAX_RECORDS:]        # keep the newest
        return fresh, dropped

    # ── flushing ──────────────────────────────────────────────

    def flush(self, report) -> tuple[int, int]:
        """Report spooled records oldest-first via `report(record)`. Returns (sent, remaining).

        Stops at the first failure and keeps that record and everything after it, so a server that
        is still unreachable costs one attempt rather than one per record. Each record goes out with
        `late: true`.
        """
        try:
            with self._lock:
                records, dropped = self._bounded(self._read())
                sent = 0
                for rec in records:
                    try:
                        report({**rec, "late": True})
                    except Exception:
                        break
                    sent += 1
                remaining = records[sent:]
                if sent or dropped:
                    self._write(remaining)
                if dropped:
                    self._bump_dropped(dropped)
                return sent, len(remaining)
        except Exception:
            return 0, 0
