"""
Recruiter contact card for a `recruiter_outreach` email (INTEGRATION_SPEC §3.5).

V1 had an LLM write the card. Jev cannot write text, so the card is built the V2 way — every value is
copied from the email, never generated:

  name          From display name, with relay/ID decorations removed ("(via LinkedIn)", "(#TSI2917)")
  email         the sender address — except for SHARED relay senders (LinkedIn InMail, …), where it is
                not the recruiter's own: then the first address in the signature, if any. Dice's
                per-recruiter relay (user.dice.com) reaches that one recruiter, so it is kept as a
                fallback, but a signature address is preferred.
  phone         first phone-looking run in the signature, verbatim
  linkedin_url  first linkedin.com/in/… URL in the body
  title/employer  Jev PICKS one of our numbered signature fragments (or "none"); we copy the fragment
  is_agency     Jev yes/no; null in the uncertain middle (and for free webmail with no signal)

`represents` (client companies) is omitted: naming a client needs text Jev cannot produce.
All values are attacker-controlled email text — length-capped here, and Job Radar never auto-creates
a recruiter from a card (review + confirm) [C2r].

The same Jev call also extracts the ONE role the recruiter is pitching, as a posting
(`build_card_and_posting`). Nearly every recruiter email carries one, and before this they all
showed in Job Radar's inbox as "no postings extracted". The V2 rules hold: the role and the hiring
company are PICKED from short fragments of the subject and the first lines of the body, never
written; the link, if any, is the first apply/job-board link in the body. When no client is named —
agencies usually say "our client" — the company is the agency itself, marked "(agency)".
"""

from __future__ import annotations

import html
import re
from email.utils import parseaddr

from urllib.parse import urlsplit

from .extract import extract_links
from .jev import JevError, choice, noul
from .pay import employment_type, parse_pay, work_mode
from .sorter import _REPLY_MARKERS, _on_domain, sender_domain

# Sender domains that relay mail through ONE shared address — it is not the recruiter's, and it would
# merge every such recruiter into one Job Radar suggestion.
RELAY_DOMAINS = ("linkedin.com", "indeed.com", "glassdoor.com", "ziprecruiter.com")
# Relays with a distinct address per recruiter (replies reach them) — usable, but prefer the signature's.
PER_SENDER_RELAYS = ("user.dice.com",)
FREE_MAIL = ("gmail.com", "yahoo.com", "outlook.com", "hotmail.com", "aol.com", "icloud.com",
             "proton.me", "protonmail.com")

CAPS = {"name": 200, "email": 255, "phone": 50, "employer": 200, "title": 200, "linkedin_url": 500,
        "role": 200, "company": 200, "location": 200}
SIGNATURE_LINES = 15          # non-empty lines at the end of the newest message
HEAD_LINES = 8                # first non-empty lines of the newest message: where the pitch is
DESCRIPTION_CAP = 12_000      # chars of the newest message sent as the posting's description
MAX_FRAGMENTS = 24
PICK_MIN = 0.6                # a title/employer pick below this probability is omitted
AGENCY_TRUE, AGENCY_FALSE = 0.75, 0.25

# Where a recruiter's "apply here" link usually points. Checked by host; a profile link
# (linkedin.com/in/…) is the recruiter's, not the job's.
JOB_HOSTS = ("dice.com", "linkedin.com", "indeed.com", "glassdoor.com", "ziprecruiter.com",
             "monster.com", "greenhouse.io", "lever.co", "ashbyhq.com", "myworkdayjobs.com",
             "smartrecruiters.com", "workable.com", "jobvite.com", "icims.com")
_APPLY_WORDS = re.compile(r"\b(apply|job|posting|position|opening|details|view)\b", re.I)

