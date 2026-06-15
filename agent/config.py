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


def llm_from_config_bundle(bundle: dict) -> LLMClient | None:
    """Build a Classifier client from a GET /agent/config bundle (cloud path). None if no LLM key."""
    llm = bundle.get("llm")
    if not llm or not llm.get("api_key"):
        return None
    return LiteLLMClient(llm.get("provider", "anthropic"),
                         llm.get("preferred_model") or llm.get("model", ""),
                         llm["api_key"])
