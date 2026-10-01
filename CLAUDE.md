# Job Radar Email Agent — CLAUDE.md

Turns job-alert emails into postings in Job Radar's Inbox. **V2 design:** deterministic link
extraction → ONE small LLM call that picks posting links by number → deterministic verification with
a feedback-only retry loop → dedup → write. Standalone **public** portfolio repo.

**Companion repo:** `job-radar` (../job-radar) — the platform. Contract: **`INTEGRATION_SPEC.md`**
(source of truth; change it in the same PR as any contract change). V2 is §3.6.
**V1** (LLM classify + LLM critic over every job email) is archived at tag `v1-final`.

---

## Repo layout

```
agent/
  cli.py            `job-radar-agent {run|cloud|doctor|models|version}`
  extract.py        HTML → numbered link candidates (text, safe URL, bounded nearby text)
  senders.py        sender allow-list + DMARC check (before any LLM spend)
  schemas.py        LinkPicks — the LLM's only output shape
  verify.py         deterministic checks of the picks + corrective feedback
  dedup.py          dedup keys + SqliteDedupStore (local) / NullDedupStore (cloud)
  nodes.py graph.py state.py   the LangGraph pipeline for one email
  runner.py         one pass: preflight → per-email graph → run record (always)
  bootstrap.py loop.py   local wiring + interval loop;  cloud.py   multi-user runner
  config.py         .env settings + per-user cloud-config readers
  seed_prompts/link_picker.md   the one prompt
mcp_email/          MCP Server 1 — Email Reader (stdio) + providers (proton · gmail · imap)
notifications/      notifier abstraction + slack/telegram/discord; run-summary dispatch
scripts/            run_local (dry-run report), run_loop, run_cloud, smoke_*, mint_agent_key, gmail_auth
tests/              synthetic samples only (tests/samples.py) — real emails are never committed
```

`hitl/` is an empty package (V1's human-in-the-loop resume was never built). The Writer MCP
(Server 2) lives in the job-radar repo; this repo writes over REST (`RestWriter`, dual auth:
`X-Agent-Key` locally, `X-Internal-Token` + `X-User-Id` in cloud).

---

## Pipeline (one email)

```
screen ─┬─ sender rejected ─────────────────────────────────→ finalize (→ Unprocessed)
        ├─ no links ─→ no_postings ─────────────────────────→ finalize (mark read | Unprocessed)
        └─ pick → verify ─┬─ ok ─→ write (dedup, POST /agent/inbox) → finalize (mark read)
                          ├─ issues & attempts < 3 ─→ prepare_retry → pick
                          └─ issues after 3 ─→ escalate ─────→ finalize (→ Unprocessed)
```

- **pick** is the only LLM call. Input: subject + numbered links (never the raw email). Output:
  `[{link_id, title, company}]` — never a URL.
- **verify**: link_id exists; title ⊂ link text/nearby text; company ⊂ nearby text and ≠ title.
  Nearby text is bounded by the neighboring links so a company can't be borrowed from another posting.
  Same-job duplicates collapse silently (no retry).
- **Retry feedback references only our own link numbers** — never the model's wrong values.
- **Partial success = failure** → Unprocessed (whole email).
- **finalize** is the single mailbox mutation: mark read in place, or move to Unprocessed.

---

## Key conventions & invariants

- **Never touch the Job Radar DB directly**; never create `Job`/`UserJobReview` rows (the user imports
  from the inbox via the bookmarklet).
- **Mailbox tools: read / mark-read / move-to-Unprocessed only** — no delete/archive (guardrail by
  absence). Reads use `BODY.PEEK[]` so nothing is marked read by reading.
- **Only UNREAD mail in the Postings folder** is processed. Read = the human owns it.
- **Idempotency key = RFC822 `Message-ID`**, scoped `(user_id, message_id)`.
- **Posting cap = 30 per email** (truncate, flag `truncated`).
- **Dedup keys are recorded only after Job Radar accepted the write.**
- **LLM infrastructure errors propagate** (email left unread, retried next run); only parse errors retry
  in-loop.
- **Preflight:** a run fetches tracked jobs once at the start; if Job Radar refuses (agent disabled,
  bad key), nothing is read and nothing is spent.
- **Dual design:** every per-user policy has a local source (`.env`) and a cloud source (`email_policy`
  in the cloud config); local dedup is SQLite, cloud dedup is Job Radar's DB (§3.6).
- **Credential routing [H6a]:** cloud agents fetch config IN-CLUSTER; local agents use `.env` only.
  Decrypted creds are held in memory only, one user at a time, never logged.
- **Observability:** one Langfuse trace per email (`process_email` span + `pick_links` generations),
  trace id propagated into the inbox write. Counts-only run record to `POST /agent/runs`.

---

## Security obligations (this repo's share — see INTEGRATION_SPEC §5)

- **Prompt injection [C1]:** the model sees only extracted link text/nearby text, fenced in
  `<links>` with angle brackets stripped; its output is a constrained schema, and every value is
  verified against the email. URLs never come from the model.
- **Sender spoofing:** allow-list + topmost trusted `dmarc=pass` for the From domain, before any LLM call.
- **Privacy [H2]:** subject + link texts/nearby text go to the LLM provider and Langfuse (much less than
  V1's full bodies). Document, don't claim "never leaves."
- **Cost/DoS [H4]:** per-run email cap, one LLM call per attempt (max 3), daily spend ceiling enforced
  on every entry point, cloud circuit breaker + total-email budget.
- **No link dereferencing [M2]**, **no attachment parsing / no remote fetch [M1]** — tested.
- **Minimal Gmail scope [H5]**; code only calls `messages.modify`.
- **Public-repo hygiene [M3]:** `.env.example` only; synthetic test data only.
- **Local state [M4]:** `.env` perms 600; `dedup.sqlite` 600; lockfile under `AGENT_HOME/data`.

---

## Deployment

- **Local (Proton):** pipx-installed CLI + launchd every 15 min (`docs/DEPLOYMENT.md`). Proton Bridge
  runs on the same machine.
- **Cloud (Gmail/IMAP, multi-user):** one GHCR image; k8s CronJob runs `job-radar-agent cloud`.
- **Overlap guard:** PID lockfile (stale locks self-reclaim).

## Status

V2 built and verified on real mail (dry run, 10 emails: 10/10 verified first try, $0.029 total,
18 s). ~150 offline tests. Open items: Job Radar Phase A/B for `dedup_key` + `email_policy` UI
(§3.6); cloud path unit-tested only; the MCP SDK is pinned `<2` pending a deliberate migration.
