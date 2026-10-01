"""JevClient — mocked HTTP (httpx.MockTransport), no key or network needed."""

import json

import httpx
import pytest

from agent.jev import USD_PER_INPUT_TOKEN, ChoiceAnswer, JevClient, JevError, choice

Q = choice("Which?", {"a": "first", "b": "second"})
OK = {"model": "jev-1.13.0",
      "answers": {"category": {"type": "choice", "choice": "a",
                               "probabilities": {"a": 0.9, "b": 0.1}, "confidence": 0.88}},
      "usage": {"input_tokens": 1000, "output_tokens": 20}}


def _client(handler, **kw):
    return JevClient("k", transport=httpx.MockTransport(handler), sleep=lambda s: None, **kw)


def test_ask_choice_parses_answer_sends_auth_and_tracks_cost():
    seen = {}

    def handler(req):
        seen["auth"] = req.headers["authorization"]
        seen["path"] = req.url.path
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json=OK)

    c = _client(handler)
    a = c.ask_choice({"subject": "s"}, "category", Q)
    assert (a.choice, a.confidence) == ("a", 0.88)
    assert a.margin == pytest.approx(0.8)
    assert seen["auth"] == "Bearer k" and seen["path"] == "/v1/systemone"
    assert seen["body"]["model"] == "jev-latest" and "category" in seen["body"]["questions"]
    assert c.run_cost == pytest.approx(1000 * USD_PER_INPUT_TOKEN) and c.calls == 1


def test_overload_retries_then_succeeds():
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(529) if len(calls) < 3 else httpx.Response(200, json=OK)

    assert _client(handler).ask_choice("s", "category", Q).choice == "a"
    assert len(calls) == 3


def test_persistent_rate_limit_raises_after_max_attempts():
    with pytest.raises(JevError, match="after 3 attempts"):
        _client(lambda req: httpx.Response(429)).ask("s", {"category": Q})


def test_auth_error_is_not_retried():
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(401, text="bad key")

    with pytest.raises(JevError, match="401"):
        _client(handler).ask("s", {"category": Q})
    assert len(calls) == 1


def test_missing_choice_raises():
    bad = {"answers": {"category": {"type": "noul", "noul": 0.4}}, "usage": {}}
    with pytest.raises(JevError, match="no choice"):
        _client(lambda req: httpx.Response(200, json=bad)).ask_choice("s", "category", Q)


def test_empty_key_rejected():
    with pytest.raises(ValueError):
        JevClient("")


def test_margin_single_option():
    assert ChoiceAnswer("a", 0.9, {"a": 1.0}).margin == 1.0
