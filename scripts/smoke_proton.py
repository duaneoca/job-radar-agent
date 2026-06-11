"""
Read-only smoke test for the Proton/IMAP provider — run on the host where Proton Bridge runs.

Credentials come from the environment so nothing secret is hard-coded:
    PROTON_IMAP_HOST   (default 127.0.0.1)
    PROTON_IMAP_PORT   (default 1143)
    PROTON_IMAP_USER
    PROTON_IMAP_PASSWORD   (the Bridge-specific password, NOT your Proton login)
    EMAIL_ROOT_FOLDER  (default hire-duane)

Does ONLY: login, list folders, count unread in the root. No moves, no writes, no bodies printed.
Usage:
    export PROTON_IMAP_USER=...  PROTON_IMAP_PASSWORD=...
    python scripts/smoke_proton.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mcp_email.providers.proton import ProtonProvider  # noqa: E402

HOST = os.environ.get("PROTON_IMAP_HOST", "127.0.0.1")
PORT = int(os.environ.get("PROTON_IMAP_PORT", "1143"))
USER = os.environ.get("PROTON_IMAP_USER", "")
PASSWORD = os.environ.get("PROTON_IMAP_PASSWORD", "")
ROOT = os.environ.get("EMAIL_ROOT_FOLDER", "hire-duane")
SUBFOLDERS = ["Interaction", "Postings", "Social", "Unprocessed"]


def main() -> int:
    if not USER or not PASSWORD:
        print("✗ Set PROTON_IMAP_USER and PROTON_IMAP_PASSWORD (Bridge password) first.")
        return 2

    print(f"→ connecting to {HOST}:{PORT} as {USER} ...")
    p = ProtonProvider(HOST, PORT, USER, PASSWORD)
    try:
        folders = p.list_folders()
        print(f"✓ login OK — {len(folders)} folders visible")

        expected = [ROOT] + [f"{ROOT}/{s}" for s in SUBFOLDERS]
        for f in expected:
            mark = "✓" if f in folders else "✗ MISSING (create it in your mail client)"
            print(f"   {mark}  {f}")

        if ROOT not in folders:
            print(f"✗ root folder '{ROOT}' not found — check EMAIL_ROOT_FOLDER / Bridge naming.")
            print("   all folders seen:", folders)
            return 1

        age = int(os.environ.get("MAX_EMAIL_AGE_DAYS", "14"))
        unread = p.get_unread(ROOT, since_days=age, limit=25)
        print(f"✓ {len(unread)} UNREAD (≤{age} days old, newest-first, capped 25) in '{ROOT}'")
        for m in unread[:10]:
            mid = (m.message_id or "<no Message-ID>")[:60]
            att = " [has attachment]" if m.has_attachments else ""
            print(f"   • {m.subject[:60]!r:62}  from {m.sender[:40]}  id={mid}{att}")
        if len(unread) > 10:
            print(f"   … and {len(unread) - 10} more")
        return 0
    finally:
        p.close()


if __name__ == "__main__":
    raise SystemExit(main())
