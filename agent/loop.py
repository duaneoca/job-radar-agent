"""
Interval scheduler core (testable). The script `scripts/run_loop.py` is a thin wrapper.

Dependencies (build/run/sleep) are injectable so the loop is unit-testable without real creds.
"""

from __future__ import annotations

import signal
import time
from typing import Callable

from .prompts import SeedPromptProvider

_STOP = {"flag": False}


def _install_signals():
    def handle(signum, _frame):
        _STOP["flag"] = True
    try:
        signal.signal(signal.SIGTERM, handle)
        signal.signal(signal.SIGINT, handle)
    except ValueError:
        pass  # not in main thread (e.g. tests)


def run_loop(*, once: bool, dry_run: bool, interval: int,
             build_components: Callable, run_once: Callable,
             sleep: Callable[[float], None] = time.sleep,
             install_signals: bool = True) -> int:
    if install_signals:
        _install_signals()
    components = build_components()
    iterations = 0
    try:
        while True:
            res = run_once(
                reader=components.reader, writer=components.writer,
                llm=components.llm, critic_llm=components.critic_llm,
                prompts=SeedPromptProvider(), notifier=components.notifier,
                inbox_base_url=components.inbox_base_url, environment="local", dry_run=dry_run,
            )
            iterations += 1
            ts = time.strftime("%H:%M:%S")
            if getattr(res, "skipped", False):
                print(f"[{ts}] skipped (lock held)")
            else:
                print(f"[{ts}] {res.status} processed={res.emails_processed} "
                      f"postings={res.postings_created} escalations={res.escalations}")
            if once or _STOP["flag"]:
                break
            for _ in range(interval):
                if _STOP["flag"]:
                    break
                sleep(1)
            if _STOP["flag"]:
                break
    finally:
        components.close()
    return iterations
