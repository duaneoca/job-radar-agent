"""
Discord notifier — incoming webhook with an embed. Routes user vs admin to two webhook URLs.
Note: interactive buttons require a Discord *application* + interactions endpoint, not a webhook —
so HITL buttons degrade to listing the choices as text here (link button is supported via the embed).
"""

from __future__ import annotations

import httpx

from .base import Notification
from .markup import escape_discord


class DiscordNotifier:
    def __init__(self, user_webhook: str, admin_webhook: str | None = None, timeout: float = 15.0):
        self._user = user_webhook
        self._admin = admin_webhook or user_webhook
        self._client = httpx.Client(timeout=timeout)

    def close(self) -> None:
        self._client.close()

    def send(self, n: Notification) -> None:
        url = self._admin if n.audience == "admin" else self._user
        desc = escape_discord(n.body)
        if n.link:
            desc += f"\n{escape_discord(n.link_text)}: {n.link}"
        if n.buttons:  # webhooks can't render buttons — list choices as text
            desc += "\n\nReply in the app to choose:\n" + "\n".join(
                f"• {escape_discord(b['text'])}" for b in n.buttons)
        embed = {"title": escape_discord(n.title)[:256], "description": desc[:4000]}
        if n.fields:
            embed["fields"] = [{"name": escape_discord(k), "value": escape_discord(v),
                                "inline": True} for k, v in n.fields.items()][:25]
        # Belt and braces behind the escaping: even if a mention survived, the webhook is not
        # permitted to notify anyone.
        r = self._client.post(url, json={"embeds": [embed],
                                         "allowed_mentions": {"parse": []}})
        r.raise_for_status()
