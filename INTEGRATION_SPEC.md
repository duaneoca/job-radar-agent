# INTEGRATION_SPEC — Job Radar Email Agent ⇄ Job Radar

**Version:** 0.2 (adds §3.5 recruiter-contact extraction + recruiter↔posting linkage)
**Status:** Contract of record between `job-radar-agent` (the agent) and `job-radar` (the platform).
**Audience:** both repos. Each side builds independently against this document. If reality and this doc disagree, fix the doc in the same PR.

> Security requirements are **normative** (MUST/SHOULD). They are acceptance criteria, not
> suggestions. Tags like `[C2]`, `[H1]` reference findings in `SECURITY_REVIEW.md`.

---

## 0. Roles & boundaries

| Component | Repo | Responsibility |
|---|---|---|
| LangGraph agent | job-radar-agent | Reads email, classifies, validates, decides actions |
| Email Reader MCP (Server 1) | job-radar-agent | Mailbox access (read / mark-read / move only) |
| Notifiers + HITL poller | job-radar-agent | Slack/Telegram/Discord; resumes checkpoints from decisions |
| Job Radar Writer MCP (Server 2) | job-radar | The agent's ONLY write path into Job Radar |
| tracker-api `/agent/*` | job-radar | REST endpoints behind the Writer MCP + frontend |
| Inbox UI + Ops dashboard | job-radar | Human review surface + business metrics |

**Hard boundary:** the agent NEVER touches the Job Radar database directly. All reads/writes go
through the Writer MCP → tracker-api (cloud) or the equivalent REST `/agent/*` endpoints (local).
The agent NEVER creates `Job` or `UserJobReview` rows (job creation happens later via the existing
bookmarklet `POST /jobs/manual`).

### 0.1 Two MCP servers, two write transports — rationale

There are **two MCP servers** (this hasn't changed): Server 1 (Email Reader, stdio, consumed by the
agent) and Server 2 (Job Radar Writer, consumed by the agent to write back). What's topology-specific
is **how the agent reaches Server 2**, because Server 2 runs in-cluster and is **not exposed to the
public internet** (by choice — it can reach `/agent/config`'s decrypted creds, so keeping it
in-cluster-only is safer, and the REST surface already exists).

| Agent topology | Reaches Job Radar via | Why |
|---|---|---|
| **cloud** (in-cluster) | **Server 2 MCP** at `http://jobradar-mcp-writer:8001/mcp` | in-cluster ⇒ reachable; MCP as designed |
| **local** (outside cluster) | **REST** `/api/agent/*` (`X-Agent-Key`) | can't reach an in-cluster service; we don't expose the writer publicly; REST already exists and hits the SAME `/agent/*` contract |

So MCP is **not** removed — Server 2 is consumed via MCP on the cloud path; the local path uses REST
as a transport to the identical endpoints. Consequently **no external `mcp-writer` ingress is needed**
for either path. (Note: Server 1 is published + works, but the runtime currently reads via the
provider directly — consuming Server 1 over stdio via an `McpReaderClient` is a deferred item.)

---

## 1. Data model (job-radar side — new tables)

All UUID PKs. All `user_id` FK → `users.id` ON DELETE CASCADE. Follow existing convention:
set BOTH `ondelete="CASCADE"` on the column AND `cascade="all, delete-orphan"` on the relationship.

### 1.1 `inbox_emails` — one row per processed source email
| col | type | notes |
|---|---|---|
| id | UUID PK | |
| user_id | UUID FK | |
| message_id | text | RFC822 `Message-ID`; idempotency. UNIQUE `(user_id, message_id)` `[L1]` |
| subject | text | |
| sender | text | |
| received_at | timestamptz | |
| category | enum | `recruiter_outreach \| application_confirmation \| job_alert \| network_notification` |
| confidence | float | model-justified, NOT email-settable `[C1]` |
| raw_extracted_json | jsonb | full LLM output. For `recruiter_outreach`, carries `recruiter_contact` (§3.5) — the Phase-1 home for the recruiter card until the typed `recruiter` field lands |
| validation_attempts | int | |
| escalation_reason | text null | set when status = `needs_review` |
| status | enum | `pending \| processed \| needs_review \| discarded` |
| langfuse_trace_id | text null | |
| created_at | timestamptz | |

