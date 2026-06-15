# Readiness — go-live checklist

**What works today vs. what's required before inviting other users.** The local self-host path
(Proton, single user) is built and tested end-to-end. The multi-user cloud path that *inviting
others* depends on is largely not built yet.

Last updated: 2026-06-12.

---

## ✅ Ready & tested — local self-host path (single user, Proton)
- Core agent: read → classify (Classifier + Critic with validation/retry loop) → match → route →
  write → file. Proven end-to-end against a real mailbox + real staging.
- Email Reader (IMAP/Proton): PEEK-safe reads (never marks unread), age cap + newest-first + per-run
  cap, atomic move-and-mark, no delete/archive (guardrail by absence).
- Multi-vendor LLM via LiteLLM (Anthropic/OpenAI/Google/Groq) — 100% category eval on the fixture set.
- Writer: `RestWriter` ↔ staging `/agent/*` contract verified.
- Observability: Langfuse per-email traces + classify/critic generations, `trace_id` propagated.
- Notifications: Slack (Telegram/Discord built+tested, not live-configured); user/admin tiers verified.
- Runner: lockfile overlap guard, per-email isolation, dry-run, run-record heartbeat.
- 69 tests, CI green, public repo, threat model (`SECURITY.md`).

## ⛔ NOT ready for inviting others — blockers
1. **Gmail provider** — ✅ IMPLEMENTED (Gmail API/OAuth, gmail.modify, labels-as-folders).
   Verified live END-TO-END on real labeled Gmail (read → classify → route, identical to Proton).
   (Was the #1 blocker — now cleared.)
2. **No cloud runtime.** `McpWriter` (cloud writer over MCP), multi-user runner (iterate users,
   per-user creds via `get_config`, fetch-per-user-then-discard), Docker image, k8s CronJob — unbuilt.
   The agent has only ever run locally.
3. **No UI (JR-4).** No inbox page, ops dashboard, or Agent Keys page — others can't self-serve a
   key, connect email, or see results. Keys are currently minted headlessly.
4. **Onboarding not wired.** Gmail OAuth connect, folder setup, `email_credentials` storage E2E.
5. **Interactive HITL resume** blocked on `SLACK_SIGNING_SECRET` (job-radar side).
6. **Untested at multi-tenant scale** — per-user isolation, cost caps, blast radius (H4/H6) designed
   but not exercised with concurrent real users.

---

## Go-live checklist (before inviting others)
- [ ] **`GmailProvider`** implemented (Gmail API, minimal scope `label add/remove + mark-read`,
      newest-first + `newer_than:{days}` query, no attachment/remote fetch). [H5/M1]
- [ ] **`McpWriter`** (MCP streamable-HTTP → `jobradar-mcp-writer:8001/mcp`, `X-Agent-Key` header).
- [ ] **Multi-user runner** — per-user creds from `get_config` (in-cluster), per-user lock + error
      isolation, fetch-per-user-then-discard. [H6/H6a/L3]
- [ ] **A-6**: Dockerfile + docker-compose (local) + GHCR image; local scheduling (interval loop/cron).
- [ ] **JR-5** (job-radar): external ingress for mcp-writer; agent CronJob/Deployment manifests.
- [ ] **JR-4** (job-radar): Inbox page + Ops dashboard + Email Agent settings (keys/onboarding).
- [ ] **`SLACK_SIGNING_SECRET`** set on job-radar; wire HITL resume (poller + LangGraph interrupt/checkpoint).
- [ ] **Cost/DoS caps live** — per-run token budget + daily spend ceiling + circuit breaker. [H4]
- [ ] **Multi-user E2E test** on staging (you + one friend's Gmail) before opening up.
- [ ] Confirm per-user data isolation (IDOR/H1) holds with 2+ real users.

## Reference
Full plan + phase detail: `/Users/duaneo/Claude/General/email_pipeline/EXECUTION_PLAN.md`.
Job-radar-side pending work: `JOBRADAR_NEXT_SESSION.md`. Threat model: `SECURITY.md`.
