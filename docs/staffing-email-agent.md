# Staffing Email Agent: design handoff

Status: agreed design, not started. Spans both repos: `job-radar` (API, models, UI, MCP) and
`job-radar-agent` (sort stage, dedup, draft stage, mail providers). The contract is
INTEGRATION_SPEC §3.9 (agent settings, §2.4). Job Radar's work list is in
`docs/agent-integration.md`.

Review complete (2026-11-13). The items previously marked **(proposed)** — the work-mode filter,
the location filter and the apply-link rule — are **decided** and written below as settled
behavior. §2.4's settings split is decided too, with `max_email_age_days` bounded by the operator
cap (see "Settings come from Job Radar").

Decisions of 2026-11-25:
- **Cloud and local ship together.** The feature is for non-Proton users too (Gmail, IMAP), so
  every step is built and tested for the cloud agent, not ported later. See "Deployment".
- **Closed jobs absorb duplicates.** A vendor email for the same req as a job you dismissed,
  rejected, or that expired is recorded as `duplicate` of that job. No new job, no draft.
- **"Not a duplicate"** on a duplicate inbox item detaches the email from the matched job and
  imports it as its own job. Filtered items get **"Import anyway"**.
- **Contract pay vs. salary** is compared as-is (hourly × 2,080 against `min_salary`); accepted.

From the backlog eval (2026-11-25, 150 Interaction emails, 88 in scope; `scripts/eval_staffing.py`):
- **Dedup thresholds: low 0.3, high 0.9, Jev ≥ 0.75 between.** Best-match similarity is bimodal
  (60 emails below 0.1, 15 at 0.9+, 8 between). No age factor.
- **"W2 or C2C" is plain `contract`.** All 5 emails the C2C filter dropped also offered W2; plain
  `contract` passes when either flavour is allowed.
- **No pitched role ⇒ out of scope** (15 of 103 recruiter emails): today's behavior, no outcome.
- **Location:** a labelled `Location:` line beats Jev's pick, and a role listed in several places
  passes the filter when any of them is allowed ("Berkeley Heights, NJ / SFO, CA").
- **Never the agency's location.** The label is read only above the signature, and a place that
  also appears in the sender's signature is dropped unless the subject or the label names it too
  (a Chicago job from a Chicago agency stays Chicago; the agency's Edison, NJ office in a short
  email's body head is not the job's location).

## Goal

Staffing vendor emails land in Interaction unread and pile up. Most are noise: onsite in
another state, contract terms that don't fit, or the same req from five vendors. This feature
turns each vendor email into a job (or attaches it to an existing one), filters out what
doesn't fit, and for strong matches leaves a threaded reply draft with the resume attached.
The user reviews and sends. Nothing is ever sent automatically.

Vendor emails carry the whole JD, which scraped postings often hide behind anti-bot pages. That
is why this feature creates jobs directly. It lifts the old "the agent never creates jobs" rule
for this feature only, and always through Job Radar's shared import function, never the DB.

Everything must be configurable through the UI. No behavior that only exists in a local
config file. Someone else self hosting Job Radar should get the same feature by flipping it on.

## Settings come from Job Radar, for local and cloud agents alike

Today the local agent reads every policy from `.env`, and only the cloud agent gets per-user
config (in cluster, because it carries decrypted secrets). This feature changes that:

- New `GET /agent/settings`: the user's non-secret settings. Same auth as the other agent
  endpoints (`X-Agent-Key` locally, internal token + user id in cloud), so it is reachable
  from a local agent. It never carries a key, password or token.
- New `GET /agent/resume`: the resume PDF, fetched only when its checksum in the settings
  changes.
- Read once at the start of each run, next to the tracked-jobs preflight. If Job Radar refuses,
  nothing is read and nothing is spent, as today.
- **All existing non-secret `.env` policies move there too:** sorter on/off and thresholds, bulk
  recruiter domains, allowed sender domains, require sender auth, zero-postings action, dedup
  window, retention days, max email age.
- `.env` keeps credentials and infrastructure: mail login, Job Radar URL and agent key, LLM key,
  Jev key, Langfuse keys, notifier tokens.
