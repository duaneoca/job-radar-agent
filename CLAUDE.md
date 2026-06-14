# Job Radar Email Agent — CLAUDE.md

Agentic email-processing pipeline. Reads job-related email, classifies + extracts structured data
with an LLM, self-validates via a Classifier→Critic loop, escalates ambiguous cases to a human, and
writes results into Job Radar's new Inbox via a custom MCP server. Standalone **public** portfolio
repo demonstrating enterprise agentic patterns (LangGraph, Langfuse, end-to-end MCP, HITL, threat model).

**Companion repo:** `job-radar` (../job-radar) — the platform. Contract between them:
**`INTEGRATION_SPEC.md`** (source of truth; change it in the same PR as any contract change).
**Threat model:** see `SECURITY_REVIEW.md` in the planning folder; obligations are summarized below.

---

## Repo layout

```
agent/              LangGraph graph, nodes, state, scheduler entrypoint
mcp_email/          MCP Server 1 — Email Reader (stdio)
  providers/        base.py (interface) · proton.py (IMAP/Bridge) · gmail.py (API/OAuth)
notifications/      notifier abstraction + slack/telegram/discord (signal/whatsapp/bluebubbles stubs)
hitl/               always-on poller that resumes checkpoints from resolved hitl_decisions
prompts/            SEED prompt files (bootstrap Langfuse ONCE; Langfuse owns them after)
scripts/            langfuse_seed.py, healthcheck, lock helpers
docs/               agent-integration.md (pointer for the job-radar repo)
tests/              fixtures (synthetic only — real emails are gitignored)
```

The Writer MCP (Server 2) does **NOT** live here — it's in `job-radar/services/mcp-writer/`.

**Two writer topologies** (same `JobRadarWriter` protocol):
- **local** — `RestWriter` (HTTPS to `/api/agent/*` + `X-Agent-Key`), creds from local `.env`. BUILT + contract-tested.
- **cloud** — `McpWriter` (MCP streamable-HTTP → `http://jobradar-mcp-writer:8001/mcp`, `X-Agent-Key` header), creds via in-cluster `get_config`. DEFERRED — in-cluster-only, so built/tested at cloud-deploy time (with JR-5). `SLACK_SIGNING_SECRET` is a prerequisite for HITL then.

---

## Architecture (data flow)

```
Email source (Proton Bridge IMAP local | Gmail API cloud)
  → MCP Server 1 (Email Reader, stdio)         [this repo]
  → LangGraph agent  ⇄ Langfuse (traces+prompts) [this repo]
  → MCP Server 2 (Job Radar Writer, HTTPS+key)  [job-radar repo]
  → tracker-api /agent/* → Postgres + Inbox UI + ops dashboard
Notifications → Slack/Telegram/Discord. HITL → Slack buttons + pull-model resume.
```

### LangGraph graph (core loop)
`fetch_unread → (per email) classify → critic → [valid?]` — invalid & attempts<2 loops back to
`classify` with the Critic's `issues` as feedback (max 2 retries, hard-coded). On exhaustion →
`Unprocessed` folder + `needs_review`. Valid → `[needs match?] → match_job → route_by_category →
write_postings | write_interaction | social/discard → move_and_mark → notify`. Ambiguous interaction
match → `slack_hitl` (writes checkpoint, posts buttons, EXITS; resumed later by the poller).

- **Checkpointer:** SQLite local / Postgres in-cluster (cloud) — enables HITL pause/resume.
- **Two LLM calls:** Classifier (structured JSON) + Critic (validates classification AND job match).
  Both use the user's job-radar provider+model (BYOK).

---

## Key conventions & invariants

- **The agent never touches the Job Radar DB directly.** All I/O goes through the Writer MCP.
- **The agent never creates `Job`/`UserJobReview` rows.** Job creation is the user's bookmarklet
  (`POST /jobs/manual`). The agent surfaces a LINK in the inbox; the user imports.
- **Email tools are read / mark-read / move ONLY** — no delete/archive (guardrail by absence).
  `move_and_mark(message_id, dest)` is atomic.
- **Process UNREAD email in the configured root folder only.** Read = the human owns it.
- **Writable statuses:** `applied, interviewing, offer, rejected` only. "Next round"/reminders =
  TimelineEvent, no status change.
