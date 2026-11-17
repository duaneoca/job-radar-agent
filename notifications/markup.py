"""
Escaping for untrusted text placed into a notification channel's markup.

Email subjects, senders and error strings all reach a notification body, and all of them are
attacker-controlled: whoever sends mail to the user chooses them. Every channel here renders its
body as markup — Slack `mrkdwn`, Telegram `parse_mode=Markdown`, a Discord embed description — so
unescaped text is an injection vector, not merely a cosmetic one:

  • `<!channel>` / `<!here>` in Slack notifies every member of the channel.
  • `<@U024BE7LH>` in Slack pings a specific person.
  • `<https://evil.example|Open your inbox>` in Slack renders as a trusted-looking labelled link.
  • `*…*`, `_…_`, backticks and `[label](url)` forge formatting and links on Telegram and Discord.
  • Unbalanced Telegram markup makes the Bot API reject the whole message (HTTP 400), so a crafted
    subject can suppress the notification entirely — a denial of the user's own alerts.

This lives per channel rather than in `dispatch`, deliberately: dispatch builds ONE body without
knowing which notifier will deliver it, and the three escapes are mutually incompatible. The
notifier is the only place that knows its own markup, so it is the only correct boundary.
"""

from __future__ import annotations

# Slack's documented rule for text sent as mrkdwn: these three, ampersand first so the
# replacements it introduces are not themselves re-escaped.
_SLACK = (("&", "&amp;"), ("<", "&lt;"), (">", "&gt;"))

# Telegram legacy Markdown entity characters.
_TELEGRAM_CHARS = "_*`["

# Discord markdown: emphasis, strike, spoiler, code, quote, and link syntax.
_DISCORD_CHARS = "_*`~|>[]()"


def escape_slack(text: str | None) -> str:
    """Neutralise Slack mrkdwn. Defeats <!channel>, <@U…> and <url|label> at once, since every
    one of them depends on the angle brackets."""
    out = text or ""
    for find, repl in _SLACK:
        out = out.replace(find, repl)
    return out


def escape_telegram(text: str | None) -> str:
    """Backslash-escape Telegram legacy Markdown control characters."""
    return _backslash(text, _TELEGRAM_CHARS)


def escape_discord(text: str | None) -> str:
    """Backslash-escape Discord markdown control characters."""
    return _backslash(text, _DISCORD_CHARS)


def _backslash(text: str | None, chars: str) -> str:
    out = []
    for ch in text or "":
        if ch in chars or ch == "\\":
            out.append("\\")
        out.append(ch)
    return "".join(out)