### 1.2 `inbox_postings` — one row per extracted posting (N per email; cap 30) `[D11]`
| col | type | notes |
|---|---|---|
| id | UUID PK | |
| inbox_email_id | UUID FK → inbox_emails | |
| user_id | UUID FK | |
| company | text | |
| role | text | |
| link | text null | http/https ONLY, validated server-side `[C2]` |
| action_required | bool | |
| possible_duplicate | bool | fuzzy company+role match flag, never blocks `[D12]` |
| matched_review_id | UUID null | FK → user_job_reviews if dup suspected |
| import_status | enum | `pending \| imported \| dismissed` |
| imported_review_id | UUID null | FK → user_job_reviews once user imports |
| created_at | timestamptz | |

### 1.3 `inbox_interactions` — one row per application-status-update email
| col | type | notes |
|---|---|---|
| id | UUID PK | |
| inbox_email_id | UUID FK | |
| user_id | UUID FK | |
| matched_review_id | UUID null | null ⇒ needs_review |
| match_confidence | float **null** | null when there's no match (paired with `matched_review_id` null). MUST be nullable — the agent sends null for no-match interactions `[bugfix 2026-06-24]` |
| previous_status | enum null | JobStatus |
| new_status | enum null | JobStatus, agent-writable subset only |
| applied_at | timestamptz null | when status written to the review |
| created_at | timestamptz | |

### 1.4 `agent_api_keys` — per-user agent auth `[H1]`
| col | type | notes |
|---|---|---|
| id | UUID PK | |
| user_id | UUID FK | |
| key_hash | text | hashed at rest (never store plaintext) |
| key_hint | text | last 4 chars, e.g. `…aB3x` |
| created_at / last_used_at | timestamptz | |
| revoked | bool | |

A key maps to **exactly one user**. The user is **derived** from the key on every request;
`user_id` is NEVER accepted from the caller. `[H1]`

### 1.5 `email_credentials` — per-user mailbox connection (cloud path) `[C3/H5]`
| col | type | notes |
|---|---|---|
| id | UUID PK | |
| user_id | UUID FK | |
| provider | enum | `gmail \| imap` |
| encrypted_blob | text | Fernet via **`ENCRYPTION_KEY`** (the JR-0 split key), NOT `SECRET_KEY` |
| folder_root | text | e.g. `hire-duane` |
| folder_interaction / _postings / _social / _unprocessed | text | **bare leaf names** (e.g. `Postings`); the agent joins them under `folder_root` with `/` (→ `Hire Duane/Postings`, the Gmail/IMAP full label path). Sending full paths also works (join is idempotent). `[D6]` |
| created_at / updated_at | timestamptz | |

Gmail stores an OAuth **refresh token** here — high value. SHOULD use a separate key tier / KMS
envelope. Request **minimum** Gmail scope (label add/remove + mark-read); NEVER full mail scope. `[H5]`

**`GET /agent/config` → `email_credentials` decrypted blob shape** (what the agent consumes):

*Gmail (OAuth)* — Google "authorized user" format → agent does `Credentials.from_authorized_user_info`:
```json
{ "provider": "gmail", "refresh_token": "<per-user>", "client_id": "<job-radar app>",
  "client_secret": "<job-radar app>", "token_uri": "https://oauth2.googleapis.com/token",
  "scopes": ["https://www.googleapis.com/auth/gmail.modify"] }
```
- **ONE shared Job Radar OAuth Web client** (`client_id`/`client_secret` app-level). Store only the
  **per-user `refresh_token`** (+scopes) in `EmailCredential`; merge in the shared client fields when
  building the config response. No per-user access_token/expiry needed.
- **Job Radar hosts the OAuth dance** (Web-app client, redirect → consent → callback stores the
  refresh token). Distinct from the Desktop client the LOCAL self-host path uses (`gmail_auth.py`).
