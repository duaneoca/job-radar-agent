"""
Where the installed agent finds its config + state.

A pipx-installed CLI has no repo directory, and a launchd job / a shell command run from anywhere
have different working directories — so config + state must resolve to a deterministic location, not
the CWD.

  AGENT_HOME  (per-user config/state dir):
    • $AGENT_HOME if set
    • macOS:  ~/Library/Application Support/JobRadarAgent
    • else:   ${XDG_CONFIG_HOME:-~/.config}/job-radar-agent

  .env       → <AGENT_HOME>/.env        (but a local ./.env wins, so dev-from-repo is unchanged)
  data/      → <AGENT_HOME>/data         (spend.json, checkpoints)
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def agent_home() -> Path:
    if os.environ.get("AGENT_HOME"):
        return Path(os.environ["AGENT_HOME"]).expanduser()
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "JobRadarAgent"
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "job-radar-agent"


def env_file() -> str:
    """Path pydantic-settings loads. Local ./.env (dev/repo) takes precedence over the install dir."""
    local = Path(".env")
    return str(local if local.is_file() else agent_home() / ".env")


def data_dir() -> Path:
    """Per-user state dir (created on demand — NOT at import time)."""
    d = agent_home() / "data"
    d.mkdir(parents=True, exist_ok=True)
    return d
