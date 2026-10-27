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

## Pinned dependencies — read before upgrading anything
**`litellm` is pinned EXACTLY (`==1.106.2`) on purpose. Do not loosen or "update" it casually.**
LiteLLM's PyPI releases were compromised twice in 2026 — **1.82.7 / 1.82.8 on 2026-03-24** and
**1.107.0 on 2026-10-21** (plus the Docker tags `latest` / `main-latest` / `main-stable`). Both
shipped a credential stealer as a `.pth` / `sitecustomize` file, which Python runs on *every* start,
before any of our code. The cloud agent holds decrypted Gmail/Slack tokens and every user's AI key,
so a poisoned build is the worst place for it. Until 2026-10-24 this repo declared `litellm>=1.50.0`
and the image build installed whatever was newest that day; the 2026-10-23 image only got the clean
1.106.2 because no build ran inside the two-hour window on 10/21. 1.106.3+ were published after the
incident from a pipeline whose fixes LiteLLM hasn't described yet, with no PyPI provenance
attestations — move off 1.106.2 only after reviewing that, deliberately, in its own PR.

How the build only installs bytes we've already seen:
- **`requirements.lock`** — every runtime dependency, exact version + sha256 hashes (resolved with
  `uv pip compile --universal --generate-hashes`, constrained to the package set of the known-good
  2026-10-23 image). **`requirements-build.lock`** — setuptools/wheel for building our own wheel.
- **`Dockerfile`** installs both with `pip install --require-hashes`: an unknown file is a build
  failure, not an install. Build tools live only in the builder stage.
- **CI** (`ci.yml` on every PR, `publish.yml` before every push) runs
  `scripts/check_site_packages.py` inside the built image: it fails on any unexpected `.pth`, any
  `sitecustomize.py` / `usercustomize.py` / `cook.py`, or if litellm isn't the locked version.
  A clean image has **no** `.pth` files; allow one only after review (`EXPECTED_PTH`).

Changing a dependency: edit `pyproject.toml`, then regenerate the lock and review the diff line by line:
```
uv pip compile pyproject.toml --universal --python-version 3.11 --generate-hashes --no-annotate \
  --custom-compile-command "see README.md › Pinned dependencies" -o requirements.lock
```
(without the image constraints, this re-resolves *everything* to today's newest — prefer
`--upgrade-package <name>` for a single, intentional bump). Local install, held to the same versions:
`uv tool install --force --editable . -c requirements.lock`.