- Gmail folders are **labels**: the same five folder fields map to nested labels
  (`<root>/Interaction`, …). UI: relabel "Folder"→"Label" for Gmail. User creates the labels.

*IMAP* (cloud `ImapProvider`, built 2026-06-18 — `imaplib.IMAP4_SSL` when `use_ssl`, else plain
`IMAP4` + opportunistic STARTTLS; reuses the Proton IMAP read/move/mark logic):
```json
{ "provider": "imap", "host": "...", "port": 993, "username": "...", "password": "...", "use_ssl": true }
```

**Local self-host path uses NONE of this** — it reads mailbox creds + folder config from its own
local `.env` and never calls `/agent/config` `[H6a]`.

### 1.5a Per-user agent enablement (cloud)
`email_credentials` (or a small per-user agent-settings row) carries an **`enabled` flag**. The cloud
runner iterates users who have credentials AND `enabled=true`. **No per-user schedule** — cadence is
the single global k8s CronJob. `[D21 / §2.2a]`

### 1.9 Three key types — do NOT conflate
| Key | Purpose | Storage | Reuse? |
|---|---|---|---|
| **LLM provider key** (Anthropic/OpenAI/…) | Agent calls the LLM (Classifier + Critic) | **existing `user_api_keys`** (Fernet via `ENCRYPTION_KEY`) | **REUSE — do not build new** |
| **Agent API key** (`X-Agent-Key`) | Agent authenticates TO Job Radar's Writer MCP (write-back) | `agent_api_keys` (§1.4) | new — the agent's own credential, NOT an LLM key |
| **Email credentials** | Mailbox access | `email_credentials` (§1.5) | new |

The agent calls the LLM **directly** (so Langfuse traces it), so it needs the **decrypted** LLM key
at runtime — delivered in the `GET /agent/config` bundle (server decrypts `user_api_keys`, same as the
`ai-reviewer` worker does per-request). Both Classifier and Critic use the one `{provider,
preferred_model, api_key}` bundle (per D2/Q5). Handling rules: `[H6]`.

### 1.6 `hitl_decisions` — interactive HITL resolution record `[C4]`
| col | type | notes |
|---|---|---|
| id | UUID PK | |
| user_id | UUID FK | |
| hitl_id | text | correlates to the agent's checkpoint |
| status | enum | `pending \| resolved \| abandoned` |
| choice_review_id | UUID null | the user's pick; null ⇒ "none / leave in Unprocessed" |
| created_at / resolved_at | timestamptz | abandon after 30 min (configurable) `[D14]` |

### 1.6b `agent_runs` — operational heartbeat / run health
| col | type | notes |
|---|---|---|
| id | UUID PK | |
| user_id | UUID FK | |
| environment | enum | `local \| cloud` |
| agent_version | text | |
| status | enum | `success \| partial \| failed` |
| started_at / finished_at | timestamptz | |
| emails_processed | int | |
| postings_created | int | |
| interactions_recorded | int | |
| escalations | int | |
| retries | int | |
| error_summary | text null | populated on `failed`/`partial` |

**Finalization:** the agent always posts a terminal record (status + `finished_at`) even on crash/SIGTERM (run_once `finally`; run_cloud converts SIGTERM→clean exit). A hard SIGKILL/OOM can't be finalized agent-side — **job-radar SHOULD reap records with `finished_at` NULL older than the run deadline** (mark `failed`/`expired`).

Counts only — NO subjects/senders (content lives in `inbox_emails`). Latest row per user = the
dashboard's "agent last run / health" (recent `finished_at` + `success` ⇒ healthy; nothing in >2
intervals ⇒ stale/down). LLM cost/latency are NOT here — they live in Langfuse (§6).

### 1.7 Enum alignment (existing `JobStatus`)
Full enum: `new, reviewed, applied, dismissed, interviewing, offer, rejected, expired`.
**Agent-writable subset (enforced server-side):** `applied, interviewing, offer, rejected`. `[C1/D9]`
The agent MUST NOT write `new, reviewed, dismissed, expired`.

