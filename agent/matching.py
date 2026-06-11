"""
Fuzzy matching of an email's (company, role) against the user's existing reviews.

Used two ways:
  • Interaction emails — find which tracked review a status update belongs to.
  • Posting dedup — flag `possible_duplicate` when a surfaced opportunity is already tracked.

Deliberately simple + dependency-free (difflib). Company similarity dominates; role refines.
Returns ranked candidates so the caller can decide: unique strong match, ambiguous (→ HITL), or
none (→ needs_review / not a duplicate).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any

STRONG = 0.80          # a confident match
AMBIGUOUS_BAND = 0.10  # 2+ candidates within this of the top ⇒ ambiguous

_SUFFIXES = {"inc", "llc", "ltd", "corp", "co", "company", "gmbh", "plc", "the"}


def _norm(s: str) -> str:
    s = (s or "").lower()
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    tokens = [t for t in s.split() if t and t not in _SUFFIXES]
    return " ".join(tokens)


def _sim(a: str, b: str) -> float:
    return SequenceMatcher(None, _norm(a), _norm(b)).ratio()


def score(company: str, role: str | None, review: dict[str, Any]) -> float:
    """
    Similarity of (company, role) to a review {company, title}.

    Company is the ANCHOR — a wrong company can never match (score is bounded by company sim).
    Role only REFINES: it nudges the score within the company's ceiling, so a perfect company
    match with differently-worded role (e.g. "FDE" vs "Forward Deployed Engineer") still clears,
    while two same-company roles separate enough to disambiguate. [interaction-match design]
    """
    c = _sim(company, review.get("company", ""))
    if role:
        r = _sim(role, review.get("title", ""))
        return c * (0.8 + 0.2 * r)
    return c


@dataclass
class MatchResult:
    candidates: list[dict[str, Any]]   # reviews sorted by score desc, each annotated with "_score"
    best: dict[str, Any] | None        # top review if it clears STRONG, else None
    ambiguous: bool                    # 2+ strong candidates within AMBIGUOUS_BAND ⇒ needs HITL

    @property
    def unique_strong(self) -> bool:
        return self.best is not None and not self.ambiguous


def match(company: str, role: str | None, reviews: list[dict[str, Any]]) -> MatchResult:
    ranked = sorted(
        ({**r, "_score": score(company, role, r)} for r in reviews),
        key=lambda r: r["_score"],
        reverse=True,
    )
    if not ranked or ranked[0]["_score"] < STRONG:
        return MatchResult(candidates=ranked, best=None, ambiguous=False)

    top = ranked[0]["_score"]
    strong_near_top = [r for r in ranked if r["_score"] >= STRONG and top - r["_score"] <= AMBIGUOUS_BAND]
    ambiguous = len(strong_near_top) >= 2
    return MatchResult(candidates=ranked, best=ranked[0], ambiguous=ambiguous)
