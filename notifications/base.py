"""
Notifier abstraction.

The agent emits structured `Notification`s; a concrete notifier (Slack/Telegram/Discord) delivers
them. Two audiences (D16): `user` (actionable job activity, with a deep link) and `admin`
(diagnostic ops alerts, with a Langfuse trace link). `NullNotifier` is the safe default;
`FakeNotifier` records for tests. Webhook URLs / bot tokens are secrets — treat accordingly (M5).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Protocol

Audience = Literal["user", "admin"]


@dataclass
class Notification:
    audience: Audience
    title: str
    body: str = ""
    link: str | None = None                 # deep link (user) / trace link (admin)
    link_text: str = "Open"
    fields: dict[str, str] = field(default_factory=dict)   # extra context (admin diagnostics)
    # HITL interactive prompt only: each button = {"text", "value"}; value encodes hitl_id+choice
    buttons: list[dict[str, str]] = field(default_factory=list)


class Notifier(Protocol):
    def send(self, n: Notification) -> None:
        ...


class NullNotifier:
    """Default no-op — the agent runs identically when notifications aren't configured."""

    def send(self, n: Notification) -> None:  # noqa: D401
        return None


class FakeNotifier:
    """Records notifications for assertions in tests."""

    def __init__(self):
        self.sent: list[Notification] = []

    def send(self, n: Notification) -> None:
        self.sent.append(n)


class RoutingNotifier:
    """
    Routes by audience: `user` notifications to one notifier (e.g. the user's own Slack), `admin`
    to another (the shared ops sink). Used in the cloud path where user vs ops may be different
    Slack workspaces/tokens, so a single SlackNotifier can't serve both.
    """

    def __init__(self, user: Notifier, admin: Notifier):
        self._user = user
        self._admin = admin

    def send(self, n: Notification) -> None:
        (self._admin if n.audience == "admin" else self._user).send(n)

    def close(self) -> None:
        for nt in (self._user, self._admin):
            if hasattr(nt, "close"):
                try:
                    nt.close()
                except Exception:
                    pass
