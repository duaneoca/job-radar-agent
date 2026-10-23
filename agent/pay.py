"""
Pay and employment details from a recruiter's pitch — deterministic, never the model.

Staffing roles usually quote an hourly rate ("$70-80/hr on C2C"), salaried ones an annual
figure ("$180K-$200K", "$185,000/yr"). Job Radar only stores annual salary, so hourly rates are
annualized at 40 h × 52 weeks = 2,080 h — the user's choice (2026-10-23), made explicit in the
description header so the figure never reads as the recruiter's own.

Only amounts with a clear unit are taken: a `$` plus /hr / per hour / hourly / an "Rate:" label for
hourly; K, /yr, per annum, or a comma-grouped amount ≥ 30,000 for annual. Anything outside a
plausible band is dropped rather than guessed. The first match in the text wins (subject first).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

HOURS_PER_YEAR = 2080            # 40 h/week × 52 weeks
_RETIREMENT_PLANS = {"401", "403", "457"}
HOURLY_BAND = (15, 500)          # $/h
ANNUAL_BAND = (30_000, 1_500_000)

_NUM = r"(\d{1,3}(?:,\d{3})+|\d+(?:\.\d{1,2})?)"
_DASH = r"\s*(?:-|–|—|to)\s*"
_HOURLY_UNIT = r"\s*(?:/\s*(?:hr|hour|h)\b|per\s+hour\b|an\s+hour\b|hourly\b|ph\b)"
_ANNUAL_UNIT = r"\s*(?:/\s*(?:yr|year|annum)\b|per\s+(?:year|annum)\b|annually\b|a\s+year\b|pa\b)"

_HOURLY_RANGE = re.compile(r"\$\s?" + _NUM + _DASH + r"\$?\s?" + _NUM + _HOURLY_UNIT, re.I)
_HOURLY_ONE = re.compile(r"\$\s?" + _NUM + _HOURLY_UNIT, re.I)
_RATE_LABEL = re.compile(r"\b(?:pay\s+)?rate\s*[:\-]?\s*\$\s?" + _NUM + r"(?:" + _DASH + r"\$?\s?" + _NUM + r")?", re.I)
_K_RANGE = re.compile(r"\$\s?(\d{2,4}(?:\.\d)?)\s*k" + _DASH + r"\$?\s?(\d{2,4}(?:\.\d)?)\s*k\b", re.I)
_K_ONE = re.compile(r"\$\s?(\d{2,4}(?:\.\d)?)\s*k\b", re.I)
_ANNUAL_RANGE = re.compile(r"\$\s?(\d{1,3}(?:,\d{3})+)" + _DASH + r"\$?\s?(\d{1,3}(?:,\d{3})+)", re.I)
_ANNUAL_ONE = re.compile(r"\$\s?(\d{1,3}(?:,\d{3})+)(?:" + _ANNUAL_UNIT + r")?", re.I)

_EMPLOYMENT = [   # first match wins; most specific first
    ("Contract-to-hire", re.compile(r"\bcontract[\s-]*to[\s-]*hire\b|\bC2H\b|\bCTH\b", re.I)),
    ("Contract (C2C)", re.compile(r"\bC2C\b|\bcorp[\s-]*to[\s-]*corp\b", re.I)),
    ("Contract (W2)", re.compile(r"\bW-?2\b", re.I)),
    ("Contract", re.compile(r"\bcontract(?:or)?\b", re.I)),
    ("Full-time", re.compile(r"\bfull[\s-]*time\b|\bFTE\b|\bpermanent\b|\bdirect[\s-]*hire\b", re.I)),
    ("Part-time", re.compile(r"\bpart[\s-]*time\b", re.I)),
]
_HYBRID = re.compile(r"\bhybrid\b", re.I)
_ONSITE = re.compile(r"\bon[\s-]?site\b|\bin[\s-]office\b", re.I)
_REMOTE = re.compile(r"\bremote\b|\bwork\s+from\s+home\b|\bWFH\b", re.I)


@dataclass(frozen=True)
class Pay:
    salary_min: int | None
    salary_max: int | None
    hourly: tuple[float, float] | None     # the rate as quoted, when it was hourly
    quoted: str                             # the matched text, verbatim

    def header(self) -> str:
        if self.hourly:
            lo, hi = self.hourly
            rate = f"${_fmt(lo)}/hr" if lo == hi else f"${_fmt(lo)}–{_fmt(hi)}/hr"
            yr = (f"${self.salary_min:,}" if self.salary_min == self.salary_max
                  else f"${self.salary_min:,}–{self.salary_max:,}")
            return f"{rate} (≈ {yr}/yr at 40 h/week)"
        return self.quoted


def _fmt(x: float) -> str:
    return f"{x:g}"


def _num(s: str) -> float:
    return float(s.replace(",", ""))


def _hourly(lo: float, hi: float, quoted: str) -> Pay | None:
    lo, hi = min(lo, hi), max(lo, hi)
    if not (HOURLY_BAND[0] <= lo and hi <= HOURLY_BAND[1]):
        return None
    return Pay(round(lo * HOURS_PER_YEAR), round(hi * HOURS_PER_YEAR), (lo, hi), quoted)


def _annual(lo: float, hi: float, quoted: str) -> Pay | None:
    lo, hi = min(lo, hi), max(lo, hi)
    if not (ANNUAL_BAND[0] <= lo and hi <= ANNUAL_BAND[1]):
        return None
    return Pay(round(lo), round(hi), None, quoted)


def parse_pay(text: str) -> Pay | None:
    """The first unambiguous pay figure in `text`, annualized; None when there isn't one."""
    text = text or ""
    found: list[tuple[int, Pay]] = []

    def add(m: re.Match | None, pay: Pay | None) -> None:
        if m and pay:
            found.append((m.start(), pay))

    for m in _HOURLY_RANGE.finditer(text):
        add(m, _hourly(_num(m.group(1)), _num(m.group(2)), m.group(0).strip()))
    for m in _HOURLY_ONE.finditer(text):
        add(m, _hourly(_num(m.group(1)), _num(m.group(1)), m.group(0).strip()))
    for m in _RATE_LABEL.finditer(text):
        lo = _num(m.group(1))
        hi = _num(m.group(2)) if m.group(2) else lo
        if hi < 1000:                       # "Rate: $70" is hourly; "Rate: $150,000" is not
            add(m, _hourly(lo, hi, m.group(0).strip()))
    for m in _K_RANGE.finditer(text):
        add(m, _annual(_num(m.group(1)) * 1000, _num(m.group(2)) * 1000, m.group(0).strip()))
    for m in _K_ONE.finditer(text):
        if m.group(1) in _RETIREMENT_PLANS:      # "$401k match" is a benefit, not a salary
            continue
        add(m, _annual(_num(m.group(1)) * 1000, _num(m.group(1)) * 1000, m.group(0).strip()))
    for m in _ANNUAL_RANGE.finditer(text):
        add(m, _annual(_num(m.group(1)), _num(m.group(2)), m.group(0).strip()))
    for m in _ANNUAL_ONE.finditer(text):
        add(m, _annual(_num(m.group(1)), _num(m.group(1)), m.group(0).strip()))
    if not found:
        return None
    # Earliest match; at a tie, the longest (a range beats the single figure inside it).
    found.sort(key=lambda f: (f[0], -len(f[1].quoted)))
    return found[0][1]


def employment_type(text: str) -> str | None:
    for label, rx in _EMPLOYMENT:
        if rx.search(text or ""):
            return label
    return None


def work_mode(text: str) -> str | None:
    """'Hybrid' / 'On-site' / 'Remote' — hybrid and on-site win over a stray 'remote'."""
    t = text or ""
    if _HYBRID.search(t):
        return "Hybrid"
    if _ONSITE.search(t):
        return "On-site"
    if _REMOTE.search(t):
        return "Remote"
    return None
