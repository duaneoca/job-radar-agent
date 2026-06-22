"""
`job-radar-agent models` — list the model ids your configured provider+key can actually use.

Model naming is a common stumbling point (case, dashes, prefixes), so rather than guess, we ask the
provider the same way its web UI does. Output is the BARE id to drop into LLM_MODEL — this tool adds
the provider prefix (anthropic/, gemini/, …) itself, so you never type it.
"""

from __future__ import annotations

import httpx

from .config import settings as A

_TIMEOUT = 20.0


def _anthropic(key: str) -> list[str]:
    r = httpx.get("https://api.anthropic.com/v1/models",
                  headers={"x-api-key": key, "anthropic-version": "2023-06-01"}, timeout=_TIMEOUT)
    r.raise_for_status()
    return [m["id"] for m in r.json().get("data", [])]


def _openai_style(base: str, key: str) -> list[str]:
    r = httpx.get(base, headers={"Authorization": f"Bearer {key}"}, timeout=_TIMEOUT)
    r.raise_for_status()
    return [m["id"] for m in r.json().get("data", [])]


def _google(key: str) -> list[str]:
    r = httpx.get("https://generativelanguage.googleapis.com/v1beta/models",
                  params={"key": key}, timeout=_TIMEOUT)
    r.raise_for_status()
    out = []
    for m in r.json().get("models", []):
        # only models that can actually do chat completions
        if "generateContent" in (m.get("supportedGenerationMethods") or []):
            out.append(m["name"].replace("models/", ""))
    return out


_PROVIDERS = {
    "anthropic": lambda k: _anthropic(k),
    "openai": lambda k: _openai_style("https://api.openai.com/v1/models", k),
    "groq": lambda k: _openai_style("https://api.groq.com/openai/v1/models", k),
    "google": lambda k: _google(k),
}


def run() -> int:
    provider = A.llm_provider.lower()
    fn = _PROVIDERS.get(provider)
    if not fn:
        print(f"✗ don't know how to list models for provider '{A.llm_provider}' "
              f"(known: {', '.join(sorted(_PROVIDERS))})")
        return 1
    if not A.llm_api_key:
        print(f"✗ LLM_API_KEY not set — can't query {provider}")
        return 1
    try:
        ids = sorted(fn(A.llm_api_key))
    except Exception as exc:
        print(f"✗ {provider}: {type(exc).__name__}: {str(exc)[:200]}")
        return 1

    print(f"Models available to your {provider} key "
          f"(set one as LLM_MODEL — bare id, no prefix):\n")
    for mid in ids:
        marker = "  → current" if mid == A.llm_model else ""
        print(f"  {mid}{marker}")
    print(f"\n{len(ids)} models. Current LLM_MODEL = {A.llm_model!r}")
    return 0