- **Operator cost guards stay operator config**, not user settings: daily spend ceiling, emails
  per run, retention cap per run, poll interval, and the cloud budget and circuit breaker. In
  cloud a user must not be able to raise their own spend.
- **`max_email_age_days` is a user setting bounded by the operator cap.** It is both a preference
  and a spend lever — widening it pulls more mail per run — so the agent applies
  `min(user_setting, operator_cap)` rather than taking the user's value outright. This keeps it in
  the settings document without letting a user raise their own spend. `dedup_window_days` is a
  pure preference and is taken as given.
- During the move, a setting missing from the response falls back to its `.env` value, then the
  built-in default. Once Job Radar ships the tab, the `.env` copies are removed from
  `.env.example`.

## Scope: which emails

The staffing step runs only on mail the sorter routes to Interaction with category
`recruiter_outreach`, and whose sender is not in the excluded sender domains list. An email with
no pitched role extracted is out of scope too and keeps today's behavior.

- Excluded sender domains: a setting, default `linkedin.com` (subdomains included).
- LinkedIn pitched roles keep today's behavior exactly: written to the app inbox, left
  unread in Interaction, no auto import, no draft. The user follows up on LinkedIn directly,
  and the real posting usually shows up later through LinkedIn anyway.
- `application_update`, `direct_message`, `connection_request`: untouched by this feature.
- Current mail only. The existing Interaction backlog is never processed for real. The backlog
  eval reads it, without touching any message.

## Deployment: cloud and local from the first release

The feature is not Proton-only. Cloud users (Gmail, Yahoo and other IMAP) get it from the first
release, so each step below is done when it works in both the local agent and `job-radar-agent
cloud`. What that adds:

- **The sorter runs in cloud.** Stage A lives in the sort stage, and the sorter has never run in
  cloud. It was gated on §3.6 Phase A+B because cloud dedup is `NullDedupStore`; job-radar #201
  shipped Phase B (the server skips a repeated `dedup_key`). Confirm that on production, then
  lift the gate.
- **`TYPESAFE_API_KEY` in the cluster.** The Jev key is system-wide pod env; it isn't there yet.
- **Settings from `/agent/settings`** are already per user in both modes (§2.4), so nothing here
  is `.env`-only.
- **Drafts on every provider.** Gmail `users.drafts.create` (inside `gmail.modify`), and IMAP
  APPEND to `\Drafts` for Yahoo and generic IMAP, verified the same way as the Bridge check.
- **Sender checks for non-Gmail users** need `trusted_authserv_ids` (§3.6), as the link picker
  does today; without it the DMARC gate fails closed and no drafts are written.

## Flow

### Stage A: in the sort stage (job-radar-agent `sort_stage.py`)

For an in scope `recruiter_outreach` email, after `build_card_and_posting`:

1. Extract fields (mostly exists in `recruiter.py` / `pay.py`): role, company, location,
   work mode, pay, employment type, the cleaned JD, and the apply link if there is one.
2. Employment type, decided in this order:
   explicit text (C2C, W2, contract to hire, full time) > hourly pay quoted means Contract >
   `is_agency` true means Contract > Full time. C2C and W2 stay distinct, as a field; an email
   naming both ("W2 or C2C") is plain Contract.
3. Work mode has three values: onsite, hybrid, remote. Job Radar replaces its `remote` boolean
   with `work_mode` and migrates existing jobs. Vague or missing work mode is left empty.
4. Location is cleaned to `City, ST`, or `ST` when only a state is named. Deterministic, no LLM.
   A labelled `Location:` line is read before Jev's pick; every place named is kept for the filter.
