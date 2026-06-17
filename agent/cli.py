"""
`job-radar-agent` CLI — the console entry point installed by pip/pipx.

    job-radar-agent run   [--once] [--dry-run]   # local: interval loop (default) / single pass
    job-radar-agent cloud [--dry-run]            # multi-user cloud run (the k8s CronJob CMD)
    job-radar-agent doctor                        # preflight checklist
    job-radar-agent version

launchd runs `job-radar-agent run --once` every 15 min. Config/state resolve via agent.paths
(AGENT_HOME → ~/Library/Application Support/JobRadarAgent on macOS).
"""

from __future__ import annotations

import sys

__version__ = "0.1.0"

_USAGE = "usage: job-radar-agent {run|cloud|doctor|version} [--once] [--dry-run]"


def _run(argv: list[str]) -> int:
    from .bootstrap import build_components
    from .config import settings
    from .loop import run_loop
    from .runner import run_once
    once = "--once" in argv
    dry_run = "--dry-run" in argv
    print(f"agent {'DRY-RUN' if dry_run else 'COMMIT'} "
          f"({'once' if once else f'loop every {settings.poll_interval_seconds}s'})")
    run_loop(once=once, dry_run=dry_run, interval=settings.poll_interval_seconds,
             build_components=build_components, run_once=run_once)
    return 0


def _cloud(argv: list[str]) -> int:
    import os
    from .budget import DailySpendStore
    from .cloud import CloudConfigClient, cloud_run
    from .config import make_notifier, settings
    from .prompts import SeedPromptProvider
    from mcp_email.config import settings as email_settings
    token = os.environ.get("AGENT_INTERNAL_TOKEN", "")
    if not token:
        print("✗ AGENT_INTERNAL_TOKEN not set"); return 2
    dry_run = "--dry-run" in argv
    cc = CloudConfigClient(settings.jobradar_api_url, token)
    try:
        s = cloud_run(cc, base_url=settings.jobradar_api_url, internal_token=token,
                      prompts=SeedPromptProvider(), notifier=make_notifier(),
                      since_days=email_settings.max_email_age_days or None,
                      limit=email_settings.max_emails_per_run, dry_run=dry_run,
                      daily_ceiling=settings.daily_spend_ceiling_usd, spend_store=DailySpendStore())
    finally:
        cc.close()
    print(f"users={s['users']} processed={s['processed']} escalations={s['escalations']} "
          f"errors={len(s['errors'])}" + (f" stopped={s['stopped']}" if s["stopped"] else ""))
    for e in s["errors"]:
        print("  ! " + e)
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    cmd = argv[0] if argv else "run"
    rest = argv[1:]
    if cmd == "run":
        return _run(rest)
    if cmd == "cloud":
        return _cloud(rest)
    if cmd == "doctor":
        from .doctor import run as doctor_run
        return doctor_run()
    if cmd in ("version", "--version", "-v"):
        print(f"job-radar-agent {__version__}")
        return 0
    print(_USAGE)
    return 2 if cmd in ("-h", "--help", "help") else 1


if __name__ == "__main__":
    raise SystemExit(main())