### 1.8 Retention
Inbox rows auto-deleted after **14 days** (align with existing `terminal_ttl_days`), EXCEPT
`needs_review` (kept until resolved). User-facing delete button on inbox rows. Deleting the last
posting of an email deletes the `inbox_emails` row (mirror existing last-review-deletes-Job). `[Q7]`

---

## 2. tracker-api `/agent/*` endpoints (job-radar implements)

**Auth model `[H1]`:**
- **Agent-facing** endpoints: authenticated by **agent API key** (header `X-Agent-Key`); user derived
  from key. NOT the user JWT.
- **Frontend-facing** endpoints: existing user **JWT** (`get_current_user`).
- **Slack-facing** callback: Slack **signing-secret** verification, no app auth. `[C4]`
- Respect the route-ordering gotcha (literal routes before `{param}`) per job-radar `CLAUDE.md`.

### 2.1 Agent-facing (called by Writer MCP)
| Method/Path | Body / Query | Returns | Notes |
|---|---|---|---|
| `GET /agent/config` | (user from key) | `{provider, folders{...}, llm{provider, preferred_model, api_key}, email_credentials{...decrypted...}}` | **Cloud path only.** Returns DECRYPTED secrets — so it MUST be called **in-cluster** (`http://tracker-api/...`), never through Cloudflare `[H6a]`. **Local self-host path does NOT call this** — the local agent uses its own LLM key + Proton creds from local `.env`. Server-side decrypt; strongest auth; rate-limited; audit-logged. `[H6/C3]` |
| `GET /agent/reviews` | (user from key) | `[{review_id, company, title, status, url}]` | The user's `UserJobReview` rows, for matching/dedup. |
| `POST /agent/inbox` | inbox payload (§3.1) | `{inbox_email_id, posting_ids[]}` | Creates email + ≤30 postings. Validates link scheme `[C2]`. On `recruiter_outreach` may carry a recruiter card (§3.5) — typed `recruiter` and/or `raw_extracted_json.recruiter_contact`. |
| `POST /agent/interactions` | interaction payload (§3.2) | `{interaction_id, applied_status?}` | If matched, updates the review status + timeline (reuses existing PATCH logic) `[D9/D10]`. |
| `POST /agent/hitl/register` | `{hitl_id, candidates[review_id]}` | `{ok}` | Agent registers a pending decision before/while posting Slack. |
| `GET /agent/hitl/pending` | (user from key) | `[{hitl_id, status, choice_review_id}]` | Poller pulls resolved decisions `[C4]`. |
| `POST /agent/hitl/consume` | `{hitl_id}` | `{ok}` | Poller marks a decision consumed after resuming. |
| `POST /agent/runs` | run record (§3.4) | `{run_id}` | Operational heartbeat: agent reports each run's outcome + counts. Powers "agent last run / health". Counts only — no subjects/senders. Goes via normal write path (no creds → Cloudflare-fine). |

### 2.1b Cloud enumeration (internal-token, IN-CLUSTER ONLY)

How the single cloud CronJob discovers + processes all users (it does NOT hold per-user
`X-Agent-Key`s). Auth = shared **`X-Internal-Token`** (the agent authenticates as *itself*, not a
user); both endpoints in-cluster-only (NetworkPolicy + nginx block, same posture as `/agent/config`).

| Method/Path | Returns | Notes |
|---|---|---|
| `GET /agent/cloud/users` | `[{user_id, provider, enabled}]` | **No secrets.** Enabled cloud users with stored creds. |
| `GET /agent/cloud/config/{user_id}` | `{llm, folders, email_credentials}` | One user's decrypted config (same shape as `/agent/config`). |

**Split enumerate from fetch on purpose** `[H6]`: the runner loops `users` → fetches ONE
`config/{user_id}` → processes → **discards that user's creds** → next. Never holds all users' secrets
at once (a bulk "all configs" response would violate H6). One user in memory at a time = blast radius
of one.

