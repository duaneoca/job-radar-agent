"""
Backlog eval for the staffing email agent (INTEGRATION_SPEC §3.9, design: docs/staffing-email-agent.md).

READ-ONLY, like scripts/eval_sorter.py: the Interaction folder is opened with IMAP EXAMINE / Gmail
list, bodies are fetched with BODY.PEEK[], and nothing is marked, moved, or written to Job Radar.
The only external calls are Jev: the sorter's category question (to keep `recruiter_outreach`),
the recruiter card + pitched-role question, and — with --jev-gray — "same job?" on gray-zone pairs.

What it reports, for picking the dedup thresholds and sanity-checking the filters:
  1. field coverage — how often work mode, location, pay and a named client are found
  2. outcomes — imported / duplicate / filtered (with reasons), simulated in arrival order:
     filter first, then dedup against the jobs "imported" so far, as Stage A will
  3. similarity — each email's best Jaccard match among earlier in-scope emails, as a histogram,
     and the top pairs side by side (terminal only), so the high / low cutoffs can be read off

Criteria come from a JSON file in the shape of `staffing.criteria` (§2.4), because Job Radar's
/agent/settings doesn't exist yet. Without it, only the employment-type filter applies.

Usage:
    python scripts/eval_staffing.py                               # 100 most recent Interaction emails
    python scripts/eval_staffing.py --limit 300 --criteria criteria.json
    python scripts/eval_staffing.py --high 0.6 --low 0.25 --jev-gray --pairs 40
"""

import argparse
import json
import os
import sys
from collections import Counter
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.bootstrap import _build_provider            # noqa: E402
from agent.config import make_jev, settings            # noqa: E402
from agent.jev import JevError, noul                   # noqa: E402
from agent.pay import parse_pay                        # noqa: E402
from agent.reader import to_ref                        # noqa: E402
from agent.recruiter import build_card_and_posting, clean_name, newest_message   # noqa: E402
from agent.sorter import _on_domain, classify, sender_domain                     # noqa: E402
from agent.staffing import (EMPLOYMENT_TYPES, Criteria, clean_location, employment_type_enum,  # noqa: E402
                            hard_filter, jaccard, jd_text, labelled_location, mentions_c2c_and_w2,
                            shingles, work_mode_enum)
from mcp_email.config import folders                   # noqa: E402

BINS = [0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.01]
SAME_JOB = noul(
    "These are the job descriptions from two recruiter emails. Are they the same job opening "
    "(the same role at the same end client), possibly sent by different recruiters or agencies? "
    "Similar skills or the same tech stack alone do not make them the same opening.",
    true="The same opening, described by two senders.",
    false="Two different openings.")


def same_job_probability(jev, a: str, b: str) -> float | None:
    try:
        ans = jev.ask({"job_a": a[:3000], "job_b": b[:3000]}, {"same": SAME_JOB})
    except JevError:
        return None
    p = (ans.get("same") or {}).get("noul")
    return float(p) if p is not None else None


def _when(ref) -> str:
    return (ref.get("received_at") or "")[:10]


def extract(jev, ref: dict) -> dict | None:
    """Stage A step 1 for one email, or None when Jev finds no pitched role."""
    card, posting = build_card_and_posting(jev, ref)
    if posting is None:
        return None
    newest = newest_message(ref.get("body_text") or "")
    text = f"{ref.get('subject') or ''}\n{newest}"
    pay = parse_pay(text)
    is_agency = (card or {}).get("is_agency")
    company = posting.get("company") or ""
    return {
        "role": posting["role"],
        "company": company,
        "client_named": not (company.endswith("(agency)") or company == "Unknown company"),
        "is_agency": is_agency,
        "employment_type": employment_type_enum(text, pay, is_agency),
        "c2c_and_w2": mentions_c2c_and_w2(text),
        "work_mode": work_mode_enum(text),
        "location": clean_location(posting.get("location"), labelled_location(newest)),
        "salary_max": pay.salary_max if pay else None,
        "hourly": bool(pay and pay.hourly),
        "jd": jd_text(newest, clean_name(ref.get("sender") or "")),
        "link": posting.get("link"),
    }


