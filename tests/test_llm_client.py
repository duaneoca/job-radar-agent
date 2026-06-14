"""
Unit tests for LiteLLMClient — mocked litellm, no API key or network needed.
Covers: model-string mapping, JSON-fence/prose stripping, schema validation, and raise-on-garbage.
"""

import sys
import types

import pytest

from agent.llm_litellm import LiteLLMClient, _extract_json, _model_string
from agent.schemas import Category, Classification


def test_model_string_adds_provider_prefix():
    assert _model_string("anthropic", "claude-sonnet-4-6") == "anthropic/claude-sonnet-4-6"
    assert _model_string("groq", "llama-3.3-70b") == "groq/llama-3.3-70b"
    # already-prefixed strings are passed through untouched
    assert _model_string("anthropic", "anthropic/claude-x") == "anthropic/claude-x"
    # unknown provider → no prefix
    assert _model_string("mystery", "model-x") == "model-x"


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


def test_structured_raises_on_unparseable(monkeypatch):
    _install_fake_litellm("I'm sorry, I cannot do that.")
    client = LiteLLMClient("anthropic", "claude-sonnet-4-6", "sk-test")
    with pytest.raises(Exception):
        client.structured(system="sys", user="usr", schema=Classification)


def teardown_module(module):
    sys.modules.pop("litellm", None)
