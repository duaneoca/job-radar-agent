"""
Read-only smoke test for the Gmail provider (after gmail_auth.py). Lists labels, confirms the
configured Hire-Duane labels exist, and shows unread in the root label. No moves, no writes.

Usage:
    EMAIL_ROOT_FOLDER="Hire Duane" python scripts/smoke_gmail.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mcp_email.providers.gmail import GmailProvider  # noqa: E402

ROOT = os.environ.get("EMAIL_ROOT_FOLDER", "Hire Duane")
SUBS = ["Interaction", "Postings", "Social", "Unprocessed"]
AGE = int(os.environ.get("MAX_EMAIL_AGE_DAYS", "14"))


def main() -> int:
    p = GmailProvider(
        token_file=os.environ.get("GMAIL_TOKEN_FILE", "token.json"),
        credentials_file=os.environ.get("GMAIL_CREDENTIALS_FILE", "credentials.json"),
        root_folder=ROOT,
    )
    folders = p.list_folders()
    print(f"✓ {len(folders)} labels visible")
    for f in [ROOT] + [f"{ROOT}/{s}" for s in SUBS]:
        print(f"   {'✓' if f in folders else '✗ MISSING'}  {f}")
    if ROOT not in folders:
        print(f"✗ root label '{ROOT}' not found"); return 1

    age = AGE if AGE > 0 else None
    unread = p.get_unread(ROOT, since_days=age, limit=25)
    print(f"✓ {len(unread)} UNREAD ({'≤%dd' % age if age else 'all ages'}, newest-first) in '{ROOT}'")
    for m in unread[:10]:
        att = " [attachment]" if m.has_attachments else ""
        print(f"   • {m.subject[:60]!r:62} from {m.sender[:38]} id={(m.message_id or '<none>')[:40]}{att}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
