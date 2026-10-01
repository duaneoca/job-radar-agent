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

## Agent V2 work items (spec §3.6) — two-phase
- **Phase A:** add nullable `inbox_postings.dedup_key` and accept it on `AgentPostingIn`; add an
  **Email policy** section to Settings → Email Agent (allowed sender domains, require sender
  authentication, zero-postings action) and return it as `email_policy` in `/agent/cloud/config`.
- **Phase B:** partial unique index `(user_id, dedup_key)`; skip conflicting postings on insert
  instead of rejecting the email. Until then, cloud users get no cross-email duplicate suppression.
- V2's link picker does not call `/agent/interactions`; keep that endpoint for now.

## Agent sorter work items (spec §3.7)
The optional sorter (Jev decision model) files root-folder mail and writes **Interaction** mail to
`POST /agent/inbox` with **no postings** (categories `recruiter_outreach`, `application_confirmation`,
`network_notification`), plus a §3.5 recruiter card for recruiter outreach. Works with today's API.
- **Inbox:** zero-posting rows already render ("No postings or status updates extracted"); consider
  showing the recruiter card on the row, and hiding the "import" hint when there are no postings.
- **Recruiter suggestions (recommended):** `/recruiters/suggestions` keys by card email, else sender.
  Skip shared relay / no-reply senders (`*@linkedin.com`, `*@indeed.com`, `noreply`/`no-reply`) or key
  them by name + `linkedin_url`. Until then the agent files LinkedIn InMail recruiters as
  `network_notification` so they don't merge into one suggestion.
- **Typed `recruiter` field** on `AgentInboxIn` (§3.5 Phase 2) — the agent already sends it.
- **Cloud folders:** the sorter uses all five `folders` keys from `/agent/cloud/config`; make sure the
  Email Agent settings page lets users name Interaction and Social too.
- **Jev key storage (required to run the sorter in cloud):** the Jev key is **system-wide** (one
  TypeSafe account for all users; not BYOK, not per-user). Store it as `TYPESAFE_API_KEY` in the
  existing **`email-agent-secrets`** Secret — the agent CronJob (`k8s/base/email-agent/cronjob.yaml`)
  already loads that Secret via `envFrom`. Put the non-secret switches in the **`email-agent-config`**
  ConfigMap: `SORTER_ENABLED=true` (and optionally `JEV_MODEL`, `SORTER_MIN_CONFIDENCE`,
  `SORTER_MIN_MARGIN`, `BULK_RECRUITER_DOMAINS`). Per environment (staging and production), then run
  `scripts/check-secret-backup.py` so the new key is in the secrets backup. It never goes in the DB,
  the cloud config bundle, or the UI. Rotating it = update the Secret; the next CronJob run picks it up.

## Non-negotiable security obligations on job-radar (see spec §5)
- `[C2]` URL scheme allowlist + output sanitization for all agent-derived fields (stored-XSS → account takeover).
- `[H1]` Identity derived from API key; never trust `user_id` from the request; validate ownership of every id.
- `[C4]` Slack callback: verify signature + timestamp, rate-limit, validate decision ownership.
- `[C3/H5]` Email creds encrypted with `ENCRYPTION_KEY`; minimal Gmail scope; refresh tokens strongest key tier.
- `[H3]` Internal MCP→tracker-api calls NetworkPolicy-restricted; Cloudflare→origin TLS Full (Strict).
