"""Thin wrapper — preflight check. Prefer `job-radar-agent doctor` once installed."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.doctor import run  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(run())