def main(argv) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=100, help="most recent Interaction emails to read")
    ap.add_argument("--criteria", help="JSON file shaped like staffing.criteria (§2.4)")
    ap.add_argument("--allowed-types", default=",".join(EMPLOYMENT_TYPES))
    ap.add_argument("--excluded-domains", default="linkedin.com")
    ap.add_argument("--high", type=float, default=0.6, help="provisional: ≥ this ⇒ duplicate")
    ap.add_argument("--low", type=float, default=0.25, help="provisional: ≤ this ⇒ new")
    ap.add_argument("--jev-gray", action="store_true", help="ask Jev 'same job?' on gray-zone pairs")
    ap.add_argument("--pairs", type=int, default=25, help="top similar pairs to print")
    ap.add_argument("--no-classify", action="store_true",
                    help="skip the category question; treat every email as recruiter_outreach")
    args = ap.parse_args(argv)

    if not settings.typesafe_api_key:
        raise SystemExit("✗ TYPESAFE_API_KEY not set")
    crit_dict = json.load(open(args.criteria)) if args.criteria else {}
    crit = Criteria.from_dict(crit_dict, [t for t in args.allowed_types.split(",") if t])
    excluded = tuple(d.strip().lower() for d in args.excluded_domains.split(",") if d.strip())

    provider, jev = _build_provider(), make_jev()
    scope = Counter()
    items = []   # (ref, fields)
    try:
        msgs = provider.get_recent(folders.interaction, args.limit)
        print(f"• {folders.interaction}: {len(msgs)} messages")
        for m in msgs:
            ref = to_ref(m)
            if not args.no_classify:
                cat = classify(jev, ref).choice
                if cat != "recruiter_outreach":
                    scope[f"not recruiter_outreach ({cat})"] += 1
                    continue
            if _on_domain(sender_domain(ref.get("sender") or ""), excluded):
                scope["excluded sender domain"] += 1
                continue
            fields = extract(jev, ref)
            if fields is None:
                scope["no pitched role found"] += 1
                continue
            scope["in scope"] += 1
            items.append((ref, fields))

        # Arrival order: Stage A sees the oldest first.
        items.sort(key=lambda it: it[0].get("received_at") or "")
        sh = [shingles(f["jd"]) for _, f in items]

        # All earlier pairs, for the histogram and the top-pairs list (independent of filters).
        best_prior: list[tuple[float, int | None]] = []
        pairs = []
        for i in range(len(items)):
            best = (0.0, None)
            for j in range(i):
                s = jaccard(sh[i], sh[j])
                pairs.append((s, j, i))
                if s > best[0]:
                    best = (s, j)
            best_prior.append(best)

        # Outcome simulation: filter, then dedup against jobs imported so far.
        outcomes, reasons, imported = [], Counter(), []
        gray_asked = []
        for i, (ref, f) in enumerate(items):
            reason = hard_filter(f, crit)
            if reason:
                outcomes.append(("filtered", reason, None))
                reasons[("Location" if reason.startswith("Located") else "Pay" if reason.startswith("Pay")
                         else reason.split(";")[0][:40])] += 1
                continue
            sim, j = max(((jaccard(sh[i], sh[k]), k) for k in imported), default=(0.0, None))
            if sim >= args.high:
                outcomes.append(("duplicate", f"sim {sim:.2f}", j))
            elif sim <= args.low or j is None:
                outcomes.append(("imported", f"best {sim:.2f}", j))
                imported.append(i)
            elif args.jev_gray:
                p = same_job_probability(jev, items[j][1]["jd"], f["jd"])
                gray_asked.append((sim, p, j, i))
                if p is not None and p >= 0.75:
                    outcomes.append(("duplicate", f"gray {sim:.2f}, Jev {p:.2f}", j))
                else:
                    outcomes.append(("imported", f"gray {sim:.2f}, Jev {p if p is None else round(p, 2)}", j))
                    imported.append(i)
            else:
                outcomes.append(("gray", f"sim {sim:.2f}", j))
                imported.append(i)
    finally:
        provider.close() if hasattr(provider, "close") else None
        jev.close()

    print("\nScope:", dict(scope))
    n = len(items)
    if not n:
        print("no in-scope emails")
        return 1

    def pct(k):
        return f"{sum(1 for _, f in items if f.get(k)) / n:.0%}"

    print(f"\nField coverage over {n} in-scope emails: work_mode {pct('work_mode')}, "
          f"location {pct('location')}, pay {pct('salary_max')} (hourly {pct('hourly')}), "
          f"client named {pct('client_named')}, apply link {pct('link')}, "
          f"is_agency known {sum(1 for _, f in items if f['is_agency'] is not None) / n:.0%}")
    print("Employment types:", dict(Counter(f["employment_type"] for _, f in items)),
          f"— both C2C and W2 named: {sum(f['c2c_and_w2'] for _, f in items)}")
    print("Work modes:", dict(Counter(f["work_mode"] or "unknown" for _, f in items)))

    print(f"\nOutcomes (high {args.high}, low {args.low}"
          f"{', Jev on gray' if args.jev_gray else ''}):", dict(Counter(o for o, _, _ in outcomes)))
    if reasons:
        print("Filter reasons:", dict(reasons))
    if not crit_dict:
        print("  (no --criteria: work mode, location, pay and company filters were not applied)")

    print("\nBest match among EARLIER in-scope emails (per email):")
    hist = Counter()
    for s, _ in best_prior:
        for lo, hi in zip(BINS, BINS[1:]):
            if lo <= s < hi:
                hist[lo] += 1
    for lo, hi in zip(BINS, BINS[1:]):
        print(f"  {lo:.1f}–{min(hi, 1.0):.1f} {hist[lo]:>4} " + "█" * hist[lo])

    print(f"\nTop {args.pairs} pairs by similarity (terminal only):")
    for s, j, i in sorted(pairs, reverse=True)[:args.pairs]:
        a, b = items[j], items[i]
        print(f"  {s:.2f}  {_when(a[0])} {a[1]['role'][:38]!r} @ {a[1]['company'][:24]!r}")
        print(f"        {_when(b[0])} {b[1]['role'][:38]!r} @ {b[1]['company'][:24]!r}"
              f"   — {(a[0].get('sender') or '')[:28]} / {(b[0].get('sender') or '')[:28]}")
    if gray_asked:
        print("\nGray-zone pairs Jev was asked about:")
        for s, p, j, i in sorted(gray_asked, reverse=True):
            print(f"  sim {s:.2f}  Jev {'?' if p is None else f'{p:.2f}'}  "
                  f"{items[j][1]['role'][:35]!r} vs {items[i][1]['role'][:35]!r}")

    print("\nPer email (oldest first; * = names both C2C and W2):")
    for (ref, f), (o, why, _) in zip(items, outcomes):
        top = f"${f['salary_max']:,}" if f["salary_max"] else "-"
        print(f"  {_when(ref)} {o:<9} {f['employment_type'] + ('*' if f['c2c_and_w2'] else ''):<17} {(f['work_mode'] or '-'):<7} "
              f"{(f['location'] or '-')[:18]:<18} {top:>9}  {f['role'][:40]!r}  [{why}]")

    print(f"\nJev: {jev.calls} calls, {jev.input_tokens} input tokens, ${jev.run_cost:.5f}  "
          f"({datetime.now():%Y-%m-%d %H:%M})")
    print("(read-only: nothing was marked, moved, or written)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
