"""
One-time Gmail OAuth bootstrap (local / desktop flow).

Reads an OAuth *Desktop* client (credentials.json), opens a browser for you to authorize the
MINIMAL scope (gmail.modify — label add/remove + mark-read; never full mail), and writes token.json.
Both files are gitignored. Re-run only if the token is revoked/expired beyond refresh.

Scope rationale (H5): gmail.modify is the least scope that supports our move-and-mark (Gmail "moves"
are label changes) and read. We never request https://mail.google.com/ (full access).

Usage (after creating credentials.json — see the walkthrough):
    pip install google-auth-oauthlib google-api-python-client
    python scripts/gmail_auth.py
    # then verify:
    python scripts/gmail_auth.py --check
"""

import os
import sys

CREDS = os.environ.get("GMAIL_CREDENTIALS_FILE", "credentials.json")
TOKEN = os.environ.get("GMAIL_TOKEN_FILE", "token.json")
SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]


def _load_creds():
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    creds = None
    if os.path.exists(TOKEN):
        creds = Credentials.from_authorized_user_file(TOKEN, SCOPES)
    if creds and creds.valid:
        return creds
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        return creds
    return None


def authorize() -> int:
    from google_auth_oauthlib.flow import InstalledAppFlow
    if not os.path.exists(CREDS):
        print(f"✗ {CREDS} not found — download your OAuth Desktop client from Google Cloud first.")
        return 2
    creds = _load_creds()
    if creds is None:
        flow = InstalledAppFlow.from_client_secrets_file(CREDS, SCOPES)
        creds = flow.run_local_server(port=0)  # opens a browser, captures the redirect
        with open(TOKEN, "w") as f:
            f.write(creds.to_json())
        print(f"✓ authorized — wrote {TOKEN}")
    else:
        print(f"✓ already authorized ({TOKEN} valid)")
    return 0


def check() -> int:
    from googleapiclient.discovery import build
    creds = _load_creds()
    if not creds:
        print("✗ no valid token — run without --check first.")
        return 1
    svc = build("gmail", "v1", credentials=creds)
    profile = svc.users().getProfile(userId="me").execute()
    labels = svc.users().labels().list(userId="me").execute().get("labels", [])
    print(f"✓ connected as {profile.get('emailAddress')}  ({profile.get('messagesTotal')} msgs)")
    names = sorted(l["name"] for l in labels)
    hire = [n for n in names if n.lower().startswith("hire")]
    print(f"  labels: {len(names)} total; matching 'Hire*': {hire or '(none yet — create them)'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(check() if "--check" in sys.argv else authorize())
