"""
Harvest real emails into tests/fixtures/real/ for PRIVATE reference (gitignored).

Read-only (BODY.PEEK[] — never marks \\Seen, never moves). Use the same env knobs as the smoke
test; set a wide window to capture variety:
    MAX_EMAIL_AGE_DAYS=0 MAX_EMAILS_PER_RUN=200 python scripts/harvest_fixtures.py

Output: one JSON per email under tests/fixtures/real/ (gitignored — real PII never reaches git).
These are raw captures you eyeball + scrub (scripts/scrub_fixtures.py) into the committed,
anonymized golden set under tests/fixtures/synthetic/. Do NOT commit anything from real/.
"""

import dataclasses
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mcp_email.providers.proton import ProtonProvider  # noqa: E402

OUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "tests", "fixtures", "real")

HOST = os.environ.get("PROTON_IMAP_HOST", "127.0.0.1")
PORT = int(os.environ.get("PROTON_IMAP_PORT", "1143"))
USER = os.environ.get("PROTON_IMAP_USER", "")
PASSWORD = os.environ.get("PROTON_IMAP_PASSWORD", "")
ROOT = os.environ.get("EMAIL_ROOT_FOLDER", "Folders/Hire Duane")


def _slug(s: str, n: int = 40) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")[:n] or "email"


def main() -> int:
    if not USER or not PASSWORD:
        print("✗ set PROTON_IMAP_USER / PROTON_IMAP_PASSWORD first")
        return 2
    os.makedirs(OUT_DIR, exist_ok=True)

    age_raw = int(os.environ.get("MAX_EMAIL_AGE_DAYS", "0"))
    age = age_raw if age_raw > 0 else None
    cap = int(os.environ.get("MAX_EMAILS_PER_RUN", "200"))

    p = ProtonProvider(HOST, PORT, USER, PASSWORD)
    try:
        msgs = p.get_unread(ROOT, since_days=age, limit=cap)
    finally:
        p.close()

    for i, m in enumerate(msgs):
        d = dataclasses.asdict(m)
        d["received_at"] = m.received_at.isoformat() if m.received_at else None
        path = os.path.join(OUT_DIR, f"{i:03d}-{_slug(m.subject)}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(d, f, indent=2, ensure_ascii=False)
    print(f"✓ wrote {len(msgs)} raw email(s) to {OUT_DIR} (gitignored)")
    print("  Next: scrub the interesting ones into tests/fixtures/synthetic/ (PII-free, committed).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