_NAME_NOISE = re.compile(r"\s*(\(via [^)]*\)|via linkedin|\(#?[A-Z]{2,}\d+\)|\(#\w+\))\s*", re.I)
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_PHONE = re.compile(r"(?<![\w/])(\+?\(?\d[\d\s().-]{8,}\d)(?:\s*(?:x|ext\.?)\s*\d{1,5})?", re.I)
_LINKEDIN = re.compile(r"https?://(?:[a-z]{2,3}\.)?linkedin\.com/in/[\w%-]+/?", re.I)
_SPLIT = re.compile(r"\s*(?:\||•|·|•|;)\s*")
# Subject lines pack role, location, mode and type into one line with ad-hoc separators:
# "Sr. Agentic AI Engineer--Santa Clara, CA (3 days On-site/week)--Contract",
# "Forward Deployed Engineer | Mountain View, CA / NYC | On-site". Split on all of them.
_HEAD_SPLIT = re.compile(r"\s*(?:\||::|--+|–|—|•|·|;|@|\[|\]|\(|\)|\s-\s|\s/\s|:\s)\s*")
_URLISH = re.compile(r"https?://|www\.|\[link\]", re.I)


def _cap(field: str, value: str | None) -> str | None:
    if not value:
        return None
    value = re.sub(r"[<>]", "", value).strip()
    return value[:CAPS[field]] or None


def clean_name(sender: str) -> str | None:
    display, addr = parseaddr(sender or "")
    name = _NAME_NOISE.sub(" ", display or "").strip(" \"'")
    name = re.sub(r"\s{2,}", " ", name)
    # "Pavan - PRIMUS" <pavank@primusglobal.com> → "Pavan" (the suffix is the sender's company)
    head, sep, tail = name.rpartition(" - ")
    if sep and head and re.sub(r"\W", "", tail).lower() in addr.lower().rpartition("@")[2]:
        name = head.strip()
    if not name or "@" in name or name.lower() in {"linkedin", "dice", "dice recruiter", "indeed"}:
        return None
    return name


def signature_lines(body_text: str) -> list[str]:
    """Last non-empty lines of the newest message (quoted history cut)."""
    text = body_text or ""
    m = _REPLY_MARKERS.search(text)
    if m:
        text = text[:m.start()]
    text = re.sub(r"&nbsp;?", " ", html.unescape(text), flags=re.I)
    lines = [ln.strip() for ln in text.splitlines()
             if ln.strip() and not ln.lstrip().startswith(">")]
    return lines[-SIGNATURE_LINES:]


def _clean_fragments(pieces, name: str | None, *, min_len: int) -> list[str]:
    out: list[str] = []
    for frag in pieces:
        frag = frag.strip(" -–—,:*\"'")
        if not (min_len <= len(frag) <= 80) or _URLISH.search(frag) or _EMAIL.search(frag):
            continue
        if _PHONE.search(frag) or sum(c.isdigit() for c in frag) > len(frag) // 2:
            continue
        if name and frag.lower() == name.lower():
            continue
        if frag not in out:
            out.append(frag)
    return out[:MAX_FRAGMENTS]


def fragments(lines: list[str], name: str | None) -> list[str]:
    """Short signature fragments Jev may pick from — no URLs, emails, phones, or the name itself."""
    return _clean_fragments((f for ln in lines for f in _SPLIT.split(ln)), name, min_len=2)


def head_lines(body_text: str) -> list[str]:
    """First non-empty lines of the newest message — the pitch, before the signature."""
    text = body_text or ""
    m = _REPLY_MARKERS.search(text)
    if m:
        text = text[:m.start()]
    text = re.sub(r"&nbsp;?", " ", html.unescape(text), flags=re.I)
    lines = [ln.strip() for ln in text.splitlines()
             if ln.strip() and not ln.lstrip().startswith(">")]
    return lines[:HEAD_LINES]


def newest_message(body_text: str) -> str:
    """The newest message's text, line breaks kept — quoted history cut, capped. This is the
    posting's description: the recruiter's pitch IS the job description (spec §3.5: the one
    place email body text crosses to Job Radar, by the user's decision 2026-10-23)."""
    text = body_text or ""
    m = _REPLY_MARKERS.search(text)
    if m:
        text = text[:m.start()]
    text = re.sub(r"&nbsp;?", " ", html.unescape(text), flags=re.I)
    lines = [ln.rstrip() for ln in text.splitlines() if not ln.lstrip().startswith(">")]
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    return text[:DESCRIPTION_CAP]


