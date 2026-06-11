"""
LLM interface for the Classifier and Critic calls.

Both calls return *structured* output (a pydantic model). The agent is provider-agnostic — the
user's chosen provider+model (BYOK, from job-radar `user_api_keys`) drives both calls (D2/Q5).

`LLMClient` is the seam: production uses a LiteLLM-backed implementation (mirrors job-radar's
ai-reviewer); tests use `FakeLLM` with scripted responses. Keeping this a Protocol means the graph
never imports a concrete LLM and stays unit-testable offline.
"""

from __future__ import annotations

from typing import Protocol, TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


class LLMClient(Protocol):
    def structured(self, *, system: str, user: str, schema: type[T]) -> T:
        """Call the model and parse the response into `schema`. Raises on unparseable output."""
        ...


class FakeLLM:
    """
    Test double. Supply a queue of (schema-instance) responses; each `structured` call pops one.
    A callable response is invoked with (system, user, schema) for dynamic behavior.
    """

    def __init__(self, responses: list):
        self._responses = list(responses)
        self.calls: list[dict] = []

    def structured(self, *, system: str, user: str, schema: type[T]) -> T:
        self.calls.append({"system": system, "user": user, "schema": schema.__name__})
        if not self._responses:
            raise AssertionError("FakeLLM ran out of scripted responses")
        resp = self._responses.pop(0)
        if callable(resp):
            resp = resp(system, user, schema)
        if not isinstance(resp, schema):
            raise AssertionError(
                f"FakeLLM response type {type(resp).__name__} != expected {schema.__name__}"
            )
        return resp
