# Deploying the Job Radar Email Agent locally (Proton, macOS)

This is the runbook for running the agent on your own Mac against **Proton Mail Bridge**, scheduled
by **launchd**, installed as a Python app via **pipx**. It's written to be followed start-to-finish
by you today and by a future self-hoster later.

> **Why this shape?** Proton Bridge binds to `127.0.0.1`, so the agent must run on the same machine
> as Bridge (native, not in a container — see `README`/the design notes for the Docker tradeoffs).
> We use the OS-native tools: **pipx** for the app + its venv + updates, **launchd** for scheduling,
> and **`~/Library/Application Support/JobRadarAgent/`** for durable config + state.

---

## 0. What you'll end up with

| Thing | Location | Notes |
|---|---|---|
| Code | pipx venv (`~/.local/pipx/venvs/job-radar-agent`) | replaced on every `pipx upgrade` |
| CLI binary | `~/.local/bin/job-radar-agent` | on your `PATH` |
| **Secrets** | `~/Library/Application Support/JobRadarAgent/.env` (chmod 600) | **durable** — survives upgrades |
| State (spend/lock) | `~/Library/Application Support/JobRadarAgent/data/` | durable |
| Schedule | `~/Library/LaunchAgents/com.jobradar.emailagent.plist` | launchd job, every 15 min |
| Logs | `~/Library/Logs/jobradar-emailagent.log` | |

---

## 1. Prerequisites

- **Proton Mail Bridge** installed and **running**, logged into your account. Note its IMAP host/port
  (default `127.0.0.1:1143`) and the **Bridge-specific password** (Bridge → Settings → the account →
  IMAP/SMTP; this is NOT your Proton login password).
- In your Proton mailbox, create the funnel folder + four sub-folders, e.g.:
  `Hire Duane`, `Hire Duane/Interaction`, `Hire Duane/Postings`, `Hire Duane/Social`,
  `Hire Duane/Unprocessed`. (Over IMAP, Proton namespaces these under `Folders/`, so the root is
  `Folders/Hire Duane` — the doctor will confirm the exact names.)
- A **Job Radar account**, and an **agent API key** minted in Settings → Email Agent (or via the API).
- An **LLM API key** for your chosen provider (Anthropic / OpenAI / Google / Groq — BYOK).
- **pipx**: `brew install pipx && pipx ensurepath` (restart your shell after).
- *(optional)* Langfuse keys (tracing) and a Slack bot token + channel (notifications).

---

## 2. Install the app (pipx)

```bash
pipx install "git+https://github.com/duaneoca/job-radar-agent"
job-radar-agent version          # confirms the binary is on PATH
```

(For a local checkout instead: `pipx install /path/to/job-radar-agent`.)

---

## 3. Create the config dir + .env

```bash
HOME_DIR="$HOME/Library/Application Support/JobRadarAgent"
mkdir -p "$HOME_DIR/data"
# Grab the template from the repo (or copy from the editable checkout):
curl -fsSL https://raw.githubusercontent.com/duaneoca/job-radar-agent/main/.env.example \
  -o "$HOME_DIR/.env"
chmod 600 "$HOME_DIR/.env"
$EDITOR "$HOME_DIR/.env"
```

Fill in (`.env.example` documents every key):

```ini
EMAIL_PROVIDER=proton
EMAIL_ROOT_FOLDER=Folders/Hire Duane           # quote — it has a space
PROTON_IMAP_HOST=127.0.0.1
PROTON_IMAP_PORT=1143
PROTON_IMAP_USER=you@proton.me
PROTON_IMAP_PASSWORD=<bridge-specific-password>

LLM_PROVIDER=anthropic
LLM_MODEL=claude-haiku-4-5
LLM_API_KEY=<your provider key>

JOBRADAR_API_URL=https://staging.job-radar.net/api    # → https://job-radar.net/api when you go live
AGENT_API_KEY=<minted in Job Radar settings>

MAX_EMAILS_PER_RUN=25            # keeps each run ~2-3 min; drains a backlog over several runs
MAX_EMAIL_AGE_DAYS=14
DAILY_SPEND_CEILING_USD=5.00     # 0 = disabled

# optional
LANGFUSE_HOST=https://us.cloud.langfuse.com
LANGFUSE_PUBLIC_KEY=...
LANGFUSE_SECRET_KEY=...
NOTIFIER=slack
SLACK_BOT_TOKEN=xoxb-...
SLACK_USER_CHANNEL=#your-channel     # invite the bot to it
```

> **How config is found:** the agent resolves `AGENT_HOME` (defaults to the path above on macOS), and
> reads `<AGENT_HOME>/.env`. A local `./.env` takes precedence when you run from a repo checkout, so
> development is unaffected.