def head_fragments(subject: str, lines: list[str], name: str | None) -> list[str]:
    """Fragments of the subject (first — that is where the role nearly always is) and the body's
    first lines, for Jev to pick the pitched role and the hiring company from."""
    pieces = [f for f in _HEAD_SPLIT.split(subject or "")]
    for ln in lines:
        pieces.extend(_HEAD_SPLIT.split(ln))
    return _clean_fragments(pieces, name, min_len=3)


def _host_is(host: str, suffix: str) -> bool:
    return host == suffix or host.endswith("." + suffix)


def pick_link(email: dict) -> str | None:
    """The posting's link: the first job-board/ATS link that isn't a profile, else the first link
    whose text says apply/job/view. None when the pitch has no link — common, and fine."""
    cands = extract_links(email.get("body_html", "") or "", email.get("body_text", "") or "")
    for c in cands:
        parts = urlsplit(c.url)
        host = parts.netloc.lower().split(":")[0]
        if any(_host_is(host, h) for h in JOB_HOSTS) and "/in/" not in parts.path:
            return c.url
    for c in cands:
        if _APPLY_WORDS.search(c.text or ""):
            return c.url
    return None


def _org_from_domain(domain: str) -> str | None:
    if not domain or _on_domain(domain, FREE_MAIL) or _on_domain(domain, RELAY_DOMAINS + PER_SENDER_RELAYS):
        return None
    label = domain.split(".")[-2] if "." in domain else domain
    return label.capitalize() if label else None


def _pick(answer: dict | None, frags: list[str], prefix: str = "f") -> str | None:
    """The fragment Jev chose, or None: 'none', a low probability, or a key that isn't one of ours.
    Signature fragments are keyed f1…, subject/body-head fragments h1…"""
    if not answer or answer.get("type") != "choice":
        return None
    key = answer.get("choice", "none")
    if key == "none" or float((answer.get("probabilities") or {}).get(key, 0.0)) < PICK_MIN:
        return None
    if not key.startswith(prefix) or not key[len(prefix):].isdigit():
        return None
    i = int(key[len(prefix):]) - 1
    return frags[i] if 0 <= i < len(frags) else None


def build_card(jev, email: dict) -> dict | None:
    """The §3.5 card for the email's sender, or None when there is no usable name."""
    return build_card_and_posting(jev, email)[0]


