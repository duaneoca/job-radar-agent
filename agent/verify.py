"""
Deterministic verifier for the link picker's output (V2).

Every value the model returns is checked against the email itself:
  • the link number must exist in the extracted list
  • the title must appear in that link's text (or the text right next to it)
  • the company must appear in the text right next to that link, and differ from the title

Failures become short, corrective instructions for the next attempt. They never repeat the model's
wrong values (to avoid reinforcing them) — they only reference our own link numbers, which are
trusted data from `agent/extract.py`.

Harmless issues are fixed here instead of costing a retry: the same link picked twice, or the same
job picked through two links (Monster links each job as "Title", "QUICK APPLY" and "VIEW JOB"),
collapse to the first pick. Beyond `max_postings` the list is truncated, not rejected.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .extract import LinkCandidate, match_key, norm
from .schemas import LinkPicks

MIN_LEN = 2


@dataclass(frozen=True)
class VerifiedPosting:
    link_id: int
    title: str
    company: str
    url: str            # always from the extracted candidate, never from the model


@dataclass
class Verdict:
    postings: list[VerifiedPosting] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)
    truncated: bool = False

    @property
    def ok(self) -> bool:
        return not self.issues


def verify_picks(picks: LinkPicks, candidates: list[LinkCandidate], *,
                 max_postings: int = 30) -> Verdict:
    by_id = {c.id: c for c in candidates}
    out: list[VerifiedPosting] = []
    failed: list[tuple[tuple[str, str], list[str]]] = []   # (job key, issues) per rejected pick
    seen_ids: set[int] = set()
    seen_jobs: set[tuple[str, str]] = set()
    unknown = 0

    for p in picks.postings:
        c = by_id.get(p.link_id)
        if c is None:
            unknown += 1
            continue
        if c.id in seen_ids:
            continue                                   # same link twice — keep the first
        seen_ids.add(c.id)

        title_k, company_k = match_key(p.title), match_key(p.company)
        text_k, context_k = match_key(c.text), match_key(c.context)
        job = (title_k, company_k)

        problems: list[str] = []
        if len(title_k) < MIN_LEN or (title_k not in text_k and title_k not in context_k):
            problems.append(f"For link [{c.id}], the title must be copied exactly from that link's text.")
        if len(company_k) < MIN_LEN or company_k not in context_k:
            problems.append(f"For link [{c.id}], the company must be copied exactly from the nearby "
                            "text shown for that link.")
        elif company_k == title_k:
            problems.append(f"For link [{c.id}], the company must be the employer's name, not the job title.")
        if problems:
            failed.append((job, problems))
            continue
        if job in seen_jobs:
            continue                                   # same job via another link — keep the first
        seen_jobs.add(job)
        out.append(VerifiedPosting(link_id=c.id, title=norm(p.title), company=norm(p.company),
                                   url=c.url))

    # A rejected pick that names a job already verified through another link (e.g. Monster's
    # "VIEW JOB" button next to the title link) is a harmless duplicate — drop it, don't retry.
    issues = [msg for job, problems in failed if job not in seen_jobs for msg in problems]
    if unknown:
        issues.insert(0, f"{unknown} of your picks used a link number that is not in the list. "
                         "Only use numbers shown in brackets.")
    return Verdict(postings=out[:max_postings], issues=issues, truncated=len(out) > max_postings)
