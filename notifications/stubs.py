"""
Stub notifiers — abstraction placeholders for channels that need a relay/bridge (no simple webhook).
Each raises with what it would require, so the interface is complete and the path is documented.
"""

from __future__ import annotations

from .base import Notification


class _Stub:
    requires = "a relay/bridge"

    def __init__(self, *_, **__):
        raise NotImplementedError(
            f"{type(self).__name__} not implemented — requires {self.requires}. "
            "Use slack | telegram | discord."
        )

    def send(self, n: Notification) -> None:  # pragma: no cover
        raise NotImplementedError


class SignalNotifier(_Stub):
    requires = "signal-cli (or a Signal REST bridge) running locally"


class WhatsAppNotifier(_Stub):
    requires = "the WhatsApp Business Cloud API (Meta) with a registered number"


class BlueBubblesNotifier(_Stub):
    requires = "a BlueBubbles server (macOS) exposing its REST API"
