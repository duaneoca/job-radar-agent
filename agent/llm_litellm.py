"""
LiteLLMClient — production LLMClient backed by LiteLLM (provider-agnostic, BYOK).

Mirrors job-radar's ai-reviewer: one client, any provider (Anthropic/OpenAI/Google/Groq), the user's
own key. Returns structured output by instructing JSON-of-schema and validating with pydantic; on
unparseable output it RAISES, which the classify node turns into a validation-loop retry (not a crash).

`preferred_model` from job-radar is already a LiteLLM model string; if it lacks a provider prefix we
add one based on `provider`. The key is passed per-call (never set as a global/env mutation).
"""

from __future__ import annotations

import json
import re
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from .llm import LLMParseError

T = TypeVar("T", bound=BaseModel)

_PROVIDER_PREFIX = {
    "anthropic": "anthropic/",
    "openai": "openai/",
    "google": "gemini/",
    "groq": "groq/",
}

_JSON_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)


def _model_string(provider: str, model: str) -> str:
    if "/" in model:                      # already a full LiteLLM string
        return model
    return _PROVIDER_PREFIX.get(provider.lower(), "") + model


def _extract_json(text: str) -> str:
    """Strip ```json fences / surrounding prose, returning the JSON object substring."""
    s = _JSON_FENCE.sub("", text.strip())
    if not s.startswith("{"):
        a, b = s.find("{"), s.rfind("}")
        if a != -1 and b != -1 and b > a:
            s = s[a : b + 1]
    return s


_GEN_NAME = {"Classification": "classify", "Critique": "critic"}

_LITELLM_QUIETED = False


def _quiet_litellm(litellm) -> None:
    """Silence litellm's per-exception 'Give Feedback / Provider List' banners and WARNING spam — on a
    timeout storm these flood the log (thousands of lines) and bury real errors. Idempotent."""
    global _LITELLM_QUIETED
    if _LITELLM_QUIETED:
        return
    import logging
    try:
        litellm.suppress_debug_info = True       # drops the GitHub/Provider-List banners
    except Exception:
        pass
    logging.getLogger("LiteLLM").setLevel(logging.ERROR)
    _LITELLM_QUIETED = True


class LiteLLMClient:
    def __init__(self, provider: str, model: str, api_key: str, temperature: float = 0.0,
                 langfuse=None, timeout: float = 25.0, num_retries: int = 2):
        self._model = _model_string(provider, model)
        self._api_key = api_key
        self._temperature = temperature
        self._timeout = timeout                  # per-call timeout — a stalled LLM must not hang the run
        self._num_retries = num_retries          # litellm retries transient 429/timeout/5xx w/ backoff,
                                                 # honoring Retry-After — rides out brief rate-limit blips
        self._lf = langfuse                      # optional Langfuse client; None = no tracing
        self.run_cost = 0.0                      # $ accrued since last reset_cost() — H4 budget

    def reset_cost(self) -> None:
        self.run_cost = 0.0

    def structured(self, *, system: str, user: str, schema: type[T]) -> T:
        import litellm

        from .observability import generation

        _quiet_litellm(litellm)

        schema_json = json.dumps(schema.model_json_schema())
        sys_full = (
            f"{system}\n\n"
            "Respond with ONLY a single JSON object that conforms to this JSON Schema "
            "(no prose, no markdown fences):\n"
            f"{schema_json}"
        )
        messages = [
            {"role": "system", "content": sys_full},
            {"role": "user", "content": user},
        ]
        name = _GEN_NAME.get(schema.__name__, schema.__name__.lower())
        with generation(self._lf, name=name, model=self._model, messages=messages) as gen:
            # litellm transient errors (RateLimitError/Timeout/5xx) raise here and PROPAGATE — the
            # runner leaves the email unmoved for the next run rather than misfiling it.
            resp = litellm.completion(
                model=self._model, api_key=self._api_key, temperature=self._temperature,
                messages=messages, response_format={"type": "json_object"},
                timeout=self._timeout, num_retries=self._num_retries,
            )
            content = resp.choices[0].message.content or ""
            usage = getattr(resp, "usage", None)
            try:
                self.run_cost += float(litellm.completion_cost(completion_response=resp) or 0.0)
            except Exception:
                pass  # unknown model / pricing → best-effort, don't fail the call
            gen.finish(
                output=content,
                usage={"input": getattr(usage, "prompt_tokens", None),
                       "output": getattr(usage, "completion_tokens", None)} if usage else None,
            )
            # Bad JSON / schema mismatch is a RETRYABLE classification failure (handled in-loop),
            # distinct from infra errors above — surface it as LLMParseError (a ValueError).
            try:
                return schema.model_validate_json(_extract_json(content))
            except ValidationError as exc:
                raise LLMParseError(f"output failed {schema.__name__} schema validation: {exc}") from exc
