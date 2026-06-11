"""
Best-effort scrubber: a real capture (tests/fixtures/real/*.json) → a CANDIDATE synthetic fixture.

This is an ASSIST, not a guarantee. It redacts obvious PII (email addresses, the owner's name, long
digit runs, tracking-id query params) and emits a candidate you MUST review by hand before moving it
into tests/fixtures/synthetic/ and committing. Never commit straight from this tool.

Usage:
    OWNER_NAME="Duane O" OWNER_EMAIL="duaneo@duanesworld.org" \
        python scripts/scrub_fixtures.py tests/fixtures/real/003-*.json
"""

import json
import os
import re
import sys

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
LONGNUM_RE = re.compile(r"\b\d{6,}\b")
TRACKING_RE = re.compile(r"([?&])(utm_[a-z]+|trk|trkEmail|midToken|eid|li_[a-z]+)=[^&\s]+", re.I)


def scrub(text: str, owner_name: str, owner_email: str) -> str:
    if owner_email:
        text = text.replace(owner_email, "candidate@example.com")
    if owner_name:
        text = re.sub(re.escape(owner_name), "Jordan Candidate", text, flags=re.I)
    text = EMAIL_RE.sub("redacted@example.com", text)
    text = TRACKING_RE.sub(r"\1\2=REDACTED", text)
    text = LONGNUM_RE.sub("000000", text)
    return text


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    owner_name = os.environ.get("OWNER_NAME", "")
    owner_email = os.environ.get("OWNER_EMAIL", "")
    for path in argv:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
        candidate = {
            "name": os.path.splitext(os.path.basename(path))[0],
            "description": "CANDIDATE — review + label before committing",
            "email": {
                "message_id": "<synthetic@example.com>",
                "subject": scrub(raw.get("subject", ""), owner_name, owner_email),
                "sender": "Synthetic Sender <sender@example.com>",
                "received_at": raw.get("received_at"),
                "body_text": scrub(raw.get("body_text", ""), owner_name, owner_email),
                "has_attachments": raw.get("has_attachments", False),
            },
            "expected": {"category": "TODO", "destination": "TODO"},
        }
        out = path.replace("/real/", "/synthetic/").replace(".json", ".candidate.json")
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with open(out, "w", encoding="utf-8") as f:
            json.dump(candidate, f, indent=2, ensure_ascii=False)
        print(f"→ {out}  (REVIEW: confirm no PII, set expected.category/destination, drop .candidate)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
