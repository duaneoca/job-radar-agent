"""
Local end-to-end runner with a readable per-email report (the CLI `run --once` prints a summary only).

DRY-RUN by default: sorts the root folder (when SORTER_ENABLED), extracts links, calls the LLM,
verifies, counts what retention WOULD move to Trash, and prints what WOULD happen — but marks nothing,
moves nothing, trashes nothing, writes nothing, and records no duplicate keys. Pass --commit to act.

In a dry run the sorter's decisions are only reported, so mail it would file into Postings is not yet
there for the postings stage to see.

Config comes from the same `.env` as the agent (see docs/DEPLOYMENT.md).

Usage:
    python scripts/run_local.py            # dry run
    python scripts/run_local.py --commit   # for real
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.bootstrap import build_components   # noqa: E402
from agent.prompts import SeedPromptProvider   # noqa: E402
from agent.runner import run_once              # noqa: E402
from mcp_email.config import folders           # noqa: E402


def main(argv) -> int:
    commit = "--commit" in argv
    c = build_components()
    mode = "COMMIT" if commit else "DRY-RUN"
    print(f"=== {mode} — {'sorting ' + folders.root + ', then ' if c.sort_stage else ''}"
          f"reading {folders.source} → {c.inbox_base_url} ===")
    try:
        res = run_once(
            reader=c.reader, writer=c.writer, llm=c.llm, prompts=SeedPromptProvider(),
            policy=c.policy, dedup=c.dedup, zero_postings_action=c.zero_postings_action,
            notifier=c.notifier, inbox_base_url=c.inbox_base_url, environment="local",
            dry_run=not commit, spend_key="local", daily_ceiling=c.daily_ceiling,
            spend_store=c.spend_store, sort_stage=c.sort_stage,
            retention_stage=c.retention_stage,
        )
    finally:
        c.close()

    if res.skipped:
        print("• skipped (another run holds the lock)")
        return 0
    if c.sort_stage:
        print(f"\n— sort ({folders.root}) —")
        for d in res.sort_details:
            tag = d["folder"] + (f" via {d['rule']}" if d["rule"] else "")
            card = d["card"]
            extra = (f"  card: {', '.join(f'{k}={v}' for k, v in card.items())}" if card else "")
            print(f"  [{tag:22}] {d['category']:<19} {d['confidence']:.2f}  {d['subject']!r}"
                  + (f"  ⚠ {d['reason']}" if d["reason"] else "") + extra)
        print(f"  sorted: {res.sorted}  inbox rows: {res.interactions_recorded}")
        print(f"\n— postings ({folders.source}) —")
    for d in res.details:
        print(f"  [{d['outcome'] or 'ERR':15}] postings={d['postings']:<3} dup={d['duplicates']:<3} "
              f"tries={d['attempts']}  {d['subject']!r}" + (f"  ⚠ {d['reason']}" if d["reason"] else ""))
    print(f"\n{mode} summary: status={res.status} emails={res.emails_processed} "
          f"postings={res.postings_created} duplicates={res.duplicates_skipped} "
          f"unprocessed={res.escalations} retries={res.retries} "
          f"llm_cost=${getattr(c.llm, 'run_cost', 0.0):.4f}"
          + (f" jev_cost=${c.sort_stage.jev.run_cost:.5f}" if c.sort_stage else ""))
    if c.retention_stage:
        verb = "moved to Trash" if commit else "WOULD move to Trash"
        print(f"\n— retention — {verb}: {res.trashed or 'nothing'} "
              f"(older than {c.retention_stage.policy.days} days, not starred)")
    for e in res.errors:
        print("  ! " + e)
    if not commit:
        print("\n(nothing marked, moved, or written — re-run with --commit to act)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
