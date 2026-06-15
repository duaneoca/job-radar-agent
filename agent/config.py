"""
Agent runtime config (local self-host path).

The LOCAL agent reads its LLM provider/model/key from the environment (BYOK, owner's own key) — it
does NOT call GET /agent/config (H6a). The CLOUD path instead fetches these from the config bundle;
`llm_from_config_bundle` builds the same client from that dict.
"""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict

from .llm import LLMClient
from .llm_litellm import LiteLLMClient
from .observability import get_langfuse


class AgentSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Job Radar (writer)
    jobradar_api_url: str = "https://staging.job-radar.net/api"
    agent_api_key: str = ""

    # Scheduling (local loop)
    poll_interval_seconds: int = 900

    # LLM (BYOK) — local path
    llm_provider: str = "anthropic"
    llm_model: str = "claude-sonnet-4-6"
    llm_api_key: str = ""
    # Optional cheaper model for the Critic; falls back to the main model if unset.
    critic_model: str = ""

    # Run controls (mirrored from the email reader for the runner)
    max_emails_per_run: int = 100
    daily_spend_ceiling_usd: float = 5.0
    hitl_abandon_minutes: int = 30

    # Notifications
    notifier: str = "null"               # slack | telegram | discord | null
    slack_bot_token: str = ""
    slack_user_channel: str = ""
    slack_admin_channel: str = ""
    telegram_bot_token: str = ""
    telegram_user_chat_id: str = ""
    telegram_admin_chat_id: str = ""
    discord_webhook_url: str = ""
    discord_admin_webhook_url: str = ""


settings = AgentSettings()


def make_llm(settings: AgentSettings = settings) -> LLMClient:
    """Classifier LLM from local env (BYOK), Langfuse-traced when configured."""
    return LiteLLMClient(settings.llm_provider, settings.llm_model, settings.llm_api_key,
                         langfuse=get_langfuse())


def make_critic_llm(settings: AgentSettings = settings) -> LLMClient:
    """Critic LLM — same provider/key, optionally a cheaper model (D2/Q5)."""
    model = settings.critic_model or settings.llm_model
    return LiteLLMClient(settings.llm_provider, model, settings.llm_api_key,
                         langfuse=get_langfuse())


def make_notifier(settings: AgentSettings = settings):
    """Build the configured notifier (defaults to a no-op NullNotifier)."""
    from notifications.base import NullNotifier
    kind = (settings.notifier or "null").lower()
    if kind == "slack" and settings.slack_bot_token:
        from notifications.slack import SlackNotifier
        return SlackNotifier(settings.slack_bot_token, settings.slack_user_channel,
                             settings.slack_admin_channel or None)
    if kind == "telegram" and settings.telegram_bot_token:
        from notifications.telegram import TelegramNotifier
        return TelegramNotifier(settings.telegram_bot_token, settings.telegram_user_chat_id,
                                settings.telegram_admin_chat_id or None)
    if kind == "discord" and settings.discord_webhook_url:
        from notifications.discord import DiscordNotifier
        return DiscordNotifier(settings.discord_webhook_url,
                               settings.discord_admin_webhook_url or None)
    return NullNotifier()


def llm_from_config_bundle(bundle: dict) -> LLMClient | None:
    """Build a Classifier client from a GET /agent/config bundle (cloud path). None if no LLM key."""
    llm = bundle.get("llm")
    if not llm or not llm.get("api_key"):
        return None
    return LiteLLMClient(llm.get("provider", "anthropic"),
                         llm.get("preferred_model") or llm.get("model", ""),
                         llm["api_key"])
