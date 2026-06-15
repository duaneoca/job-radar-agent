"""
Slack notifier — Block Kit via chat.postMessage (bot token). Routes user vs admin to two channels.
HITL prompts render as interactive buttons whose action value encodes hitl_id+choice; the click is
delivered to job-radar's signed callback (the agent only POSTS the message, it never receives).
"""

from __future__ import annotations

import httpx

from .base import Notification


class SlackNotifier:
    def __init__(self, bot_token: str, user_channel: str, admin_channel: str | None = None,
                 timeout: float = 15.0):
        self._token = bot_token
        self._user_channel = user_channel
        self._admin_channel = admin_channel or user_channel
        self._client = httpx.Client(
            base_url="https://slack.com/api",
            headers={"Authorization": f"Bearer {bot_token}", "Content-Type": "application/json"},
            timeout=timeout,
        )

    def close(self) -> None:
        self._client.close()

    def _blocks(self, n: Notification) -> list[dict]:
        blocks: list[dict] = [
            {"type": "header", "text": {"type": "plain_text", "text": n.title[:150]}},
        ]
        if n.body:
            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": n.body[:3000]}})
        if n.fields:
            flds = [{"type": "mrkdwn", "text": f"*{k}:*\n{v}"} for k, v in list(n.fields.items())[:10]]
            blocks.append({"type": "section", "fields": flds})
        if n.buttons:
            blocks.append({"type": "actions", "elements": [
                {"type": "button", "text": {"type": "plain_text", "text": b["text"][:75]},
                 "value": b["value"], "action_id": f"hitl:{b['value']}"[:255]}
                for b in n.buttons[:5]
            ]})
        elif n.link:
            blocks.append({"type": "actions", "elements": [
                {"type": "button", "text": {"type": "plain_text", "text": n.link_text[:75]},
                 "url": n.link}
            ]})
        return blocks

    def send(self, n: Notification) -> None:
        channel = self._admin_channel if n.audience == "admin" else self._user_channel
        r = self._client.post("/chat.postMessage", json={
            "channel": channel, "text": n.title, "blocks": self._blocks(n),
        })
        r.raise_for_status()
        data = r.json()
        if not data.get("ok"):
            raise RuntimeError(f"slack error: {data.get('error')}")
