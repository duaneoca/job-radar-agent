"""
Single-instance lockfile (overlap guard, D17).

Cron / the local interval loop must not start a run while a prior one is still going. We hold a
lockfile containing the owning PID. A stale lock from a dead PID is reclaimed automatically; a live
PID means "skip this run". This is best-effort single-host mutual exclusion, not distributed locking
(the cloud path uses per-user locks / k8s `activeDeadlineSeconds` instead).
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path


class LockHeld(Exception):
    """Raised when another live run already holds the lock."""


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by someone else
    return True


@contextmanager
def run_lock(path: str = "/tmp/job-radar-agent.lock"):
    p = Path(path)
    if p.exists():
        try:
            owner = int(p.read_text().strip() or "0")
        except ValueError:
            owner = 0
        if owner and _pid_alive(owner):
            raise LockHeld(f"run already in progress (pid {owner})")
        # stale lock from a dead process — reclaim it

    p.write_text(str(os.getpid()))
    try:
        yield
    finally:
        # only remove if we still own it
        try:
            if p.exists() and p.read_text().strip() == str(os.getpid()):
                p.unlink()
        except OSError:
            pass
