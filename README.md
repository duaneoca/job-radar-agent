# Job Radar Email Agent

[![CI](https://github.com/duaneoca/job-radar-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/duaneoca/job-radar-agent/actions/workflows/ci.yml)

Turns job-alert emails (LinkedIn, Glassdoor, Indeed, Monster, …) into postings in
[Job Radar](https://job-radar.net)'s Inbox — for about **$0.003 per email**.

A mail filter (e.g. a Proton sieve rule) sorts job alerts into a `Postings` folder. For each unread
email there, the agent:

1. **Screens the sender** — domain allow-list + DMARC check. No LLM spend on anything suspicious.
2. **Extracts every link** from the HTML into a short numbered list (text, safe URL, nearby text).
3. **Asks an LLM once** which numbers are job postings, and for each one's title and company.
4. **Verifies deterministically** that every title and company really appears next to that link.
   Mistakes get up to two retries with corrective instructions only — the wrong answer is never
   fed back. Anything still wrong goes to an `Unprocessed` folder.
5. **Writes new postings** (duplicates across emails suppressed) and marks the email read.

The model never produces a URL and never sees the raw email — it only chooses from links the code
extracted, and everything it says is checked against the email.

```
Postings folder (Proton Bridge IMAP | Gmail API | IMAP)
  → screen (allow-list + DMARC) → extract links → LLM pick → verify ⟲ ×3
  → dedup → POST /agent/inbox → mark read          (or → Unprocessed)
Tracing: Langfuse.  Orchestration: LangGraph.  Mailbox access: MCP Server 1 (stdio) or in-process.
```

## History
**V1** (tag [`v1-final`](https://github.com/duaneoca/job-radar-agent/tree/v1-final)) classified
every job-related email with an LLM and audited it with a second "critic" call. It worked, but cost
~$1/email, ran slowly, and duplicated what a mail filter already does. The tag message has the full
retrospective.

## Setup
**Local (Proton, macOS):** [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) — pipx install, config in
`~/Library/Application Support/JobRadarAgent/.env`, launchd schedule. `job-radar-agent doctor`
preflights everything; `python scripts/run_local.py` does a no-changes dry run with a per-email report.

**Cloud (multi-user):** one GHCR image; a k8s CronJob runs `job-radar-agent cloud`. Per-user
credentials and settings come from Job Radar in-cluster. See `INTEGRATION_SPEC.md` §2.1b and §3.6.

More: `CLAUDE.md` (architecture + conventions), `INTEGRATION_SPEC.md` (contract with Job Radar),
`SECURITY.md` (threat model).
