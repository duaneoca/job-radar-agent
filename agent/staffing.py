"""
Staffing email agent, Stage A building blocks (INTEGRATION_SPEC §3.9) — deterministic, never a model.

Pure functions only: field rules, the hard filters, and the text side of dedup. The sort stage will
call them once Job Radar ships the endpoints; today `scripts/eval_staffing.py` runs them read-only
over the Interaction backlog to pick the dedup thresholds and sanity-check the filters.

  employment_type_enum   explicit text > hourly pay ⇒ contract > is_agency ⇒ contract > full_time
  work_mode_enum         onsite | hybrid | remote | None (from agent/pay.py's labelled-first parse)
  clean_location         "City, ST", or "ST" when only a state is named; else None
  allowed_states         Criteria home_city + search_locations → two-letter codes (contract, §3.9)
  jd_text                the newest message minus greeting, signature, rate lines and boilerplate
  hard_filter            the first failing filter's reason, or None; a missing field never fails
  shingles / jaccard     5-word shingle sets and their Jaccard similarity
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field

from .pay import _HOURLY_ONE, _HOURLY_RANGE, _RATE_LABEL, Pay, employment_type, work_mode

EMPLOYMENT_TYPES = ("full_time", "contract", "contract_c2c", "contract_w2", "contract_to_hire")
JD_CAP = 20_000
SHINGLE_WORDS = 5

_EXPLICIT = {   # agent/pay.py label → §3.9 enum. Part-time has no enum value: it falls through.
    "Contract-to-hire": "contract_to_hire",
    "Contract (C2C)": "contract_c2c",
    "Contract (W2)": "contract_w2",
    "Contract": "contract",
    "Full-time": "full_time",
}
_MODES = {"Remote": "remote", "Hybrid": "hybrid", "On-site": "onsite"}

STATES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California",
    "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware", "DC": "District of Columbia",
    "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois",
    "IN": "Indiana", "IA": "Iowa", "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana",
    "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota",
    "MS": "Mississippi", "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada",
    "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York",
    "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma", "OR": "Oregon",
    "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota",
    "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont", "VA": "Virginia",
    "WA": "Washington", "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming",
}
_BY_NAME = {v.lower(): k for k, v in STATES.items()}
_STATE_NAMES = "|".join(sorted((re.escape(v) for v in STATES.values()), key=len, reverse=True))
# "Dallas, TX", "Dallas, Texas", "Dallas TX 75201". The city is 1–4 capitalised words.
_CITY_STATE = re.compile(
    r"\b((?:[A-Z][A-Za-z.'-]*\s){0,3}[A-Z][A-Za-z.'-]*),?\s+(" + _STATE_NAMES + r"|[A-Z]{2})\b")
_STATE_ONLY = re.compile(r"^\s*(" + _STATE_NAMES + r"|[A-Z]{2})\s*$", re.I)
_LOCATION_LABEL = re.compile(r"(?im)^[^\S\n]*(?:job\s*)?location\s*[:\-–—]\s*(.+)$")


# ── field rules ──────────────────────────────────────────────────────────────────────────────

def employment_type_enum(text: str, pay: Pay | None, is_agency: bool | None) -> str:
    """§3.9 order: explicit text > hourly pay quoted ⇒ contract > is_agency true ⇒ contract >
    full_time. C2C and W2 stay distinct; when both are named, the earlier rule in agent/pay.py
    (C2C) wins — `mentions_c2c_and_w2` lets the eval count how often that happens."""
    explicit = _EXPLICIT.get(employment_type(text) or "")
    if explicit:
        return explicit
    if pay is not None and pay.hourly:
        return "contract"
    if is_agency is True:
        return "contract"
    return "full_time"


def mentions_c2c_and_w2(text: str) -> bool:
    return bool(re.search(r"\bC2C\b|\bcorp[\s-]*to[\s-]*corp\b", text or "", re.I)
                and re.search(r"\bW-?2\b", text or "", re.I))


def work_mode_enum(text: str) -> str | None:
    return _MODES.get(work_mode(text) or "")


_CITY_NOISE = re.compile(r"^(?:(?:hybrid|remote|onsite|on-site|in|at|near|location|based)\s+)+", re.I)


def _state_code(token: str, *, lenient: bool = False) -> str | None:
    """"TX" / "Texas" → "TX". Two-letter codes must be upper case ("in" is a word, not Indiana)
    unless `lenient` (the user's own Criteria entries)."""
    t = token.strip().strip(".")
    if len(t) == 2 and (t.isupper() or lenient) and t.upper() in STATES:
        return t.upper()
    return _BY_NAME.get(t.lower())


def clean_location(*candidates: str | None) -> str | None:
    """The first candidate that names a US place, as "City, ST" or "ST". A labelled "Location:"
    line is the caller's to pass; nothing here is geocoded or guessed."""
    for cand in candidates:
        if not cand:
            continue
        cand = re.sub(r"\s+", " ", cand).strip()
        for m in _CITY_STATE.finditer(cand):
            code = _state_code(m.group(2))
            city = _CITY_NOISE.sub("", m.group(1).strip()).strip()
            if code and city and not (len(city) == 2 and city.isupper()):
                return f"{city}, {code}"
        if (m := _STATE_ONLY.match(cand)) and (code := _state_code(m.group(1))):
            return code
    return None


def labelled_location(newest: str) -> str | None:
    m = _LOCATION_LABEL.search(newest or "")
    return m.group(1).strip() if m else None


def state_of(location: str | None) -> str | None:
    """Two-letter state of a cleaned location ("Dallas, TX" or "TX")."""
    if not location:
        return None
    return _state_code(location.rpartition(",")[2] if "," in location else location)


def allowed_states(home_city: str | None, search_locations) -> set[str]:
    """Criteria → two-letter state codes (§3.9 contract). "Austin, TX" → TX; "TX" / "Texas" → TX;
    an entry that parses to no state is ignored. Empty set ⇒ no location filter."""
    out: set[str] = set()
    for entry in [home_city, *(search_locations or [])]:
        if not entry or not isinstance(entry, str):
            continue
        e = entry.strip()
        if (m := _STATE_ONLY.match(e)) and (code := _state_code(m.group(1), lenient=True)):
            out.add(code)
            continue
        tail = e.rpartition(",")[2].strip() if "," in e else ""
        tail = re.sub(r"\s+\d{5}(?:-\d{4})?$", "", tail)
        if tail and (code := _state_code(tail, lenient=True)):
            out.add(code)
    return out


# ── the cleaned JD ───────────────────────────────────────────────────────────────────────────

_GREETING = re.compile(r"^(hi|hello|hey|dear|good (morning|afternoon|evening)|greetings)\b[^\n]{0,60}$",
                       re.I)
_BOILERPLATE = re.compile(
    r"hope (you are|you're|this (email|message) finds you)|i came across your (profile|resume)"
    r"|please (share|send) (me )?(your )?(updated |latest )?(resume|cv)"
    r"|(let me know|reply) if (you are|you're) (interested|available)"
    r"|looking forward to (hearing|your)|this is \w+ from|my name is \w+"
    r"|unsubscribe|equal opportunity employer|if you (are not|aren't) interested"
    r"|please (find|see) (the )?(job )?(details|description) below|kindly",
    re.I)
_SIGNOFF = re.compile(r"^(thanks|thank you|regards|best|warm regards|kind regards|sincerely|cheers)"
                      r"\b[\s,&!.a-z]{0,25}$", re.I)


def _is_rate_line(line: str) -> bool:
    return bool(_HOURLY_RANGE.search(line) or _HOURLY_ONE.search(line) or _RATE_LABEL.search(line)
                or re.search(r"(?i)^\s*(pay\s*)?rate\s*[:\-]", line))


def jd_text(newest: str, name: str | None) -> str:
    """`newest` (agent.recruiter.newest_message) minus greeting, signature, recruiter name, rate
    lines and stock boilerplate — the text dedup compares and Job Radar stores."""
    text = re.sub(r"&nbsp;?", " ", html.unescape(newest or ""), flags=re.I)
    lines = [ln.strip() for ln in text.splitlines()]
    n = len(lines)
    # Cut the signature: from the last line that IS the name, else from the last sign-off line,
    # either only in the bottom half (a "Thanks" up top is not a sign-off).
    cut = n
    key = (name or "").strip().lower()
    for i in range(n - 1, n // 2 - 1, -1):
        ln = lines[i].lower().rstrip(",:;-")
        if key and (ln == key or (ln.startswith(key) and len(ln) <= len(key) + 40)):
            cut = i
            break
    for i in range(min(cut, n) - 1, n // 2 - 1, -1):
        if _SIGNOFF.match(lines[i]):
            cut = i
            break
    kept: list[str] = []
    for ln in lines[:cut]:
        if not ln:
            if kept and kept[-1]:
                kept.append("")
            continue
        if _GREETING.match(ln) or _BOILERPLATE.search(ln) or _is_rate_line(ln):
            continue
        if key and ln.lower().rstrip(",") == key:
            continue
        kept.append(ln)
    return "\n".join(kept).strip()[:JD_CAP]


# ── dedup (text side) ────────────────────────────────────────────────────────────────────────

def shingles(text: str, k: int = SHINGLE_WORDS) -> frozenset:
    words = re.findall(r"[a-z0-9+#]+", (text or "").lower())
    if len(words) < k:
        return frozenset([tuple(words)]) if words else frozenset()
    return frozenset(tuple(words[i:i + k]) for i in range(len(words) - k + 1))


def jaccard(a: frozenset, b: frozenset) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


# ── hard filters ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Criteria:
    """The `staffing.criteria` block of /agent/settings (§2.4), plus the allowed employment types."""
    work_style: str | None = None                   # remote | hybrid | onsite | any
    home_city: str | None = None
    search_locations: tuple[str, ...] = ()
    min_salary: int | None = None
    excluded_companies: tuple[str, ...] = ()
    allowed_employment_types: tuple[str, ...] = EMPLOYMENT_TYPES
    states: frozenset = field(default=frozenset(), compare=False)

    @classmethod
    def from_dict(cls, d: dict, allowed_types=None) -> "Criteria":
        return cls(work_style=(d.get("work_style") or None),
                   home_city=d.get("home_city"),
                   search_locations=tuple(d.get("search_locations") or ()),
                   min_salary=d.get("min_salary"),
                   excluded_companies=tuple(d.get("excluded_companies") or ()),
                   allowed_employment_types=tuple(allowed_types or EMPLOYMENT_TYPES),
                   states=frozenset(allowed_states(d.get("home_city"), d.get("search_locations"))))


_WORK_STYLE_PASSES = {"remote": {"remote"}, "hybrid": {"remote", "hybrid"}}


def hard_filter(posting: dict, c: Criteria) -> str | None:
    """The first failing filter's reason (human-readable, ≤ 300 chars), or None when it passes.
    `posting` keys: work_mode, location, employment_type, salary_max, company, client_named."""
    mode = posting.get("work_mode")
    allowed_modes = _WORK_STYLE_PASSES.get((c.work_style or "any").lower())
    if mode and allowed_modes and mode not in allowed_modes:
        return f"Work mode {mode}; your search is {c.work_style}"
    if mode != "remote" and c.states:
        st = state_of(posting.get("location"))
        if st and st not in c.states:
            return f"Located in {posting['location']}; outside {', '.join(sorted(c.states))}"
    et = posting.get("employment_type")
    if et and et not in c.allowed_employment_types:
        return f"Employment type {et} is not in your allowed types"
    top = posting.get("salary_max")
    if top and c.min_salary and top < c.min_salary:
        return f"Pay tops out at ${top:,}/yr; your minimum is ${c.min_salary:,}"
    company = (posting.get("company") or "").strip().lower()
    if posting.get("client_named") and company:
        for ex in c.excluded_companies:
            if ex and ex.strip().lower() == company:
                return f"{posting['company']} is in your excluded companies"
    return None
