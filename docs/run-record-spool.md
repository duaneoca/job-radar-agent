# Design note — spooling run records when Job Radar is unreachable

**Status:** proposal, not implemented. Design approval needed before any code.
**Date:** 2026-11-17
**Problem owner:** the user looking at their dashboard during an outage.

## The problem, measured

The local agent has 298 failed runs in its lifetime log. Their reasons are *not* opaque — every
failure in the current log format prints its cause on the following line, and 291 of the 298 say the
same thing: `Job Radar preflight failed — nothing processed`, from transient network faults
(`SSL: UNEXPECTED_EOF_WHILE_READING`, `[Errno 8] nodename nor servname provided`, `ReadTimeout`).

The real gap is one step further on. In **283 of those 298 runs, `report_run` also failed** — and it
could not have succeeded, because reporting the run needs the same network connection that had just
failed in preflight:

```
[11:00:38] failed emails=0 postings=0 duplicates=0 unprocessed=0 retries=0
    ! Job Radar preflight failed — nothing processed: ConnectError: [Errno 8] nodename nor servname …
    ! report_run failed: [Errno 8] nodename nor servname provided, or not known
```

`agent/runner.py` finalises in a `finally` so a run never dangles, which is right — but when the
write itself fails, the record is lost. The run leaves no row in `agent_runs`. From the server's
point of view those 283 runs **never happened**.

The user-visible consequence: during an outage the dashboard shows silence, which is
indistinguishable from "no mail arrived". The agent cannot be seen to be failing. It is precisely
the moment a status page exists for, and it is blank.

### Why this matters more in cloud than locally

A local user can read `~/Library/Logs/jobradar-emailagent.log`. **A cloud user cannot.** The pod's
stdout is not theirs to read, so for a cloud user a failed run that never reached the DB is not
merely unexplained, it is invisible — there is no second source of truth. The dashboard is the only
surface they have, and it is the one that goes quiet.

## Proposed approach — spool and flush

1. **On `report_run` failure, write the record to a local spool** instead of discarding it, as
   newline-delimited JSON in `<AGENT_HOME>/data/pending_runs.jsonl`. The record is already built by
   `RunResult.as_run_record()`, so nothing new has to be computed.
2. **At the start of the next run that reaches the server, flush the spool**: POST each record
   oldest-first, dropping each one that is accepted. A run that still cannot reach the server leaves
   the spool untouched and simply appends to it.
3. **Bound it.** Cap the spool (suggest 500 records, oldest dropped first) so a long outage cannot
   grow a file without limit, and record the drop count in the next successful record so the loss is
   itself reported rather than silent.
4. **Never let spooling fail a run.** Every spool read/write wrapped so that a full disk or a
   read-only volume degrades to today's behaviour — a lost record — rather than turning a network
   failure into a crash.

Records carry `started_at` / `finished_at` already, so a flushed record is still accurate about when
it happened; it is late, not wrong. The server must therefore accept a record whose timestamps are
in the past, which it does today.

## What the cloud needs that local does not

This is the part needing a decision, not just implementation:

- **The spool is per user, but the disk is per pod.** A CronJob pod is ephemeral and shared across
  users, so a pod-local file is both the wrong lifetime and a cross-user data boundary — one user's
  run records sitting in a path another user's iteration can read. Either the spool is keyed by
  `user_id` and stored somewhere durable per user, or cloud skips spooling and solves visibility
  server-side instead.
- **Server-side is probably the better answer for cloud.** If the agent cannot report, the server
  can still notice: `agent_runs` has a last-seen timestamp per user, so job-radar can infer
  "unreachable" from absence without the agent telling it anything. That requires no new agent state
  and no cross-user file, and it also covers the case where the agent is not running at all — which
  spooling cannot, since a dead agent spools nothing.
- Consequence: **spool for local, infer for cloud.** The same user-visible outcome by two
  mechanisms, which the dual-design table in `INTEGRATION_SPEC.md` §3.6 already has a shape for.

## How the dashboard should show it

- A per-user state derived from the newest `agent_runs` row: healthy / **unreachable since `<ts>`** /
  never connected.
- Threshold tied to the user's own schedule rather than a constant: unreachable once more than, say,
  three expected intervals have passed with no row. A 15-minute local schedule and a daily cloud
  schedule should not share a timeout.
- A late-arriving flushed record backfills the history, so the gap closes retroactively and the
  outage remains visible as a period rather than vanishing.
- Not a notification. The agent already notifies the admin tier on failure; the dashboard's job here
  is the honest resting state, and an outage that has already ended should not page anyone.

## Open questions for review

1. Spool for local only, with cloud visibility inferred server-side — agreed, or spool in both?
2. Is 500 records / oldest-dropped-first the right bound, or should it be time-based (drop anything
   older than 7 days, since a month-old run record has little value)?
3. Does job-radar want a `late: true` marker on a flushed record, so the dashboard can distinguish
   "reported on time" from "backfilled after an outage"?
4. Unreachable thresholds are a per-user setting in principle. Operator-capped, like
   `max_email_age_days` in §3.9, or fixed by schedule?

## Not in scope

Retrying the *work*. A failed preflight means nothing was processed, and the mail is still unread,
so the next run picks it up normally. This note is only about not losing the record that a run was
attempted and failed.
