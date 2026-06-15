"""
Telegram notifier — Bot API sendMessage with an optional inline-keyboard button (link or HITL).
HITL buttons use callback_data (the click goes to a bot webhook; for the pull-model the agent only
sends, job-radar's callback records the decision). Routes user vs admin to two chat ids.
"""

from __future__ import annotations

import httpx

from .base import Notification


class TelegramNotifier:
    def __init__(self, bot_token: str, user_chat_id: str, admin_chat_id: str | None = None,
                 timeout: float = 15.0):
        self._user_chat = user_chat_id
        self._admin_chat = admin_chat_id or user_chat_id
        self._client = httpx.Client(base_url=f"https://api.telegram.org/bot{bot_token}",
                                    timeout=timeout)

    def close(self) -> None:
        self._client.close()

    def send(self, n: Notification) -> None:
        chat = self._admin_chat if n.audience == "admin" else self._user_chat
        text = f"*{n.title}*\n{n.body}" if n.body else f"*{n.title}*"
        if n.fields:
            text += "\n" + "\n".join(f"_{k}_: {v}" for k, v in n.fields.items())
        payload: dict = {"chat_id": chat, "text": text[:4000], "parse_mode": "Markdown"}
        if n.buttons:
            payload["reply_markup"] = {"inline_keyboard": [
                [{"text": b["text"], "callback_data": b["value"][:64]}] for b in n.buttons[:5]
            ]}
        elif n.link:
            payload["reply_markup"] = {"inline_keyboard": [
                [{"text": n.link_text, "url": n.link}]
            ]}
        r = self._client.post("/sendMessage", json=payload)
        r.raise_for_status()