**Cloud per-user auth (reads AND writes):** the cloud agent has no per-user `X-Agent-Key` (and JR
stores keys hashed — can't hand plaintext back). So cloud calls use **`X-Internal-Token` + explicit
`user_id`** (`user_id` is trustworthy *because* the caller holds the internal token behind the
NetworkPolicy). Rule: **every per-user operational endpoint accepts EITHER auth mode** —
`X-Agent-Key` (local → derive user) **or** `X-Internal-Token` + `user_id` (cloud):
- **read:** `GET /agent/reviews` (needed for matching/dedup — fetched per user at match time, NOT
  bundled into `cloud/config`)
- **write:** `POST /agent/{inbox, interactions, runs, hitl/register, hitl/consume}`

Only enumeration (`/agent/cloud/users`) and the creds bootstrap (`/agent/cloud/config/{user_id}`)
are cloud-specific endpoints; everything else is the existing per-user endpoint with dual auth.

**Header convention (cloud mode):** the agent sends `X-Internal-Token: <token>` + `X-User-Id: <uuid>`
on the dual-auth reads/writes. (`/agent/cloud/config/{user_id}` carries the id in the path instead.)
⚠️ job-radar must read `X-User-Id` for the cloud auth path — confirm this matches the implementation.

### 2.2 Frontend-facing (JWT)
| Method/Path | Returns |
|---|---|
| `GET /agent/inbox` | user's inbox emails + postings/interactions (paginated) |
| `PATCH /agent/inbox/{id}` | dismiss / mark-handled |
| `DELETE /agent/inbox/{id}` | delete (cascades postings; mirrors job delete) |
| `GET /agent/stats` | ops-dashboard metrics (per-user); admin variant returns global |
| `GET /agent/keys` | list this user's agent keys (`key_hint` only, never plaintext) |
| `POST /agent/keys` | mint a new agent key; returns the plaintext **once** |
| `DELETE /agent/keys/{id}` | revoke a key |

### 2.2a "Email Agent" settings page (frontend, JR-4)
A dedicated **Settings → Email Agent** page is the single per-user home for this feature. Sections:
| Section | Backing |
|---|---|
| **Agent key** — generate / revoke; show `key_hint` (last 4); plaintext shown once on create | `agent_api_keys` / `/agent/keys` |
| **Email connection** — Gmail "Connect" (OAuth) or IMAP creds (cloud users); local self-host uses local `.env` | `email_credentials` `[C3/H5]` |
| **Folder config** — root + subfolder names (Interaction/Postings/Social/Unprocessed) | folder layout `[D6]` |
| **Notifications** — Slack/Telegram/Discord channel + connect | notifier config `[D16]` |
| **Agent status & stats** — last run / health + the per-user business stats | `agent_runs` + `GET /agent/stats` `[D21/Q8]` |
| **Enable / disable** — pause the agent for this user | (toggle) |

LLM provider/model keys stay on the EXISTING API Keys page (shared with scoring/research; the agent
reuses them — do NOT duplicate). The global ops dashboard lives on the admin page; per-user stats
live here on the user's Email Agent page.

### 2.3 Slack-facing (signature-verified)
| Method/Path | Notes |
|---|---|
| `POST /agent/hitl/callback` | Slack interactive callback. MUST verify `X-Slack-Signature` + timestamp (reject >5 min skew). Treat `hitl_id`/`choice` as untrusted; validate `choice_review_id` belongs to the same user as the decision `[C4/H1]`. Writes `hitl_decisions.status=resolved`. Rate-limited. |

> Cloud-deployed agents resume directly from the cluster Postgres checkpointer; local agents resume
> via the `GET /agent/hitl/pending` poll. The callback endpoint only RECORDS the decision — it is
> checkpoint-location-agnostic. `[§8.15]`

---

## 3. Payload shapes

### 3.1 `POST /agent/inbox`
```json
{
  "message_id": "<CA+...@mail.gmail.com>",
  "subject": "…", "sender": "recruiter@acme.com", "received_at": "2026-06-10T14:00:00Z",
  "category": "job_alert",
  "confidence": 0.93,
  "langfuse_trace_id": "trace_abc",
  "raw_extracted_json": { ... },
  "recruiter": { ... §3.5 object; OPTIONAL, recruiter_outreach only ... },
  "postings": [
    { "company": "Acme", "role": "FDE", "link": "https://…",
      "action_required": true, "possible_duplicate": false, "matched_review_id": null }
  ]
}
```
Server: enforce ≤30 postings, http/https links only, dedup `(user_id, message_id)`. `recruiter` is
optional (§3.5); when present it is the email-level recruiter for every posting above.

### 3.2 `POST /agent/interactions`
```json
{
  "message_id": "<...>", "subject": "…", "sender": "talent@acme.com",
  "received_at": "2026-06-10T14:00:00Z",
  "category": "application_confirmation",
  "confidence": 0.88, "langfuse_trace_id": "trace_xyz",
  "matched_review_id": "uuid-or-null",
  "match_confidence": 0.91,                  // float OR null — null when matched_review_id is null (no match)
  "new_status": "interviewing",
  "timeline_note": "Interview scheduled (from email)"
}
```
Server: if `matched_review_id` present AND `new_status` in the writable subset → update the review
(auto-timeline + auto `date_applied` on `applied`, reusing existing PATCH logic). Else record as
`needs_review`. Reject any `new_status` outside `applied|interviewing|offer|rejected`. `[C1/D9]`

### 3.3 Notification payloads (agent → notifier; informational contract)
- **User:** `{kind, company, role, link?, deep_link_to_inbox_entry}` → rich Block Kit + button.
- **Admin:** `{level, run_id, stage, error, message_id?, langfuse_trace_url}` → diagnostic text.
- **HITL prompt:** `{hitl_id, company, candidates:[{review_id,label}]}` → buttons + "None".

### 3.4 `POST /agent/runs` (run heartbeat)
```json
{
  "environment": "local",
  "agent_version": "0.1.0",
  "status": "success",
  "started_at": "2026-06-10T14:00:00Z",
  "finished_at": "2026-06-10T14:00:42Z",
  "emails_processed": 7, "postings_created": 12,
  "interactions_recorded": 2, "escalations": 1, "retries": 3,
  "error_summary": null
}
```

### 3.5 Recruiter contact (recruiter_outreach only) `[D22 extract / D23 linkage]`

Job Radar never receives the email body (content minimization), so the recruiter's name, phone,
agency, LinkedIn, and the client they represent — all in the signature/body — are extractable ONLY by
the agent. On a `recruiter_outreach` email the agent extracts **ONE** recruiter (the sender) at the
**email level**; every posting in that email is implicitly attributable to that recruiter `[D23]`
(a recruiter email = this recruiter, these roles). No per-posting recruiter ref.

A recruiter card is **extracted structured data — the same class as the company/role already in
`postings`**, not email content. It therefore stays inside the content-minimization boundary.

**Object** (all optional except `name`; **OMIT** unknown fields — never empty strings or guesses;
**plain strings only — NO markup, NO raw body / free-text snippets**):

| field | type | max | notes |
|---|---|---|---|
| `name` | string (req) | 200 | prefer the signature's full name over the From display name |
| `email` | string | 255 | reply-to; the signature's address if it differs from `sender` |
| `phone` | string | 50 | verbatim — do NOT normalize |
| `employer` | string | 200 | the recruiter's own firm/agency (or the hiring company if in-house) |
| `title` | string | 200 | e.g. "Senior Technical Recruiter" |
| `linkedin_url` | string | 500 | full `http(s)` URL or omit (job-radar safeHref-guards it) |
| `is_agency` | bool \| null | — | agent inference (below); `null` if unsure — don't force it |
| `represents` | string[] | — | client companies named; agency → the clients, in-house → usually `[employer]` |
| `recruiter_confidence` | float | — | optional; confidence in the *extraction* (≠ email-classification confidence) |

**`is_agency` heuristic** (the agent has the sender domain + the posting company; job-radar only has
the parsed strings, so this is ours to do):
- sender domain == / a clear variant of the hiring company → in-house (`false`)
- third-party recruiting/staffing domain, or names a client distinct from the employer → agency (`true`)
- generic mailbox (gmail/outlook) with no corroborating signal → `null`

**Linkage `[D23]`:** the email-level recruiter **is** the link — no per-posting refs. On
import-and-confirm, job-radar auto-links the recruiter to jobs imported from that email. (If a single
email ever genuinely mixes recruiters — not expected for `recruiter_outreach` — we'd add an optional
`recruiter_ref` on `AgentPostingIn`. Flagged, not built.)

**Phased rollout — the agent emits BOTH from day one** (job-radar accepts both during transition):
- **Phase 1** (no wire change): nested at `raw_extracted_json.recruiter_contact` — an
  already-persisted column; `/recruiters/suggestions` reads it. Falls out of the agent's existing
  `raw_extracted_json = classification.model_dump()` for free.
- **Phase 2** (typed): top-level optional `recruiter` on `POST /agent/inbox` (§3.1). Optional ⇒
  backward-compatible; job-radar reads `recruiter` when present, else falls back to the Phase-1 key.

Emitting both simultaneously lets job-radar adopt the typed field whenever ready with **no agent
change** (answers open-Q3: yes, run both at once).

**Edge cases & trust:**
- Omit the object entirely for non-recruiter categories.
- `recruiter_outreach` with no clean posting → still send the recruiter card (nothing to link yet;
  fine — the CRM suggestion is still seeded).
- Idempotent on `(user_id, message_id)`; extraction happens on first processing only — there is **no
  re-send enrichment path** (a duplicate re-POST returns the existing row unchanged) `[§8]`.
- **Trust `[C2]`:** all fields are attacker-controlled email content. Agent: length-cap per the table,
  omit unknowns, no markup. job-radar (non-negotiable): length-cap, run `linkedin_url` through
  safeHref, escape on render, and **never auto-create** — always review-and-confirm.

**Object example (`recruiter` typed field == `raw_extracted_json.recruiter_contact`):**
```json
{
  "name": "Jane Smith", "email": "jane@agency.com", "phone": "+1 555-1212",
  "employer": "Best Recruiting", "title": "Senior Technical Recruiter",
  "linkedin_url": "https://www.linkedin.com/in/janesmith",
  "is_agency": true, "represents": ["Acme Corp"], "recruiter_confidence": 0.9
}
```

---

## 4. Writer MCP (Server 2) tool surface (job-radar implements)

Transport HTTPS (streamable). Auth: `X-Agent-Key` per user. Each tool wraps one §2.1 call.
| Tool | Maps to |
|---|---|
| `get_config()` | `GET /agent/config` |
| `get_reviews()` | `GET /agent/reviews` |
| `create_inbox_entry(payload)` | `POST /agent/inbox` |
| `record_interaction(payload)` | `POST /agent/interactions` |
| `register_hitl(hitl_id, candidates)` | `POST /agent/hitl/register` |
| `report_run(record)` | `POST /agent/runs` |

Internal MCP→tracker-api calls protected by NetworkPolicy + shared internal token; not publicly
reachable. Cloudflare→origin TLS Full (Strict). `[H3]`

---

## 5. Cross-cutting security obligations (both sides)

| # | Obligation | Owner |
|---|---|---|
| C1 | Email body treated as DATA (delimited); confidence model-justified; status subset enforced server-side | both |
| C2 | URL scheme allowlist (http/https) at write AND render; sanitize/escape all agent-derived fields; never render raw HTML email | job-radar |
| C2r | Recruiter card (§3.5) fields are agent-derived email content: job-radar length-caps each, runs `linkedin_url` through safeHref, escapes on render, and never auto-creates (review+confirm); the agent sends plain strings only (no markup/body), length-caps per the table, and omits unknowns | both |
| C3 | Email creds encrypted with dedicated `ENCRYPTION_KEY` (JR-0); decryption server-side only | job-radar |
| C4 | Slack callback signature-verified, rate-limited, ownership-validated | job-radar |
| H1 | Identity derived from API key; `user_id` never trusted from request; validate ownership of every id | job-radar |
| H2 | Email body in LLM/Langfuse disclosed or redacted; admin channel membership controlled | agent |
| H4 | Per-run email/token caps + daily spend ceiling + circuit breaker | agent |
| H5 | Minimal Gmail scope; refresh tokens strongest key tier | both |
| H6 | **Decrypted-credential transit & handling** — `GET /agent/config` returns plaintext secrets. TLS-only; agent holds them ephemerally in memory only (never log, never write to disk/checkpoint); minimize lifetime; endpoint rate-limited + audit-logged. **Cloud multi-user:** per-user isolation so one run's compromise ≠ all users' keys; consider not holding all users' creds simultaneously (fetch-per-user, discard after). | both |
| H6a | **Decrypted creds never traverse Cloudflare.** Cloud agents call `GET /agent/config` **in-cluster** (`http://tracker-api`), bypassing the Cloudflare TLS-termination edge. **Local agents don't call it at all** — they use local `.env` creds (the owner's own LLM key + Proton creds; Gmail tokens for cloud users stay in-cluster). Result/telemetry data (inbox writes, `POST /agent/runs`) carries no creds, so it uses the normal Cloudflare path. **Enforced (2026-06-12):** job-radar gates `/agent/config` to in-cluster only; the other `/agent/*` endpoints stay externally reachable via `X-Agent-Key`. | both |
| M1/M2 | No attachment parsing, no remote content fetch, agent never dereferences links | agent |
| L5 | Posting links extracted from email: scheme-allowlisted agent-side (`clean_link`, http/https only) before send AND re-validated by job-radar at write+render (C2); never dereferenced (M2); residual phishing-on-click mitigated by human review + showing host | both |

