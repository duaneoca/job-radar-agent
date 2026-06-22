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
2. **Cloud runtime — built, not yet live.** ✅ Docker image + multi-user runner (`agent/cloud.py` /
   `scripts/run_cloud.py`: iterate users → per-user `get_config` → fetch-per-user-discard), both
   **Gmail and generic IMAP** providers, daily spend ceiling + circuit breaker — built & unit-tested.
   `McpWriter` is SHELVED (cloud writes via REST-internal, decision A). STILL pending: live multi-user
   E2E on staging + the k8s CronJob/Deployment; the agent has only run locally so far.
3. **No UI (JR-4).** No inbox page, ops dashboard, or Agent Keys page — others can't self-serve a
   key, connect email, or see results. Keys are currently minted headlessly.
4. **Onboarding not wired.** Gmail OAuth connect, folder setup, `email_credentials` storage E2E.
5. **Interactive HITL resume** blocked on `SLACK_SIGNING_SECRET` (job-radar side).
6. **Untested at multi-tenant scale** — per-user isolation, cost caps, blast radius (H4/H6) designed
   but not exercised with concurrent real users.

---

## Go-live checklist (before inviting others)
- [x] **`GmailProvider`** (Gmail API, gmail.modify, labels, newest-first + age, no attachment/remote
      fetch). Live-verified. [H5/M1]
- [x] **GmailProvider from blob** — `creds_info` dict via `from_authorized_user_info` (cloud path).
- [x] **A-6**: Dockerfile + docker-compose + interval scheduler. Image builds.
- [x] **Image publishing** — GHCR workflow live (`ghcr.io/duaneoca/job-radar-agent:latest`).
- [x] **Multi-user cloud runner** (`agent/cloud.py` + `scripts/run_cloud.py`) — enumerate
      `/agent/cloud/users` → fetch each `/agent/cloud/config/{user_id}` → process one user + discard →
      next; skip disabled; cloud writes via `X-Internal-Token`+`X-User-Id`; circuit breaker + total-
      email budget. Unit-tested. **Pending: live multi-user E2E** (needs job-radar `/agent/cloud/*`
      reachable + the shared internal token). SPEC §2.1b. [H6/H6a/L3]
- [~] **`McpWriter`** — SHELVED. Cloud writes go via REST-internal (decision A); `mcp-writer` not in
      the path. (Optionally revive if Server 2 should be consumed via MCP.)
- [x] **`McpReaderClient`** — consumes Server 1 (Email Reader) over stdio (`EMAIL_READER_TRANSPORT=mcp`);
      live-verified against real Gmail (spawns the server, MCP handshake, `get_unread_emails`). Default
      transport stays `direct` (in-process) for robustness; MCP is the opt-in "genuinely consumes MCP"
      path. So the agent both publishes (Server 1) AND consumes MCP at runtime.
- [x] **Cost/DoS caps** — per-run email cap + cloud-run total-email budget + **daily $ spend ceiling**
      (`agent/budget.py` `DailySpendStore`, per-user, persisted; enforced in `run_once` — skips run if
      over, halts mid-run, persists accrued cost). litellm `completion_cost` per call. [H4]
- [ ] **`SLACK_SIGNING_SECRET`** on job-radar; wire HITL resume (poller + LangGraph interrupt).
- [ ] **Multi-user E2E test** on staging (you + a friend's Gmail) before opening up.
- [ ] Confirm per-user data isolation (IDOR/H1) holds with 2+ real users.
- [x] **JR-4 / JR-5** (job-radar): UI + deploy — reported done by the job-radar thread.

CronJob CMD = `python scripts/run_cloud.py` (NOT `run_loop.py --once`, which is the single-user
local loop). Set `concurrencyPolicy: Forbid` + `activeDeadlineSeconds`.

## Reference
Full plan + phase detail: `/Users/duaneo/Claude/General/email_pipeline/EXECUTION_PLAN.md`.
Job-radar-side pending work: `JOBRADAR_NEXT_SESSION.md`. Threat model: `SECURITY.md`.
