# Design note — spooling run records when Job Radar is unreachable

**Status:** design approved 2026-11-17. Local spool implementation pending; server side assigned
to Job Radar. The decisions below are settled, not open.
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
3. **Bound it, both ways.** A record older than **7 days** is dropped (a month-old run record has
   little value), AND the spool is capped at **500 records**, oldest dropped first. Whichever bound
   bites, the number of records dropped is reported in the next successful record, so the loss is
   itself visible rather than silent.
4. **Never let spooling fail a run.** Every spool read/write wrapped so that a full disk or a
   read-only volume degrades to today's behaviour — a lost record — rather than turning a network
   failure into a crash.

Records carry `started_at` / `finished_at` already, so a flushed record is still accurate about when
it happened; it is late, not wrong. The server must therefore accept a record whose timestamps are
in the past, which it does today.

A flushed record additionally carries **`late: true`**, a new optional boolean on
`POST /agent/runs` (absent means `false`). It lets the dashboard distinguish "reported on time" from
"backfilled after an outage" without inferring it from clock skew. Added to `INTEGRATION_SPEC.md`
alongside the implementation.

## What the cloud needs that local does not

This was the part needing a decision rather than implementation; it is now decided — cloud does
not spool.

- **The spool is per user, but the disk is per pod.** A CronJob pod is ephemeral and shared across
  users, so a pod-local file is both the wrong lifetime and a cross-user data boundary — one user's
  run records sitting in a path another user's iteration can read. Either the spool is keyed by
  `user_id` and stored somewhere durable per user, or cloud skips spooling and solves visibility
  server-side instead. **Decided: cloud skips spooling.**
- **Server-side is the answer for cloud.** If the agent cannot report, the server
  can still notice: `agent_runs` has a last-seen timestamp per user, so job-radar can infer
  "unreachable" from absence without the agent telling it anything. That requires no new agent state
  and no cross-user file, and it also covers the case where the agent is not running at all — which
  spooling cannot, since a dead agent spools nothing.
- Consequence: **spool for local, infer for cloud.** The same user-visible outcome by two
  mechanisms, which the dual-design table in `INTEGRATION_SPEC.md` §3.6 already has a shape for.

## How the dashboard should show it

- A per-user state derived from the newest `agent_runs` row: healthy / **unreachable since `<ts>`** /
  never connected.
- Threshold derived from the user's own cadence rather than a constant: the server takes the
  **median interval of the last ~20 `agent_runs` rows** and flags unreachable after **3 missed
  intervals**, with a **floor of 1 hour** so a jittery or very frequent schedule cannot raise a
  false alarm. Cloud uses its CronJob interval directly, which it already knows. A 15-minute local
  schedule and a daily cloud schedule must not share a timeout, and neither needs a user setting.
- A late-arriving flushed record backfills the history, so the gap closes retroactively and the
  outage remains visible as a period rather than vanishing.
- Not a notification. The agent already notifies the admin tier on failure; the dashboard's job here
  is the honest resting state, and an outage that has already ended should not page anyone.

## Decisions (settled on review, 2026-11-17)

1. **Spool for local, infer for cloud.** Cloud does not spool, for the reasons above — the pod disk
   is the wrong lifetime and a cross-user boundary.
2. **Both bounds:** drop older than 7 days and cap at 500, oldest first, with the drop count carried
   in the next successful record.
3. **`late: true`** on a flushed record; an optional field on `POST /agent/runs`, absent = `false`.
4. **Unreachable thresholds are fixed by schedule and operator-only** — no per-user setting for now.
   The server has no reliable knowledge of a local agent's schedule, so it **infers the cadence from
   the user's own recent history**: the median interval of the last ~20 `agent_runs` rows, flagging
   unreachable after 3 missed intervals, with a floor of 1 hour so a jittery or very frequent
   schedule cannot produce a false alarm. Cloud uses the CronJob interval directly, which it knows.

   This supersedes the "tied to the user's own schedule" sketch above: the mechanism is a median of
   observed intervals, not a configured value.

## Split of work

- **This repo (agent):** the local spool — write on `report_run` failure, flush oldest-first,
  enforce both bounds, never fail a run — plus the `late` field in `INTEGRATION_SPEC.md`.
- **Job Radar (server):** accept `late` on `POST /agent/runs`, derive the per-user reachability state
  from `agent_runs`, and render it on the dashboard.

## Not in scope

Retrying the *work*. A failed preflight means nothing was processed, and the mail is still unread,
so the next run picks it up normally. This note is only about not losing the record that a run was
attempted and failed.