5. Hard filters, deterministic, no LLM. They drop clear misses only. Closer calls are left to
   the AI review, which already ranks location. A missing field never fails a filter.
   - Work mode: from Criteria `work_style`. `remote` passes remote only; `hybrid` passes remote
     and hybrid; `onsite` and `any` pass everything. The ladder is deliberately permissive at the
     `onsite` end — an onsite-preferring user should still have remote roles scored, not deleted
     before the AI review sees them.
   - Location: a remote role passes. An onsite or hybrid role passes when any of its states is in the
     user's **allowed state set**. No geocoding; commute distance stays with the AI review.
     The allowed state set is built from Criteria `home_city` **and** `search_locations`, both
     reduced to two-letter state codes, because the two fields carry different shapes
     (`"Austin, TX"` vs `["TX"]`):
     - An entry naming a city with a state (`"Austin, TX"`) contributes that state.
     - An entry that is a bare state (`"TX"`, `"Texas"`) contributes that state.
     - An entry that parses to no state is **ignored** — it never fails the filter and never
       empties the set.
     - An empty allowed state set means no location filter at all.
     Both repos implement this same reduction; it is contract, not an implementation detail.
   - Employment type: the allowed types setting (new, in the Email Agent tab).
   - Pay: the top of the annualized range (hourly × 2,080) is at least Criteria `min_salary`.
     A C2C or hourly rate carries no benefits or employer taxes, so this lets some contract
     roles through that a loaded-cost comparison would drop. Accepted: the filters only drop
     clear misses, and the AI review sees the rate.
   - Company: not in Criteria `excluded_companies`, when an end client is named.
6. Dedup (see below). On a match, this email becomes another source of the existing job.
7. If it passes filters and is not a duplicate: import it as a job, which queues the AI review.
8. The outcome is recorded on the inbox item in the same write: `filtered` (with reason),
   `duplicate` (with the matched job), `imported` (with the new job). The reason is shown in
   the app inbox, which keeps every email, with **"Import anyway"** on filtered items and
   **"Not a duplicate"** on duplicate items (see Dedup).
9. Mark the email read in the mailbox, per category setting: default read for
   `recruiter_outreach` handled by this step, unread for everything else. This replaces the
   blanket `KEEP_UNREAD` for Interaction. Nothing is lost by marking it read, since the app
   inbox has the email and its outcome.

### Apply links

Some vendor emails include an apply link. The link does not decide whether a reply makes sense;
who sent it does. A vendor's own portal or a job board listing still goes through that vendor,
so a reply is still the faster path. An in-house recruiter pointing at their own company's
careers page means the link is the real way in.

- The link is always captured and stored as the job's URL. URLs come from deterministic
  extraction (`extract.py`); Jev only picks which numbered link is the apply link. Links are
  never followed [M2], so the host of a click-tracking redirect cannot tell us where it goes.
- `is_agency` false and an apply link present: **no draft — the job is still imported.** Only the
  reply draft is skipped; filtering, dedup, import and AI review all proceed exactly as they
  otherwise would. The inbox and Slack say "apply directly".
- **Ambiguous `is_agency` resolves to `true` (agency).** The whole branch turns on one
  LLM-extracted boolean, and its two failure modes are not symmetric: a wrong `true` costs a draft
  the user deletes in seconds, while a wrong `false` silently removes a draft the user never
  learns existed. When extraction is unsure, draft.
- Otherwise drafting proceeds as usual, and the Slack message mentions the apply link.

### Stage B: draft stage (new, runs each agent run after the sort stage)

Scoring is async, so drafting is a separate pass:

1. Ask Job Radar for draft candidates: email sourced jobs where the AI review is done, score
   >= threshold, no draft yet, no application recorded, and not dismissed, rejected or expired.
2. Fetch the original email from the mailbox by Message-ID. If it is gone, skip the draft and
   say why in Slack.
