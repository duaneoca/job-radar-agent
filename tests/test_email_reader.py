"""
Unit tests for the Email Reader's pure logic — no live mailbox, no MCP runtime required.
Covers: text extraction + attachment skipping, HTML stripping, Message-ID parsing, folder
resolution, and the move destination guardrail.
"""

import email
import re
from datetime import datetime

import pytest

from mcp_email.config import Folders, Settings
from mcp_email.providers.base import EmailMessage, EmailProvider
from mcp_email.providers.proton import _best_text, _choose_text, _strip_html


def test_strip_html_removes_tags_scripts_styles():
    html = "<style>p{color:red}</style><p>Hello <b>world</b></p><script>evil()</script>"
    assert _strip_html(html) == "Hello world"


def test_strip_html_preserves_link_urls():
    # Job-alert links live in <a href>; they must survive the strip so the classifier can extract them.
    html = '<p>FDE at Origin <a href="https://linkedin.com/jobs/view/123?ref=x">View job</a></p>'
    out = _strip_html(html)
    assert "https://linkedin.com/jobs/view/123?ref=x" in out
    assert "View job (https://linkedin.com/jobs/view/123?ref=x)" in out


def test_strip_html_drops_non_http_hrefs():
    # Only http/https survive (matches the writer's clean_link allowlist [C2]).
    assert "javascript" not in _strip_html('<a href="javascript:void(0)">x</a>')
    assert "data:" not in _strip_html('<a href="data:text/html,evil">x</a>')


def test_choose_text_uses_html_when_plain_lacks_links():
    # LinkedIn-style: plain part has no URLs, links only in HTML → fall back to link-preserving HTML.
    plain = "FDE at Origin\nView job"
    html = '<a href="https://x.com/job/1">View job</a>'
    assert _choose_text(plain, html) == "View job (https://x.com/job/1)"
    # but a plain body that already has a URL is preferred (cleaner)
    assert _choose_text("apply: https://y.com/1", html) == "apply: https://y.com/1"


def test_best_text_prefers_plain_and_skips_attachments():
    raw = (
        "From: r@acme.com\r\n"
        "Subject: hi\r\n"
        "MIME-Version: 1.0\r\n"
        'Content-Type: multipart/mixed; boundary="b"\r\n\r\n'
        "--b\r\nContent-Type: text/plain\r\n\r\nPlain body here\r\n"
        "--b\r\nContent-Type: application/pdf\r\n"
        'Content-Disposition: attachment; filename="resume.pdf"\r\n\r\n'
        "%PDF-1.4 binary junk\r\n"
        "--b--\r\n"
    )
    msg = email.message_from_string(raw)
    body, has_att = _best_text(msg)
    assert body == "Plain body here"
    assert has_att is True
    assert "PDF" not in body  # attachment payload never enters the body


def test_best_text_falls_back_to_stripped_html():
    raw = (
        "Subject: hi\r\nMIME-Version: 1.0\r\n"
        "Content-Type: text/html\r\n\r\n<p>Click <a href='x'>here</a></p>"
    )
    body, has_att = _best_text(email.message_from_string(raw))
    assert body == "Click here"
    assert has_att is False


def test_folder_resolution_nests_under_root():
    s = Settings(email_root_folder="hire-duane", email_folder_interaction="Interaction")
    f = Folders.from_settings(s)
    assert f.root == "hire-duane"
    assert f.interaction == "hire-duane/Interaction"
    assert "hire-duane/Unprocessed" in f.all_subfolders()


def test_interface_has_no_delete_or_archive():
    # Guardrail by absence: the provider contract exposes no destructive operation.
    names = set(dir(EmailProvider))
    assert "move_and_mark" in names
    assert not {"delete", "delete_email", "archive", "trash", "expunge"} & names


def test_email_message_uses_message_id_as_key():
    m = EmailMessage(
        message_id="<abc@mail>", native_id="42", folder="hire-duane",
        subject="s", sender="a@b.com", received_at=datetime(2026, 6, 10), body_text="x",
    )
    assert m.message_id == "<abc@mail>"
    assert m.native_id == "42"  # native id is incidental, never the key


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))


# ── regression: reads must NEVER set \Seen (BODY.PEEK), only move_and_mark does ──

