#!/usr/bin/env bash
# Wrapper launchd runs every interval: load .env and fire one agent pass.
# Resolves the repo root from this script's location, so it's path-portable.
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO"
set -a; source .env; set +a
exec .venv/bin/python scripts/run_loop.py --once "$@"