3. Senders that fail the DMARC check (the link picker's `senders.py`) get no draft. The job is
   still imported; only the reply with the resume attached is withheld. Dice's relay counts as
   Dice's domain.
4. Generate the intro note (see Draft note).
5. Write a threaded plain-text reply draft to the mailbox's Drafts folder:
   `Re:` subject, `In-Reply-To` and `References` pointing at the original Message-ID,
   intro note, then the original email quoted below an "On (date), (sender) wrote:" line,
   resume PDF attached. The draft's own Message-ID is derived from the job id, so a retry
   finds the existing draft instead of writing a second one.
6. Stamp the job with the draft (vendor, time), so it never gets a second draft.
7. Slack notification (existing Slack OAuth and channel settings): job, score, vendor,
   "draft is in Drafts", and whether the note is generated or the fixed template. Settings
   decide whether Slack is used.

## Dedup

Recruiter emails rarely match scraped postings, so compare only against other email sourced
jobs.

1. Candidates: email sourced jobs from the last 30 days (window is a setting), fetched once per
   run. Jobs imported earlier in the same run are added as they are created, so two vendors
   in one batch still match.
2. Clean the text down to the JD: strip greeting, signature (reuse `signature_lines`),
   recruiter name, rate lines, and common boilerplate ("Hope you're doing well", "Please
   share your updated resume"). The cleaned JD is sent with the posting and stored, so later
   runs compare the same text.
3. Similarity: Jaccard over five word shingles of the cleaned text. Vendors paste the JD
   nearly verbatim, so true duplicates score high and different reqs score low.
   Embeddings were considered and rejected for v1: they measure meaning, so two different
   reqs with the same stack would score close and merge wrongly.
4. Gray zone between two thresholds: ask Jev a yes/no "same job?".
5. On a match, the new email is a source on the existing job.
6. For in scope mail this replaces today's company + role dedup key. Vendors write "our client"
   or their own name as the company, so that key both misses real duplicates and merges
   unrelated reqs. An exact re-send goes through the same path and is recorded as `duplicate`,
   so every in scope email gets exactly one outcome.
7. Closed jobs stay in the candidate set. A match against a job you dismissed, rejected, or
   that expired is recorded as `duplicate` of that job, attached as a source for the record,
   with a reason naming its status ("Same req as a job you dismissed on Nov 2"). No new job is
   created and no draft is written, so a closed req doesn't come back through another vendor.
8. **"Not a duplicate."** When dedup is wrong (two different reqs, often the same stack at the
   same client), this button on the duplicate inbox item detaches the email from the matched job
   and imports it as its own job. It then gets its own AI review, and a draft if it scores high
   enough. It replaces the generic "process anyway" for duplicates; a filtered item's button is
   "Import anyway".

Thresholds come from the backlog eval: **low 0.3, high 0.9**, Jev "same job?" ≥ 0.75 between
(`agent/staffing.py`: `DEDUP_LOW`, `DEDUP_HIGH`, `SAME_JOB_MIN`).

**One draft per job.** Dedup sends every later vendor email for the same req to the existing
job, and the draft stamp blocks a second draft. The job records how the user applied: the
vendor the draft went to by default, editable in the UI ("via vendor X" or "applied directly").
Either one blocks further drafts. The agent never reads the Sent folder. The draft goes to the
first vendor that emailed, not necessarily the one with the best rate.

## Draft note

- Generated by the configured LLM (`llm_litellm.py`), not Jev. Jev stays the decision maker.
- Prompt is user editable in settings. The default carries the user's writing rules: plain
  and direct, short sentences, no em dashes, no corporate filler, no sales language, no stock
  AI phrasing. Job Radar's writing skills apply to it as a new `email_draft` scope, so the
  same style blocks used elsewhere apply here.
- Inputs: role, company, location, the cleaned JD (fenced, angle brackets stripped, never the
  raw email), recruiter first name, the user's profile summary, the AI review's pros.
- The model sees attacker controlled text, and the draft goes back to that same sender. Taking
  data out is not the main risk (the profile summary and resume go to the vendor anyway). The
  risk is the note saying something the user didn't mean, such as a rate, a start date, or
  consent to a "right to represent", which can make one vendor exclusive for that client.
- So every generated note is checked by code before use: no URLs, email addresses, phone
  numbers, dollar amounts or dates; none of "represent", "authorize", "exclusive", "RTR"; at
  most about 120 words. A note that fails falls back to a fixed template (role, company,
  recruiter first name, resume attached). No retry loop, so a hostile email always ends in the
  template.
- No marker in the draft body, so nothing generated can be sent by accident. Slack, the app and
  the Langfuse trace say whether the note was generated.
- No send capability anywhere, and draft writing is not an MCP tool.

## Settings

### Profile page: resume PDF next to the text resume

The PDF upload sits next to the existing text resume field (`Profile.resume_text`), labeled
"Used by Job Radar Agent for reply drafts". It is hidden unless the agent and auto-draft are both
on. Both fields show their upload date, so a stale one is obvious. One file per user. Job Radar
does not extract text from the PDF.

### Profile page, "Email Agent" tab

Next to the existing `AIPromptsTab`:

- Enable staffing email agent (on/off), and auto-draft (on/off)
- Excluded sender domains (default `linkedin.com`)
- Allowed employment types. Work mode, locations, minimum pay and excluded companies come
  from the existing search criteria; the tab links to them instead of repeating them.
- Score threshold for drafts (default 8.5 of 10)
- Dedup window in days (default 30)
- Mark read per category (defaults above)
- Draft prompt (editable, with reset to default)
- Slack notifications on/off
- The settings that move from `.env`: sorter, email policy, retention

Mail credentials and folder settings stay where they are today.

## job-radar work items

1. Settings storage, the Email Agent tab, and `GET /agent/settings`; the moved `.env` settings.
2. `work_mode` (onsite | hybrid | remote) replaces `Job.remote`, with a data migration.
   Employment type field on inbox postings and jobs. A `cleaned JD` field on inbox postings.
3. Import endpoint: create the job, mark the posting imported, queue the AI review. One server
   function shared by the inbox UI's Add Job, the agent (through the inbox write), and the MCP.
   Idempotent on (user, Message-ID). Plus an MCP tool wrapping it.
4. Attach email as a source of an existing job, idempotent the same way; how the user applied
   (vendor or direct) on the job, editable in the UI.
5. Outcome and reason on inbox postings; the inbox UI shows them, dims filtered and duplicate
   items, and offers "Import anyway" (filtered) and "Not a duplicate" (duplicate: detach from
   the matched job, then import).
6. Email sourced job candidates for dedup, including dismissed, rejected and expired jobs with
   their status; draft candidates (never closed jobs); the draft stamp.
7. Resume PDF storage (one file per user) next to the text resume, and `GET /agent/resume`.
8. `email_draft` writing-skill scope.

## job-radar-agent work items

1. Backlog eval script (first, read only): run extraction, filters, and dedup over the
   existing Interaction backlog and print outcomes and matched pairs with scores. Pattern it
   on `scripts/eval_sorter.py`. Used to pick dedup thresholds and sanity check filters.
2. Verify Proton Bridge accepts IMAP APPEND to Drafts with an attachment. If it doesn't, the
   draft design changes, so do this before building Stage B.
3. Read settings from `GET /agent/settings`, with `.env` fallback during the move.
4. Employment type order, work mode, location cleaning, apply link pick, excluded sender
   domains, hard filters in the sort stage.
5. Dedup module (cleaning, shingles, Jaccard, Jev gray zone).
6. Outcome driven mark read, replacing the blanket `KEEP_UNREAD` for Interaction.
7. Draft write in providers: IMAP APPEND (Proton, Yahoo, generic IMAP) and Gmail drafts API.
   Drafts folder only; never an MCP tool.
8. Draft stage with checked LLM note and template fallback, DMARC gate, threading headers,
   quoted original, PDF attachment, stamp, Slack notify.
9. Cloud (see Deployment): lift the cloud sorter gate once Phase B is confirmed, put
   `TYPESAFE_API_KEY` in the cluster, and test each step under `job-radar-agent cloud`.

## Suggested order

1. Backlog eval script, and the Bridge Drafts check.
2. Settings endpoint and the move off `.env`.
3. Employment type, work mode, location, exclusions, filters, import endpoint and MCP tool.
4. Dedup.
5. Outcomes, mark read, Email Agent tab.
6. Draft stage and Slack.

Each step is done when it works in both local and cloud. Steps 1 to 3 are useful on their own
even if drafting never ships.
