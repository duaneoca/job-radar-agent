"""
Agent runtime config.

Dual design — every per-user policy has two sources:
  • LOCAL agent → this module's `AgentSettings` (the `.env` file). It never calls /agent/config (H6a).
  • CLOUD agent → the per-user bundle from `GET /agent/cloud/config/{user_id}`, set by the user in
    Job Radar's Email Agent settings UI. The `*_from_config_bundle` helpers read it, falling back to
    the same defaults when a field is absent (so older Job Radar deploys keep working).
"""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict

from .dedup import DedupStore, NullDedupStore, SqliteDedupStore
from .llm import LLMClient
from .llm_litellm import LiteLLMClient
from .nodes import ZERO_POSTINGS_ACTIONS
from .observability import get_langfuse
from .paths import env_file
from .senders import DEFAULT_ALLOWED_DOMAINS, split_list, DEFAULT_TRUSTED_AUTHSERV_IDS, SenderPolicy


class AgentSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=env_file(), extra="ignore")

    # Job Radar (writer)
    jobradar_api_url: str = "https://job-radar.net/api"
    agent_api_key: str = ""

    # Scheduling (local loop)
    poll_interval_seconds: int = 900

    # LLM (BYOK) — the single link-picker call
    llm_provider: str = "anthropic"
    llm_model: str = "claude-haiku-4-5"
    llm_api_key: str = ""
    # Per-call timeout (s). A fast model answers in a few seconds; a stalled call should fail fast
    # and be retried rather than burn the whole window × retries.
    llm_timeout_seconds: float = 25.0

    # Sorter — Jev decision model (TypeSafe). SYSTEM-WIDE key, not BYOK. When enabled, each run first
    # sorts unread root-folder mail into Interaction / Postings / Social / Unprocessed.
    sorter_enabled: bool = False
    typesafe_api_key: str = ""
    jev_model: str = "jev-latest"
    # Route only a confident, clear winner; everything else → Unprocessed. Tune with
    # scripts/eval_sorter.py against your already-sorted folders.
    sorter_min_confidence: float = 0.85
    sorter_min_margin: float = 0.30
    # Recruiter mail from these sender domains is a bulk mailing → Postings (never a personal note).
    # Empty by default: Dice recruiter mail is real outreach (Interaction); Dice job alerts
    # (IntelliSearch) are recognised by Jev as job alerts.
    bulk_recruiter_domains: str = ""

    # Sender policy (checked before any LLM call). Comma-separated; an EMPTY value allows all senders.
    allowed_sender_domains: str = ",".join(DEFAULT_ALLOWED_DOMAINS)
    require_sender_auth: bool = True
    trusted_authserv_ids: str = ",".join(DEFAULT_TRUSTED_AUTHSERV_IDS)

    # What to do with an email that contains no job postings: mark_read | unprocessed
    zero_postings_action: str = "mark_read"

    # Duplicate suppression window (days since a posting was last seen); 0 disables it.
    dedup_window_days: int = 30

    # Run controls
    daily_spend_ceiling_usd: float = 5.0

    # Observability (Langfuse) — optional; host defaults to US (a blank host silently means EU).
    langfuse_host: str = "https://us.cloud.langfuse.com"
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""

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


def _zero_action(value: str | None) -> str:
    v = (value or "mark_read").strip().lower()
    if v not in ZERO_POSTINGS_ACTIONS:
        raise ValueError(f"ZERO_POSTINGS_ACTION must be one of {ZERO_POSTINGS_ACTIONS}, got {value!r}")
    return v


# ── local (.env) factories ────────────────────────────────────
def make_llm(settings: AgentSettings = settings) -> LLMClient:
    """The link-picker LLM from local env (BYOK), Langfuse-traced when configured."""
    return LiteLLMClient(settings.llm_provider, settings.llm_model, settings.llm_api_key,
                         langfuse=get_langfuse(), timeout=settings.llm_timeout_seconds)


def make_jev(settings: AgentSettings = settings):
    """The sorter's Jev client (one system-wide TypeSafe key)."""
    from .jev import JevClient
    return JevClient(settings.typesafe_api_key, model=settings.jev_model)


def bulk_recruiter_domains(settings: AgentSettings = settings) -> tuple[str, ...]:
    return split_list(settings.bulk_recruiter_domains)


def sort_config(settings: AgentSettings = settings):
    from .sort_stage import SortConfig
    return SortConfig(min_confidence=settings.sorter_min_confidence,
                      min_margin=settings.sorter_min_margin,
                      bulk_domains=bulk_recruiter_domains(settings))


def make_sort_stage(reader, settings: AgentSettings = settings):
    """The sort stage over `reader` (the ROOT folder), or None when the sorter is disabled."""
    if not settings.sorter_enabled:
        return None
    if not settings.typesafe_api_key:
        raise SystemExit("✗ SORTER_ENABLED=true but TYPESAFE_API_KEY is not set")
    from .sort_stage import SortStage
    return SortStage(reader, make_jev(settings), sort_config(settings))


def make_sender_policy(settings: AgentSettings = settings) -> SenderPolicy:
    return SenderPolicy.from_values(settings.allowed_sender_domains, settings.require_sender_auth,
                                    settings.trusted_authserv_ids)


def make_dedup_store(settings: AgentSettings = settings) -> DedupStore:
    if settings.dedup_window_days <= 0:
        return NullDedupStore()
    return SqliteDedupStore(window_days=settings.dedup_window_days)


def zero_postings_action(settings: AgentSettings = settings) -> str:
    return _zero_action(settings.zero_postings_action)


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


# ── cloud (per-user config bundle) factories ──────────────────
def llm_from_config_bundle(bundle: dict) -> LLMClient | None:
    """The link-picker LLM from the user's own provider/model/key. None if no key."""
    llm = bundle.get("llm")
    if not llm or not llm.get("api_key"):
        return None
    return LiteLLMClient(llm.get("provider", "anthropic"),
                         llm.get("preferred_model") or llm.get("model", ""),
                         llm["api_key"], langfuse=get_langfuse(),
                         timeout=settings.llm_timeout_seconds)


def policy_from_config_bundle(bundle: dict) -> SenderPolicy:
    """`email_policy` block (INTEGRATION_SPEC §3.6); absent fields fall back to the defaults."""
    p = bundle.get("email_policy") or {}
    return SenderPolicy.from_values(p.get("allowed_sender_domains"), p.get("require_sender_auth"),
                                    p.get("trusted_authserv_ids"))


def zero_action_from_config_bundle(bundle: dict) -> str:
    return _zero_action((bundle.get("email_policy") or {}).get("zero_postings_action"))
