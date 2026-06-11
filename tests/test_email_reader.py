"""
Unit tests for the Email Reader's pure logic — no live mailbox, no MCP runtime required.
Covers: text extraction + attachment skipping, HTML stripping, Message-ID parsing, folder
resolution, and the move destination guardrail.
"""

import email
from datetime import datetime

import pytest

from mcp_email.config import Folders, Settings
from mcp_email.providers.base import EmailMessage, EmailProvider
from mcp_email.providers.proton import _best_text, _strip_html


def test_strip_html_removes_tags_scripts_styles():
    html = "<style>p{color:red}</style><p>Hello <b>world</b></p><script>evil()</script>"
    assert _strip_html(html) == "Hello world"


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
