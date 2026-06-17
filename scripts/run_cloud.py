"""
Cloud multi-user entrypoint — the k8s CronJob CMD (`python scripts/run_cloud.py`).

Processes every enabled cloud user once, then exits (CronJob owns the schedule; set
concurrencyPolicy: Forbid + activeDeadlineSeconds so runs don't overlap). Reads per-user config
in-cluster via X-Internal-Token; never holds more than one user's creds at a time.

Env: JOBRADAR_API_URL (in-cluster tracker-api), AGENT_INTERNAL_TOKEN, plus Langfuse + admin-notifier
creds + run caps. Per-user LLM/email creds come from /agent/cloud/config, NOT env.

    python scripts/run_cloud.py            # process all enabled users (real)
    python scripts/run_cloud.py --dry-run  # classify + report, no moves/writes
"""

import os
import signal
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _term_to_exit(signum, _frame):
    # k8s activeDeadlineSeconds sends SIGTERM, which by default kills WITHOUT running finally blocks.
    # Convert it to SystemExit so the in-flight run_once finalizes its agent_runs record before exit.
    raise SystemExit(f"received signal {signum}")

from agent.budget import DailySpendStore               # noqa: E402
from agent.cloud import CloudConfigClient, cloud_run   # noqa: E402
from agent.config import make_notifier, settings        # noqa: E402
from agent.prompts import SeedPromptProvider            # noqa: E402
from mcp_email.config import settings as email_settings  # noqa: E402


def main(argv) -> int:
    base = settings.jobradar_api_url
    token = os.environ.get("AGENT_INTERNAL_TOKEN", "")
    if not token:
        print("✗ AGENT_INTERNAL_TOKEN not set"); return 2

    signal.signal(signal.SIGTERM, _term_to_exit)
    signal.signal(signal.SIGINT, _term_to_exit)

    dry_run = "--dry-run" in argv
    cc = CloudConfigClient(base, token)
    print(f"cloud run ({'DRY-RUN' if dry_run else 'COMMIT'}) → {base}")
    try:
        s = cloud_run(
            cc, base_url=base, internal_token=token,
            prompts=SeedPromptProvider(), notifier=make_notifier(),
            since_days=email_settings.max_email_age_days if email_settings.max_email_age_days > 0 else None,
            limit=email_settings.max_emails_per_run, dry_run=dry_run,
            daily_ceiling=settings.daily_spend_ceiling_usd, spend_store=DailySpendStore(),
        )
    finally:
        cc.close()
    print(f"users={s['users']} processed={s['processed']} escalations={s['escalations']} "
          f"errors={len(s['errors'])}" + (f" stopped={s['stopped']}" if s["stopped"] else ""))
    for e in s["errors"]:
        print("  ! " + e)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
