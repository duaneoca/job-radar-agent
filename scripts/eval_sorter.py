"""
Offline accuracy check for the Jev sorter, using your ALREADY-SORTED folders as ground truth.

READ-ONLY: folders are opened with IMAP EXAMINE / Gmail list, bodies are fetched with BODY.PEEK[] —
no flag changes, no moves, no writes to Job Radar. The only external call is Jev (trimmed From /
Subject / body per email; see agent/sorter.py).

For each of Interaction / Postings / Social it samples the N most recent messages, asks Jev, and
reports: the raw confusion matrix (Jev's top choice, no threshold), then — for a grid of confidence
thresholds — how much mail would be auto-routed (coverage) and how much of that was right (accuracy).

Usage:
    python scripts/eval_sorter.py                  # 25 per folder
    python scripts/eval_sorter.py --per-folder 60
    python scripts/eval_sorter.py --show-misses    # also print subject/sender of misroutes (terminal only)
"""

import argparse
import os
import sys
import time
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.bootstrap import _build_provider            # noqa: E402
from agent.config import bulk_recruiter_domains, make_jev, settings            # noqa: E402
from agent.reader import to_ref                        # noqa: E402
from agent.sorter import classify, decide              # noqa: E402
from mcp_email.config import folders                   # noqa: E402

TRUTH = {"interaction": folders.interaction, "postings": folders.postings, "social": folders.social}
DESTS = ["interaction", "postings", "social", "unprocessed"]
THRESHOLDS = [0.5, 0.6, 0.7, 0.8, 0.85, 0.9, 0.95]


def main(argv) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--per-folder", type=int, default=25)
    ap.add_argument("--show-misses", action="store_true")
    args = ap.parse_args(argv)

    if not settings.typesafe_api_key:
        raise SystemExit("✗ TYPESAFE_API_KEY not set")
    provider, jev = _build_provider(), make_jev()
    rows = []   # (truth, answer, email)
    try:
        for truth, folder in TRUTH.items():
            msgs = provider.get_recent(folder, args.per_folder)
            print(f"• {folder}: {len(msgs)} messages")
            for m in msgs:
                ref = to_ref(m)
                t0 = time.monotonic()
                ans = classify(jev, ref)
                rows.append((truth, ans, ref, time.monotonic() - t0))
    finally:
        provider.close() if hasattr(provider, "close") else None
        jev.close()

    if not rows:
        print("no messages sampled")
        return 1

    bulk = bulk_recruiter_domains()

    def route(a, ref, th):
        return decide(a, ref["sender"], min_confidence=th, min_margin=settings.sorter_min_margin,
                      bulk_domains=bulk)

    # Raw confusion: truth folder × routed folder with no threshold (sender rule applied).
    print("\nRaw confusion (rows = folder it's in, cols = where it would go, no threshold):")
    print(f"{'':14}" + "".join(f"{d:>13}" for d in DESTS))
    for truth in TRUTH:
        c = Counter(decide(a, ref["sender"], min_confidence=0, min_margin=0,
                           bulk_domains=bulk).folder for t, a, ref, _ in rows if t == truth)
        print(f"{truth:14}" + "".join(f"{c[d]:>13}" for d in DESTS))

    print("\nBy category:", dict(Counter(a.choice for _, a, _, _ in rows)))

    print(f"\n{'min_conf':>9} {'coverage':>9} {'accuracy':>9}   (margin ≥ {settings.sorter_min_margin})")
    for th in THRESHOLDS:
        routed = [(t, route(a, ref, th)) for t, a, ref, _ in rows]
        auto = [(t, d) for t, d in routed if d.folder != "unprocessed"]
        acc = sum(t == d.folder for t, d in auto) / len(auto) if auto else 0.0
        mark = "  ← current" if abs(th - settings.sorter_min_confidence) < 1e-9 else ""
        print(f"{th:>9.2f} {len(auto) / len(rows):>8.0%} {acc:>9.1%}{mark}")

    lat = sorted(r[3] for r in rows)
    print(f"\nJev: {jev.calls} calls, {jev.input_tokens} input tokens, ${jev.run_cost:.5f}, "
          f"median latency {lat[len(lat) // 2] * 1000:.0f} ms")

    if args.show_misses:
        decided = [(t, a, ref, route(a, ref, settings.sorter_min_confidence))
                   for t, a, ref, _ in rows]
        print("\nMisroutes at the current threshold:")
        for t, a, ref, d in decided:
            if d.folder not in (t, "unprocessed"):
                via = f" via {d.rule}" if d.rule else ""
                print(f"  [{t} → {d.folder} ({a.choice}{via} {d.confidence:.2f})] "
                      f"{ref['subject'][:70]!r}  — {ref['sender'][:50]}")
        print("\nHeld for review (→ Unprocessed) at the current threshold:")
        for t, a, ref, d in decided:
            if d.folder == "unprocessed":
                top = sorted(a.probabilities.items(), key=lambda kv: -kv[1])[:2]
                print(f"  [{t}: {d.reason}; " + ", ".join(f"{k} {v:.2f}" for k, v in top) + "] "
                      f"{ref['subject'][:60]!r}  — {ref['sender'][:45]}")
    print("\n(read-only: nothing was marked, moved, or written)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
