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
                llm=components.llm, prompts=SeedPromptProvider(),
                policy=components.policy, dedup=components.dedup,
                zero_postings_action=components.zero_postings_action,
                notifier=components.notifier,
                # V1 bug: the scheduled path never passed these, so the daily $ ceiling was
                # silently NOT enforced under launchd. Always pass them.
                spend_key="local", daily_ceiling=components.daily_ceiling,
                spend_store=components.spend_store,
                inbox_base_url=components.inbox_base_url, environment="local", dry_run=dry_run,
                sort_stage=getattr(components, "sort_stage", None),
                retention_stage=getattr(components, "retention_stage", None),
            )
            iterations += 1
            ts = time.strftime("%H:%M:%S")
            if getattr(res, "skipped", False):
                print(f"[{ts}] skipped (lock held)")
            else:
                print(f"[{ts}] {res.status} emails={res.emails_processed} "
                      f"postings={res.postings_created} duplicates={res.duplicates_skipped} "
                      f"unprocessed={res.escalations} retries={res.retries}"
                      + (f" sorted={res.sorted}" if getattr(res, "sorted", None) else "")
                      + (f" trashed={res.trashed}" if getattr(res, "trashed", None) else ""))
                for e in res.errors[:10]:
                    print(f"    ! {e}")
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