---

## 6. Observability — the two panes

### 7.1 Business pane (job-radar `GET /agent/stats`) — DERIVED, not shipped
Metrics are aggregations of rows the agent already writes; there is no separate telemetry pipe.
The local agent has no inbound connectivity — irrelevant, it only ever **pushes** results outbound.

| Metric | Source |
|---|---|
| Emails processed (today/week) | `inbox_emails.created_at` |
| Category breakdown | `inbox_emails.category` |
| Validation retry rate | `inbox_emails.validation_attempts` |
| Escalation rate | `inbox_emails.status = needs_review` |
| Jobs imported | `inbox_postings.import_status = imported` |
| Agent last run / health | latest `agent_runs` row (§1.6b) |

`GET /agent/stats`: per-user view on the user's settings page; global view on the admin page (Q8).

### 7.2 Engineering pane (Langfuse) — Option 1 (chosen)
- **One shared Langfuse project** (admin-owned). EVERY agent — your local Proton agent + all cloud
  agents — pushes traces **directly to it, outbound** (Langfuse is SaaS; source location irrelevant).
  Local agent uses Langfuse keys in its local `.env`; cloud agents use cluster-held keys (friends
  never see them). A third-party Proton self-hoster uses THEIR OWN Langfuse project / pane.
- **Every trace tagged with `user_id` + `environment`** so it's attributable/filterable.
- **Langfuse IS the admin LLM pane** — not rebuilt inside job-radar. The two panes are **cross-linked**
  via `inbox_emails.langfuse_trace_id` (click a job-radar row → its Langfuse trace). Cost/latency/
  token data stay in Langfuse only. (Option 2 — pulling Langfuse aggregates into job-radar via its
  API server-side — is deferred, not v1.)

---

## 7. Build order (who builds what, when)

1. **JR-0** credential hardening — **DONE** (precondition for `email_credentials`).
2. **JR-1** tables (§1) + migration. **JR-2** endpoints (§2). → testable via curl + `X-Agent-Key`.
3. **JR-3** Writer MCP (§4). → testable via MCP client.
4. **A-2** Email Reader MCP → **A-3** agent happy path against real endpoints.
5. **A-4** Langfuse, **A-5** notifiers + HITL, **JR-4** UI, then deploy (A-6/JR-5).

> Contract changes are PRs against THIS file first, then both sides adapt.
