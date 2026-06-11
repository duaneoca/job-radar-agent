# Agent Integration — Job Radar side

> **This file is destined for the `job-radar` repo** (copy to `job-radar/docs/agent-integration.md`
> and reference it from job-radar's `CLAUDE.md`). It is the job-radar-facing pointer to the contract.

## What this is
The **Job Radar Email Agent** (repo: `../job-radar-agent`) is an external agentic pipeline that reads
a user's job-related email, classifies it, and writes results into Job Radar. Job Radar's role is the
**platform side** of that integration.

## Source of truth
The full contract lives in **`job-radar-agent/INTEGRATION_SPEC.md`**. Read it before implementing.
Any change to tables, endpoints, payloads, or auth is a PR against that file first.

## What job-radar must build (summary — see spec for detail)
- **JR-0 — credential hardening** (DONE): dedicated `ENCRYPTION_KEY` split from `SECRET_KEY`,
  fail-closed startup guard, `MultiFernet` rotation.
- **JR-1 — tables:** `inbox_emails`, `inbox_postings`, `inbox_interactions`, `agent_api_keys`,
  `email_credentials`, `hitl_decisions` (+ Alembic migration). No `JobSource.EMAIL_AGENT` (agent never
  creates jobs).
- **JR-2 — `/agent/*` endpoints:** agent-facing (auth via `X-Agent-Key`, user derived from key),
  frontend-facing (JWT), Slack-facing callback (signing-secret verified). Mind the route-ordering gotcha.
- **JR-3 — Writer MCP service** (`services/mcp-writer/`): HTTPS + per-user API key; wraps the
  agent-facing endpoints; NetworkPolicy-restricted internal calls.
- **JR-4 — frontend:** Inbox page (links, not auto-import; sanitized rendering) + Ops dashboard
  (per-user stats on settings page, global on admin page).
- **JR-5 — deploy:** mcp-writer + agent CronJob manifests; retire the old `email-monitor` stub.

## Non-negotiable security obligations on job-radar (see spec §5)
- `[C2]` URL scheme allowlist + output sanitization for all agent-derived fields (stored-XSS → account takeover).
- `[H1]` Identity derived from API key; never trust `user_id` from the request; validate ownership of every id.
- `[C4]` Slack callback: verify signature + timestamp, rate-limit, validate decision ownership.
- `[C3/H5]` Email creds encrypted with `ENCRYPTION_KEY`; minimal Gmail scope; refresh tokens strongest key tier.
- `[H3]` Internal MCP→tracker-api calls NetworkPolicy-restricted; Cloudflare→origin TLS Full (Strict).