- **Idempotency key = RFC822 `Message-ID`**, scoped `(user_id, message_id)`. NOT the IMAP UID
  (UIDs change on move — and we move every message).
- **Posting cap = 30 per email** (truncate + log warning).
- **Prompts live in Langfuse**, versioned; the `prompts/` files only seed it once.
- **Langfuse manual spans** (not the LangChain callback): `fetch, classify, critic,
  validation_attempt_N, match_job, write_*, notify` with model/latency/cost/retry/action/prompt-version.
- **Credential routing [H6a]:** cloud agents fetch `GET /agent/config` IN-CLUSTER (never via Cloudflare);
  **local agents don't call it — they use local `.env` creds.** Decrypted creds never cross Cloudflare;
  held in memory only, never logged or written to checkpoint state.
- **Run telemetry:** each run posts a counts-only heartbeat to `POST /agent/runs` (powers "agent last
  run / health"). Business stats are derived from the rows the agent already writes — no separate pipe.
- **Observability = two panes:** business (job-radar `GET /agent/stats`, derived) + engineering
  (ONE shared Langfuse project, every agent pushes directly, traces tagged `user_id`+`environment`).
  Panes cross-linked via `inbox_emails.langfuse_trace_id`. Cost/latency live in Langfuse only.

---

## Security obligations (this repo's share — see INTEGRATION_SPEC §5)

- **Prompt injection [C1]:** wrap email body as DATA with delimiters; system prompt forbids treating
  it as instructions. Critic is NOT a security control — HITL + constrained tools are the backstop.
- **Privacy honesty [H2]:** email body DOES leave the machine (LLM API) and DOES appear in Langfuse
  prompt inputs. Either redact/truncate before spans or document it. Don't claim "never leaves."
- **Cost/DoS [H4]:** per-run max email count, per-run token budget, per-day spend ceiling, circuit
  breaker → admin alert.
- **No link dereferencing [M2]**, **no attachment parsing / no remote content fetch [M1]** — tested invariants.
- **Minimal Gmail scope [H5]:** label add/remove + mark-read; never full mail scope.
- **Public-repo hygiene [M3]:** `.env.example` only; gitleaks pre-commit; real email fixtures are
  gitignored (`tests/fixtures/real/`), synthetic fixtures only in git.
- **Local secrets [M4]:** `.env` perms 600; protect the checkpointer DB.

---

## Deployment

- **Local (Proton, self-host):** `docker-compose up` → email MCP + agent (in-container 15-min interval
  loop, lock-gated) + always-on HITL poller. Proton Bridge runs on the host; IMAP via
  `host.docker.internal:1143`.
- **Cloud (Gmail, multi-user):** one GHCR image. k8s **CronJob** runs the agent; a long-lived
  Deployment runs the HITL flow; both share the cluster Postgres checkpointer. Per-user creds + BYOK
  keys fetched at run start via `GET /agent/config`.
- **Scheduling:** scheduler triggers, LangGraph orchestrates one run. (Celery Beat rejected: can't
  reach local; would duplicate agent code. LangGraph has no built-in scheduler.)
- **Overlap guard:** lockfile keyed on live PID (stale lock from a dead PID auto-cleared). Hung run:
  k8s `activeDeadlineSeconds` / local container restart.

---

## Status

Planning complete; JR-0 (job-radar credential hardening) done. M0 (contract + context) done.
**A-2 Email Reader MCP** built + live-validated against Proton Bridge. **A-3 LangGraph agent** built
with fakes (LLM/Writer/Reader seams) — classify→critic→retry→escalate loop, routing, matching; 18
tests green. NOT yet wired: real LLM client (litellm), real Writer MCP (needs job-radar JR-2/JR-3),
Langfuse spans (A-4), notifiers + HITL resume (A-5), the top-level fan-out runner + scheduler/lock.
Next: job-radar builds JR-1/JR-2/JR-3 against the spec; here, wire the real LLM + Writer, then A-4/A-5.
Full plan: `/Users/duaneo/Claude/General/email_pipeline/EXECUTION_PLAN.md`.
