"""
Interval scheduler — the local self-host entrypoint (the agent container's CMD).

Runs the agent every POLL_INTERVAL_SECONDS, lock-gated (overlapping runs skip). Scheduler triggers,
LangGraph orchestrates one run; the cloud path uses a k8s CronJob with --once instead.

    python scripts/run_loop.py            # loop forever (container default), COMMITS
    python scripts/run_loop.py --once     # single run then exit (cron-style)
    python scripts/run_loop.py --dry-run  # observe without moving/writing
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.bootstrap import build_components   # noqa: E402
from agent.config import settings              # noqa: E402
from agent.loop import run_loop                # noqa: E402
from agent.runner import run_once              # noqa: E402


def main(argv) -> int:
    once = "--once" in argv
    dry_run = "--dry-run" in argv
    mode = "DRY-RUN" if dry_run else "COMMIT"
    print(f"agent loop ({mode}, every {settings.poll_interval_seconds}s, "
          f"provider={os.environ.get('EMAIL_PROVIDER', 'proton')})")
    run_loop(once=once, dry_run=dry_run, interval=settings.poll_interval_seconds,
             build_components=build_components, run_once=run_once)
    print("agent loop stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
