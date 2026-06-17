"""
Unit tests for LiteLLMClient — mocked litellm, no API key or network needed.
Covers: model-string mapping, JSON-fence/prose stripping, schema validation, and raise-on-garbage.
"""

import sys
import types

import pytest

from agent.llm_litellm import LiteLLMClient, _extract_json, _model_string
from agent.schemas import Category, Classification


def test_model_string_all_four_vendors():
    # job-radar's actual default model strings for each BYOK vendor (LLM_multivendor doc)
    assert _model_string("anthropic", "claude-haiku-4-5") == "anthropic/claude-haiku-4-5"
    assert _model_string("openai", "gpt-4o-mini") == "openai/gpt-4o-mini"
    # google/groq defaults already carry the LiteLLM prefix → passed through untouched
    assert _model_string("google", "gemini/gemini-1.5-flash") == "gemini/gemini-1.5-flash"
    assert _model_string("groq", "groq/llama-3.3-70b-versatile") == "groq/llama-3.3-70b-versatile"
    # bare google/groq models get the right prefix too
    assert _model_string("google", "gemini-1.5-pro") == "gemini/gemini-1.5-pro"
    assert _model_string("groq", "llama-3.1-8b-instant") == "groq/llama-3.1-8b-instant"
    # any already-prefixed string is passed through; unknown provider → no prefix
    assert _model_string("anthropic", "anthropic/claude-x") == "anthropic/claude-x"
    assert _model_string("mystery", "model-x") == "model-x"


def test_llm_from_config_bundle_matches_agent_llm_config():
    from agent.config import llm_from_config_bundle
    # AgentLLMConfig shape: {provider, preferred_model, api_key}
    c = llm_from_config_bundle({"llm": {"provider": "openai", "preferred_model": "gpt-4o", "api_key": "sk"}})
    assert c is not None and c._model == "openai/gpt-4o" and c._api_key == "sk"
    # null llm (user has no LLM key configured) → None
    assert llm_from_config_bundle({"llm": None}) is None
    assert llm_from_config_bundle({}) is None


def test_extract_json_strips_fences_and_prose():
    assert _extract_json('```json\n{"a":1}\n```') == '{"a":1}'
    assert _extract_json('here you go: {"a": 1} thanks') == '{"a": 1}'
    assert _extract_json('{"a":1}') == '{"a":1}'


def _install_fake_litellm(content: str):
    """Install a fake `litellm` module whose completion() returns `content`."""
    msg = types.SimpleNamespace(content=content)
    choice = types.SimpleNamespace(message=msg)
    resp = types.SimpleNamespace(choices=[choice])
    fake = types.ModuleType("litellm")
    calls = {}

    def completion(**kwargs):
        calls.update(kwargs)
        return resp

    fake.completion = completion
    sys.modules["litellm"] = fake
    return calls


def test_structured_parses_valid_json(monkeypatch):
    payload = Classification(category=Category.job_alert, confidence=0.9, reasoning="ok").model_dump_json()
    calls = _install_fake_litellm(f"```json\n{payload}\n```")
    client = LiteLLMClient("anthropic", "claude-sonnet-4-6", "sk-test")
    out = client.structured(system="sys", user="usr", schema=Classification)
    assert isinstance(out, Classification)
    assert out.category == Category.job_alert
    # routed to the right model, key passed per-call, schema instructions injected
    assert calls["model"] == "anthropic/claude-sonnet-4-6"
    assert calls["api_key"] == "sk-test"
    assert "JSON Schema" in calls["messages"][0]["content"]
    assert calls["timeout"] == 60.0   # per-call timeout passed (no infinite hang)


def test_structured_raises_on_unparseable(monkeypatch):
    _install_fake_litellm("I'm sorry, I cannot do that.")
    client = LiteLLMClient("anthropic", "claude-sonnet-4-6", "sk-test")
    with pytest.raises(Exception):
        client.structured(system="sys", user="usr", schema=Classification)


def teardown_module(module):
    sys.modules.pop("litellm", None)
