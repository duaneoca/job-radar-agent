"""
Untrusted text must not be able to inject channel markup.

Email subjects reach the notification body (the Unprocessed listing), and whoever emails the user
chooses them. Each channel renders its body as markup, so an unescaped subject is an injection:
a channel-wide ping, a ping of a named person, a trusted-looking link, or forged formatting.

These tests drive the real notifiers with a fake httpx client and assert on the exact payload
that would go over the wire.
"""

from __future__ import annotations

import importlib
import sys
import types

import pytest

from notifications import markup
from notifications.base import Notification

# A single subject carrying every trick at once.
HOSTILE = "<!channel> <!here> <@U024BE7LH> <https://evil.example|Open your inbox> *bold* _it_ `c` [l](u)"


# ── the helpers ───────────────────────────────────────────────

def test_slack_escape_neutralises_mentions_and_links():
    out = markup.escape_slack("<!channel> <@U1> <https://evil.example|Click>")
    assert "<" not in out and ">" not in out
    assert "&lt;!channel&gt;" in out


def test_slack_escape_does_ampersand_first():
    """& must be escaped before < and >, or &lt; would become &amp;lt;."""
    assert markup.escape_slack("a & b < c") == "a &amp; b &lt; c"


@pytest.mark.parametrize("ch", list("_*`["))
def test_telegram_escape_covers_each_control_char(ch):
    assert markup.escape_telegram(f"x{ch}y") == f"x\\{ch}y"


@pytest.mark.parametrize("ch", list("_*`~|>[]()"))
def test_discord_escape_covers_each_control_char(ch):
    assert markup.escape_discord(f"x{ch}y") == f"x\\{ch}y"


def test_backslash_itself_is_escaped():
    assert markup.escape_telegram("a\\b") == "a\\\\b"
    assert markup.escape_discord("a\\b") == "a\\\\b"


def test_escapes_tolerate_none():
    assert markup.escape_slack(None) == ""
    assert markup.escape_telegram(None) == ""
    assert markup.escape_discord(None) == ""


# ── through the real notifiers ────────────────────────────────

def _fake_httpx(monkeypatch, calls):
    class _Resp:
        def raise_for_status(self): ...
        def json(self): return {"ok": True}

    class _Client:
        def __init__(self, **kw): ...
        def post(self, path, json=None):
            calls["path"] = path
            calls["json"] = json
            return _Resp()
        def close(self): ...

    fake = types.ModuleType("httpx")
    fake.Client = _Client
    monkeypatch.setitem(sys.modules, "httpx", fake)


def test_slack_body_cannot_ping_the_channel(monkeypatch):
    calls: dict = {}
    _fake_httpx(monkeypatch, calls)
    slack = importlib.reload(importlib.import_module("notifications.slack"))
    slack.SlackNotifier("xoxb-t", "#user").send(
        Notification("user", "2 item(s) need your review", body=f"• {HOSTILE}"))

    blocks = calls["json"]["blocks"]
    body = next(b["text"]["text"] for b in blocks if b.get("text", {}).get("type") == "mrkdwn")
    # No unescaped angle bracket survives, which is what every Slack trick depends on.
    assert "<" not in body and ">" not in body
    assert "&lt;!channel&gt;" in body
    # The link attempt remains as inert visible text rather than a rendered labelled link —
    # the user sees that something tried, which is better than it disappearing.
    assert "&lt;https://evil.example|Open your inbox&gt;" in body
    # the fallback `text` is parsed for mentions too
    assert "<!channel>" not in calls["json"]["text"]


def test_slack_header_stays_plain_text(monkeypatch):
    """plain_text is not parsed, so the title must NOT be double-escaped into visible entities."""
    calls: dict = {}
    _fake_httpx(monkeypatch, calls)
    slack = importlib.reload(importlib.import_module("notifications.slack"))
    slack.SlackNotifier("xoxb-t", "#user").send(Notification("user", "3 item(s) need your review"))
    header = calls["json"]["blocks"][0]
    assert header["text"]["type"] == "plain_text"
    assert header["text"]["text"] == "3 item(s) need your review"


def test_telegram_body_cannot_forge_markup(monkeypatch):
    calls: dict = {}
    _fake_httpx(monkeypatch, calls)
    tg = importlib.reload(importlib.import_module("notifications.telegram"))
    tg.TelegramNotifier("tok", "123").send(
        Notification("user", "2 item(s) need your review", body=f"• {HOSTILE}"))

    text = calls["json"]["text"]
    assert calls["json"]["parse_mode"] == "Markdown"
    # our own bold wrapper around the title survives
    assert text.startswith("*2 item(s) need your review*")
    # every control character from the subject is neutralised
    body = text.split("\n", 1)[1]
    assert "\\*bold\\*" in body and "\\_it\\_" in body and "\\`c\\`" in body
    assert "*bold*" not in body and "_it_" not in body
    # An inline link needs an unescaped opening bracket, so escaping `[` alone defeats it.
    # `]`, `(` and `)` are deliberately NOT escaped: they carry no meaning on their own and
    # backslashes in front of them would disfigure ordinary subjects like "Engineer (Remote)".
    assert "\\[l](u)" in body


def test_discord_body_cannot_forge_markup_and_cannot_mention(monkeypatch):
    calls: dict = {}
    _fake_httpx(monkeypatch, calls)
    dc = importlib.reload(importlib.import_module("notifications.discord"))
    dc.DiscordNotifier("https://discord.example/hook").send(
        Notification("user", "2 item(s) need your review", body=f"• {HOSTILE}"))

    desc = calls["json"]["embeds"][0]["description"]
    assert "\\*bold\\*" in desc and "\\[l\\]\\(u\\)" in desc
    assert calls["json"]["allowed_mentions"] == {"parse": []}


def test_admin_fields_are_escaped_too(monkeypatch):
    """Admin bodies embed exception strings, which can carry subject text."""
    calls: dict = {}
    _fake_httpx(monkeypatch, calls)
    slack = importlib.reload(importlib.import_module("notifications.slack"))
    slack.SlackNotifier("xoxb-t", "#user", "#admin").send(
        Notification("admin", "Agent run failed", body="<!here> boom",
                     fields={"processed": "<!channel>"}))
    payload = str(calls["json"])
    assert "<!here>" not in payload and "<!channel>" not in payload
