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
"""

from __future__ import annotations

import html
import re
from email.utils import parseaddr

from .jev import JevError, choice, noul
from .sorter import _REPLY_MARKERS, _on_domain, sender_domain

# Sender domains that relay mail through ONE shared address — it is not the recruiter's, and it would
# merge every such recruiter into one Job Radar suggestion.
RELAY_DOMAINS = ("linkedin.com", "indeed.com", "glassdoor.com", "ziprecruiter.com")
# Relays with a distinct address per recruiter (replies reach them) — usable, but prefer the signature's.
PER_SENDER_RELAYS = ("user.dice.com",)
FREE_MAIL = ("gmail.com", "yahoo.com", "outlook.com", "hotmail.com", "aol.com", "icloud.com",
             "proton.me", "protonmail.com")

CAPS = {"name": 200, "email": 255, "phone": 50, "employer": 200, "title": 200, "linkedin_url": 500}
SIGNATURE_LINES = 15          # non-empty lines at the end of the newest message
MAX_FRAGMENTS = 24
PICK_MIN = 0.6                # a title/employer pick below this probability is omitted
AGENCY_TRUE, AGENCY_FALSE = 0.75, 0.25

_NAME_NOISE = re.compile(r"\s*(\(via [^)]*\)|via linkedin|\(#?[A-Z]{2,}\d+\)|\(#\w+\))\s*", re.I)
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_PHONE = re.compile(r"(?<![\w/])(\+?\(?\d[\d\s().-]{8,}\d)(?:\s*(?:x|ext\.?)\s*\d{1,5})?", re.I)
_LINKEDIN = re.compile(r"https?://(?:[a-z]{2,3}\.)?linkedin\.com/in/[\w%-]+/?", re.I)
_SPLIT = re.compile(r"\s*(?:\||•|·|•|;)\s*")
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


def fragments(lines: list[str], name: str | None) -> list[str]:
    """Short signature fragments Jev may pick from — no URLs, emails, phones, or the name itself."""
    out: list[str] = []
    for ln in lines:
        for frag in _SPLIT.split(ln):
            frag = frag.strip(" -–—,:")
            if not (2 <= len(frag) <= 80) or _URLISH.search(frag) or _EMAIL.search(frag):
                continue
            if _PHONE.search(frag) or sum(c.isdigit() for c in frag) > len(frag) // 2:
                continue
            if name and frag.lower() == name.lower():
                continue
            if frag not in out:
                out.append(frag)
    return out[:MAX_FRAGMENTS]


def _pick(answer: dict | None, frags: list[str]) -> str | None:
    if not answer or answer.get("type") != "choice":
        return None
    key = answer.get("choice", "none")
    if key == "none" or float((answer.get("probabilities") or {}).get(key, 0.0)) < PICK_MIN:
        return None
    if not key.startswith("f") or not key[1:].isdigit():
        return None
    i = int(key[1:]) - 1
    return frags[i] if 0 <= i < len(frags) else None


def build_card(jev, email: dict) -> dict | None:
    """The §3.5 card for the email's sender, or None when there is no usable name."""
    sender = email.get("sender") or ""
    name = clean_name(sender)
    if not name:
        return None
    body = email.get("body_text") or ""
    lines = signature_lines(body)
    sig = "\n".join(lines)
    domain = sender_domain(sender)

    card: dict = {"name": _cap("name", name)}
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
    questions = {
        "is_agency": noul(
            "Is the sender a recruiter at a third-party staffing or recruiting agency, rather than an "
            "in-house recruiter or employee of the company that is hiring?"),
    }
    if frags:
        questions["title"] = choice(
            "Which fragment of the email signature is the sender's own job title? Answer 'none' if "
            "no fragment is a job title.", {**options, "none": "No fragment is the sender's job title."})
        questions["employer"] = choice(
            "Which fragment of the email signature is the name of the company the sender works for? "
            "Answer 'none' if no fragment is a company name.",
            {**options, "none": "No fragment names the sender's company."})
    state = {"from": sender, "subject": email.get("subject") or "", "signature": sig[:2000]}
    try:
        answers = jev.ask(state, questions)
    except JevError:
        return card          # the card is best-effort; the header-derived fields still help

    # A fragment that also appears in the subject describes the ROLE on offer, not the sender.
    subject = (email.get("subject") or "").lower()

    def _own(frag: str | None) -> str | None:
        return frag if frag and frag.lower() not in subject else None

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
    return card
