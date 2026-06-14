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

from pydantic import BaseModel

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


class LiteLLMClient:
    def __init__(self, provider: str, model: str, api_key: str, temperature: float = 0.0):
        self._model = _model_string(provider, model)
        self._api_key = api_key
        self._temperature = temperature

    def structured(self, *, system: str, user: str, schema: type[T]) -> T:
        import litellm

        schema_json = json.dumps(schema.model_json_schema())
        sys_full = (
            f"{system}\n\n"
            "Respond with ONLY a single JSON object that conforms to this JSON Schema "
            "(no prose, no markdown fences):\n"
            f"{schema_json}"
        )
        resp = litellm.completion(
            model=self._model,
            api_key=self._api_key,
            temperature=self._temperature,
            messages=[
                {"role": "system", "content": sys_full},
                {"role": "user", "content": user},
            ],
            response_format={"type": "json_object"},
        )
        content = resp.choices[0].message.content or ""
        return schema.model_validate_json(_extract_json(content))