class _FakeIMAP:
    """Minimal IMAP double that records every uid() command for assertions."""

    def __init__(self):
        self.commands = []
        self._raw = (
            b"Message-ID: <m1@x>\r\nFrom: r@acme.com\r\nSubject: hi\r\n"
            b"Content-Type: text/plain\r\n\r\nbody\r\n"
        )

    def list(self):
        return "OK", [b'(\\HasNoChildren) "/" "Folders/Hire Duane"']

    def select(self, folder):
        return "OK", [b"1"]

    def uid(self, command, *args):
        self.commands.append((command.upper(), args))
        cmd = command.upper()
        if cmd == "SEARCH":
            return "OK", [b"1 2"]
        if cmd == "FETCH":
            return "OK", [(b"1 (BODY[] {44}", self._raw), b")"]
        if cmd in ("STORE", "COPY", "MOVE", "EXPUNGE"):
            return "OK", [b""]
        return "OK", [b""]


def _provider_with_fake(fake):
    from mcp_email.providers.proton import ProtonProvider
    p = ProtonProvider("h", 1, "u", "pw")
    p._conn = fake
    return p


def test_get_unread_never_sets_seen_and_uses_peek():
    fake = _FakeIMAP()
    p = _provider_with_fake(fake)
    p.get_unread("Folders/Hire Duane")
    fetches = [a for c, a in fake.commands if c == "FETCH"]
    assert fetches, "expected at least one FETCH"
    assert all("BODY.PEEK[]" in " ".join(map(str, a)) for a in fetches), "reads must use BODY.PEEK"
    # No flag mutation may occur during a read.
    stores = [a for c, a in fake.commands if c == "STORE"]
    assert stores == [], f"reads must not STORE flags, got {stores}"


def test_move_and_mark_sets_seen_then_moves():
    fake = _FakeIMAP()
    p = _provider_with_fake(fake)
    p.move_and_mark("<m1@x>", "Folders/Hire Duane/Postings")
    seen_stores = [a for c, a in fake.commands
                   if c == "STORE" and "\\Seen" in " ".join(map(str, a))]
    moves = [c for c, _ in fake.commands if c in ("MOVE", "COPY")]
    assert seen_stores, "move_and_mark must mark \\Seen"
    assert moves, "move_and_mark must relocate the message"


def test_move_and_mark_can_leave_unread():
    fake = _FakeIMAP()
    p = _provider_with_fake(fake)
    p.move_and_mark("<m1@x>", "Folders/Hire Duane/Interaction", mark_read=False)
    seen_stores = [a for c, a in fake.commands
                   if c == "STORE" and "\\Seen" in " ".join(map(str, a))]
    moves = [c for c, _ in fake.commands if c in ("MOVE", "COPY")]
    assert seen_stores == [], "mark_read=False must NOT set \\Seen"
    assert moves, "must still relocate the message"


# ── age cutoff + newest-first + limit ─────────────────────────

def test_imap_date_format():
    from mcp_email.providers.proton import _imap_date
    s = _imap_date(14)
    assert re.match(r"^\d{2}-[A-Z][a-z]{2}-\d{4}$", s), s


class _DatedFakeIMAP(_FakeIMAP):
    """Serves 3 unread messages with distinct Dates so we can check ordering/limit."""

    def __init__(self):
        super().__init__()
        self._msgs = {
            b"1": b"Message-ID: <old@x>\r\nSubject: old\r\nDate: Mon, 01 Jun 2026 10:00:00 +0000\r\n\r\nx\r\n",
            b"2": b"Message-ID: <new@x>\r\nSubject: new\r\nDate: Wed, 10 Jun 2026 10:00:00 +0000\r\n\r\nx\r\n",
            b"3": b"Message-ID: <mid@x>\r\nSubject: mid\r\nDate: Tue, 05 Jun 2026 10:00:00 +0000\r\n\r\nx\r\n",
        }

    def uid(self, command, *args):
        self.commands.append((command.upper(), args))
        cmd = command.upper()
        if cmd == "SEARCH":
            return "OK", [b"1 2 3"]
        if cmd == "FETCH":
            uid = args[0]
            return "OK", [(b"x (BODY[] {n}", self._msgs[uid]), b")"]
        return "OK", [b""]


def test_get_unread_includes_since_and_sorts_newest_first():
    fake = _DatedFakeIMAP()
    p = _provider_with_fake(fake)
    msgs = p.get_unread("Folders/Hire Duane", since_days=14)
    # SINCE present in the SEARCH
    search_args = [a for c, a in fake.commands if c == "SEARCH"][0]
    assert "SINCE" in search_args
    # newest-first by Date header
    assert [m.subject for m in msgs] == ["new", "mid", "old"]


def test_get_unread_limit_caps_newest():
    fake = _DatedFakeIMAP()
    p = _provider_with_fake(fake)
    msgs = p.get_unread("Folders/Hire Duane", since_days=14, limit=2)
    assert [m.subject for m in msgs] == ["new", "mid"]
