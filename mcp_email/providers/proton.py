"""
Proton provider — IMAP via Proton Bridge (local self-host path).

Proton Bridge runs on the host and exposes a local IMAP server (default 127.0.0.1:1143) with a
Bridge-specific password. From inside Docker on a Mac, reach it via `host.docker.internal`.

Implements read / mark-read / move only. `move_and_mark` prefers the atomic IMAP MOVE command
(RFC 6851) and falls back to COPY + \\Seen + \\Deleted + UID EXPUNGE where MOVE is unsupported.
We only ever expunge the single message we moved (UID EXPUNGE), never a blanket expunge.
"""

from __future__ import annotations

import email
import imaplib
import re
from datetime import datetime, timedelta, timezone
from email.header import decode_header, make_header
from email.utils import parsedate_to_datetime

from .base import EmailMessage, EmailProvider

_MESSAGE_ID_RE = re.compile(rb"Message-ID", re.IGNORECASE)


def _imap_date(since_days: int) -> str:
    """IMAP SEARCH SINCE date, e.g. '28-May-2026'. Date-granular (no time), per RFC 3501."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=since_days)
    return cutoff.strftime("%d-%b-%Y")


def _decode(value: str | None) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value


def _best_text(msg: email.message.Message) -> tuple[str, bool]:
    """Extract text/plain (preferred) or strip a text/html part. Never touch attachments. [M1]"""
    has_attachments = False
    plain: str | None = None
    html: str | None = None

    for part in msg.walk():
        if part.is_multipart():
            continue
        disp = str(part.get("Content-Disposition") or "")
        if "attachment" in disp.lower():
            has_attachments = True
            continue  # NEVER parse attachment payloads
        ctype = part.get_content_type()
        if ctype == "text/plain" and plain is None:
            plain = _payload(part)
        elif ctype == "text/html" and html is None:
            html = _payload(part)

    if plain is not None:
        return plain, has_attachments
    if html is not None:
        return _strip_html(html), has_attachments
    return "", has_attachments


def _payload(part: email.message.Message) -> str:
    try:
        raw = part.get_payload(decode=True) or b""
        charset = part.get_content_charset() or "utf-8"
        return raw.decode(charset, errors="replace")
    except Exception:
        return ""


def _strip_html(html: str) -> str:
    """Crude tag strip. We do NOT render HTML and do NOT fetch remote content. [M1/C2]"""
    text = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", html)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


class ProtonProvider(EmailProvider):
    def __init__(self, host: str, port: int, user: str, password: str):
        self._host, self._port = host, port
        self._user, self._password = user, password
        self._conn: imaplib.IMAP4 | None = None

    # ── connection ────────────────────────────────────────────
    def _imap(self) -> imaplib.IMAP4:
        if self._conn is None:
            # Bridge typically offers STARTTLS on 1143; fall back to plain on the local loopback.
            conn = imaplib.IMAP4(self._host, self._port)
            try:
                conn.starttls()
            except (imaplib.IMAP4.error, OSError):
                pass
            conn.login(self._user, self._password)
            self._conn = conn
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.logout()
            finally:
                self._conn = None

    # ── reads ─────────────────────────────────────────────────
    def list_folders(self) -> list[str]:
        conn = self._imap()
        typ, data = conn.list()
        if typ != "OK":
            return []
        out: list[str] = []
        for raw in data:
            if not raw:
                continue
            line = raw.decode(errors="replace")
            # last quoted token (or last whitespace token) is the folder path
            m = re.search(r'"([^"]*)"\s*$', line) or re.search(r"(\S+)\s*$", line)
            if m:
                out.append(m.group(1))
        return out

    def get_unread(
        self, folder: str, since_days: int | None = None, limit: int | None = None
    ) -> list[EmailMessage]:
        conn = self._imap()
        if conn.select(self._quote(folder))[0] != "OK":
            raise RuntimeError(f"cannot select folder: {folder}")
        # Filter at the server: an old unread backlog never gets fetched or classified.
        criteria: list = ["UNSEEN"]
        if since_days is not None:
            criteria += ["SINCE", _imap_date(since_days)]
        typ, data = conn.uid("SEARCH", None, *criteria)
        if typ != "OK" or not data or not data[0]:
            return []
        uids = data[0].split()
        messages = [m for uid in uids if (m := self._fetch_by_uid(folder, uid)) is not None]
        # Newest-first so a capped run handles the most recent (most relevant) mail.
        messages.sort(key=lambda m: m.received_at or datetime.min.replace(tzinfo=timezone.utc),
                      reverse=True)
        return messages[:limit] if limit else messages

    def get_email(self, message_id: str) -> EmailMessage | None:
        conn = self._imap()
        for folder in self.list_folders():
            if conn.select(self._quote(folder))[0] != "OK":
                continue
            uid = self._find_uid(message_id)
            if uid:
                return self._fetch_by_uid(folder, uid)
        return None

    # ── the one mutation: mark-read + move (never delete) ─────
    def move_and_mark(self, message_id: str, dest_folder: str, mark_read: bool = True) -> None:
        conn = self._imap()
        # locate the message's current folder + uid
        for folder in self.list_folders():
            if conn.select(self._quote(folder))[0] != "OK":
                continue
            uid = self._find_uid(message_id)
            if not uid:
                continue
            if mark_read:
                conn.uid("STORE", uid, "+FLAGS", "(\\Seen)")
            # Prefer atomic MOVE (RFC 6851)
            typ, _ = conn.uid("MOVE", uid, self._quote(dest_folder))
            if typ == "OK":
                return
            # Fallback: COPY + flag deleted + UID EXPUNGE (only this uid)
            if conn.uid("COPY", uid, self._quote(dest_folder))[0] != "OK":
                raise RuntimeError(f"COPY to {dest_folder} failed for {message_id}")
            conn.uid("STORE", uid, "+FLAGS", "(\\Deleted)")
            try:
                conn.uid("EXPUNGE", uid)  # RFC 4315 UID EXPUNGE — only the moved message
            except imaplib.IMAP4.error:
                conn.expunge()
            return
        raise LookupError(f"message not found: {message_id}")

    # ── helpers ───────────────────────────────────────────────
    @staticmethod
    def _quote(folder: str) -> str:
        return f'"{folder}"'

    def _find_uid(self, message_id: str) -> bytes | None:
        conn = self._imap()
        typ, data = conn.uid("SEARCH", None, "HEADER", "Message-ID", message_id)
        if typ == "OK" and data and data[0]:
            return data[0].split()[0]
        return None

    def _fetch_by_uid(self, folder: str, uid: bytes) -> EmailMessage | None:
        conn = self._imap()
        # BODY.PEEK[] reads WITHOUT setting \\Seen. Plain RFC822/BODY[] would implicitly mark the
        # message read — which must NEVER happen on a read. \\Seen is set ONLY by move_and_mark.
        typ, data = conn.uid("FETCH", uid, "(BODY.PEEK[])")
        if typ != "OK" or not data or not data[0]:
            return None
        raw = data[0][1]
        if not isinstance(raw, (bytes, bytearray)):
            return None
        msg = email.message_from_bytes(raw)
        body, has_att = _best_text(msg)
        received: datetime | None = None
        if msg.get("Date"):
            try:
                received = parsedate_to_datetime(msg["Date"])
            except (TypeError, ValueError):
                received = None
        return EmailMessage(
            message_id=(msg.get("Message-ID") or "").strip(),
            native_id=uid.decode() if isinstance(uid, bytes) else str(uid),
            folder=folder,
            subject=_decode(msg.get("Subject")),
            sender=_decode(msg.get("From")),
            received_at=received,
            body_text=body,
            has_attachments=has_att,
            headers={k: _decode(v) for k, v in msg.items()},
        )