---

## 4. Preflight

```bash
AGENT_HOME="$HOME/Library/Application Support/JobRadarAgent" job-radar-agent doctor
```

It checks: Bridge login, the folders exist, LLM key set, Job Radar reachable + key valid, and the
optional integrations. **Fix any ✗ before scheduling.**

---

## 5. First real run (supervised), then schedule

Do one **small, supervised** commit before handing it to launchd:

```bash
AGENT_HOME="$HOME/Library/Application Support/JobRadarAgent" \
  MAX_EMAILS_PER_RUN=5 job-radar-agent run --once         # real: moves 5 emails + writes + Slack
```

Watch: 5 emails move to their sub-folders, rows appear in Job Radar, Slack pings (if configured).
Use `--dry-run` first if you want a no-mutation preview.

Then install the schedule:

```bash
# Edit the plist's paths if your username/pipx-bin differ, then:
cp deploy/local/com.jobradar.emailagent.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.jobradar.emailagent.plist
launchctl print gui/$(id -u)/com.jobradar.emailagent | grep -E "state|last exit"   # status
```

It runs at load and every 15 minutes thereafter.

---

## 6. Operating it

```bash
# tail logs
tail -f ~/Library/Logs/jobradar-emailagent.log

# run one pass right now (force)
launchctl kickstart -k gui/$(id -u)/com.jobradar.emailagent

# status
launchctl print gui/$(id -u)/com.jobradar.emailagent

# stop / start (unload / reload)
launchctl bootout  gui/$(id -u)/com.jobradar.emailagent
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.jobradar.emailagent.plist
```

- **Engineering view:** Langfuse (per-email traces, cost, latency).
- **Business view:** Job Radar inbox + your Slack pings.

---

## 7. Updating

```bash
pipx upgrade job-radar-agent
```

That swaps **only the code** — your `.env`, spend history, and schedule are untouched. Because each
run is a fresh `--once` process, the next 15-min fire uses the new code automatically; no reload
needed unless the plist itself changed.

---

## 8. Uninstall

```bash
launchctl bootout gui/$(id -u)/com.jobradar.emailagent
rm ~/Library/LaunchAgents/com.jobradar.emailagent.plist
pipx uninstall job-radar-agent
# secrets/state remain until you remove them:
# rm -rf "$HOME/Library/Application Support/JobRadarAgent"
```

---

## 9. Troubleshooting

- **doctor: "email provider login" ✗** — Bridge isn't running, wrong port, or wrong Bridge password.
  Confirm Bridge is up and the IMAP password matches Bridge → Settings.
- **doctor: folder ✗** — create the missing folder/label in Proton (the agent never creates folders).
- **"Job Radar reachable" ✗** — check `JOBRADAR_API_URL` and that the agent key is valid/not revoked.
- **A run takes a long time** — it's processing the unread backlog (up to `MAX_EMAILS_PER_RUN`). Lower
  the cap; it drains over several runs. (A genuine network stall is bounded by built-in 30s IMAP /
  60s LLM timeouts — a stuck run fails fast and releases the lock rather than hanging.)
- **Runs seem skipped** — overlapping runs are guarded by a lockfile; if a previous run is still going
  the next fire is skipped (by design). A crashed run's stale lock is auto-reclaimed next fire.
- **Laptop asleep** — the agent only runs while the Mac is awake; missed runs just mean a slightly
  larger queue next time (the age cap + per-run cap keep it bounded).
- **Cost** — `DAILY_SPEND_CEILING_USD` caps daily LLM spend per the BYOK key; a run that would exceed
  it is skipped/halted. Watch the first day during a backlog drain.

---

## 10. Going to production

When you're comfortable after a few supervised cycles on **staging**, flip `JOBRADAR_API_URL` to
`https://job-radar.net/api` in `<AGENT_HOME>/.env` and `launchctl kickstart` once to confirm. No code
change needed.

---

## Notes for a future self-hoster
- **Gmail** instead of Proton: set `EMAIL_PROVIDER=gmail` and run the one-time OAuth (`scripts/gmail_auth.py`);
  no Bridge needed. (Most non-Proton users go through Job Radar's cloud path instead.)
- **Linux**: same pipx model; replace launchd with a **systemd user timer** (or cron) running
  `job-radar-agent run --once`. Config dir is `~/.config/job-radar-agent/` (XDG). Proton on headless
  Linux additionally needs a headless Bridge (keyring) — see the design notes.
- Security posture: `SECURITY.md`. Threat model + go-live checklist: `READINESS.md`.
