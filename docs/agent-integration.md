# Agent Integration — Job Radar side

> The Job Radar-facing summary of the contract. It lives **only here** — job-radar's `CLAUDE.md`
> points at `INTEGRATION_SPEC.md` rather than keeping a copy that would drift.

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
  creates jobs) — superseded by the staffing work items below (`JobSource.EMAIL`).
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
- **Recruiter suggestions** — DONE (job-radar #150): shared relay / no-reply senders are keyed by
  `linkedin_url`, else name, with no email; relay recruiter mail is now sent as `recruiter_outreach`.
  Keep job-radar's relay list in step with the agent's `RELAY_DOMAINS`.
- **Typed `recruiter` field** on `AgentInboxIn` (§3.5 Phase 2) — DONE (job-radar #150): stored at
  `raw_extracted_json.recruiter_contact`, wins over the nested copy; a malformed card is dropped,
  never the email.
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

## Mailbox retention work items (spec §3.8)
- **Settings → Email Agent → "Mailbox retention":** per user, "Move to Trash after N days" for
  **Social** and **Postings** (each off or 1–365; suggest 14). Help text: read *and* unread mail older
  than N days goes to Trash; starred mail is kept; Interaction and Unprocessed are never cleaned; the
  mail provider empties Trash (Gmail 30 days; Proton needs "auto-delete unwanted messages" on).
- **Storage + API:** two nullable ints (e.g. `retention_social_days`, `retention_postings_days`),
  validated 0/1–365 server-side; return them as the optional `retention` block in
  `GET /agent/cloud/config/{user_id}`: `{"social_days": 14, "postings_days": 14}`. Absent ⇒ off.
- **Optional:** accept a `trashed` count on `POST /agent/runs` and show it in the Email Agent stats.

## Staffing email agent work items (spec §2.4, §3.9)
Design: `job-radar-agent/docs/staffing-email-agent.md`. Recruiter mail becomes jobs (or a source on an
existing one), filtered deterministically, with threaded reply drafts for strong matches. The agent
never sends. Ship in the order below; the agent treats a missing endpoint as "feature off".

**1. Agent settings (§2.4) — first, also unblocks moving `.env` policy into the UI**
- Store per user: `sorter` (enabled, min confidence, min margin, bulk recruiter domains),
  `email_policy` (+ `dedup_window_days`, `max_email_age_days`), `retention`, and the `staffing`
  block. Validate server-side (bare hostnames, closed enums, day and score ranges).
- `GET /agent/settings`, dual auth (`X-Agent-Key`, or `X-Internal-Token` + `X-User-Id`),
  externally reachable. **No secrets in it, ever**, and no operator cost guards (spend ceiling,
  emails per run, retention cap, poll interval). Copy `criteria` from the active `Criteria` row and
  `writing_skills` from enabled skills scoped `email_draft`.
- Keep `email_policy` / `retention` in `/agent/cloud/config` until the agent reads `/agent/settings`
  everywhere; then drop them.

**2. Schema**
- **`work_mode`** (`onsite | hybrid | remote`, nullable) **replaces `Job.remote`** (and
  `inbox_postings.remote`): data migration `true ⇒ remote`, `false ⇒ null`. Update scrapers, the
  scorer prompt, filters and UI that read `remote`.
- `employment_type` (`full_time | contract | contract_c2c | contract_w2 | contract_to_hire`) on inbox
  postings and jobs.
- On `inbox_postings`: `location` (the column exists; store the cleaned value), `jd_text`,
  `outcome`, `outcome_reason`, `outcome_review_id`, `apply_directly` (spec §1.2).
- `JobSource.EMAIL` for jobs created this way; job sources (the emails attached to a job: message id,
  sender, received at); on the review: `applied_via` (vendor or `direct`), draft stamp
  (`draft_message_id`, `vendor`, `note_kind`, `drafted_at`).

**3. Inbox write with outcomes (§3.1, §3.9 Stage A step 4)**
- Accept the new posting fields and the `staffing` block. In the same transaction: `imported` ⇒
  the shared import function; `duplicate` ⇒ attach as a source of `duplicate_of_review_id`
  (ownership-checked); `filtered` ⇒ store only. Idempotent on `(user_id, message_id)`: a repeat
  returns the existing rows and creates no second job. Return `review_ids[]` aligned with
  `posting_ids[]`.
- **One shared import function** used by the inbox UI's Add Job, "process anyway"
  (`POST /agent/inbox/postings/{id}/import`), the agent, and a new MCP tool `import_posting`.
  It queues the AI review.

**4. Inbox UI**
- Show outcome + reason per posting; dim `filtered` and `duplicate` (link to the matched job);
  "process anyway" button; "apply directly" badge.
- On the job: its email sources, and "How I applied" (vendor / directly), editable.

**5. Dedup and draft feeds**
- `GET /agent/email-jobs?since_days=N` (§2.1).
- `GET /agent/draft-candidates` (§3.9 Stage B): server applies the score threshold, "review done",
  no stamp, `applied_via` null, not `apply_directly`; includes `profile_summary`
  (`Profile.summary`) and the review's `ai_pros`.
- `POST /agent/reviews/{review_id}/draft` stamp; idempotent; sets `applied_via` to the vendor by
  default.

**6. Resume PDF**
- Upload on the **Profile page next to the text resume** (`Profile.resume_text`), labeled "Used by
  Job Radar Agent for reply drafts". Hidden unless the agent is enabled and `staffing.auto_draft`
  is on. Show both fields' upload dates so a stale one is obvious. One file per user; PDF only
  (check the magic bytes), size-capped (e.g. 5 MB); no text extraction.
- `GET /agent/resume` (dual auth) returns the bytes; `/agent/settings` carries `{sha256, filename,
  uploaded_at}` so the agent downloads only on change.

**7. Settings UI — Profile → "Email Agent" tab** (next to `AIPromptsTab`)
- Staffing: on/off, auto-draft on/off, excluded sender domains (default `linkedin.com`), allowed
  employment types, draft score threshold (default 8.5), dedup window (default 30 days), mark read
  per category (default: handled recruiter outreach read; the rest unread), draft prompt (editable,
  reset to default), Slack on/off.
- Work mode, locations, min salary and excluded companies are **not repeated**: show them read-only
  from the search criteria with a link to edit there.
- Move here: sorter settings, email policy, retention (spec §2.2a).
- New writing-skill scope `email_draft`.

## Non-negotiable security obligations on job-radar (see spec §5)
- `[C2]` URL scheme allowlist + output sanitization for all agent-derived fields (stored-XSS → account takeover).
- `[H1]` Identity derived from API key; never trust `user_id` from the request; validate ownership of every id.
- `[C4]` Slack callback: verify signature + timestamp, rate-limit, validate decision ownership.
- `[C3/H5]` Email creds encrypted with `ENCRYPTION_KEY`; minimal Gmail scope; refresh tokens strongest key tier.
- `[H3]` Internal MCP→tracker-api calls NetworkPolicy-restricted; Cloudflare→origin TLS Full (Strict).
- `[D2]` Agent-requested job creation only through the shared import function, idempotent, ownership-checked.
- `[H6a]` `/agent/settings` is external, so it must never carry a secret; credentials stay in `/agent/config` (in-cluster).
