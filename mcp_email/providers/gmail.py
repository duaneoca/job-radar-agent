"""
Gmail provider — Gmail API via OAuth2 (cloud multi-user path; also usable locally).

Folders are Gmail LABELS. The root (e.g. "Hire Duane") and its nested subfolders
("Hire Duane/Interaction", …) are labels. "Move" = label swap: add the destination label, remove
the root label, and remove UNREAD (mark read). We only ever call list/get/modify — never send or
trash — so the no-send/no-delete guarantee is enforced in code (the H5 residual-scope risk).

Scope: gmail.modify (minimum that supports messages.modify). Reads use format=full but never fetch
attachment bodies. `message_id` = RFC 822 Message-ID header (stable); `native_id` = Gmail message id.
"""

from __future__ import annotations

import base64
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

from .base import EmailMessage, EmailProvider
from .proton import _strip_html  # shared HTML→text helper

SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]


def _b64(data: str | None) -> str:
    if not data:
        return ""
    try:
        return base64.urlsafe_b64decode(data.encode()).decode("utf-8", errors="replace")
    except Exception:
        return ""


class GmailProvider(EmailProvider):
    def __init__(self, token_file: str = "token.json", credentials_file: str = "credentials.json",
                 root_folder: str = "Hire Duane"):
        self._token_file = token_file
        self._creds_file = credentials_file
        self._root = root_folder
        self._svc = None
        self._label_ids: dict[str, str] | None = None   # name → id

    # ── service / labels ──────────────────────────────────────
    def _service(self):
        if self._svc is None:
            from google.oauth2.credentials import Credentials
            from google.auth.transport.requests import Request
            from googleapiclient.discovery import build
            creds = Credentials.from_authorized_user_file(self._token_file, SCOPES)
            if creds.expired and creds.refresh_token:
                creds.refresh(Request())
                with open(self._token_file, "w") as f:
                    f.write(creds.to_json())
            self._svc = build("gmail", "v1", credentials=creds, cache_discovery=False)
        return self._svc

    def _labels(self) -> dict[str, str]:
        if self._label_ids is None:
            data = self._service().users().labels().list(userId="me").execute()
            self._label_ids = {l["name"]: l["id"] for l in data.get("labels", [])}
        return self._label_ids

    def _label_id(self, name: str) -> str:
        lid = self._labels().get(name)
        if not lid:
            raise LookupError(f"Gmail label not found: {name!r} (create it in Gmail)")
        return lid

    # ── reads ─────────────────────────────────────────────────
    def list_folders(self) -> list[str]:
        return sorted(self._labels().keys())

    def get_unread(self, folder: str, since_days: int | None = None,
                   limit: int | None = None) -> list[EmailMessage]:
        svc = self._service()
        label_ids = [self._label_id(folder), "UNREAD"]
        kwargs = {"userId": "me", "labelIds": label_ids, "maxResults": limit or 100}
        if since_days is not None:
            kwargs["q"] = f"newer_than:{since_days}d"
        resp = svc.users().messages().list(**kwargs).execute()   # newest-first by internalDate
        ids = [m["id"] for m in resp.get("messages", [])]
        msgs = [self._fetch(mid, folder) for mid in ids]
        return [m for m in msgs if m is not None]

    def get_email(self, message_id: str) -> EmailMessage | None:
        svc = self._service()
        resp = svc.users().messages().list(
            userId="me", q=f"rfc822msgid:{message_id}", maxResults=1).execute()
        ids = [m["id"] for m in resp.get("messages", [])]
        return self._fetch(ids[0], self._root) if ids else None

    # ── the one mutation: label swap + mark read (never send/trash) ──
    def move_and_mark(self, message_id: str, dest_folder: str) -> None:
        svc = self._service()
        resp = svc.users().messages().list(
            userId="me", q=f"rfc822msgid:{message_id}", maxResults=1).execute()
        ids = [m["id"] for m in resp.get("messages", [])]
        if not ids:
            raise LookupError(f"message not found: {message_id}")
        svc.users().messages().modify(userId="me", id=ids[0], body={
            "addLabelIds": [self._label_id(dest_folder)],
            "removeLabelIds": [self._label_id(self._root), "UNREAD"],
        }).execute()

    # ── parsing ───────────────────────────────────────────────
    def _fetch(self, gmail_id: str, folder: str) -> EmailMessage | None:
        msg = self._service().users().messages().get(
            userId="me", id=gmail_id, format="full").execute()
        payload = msg.get("payload", {})
        headers = {h["name"]: h["value"] for h in payload.get("headers", [])}
        body, has_att = self._extract_body(payload)
        received = None
        if msg.get("internalDate"):
            received = datetime.fromtimestamp(int(msg["internalDate"]) / 1000, tz=timezone.utc)
        elif headers.get("Date"):
            try:
                received = parsedate_to_datetime(headers["Date"])
            except (TypeError, ValueError):
                received = None
        return EmailMessage(
            message_id=(headers.get("Message-ID") or headers.get("Message-Id") or "").strip(),
            native_id=gmail_id, folder=folder,
            subject=headers.get("Subject", ""), sender=headers.get("From", ""),
            received_at=received, body_text=body, has_attachments=has_att, headers=headers,
        )

    def _extract_body(self, payload: dict) -> tuple[str, bool]:
        """text/plain preferred; else stripped text/html. Attachments flagged, never fetched. [M1]"""
        plain: str | None = None
        html: str | None = None
        has_att = False

        def walk(part):
            nonlocal plain, html, has_att
            mime = part.get("mimeType", "")
            filename = part.get("filename")
            body = part.get("body", {})
            if filename and body.get("attachmentId"):
                has_att = True
                return                                   # never download attachment bodies
            if mime == "text/plain" and plain is None:
                plain = _b64(body.get("data"))
            elif mime == "text/html" and html is None:
                html = _b64(body.get("data"))
            for sub in part.get("parts", []) or []:
                walk(sub)

        walk(payload)
        if plain is not None:
            return plain, has_att
        if html is not None:
            return _strip_html(html), has_att
        return "", has_att
