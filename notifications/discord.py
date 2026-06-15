"""
Discord notifier — incoming webhook with an embed. Routes user vs admin to two webhook URLs.
Note: interactive buttons require a Discord *application* + interactions endpoint, not a webhook —
so HITL buttons degrade to listing the choices as text here (link button is supported via the embed).
"""

from __future__ import annotations

import httpx

from .base import Notification


class DiscordNotifier:
    def __init__(self, user_webhook: str, admin_webhook: str | None = None, timeout: float = 15.0):
        self._user = user_webhook
        self._admin = admin_webhook or user_webhook
        self._client = httpx.Client(timeout=timeout)

    def close(self) -> None:
        self._client.close()

    def send(self, n: Notification) -> None:
        url = self._admin if n.audience == "admin" else self._user
        desc = n.body
        if n.link:
            desc += f"\n{n.link_text}: {n.link}"
        if n.buttons:  # webhooks can't render buttons — list choices as text
            desc += "\n\nReply in the app to choose:\n" + "\n".join(f"• {b['text']}" for b in n.buttons)
        embed = {"title": n.title[:256], "description": desc[:4000]}
        if n.fields:
            embed["fields"] = [{"name": k, "value": v, "inline": True} for k, v in n.fields.items()][:25]
        r = self._client.post(url, json={"embeds": [embed]})
        r.raise_for_status()