def build_card_and_posting(jev, email: dict) -> tuple[dict | None, dict | None]:
    """ONE Jev call: the §3.5 card for the sender (None without a usable name) and the role being
    pitched as a posting {company, role, link, action_required} (None when Jev finds no role)."""
    sender = email.get("sender") or ""
    name = clean_name(sender)
    body = email.get("body_text") or ""
    lines = signature_lines(body)
    sig = "\n".join(lines)
    domain = sender_domain(sender)
    heads = head_lines(body)
    head_frags = head_fragments(email.get("subject") or "", heads, name)

    card: dict | None = None
    frags: list[str] = []
    questions: dict = {}
    if name:
        card = {"name": _cap("name", name)}
        addr = parseaddr(sender)[1]
        relays = RELAY_DOMAINS + PER_SENDER_RELAYS
        sig_emails = [e for e in _EMAIL.findall(sig)
                      if not _on_domain(e.split("@")[1].lower(), relays)]
        if addr and not _on_domain(domain, relays):
            card["email"] = _cap("email", addr)
        elif sig_emails:
            card["email"] = _cap("email", sig_emails[0])
        elif addr and _on_domain(domain, PER_SENDER_RELAYS):
            card["email"] = _cap("email", addr)
        if (m := _PHONE.search(sig)):
            card["phone"] = _cap("phone", m.group(0))
        if (m := _LINKEDIN.search(body)):
            card["linkedin_url"] = _cap("linkedin_url", m.group(0))

        frags = fragments(lines, name)
        options = {f"f{i + 1}": frag for i, frag in enumerate(frags)}
        questions["is_agency"] = noul(
            "Is the sender a recruiter at a third-party staffing or recruiting agency, rather than an "
            "in-house recruiter or employee of the company that is hiring?")
        if frags:
            questions["title"] = choice(
                "Which fragment of the email signature is the sender's own job title? Answer 'none' if "
                "no fragment is a job title.", {**options, "none": "No fragment is the sender's job title."})
            questions["employer"] = choice(
                "Which fragment of the email signature is the name of the company the sender works for? "
                "Answer 'none' if no fragment is a company name.",
                {**options, "none": "No fragment names the sender's company."})

    if head_frags:
        head_options = {f"h{i + 1}": frag for i, frag in enumerate(head_frags)}
        questions["job_title"] = choice(
            "Which fragment is the title of the job the sender is offering or asking about? Answer "
            "'none' if no fragment is a job title.",
            {**head_options, "none": "No fragment is the title of a job on offer."})
        questions["hiring_company"] = choice(
            "Which fragment names the company the job is AT — the employer, not the sender's own "
            "staffing agency? Answer 'none' if the hiring company is not named.",
            {**head_options, "none": "The hiring company is not named."})
        questions["job_location"] = choice(
            "Which fragment is where the job is located — a city, state or country? Answer 'none' "
            "if no fragment names the job's location.",
            {**head_options, "none": "No fragment names where the job is."})

    if not questions:
        return card, None
    state = {"from": sender, "subject": email.get("subject") or "", "signature": sig[:2000],
             "body_head": "\n".join(heads)[:1500]}
    try:
        answers = jev.ask(state, questions)
    except JevError:
        return card, None    # both are best-effort; the header-derived fields still help

    # A fragment that also appears in the subject describes the ROLE on offer, not the sender.
    subject = (email.get("subject") or "").lower()

    def _own(frag: str | None) -> str | None:
        return frag if frag and frag.lower() not in subject else None

    if card is not None:
        if (title := _own(_pick(answers.get("title"), frags))):
            card["title"] = _cap("title", title)
        if (employer := _own(_pick(answers.get("employer"), frags))) and employer != card.get("title"):
            card["employer"] = _cap("employer", employer)
        p = (answers.get("is_agency") or {}).get("noul")
        # Free webmail with no employer in the signature → no signal either way (spec §3.5 heuristic).
        if p is not None and not (_on_domain(domain, FREE_MAIL) and "employer" not in card):
            if p >= AGENCY_TRUE:
                card["is_agency"] = True
            elif p <= AGENCY_FALSE:
                card["is_agency"] = False

    role = _pick(answers.get("job_title"), head_frags, prefix="h")
    if not role:
        return card, None
    company = _pick(answers.get("hiring_company"), head_frags, prefix="h")
    if company and company.lower() == role.lower():
        company = None
    if not company:
        # Unnamed client: the agency is the only company we can truthfully name.
        employer = (card or {}).get("employer") or _org_from_domain(domain)
        if employer and (card or {}).get("is_agency") is True:
            employer = f"{employer} (agency)"
        company = employer or "Unknown company"
    posting = {"company": _cap("company", company), "role": _cap("role", role),
               "link": pick_link(email), "action_required": False}
    posting.update(job_details(email, role, _pick(answers.get("job_location"), head_frags, prefix="h")))
    return card, posting


def job_details(email: dict, role: str, location: str | None) -> dict:
    """Location / remote / annual pay / description for the pitched role. Pay, employment type and
    work mode are parsed by code (agent/pay.py); location is Jev's pick. The description opens
    with a one-line summary, so an annualized hourly rate is never mistaken for the quoted one."""
    newest = newest_message(email.get("body_text") or "")
    text = f"{email.get('subject') or ''}\n{newest}"
    pay = parse_pay(text)
    mode = work_mode(text)
    out: dict = {}
    if location and location.lower() != role.lower() and not re.fullmatch(r"(?i)remote", location):
        out["location"] = _cap("location", location)
    out["remote"] = mode == "Remote"
    if pay:
        out["salary_min"], out["salary_max"] = pay.salary_min, pay.salary_max
    header = " · ".join(x for x in (employment_type(text), mode, pay.header() if pay else None) if x)
    description = (f"{header}\n\n{newest}" if header else newest).strip()
    if description:
        out["description"] = description[:DESCRIPTION_CAP]
    return out
