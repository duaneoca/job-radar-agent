"""
Jev client — TypeSafe AI's decision model (one endpoint: POST /v1/systemone).

Jev never writes text: it answers typed questions (choice / noul / score) about a `state` with
calibrated probabilities. That makes it a fit for the sorter's routing decision and a non-fit for any
extraction (names, titles, URLs) — those stay deterministic or on the link-picker LLM.

The key is SYSTEM-WIDE (not BYOK): one TYPESAFE_API_KEY for every user. Cost is tracked per client so a
run can charge it against the daily spend ceiling like the link-picker's.

Error policy mirrors the LLM client: 429/529/5xx and transport errors retry with backoff, then
propagate (the email stays unread and is retried next run). 401/422 propagate immediately.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

import httpx

DEFAULT_BASE_URL = "https://api.typesafe.ai"
DEFAULT_MODEL = "jev-latest"
# List price: $0.042 per million input tokens; output tokens are free.
USD_PER_INPUT_TOKEN = 0.042 / 1_000_000

_RETRY_STATUS = {429, 500, 502, 503, 504, 529}


class JevError(RuntimeError):
    """Jev refused or failed after retries. Infrastructure error — never retried in-loop."""


@dataclass
class ChoiceAnswer:
    choice: str
    confidence: float
    probabilities: dict[str, float] = field(default_factory=dict)

    @property
    def margin(self) -> float:
        """Top probability minus the runner-up's (1.0 when there is only one option)."""
        ps = sorted(self.probabilities.values(), reverse=True)
        return ps[0] - (ps[1] if len(ps) > 1 else 0.0) if ps else 0.0


def choice(instructions: str, criteria: dict[str, str]) -> dict:
    return {"type": "choice", "instructions": instructions, "criteria": criteria}


def noul(instructions: str, true: str | None = None, false: str | None = None) -> dict:
    q: dict = {"type": "noul", "instructions": instructions}
    if true or false:
        q["criteria"] = {"true": true or "", "false": false or ""}
    return q


class JevClient:
    def __init__(self, api_key: str, *, model: str = DEFAULT_MODEL,
                 base_url: str = DEFAULT_BASE_URL, timeout: float = 10.0, max_attempts: int = 3,
                 transport: httpx.BaseTransport | None = None, sleep=time.sleep):
        if not api_key:
            raise ValueError("TYPESAFE_API_KEY not set")
        self._model = model
        self._max_attempts = max_attempts
        self._sleep = sleep
        self._http = httpx.Client(
            base_url=base_url, timeout=timeout, transport=transport,
            headers={"Authorization": f"Bearer {api_key}"})
        self.run_cost = 0.0
        self.input_tokens = 0
        self.calls = 0

    def ask(self, state: str | dict, questions: dict[str, dict]) -> dict:
        """Raw answers keyed by question name. Raises JevError on failure."""
        body = {"model": self._model, "state": state, "questions": questions}
        last: Exception | None = None
        for attempt in range(self._max_attempts):
            if attempt:
                self._sleep(min(8.0, 0.5 * 2 ** attempt))
            try:
                r = self._http.post("/v1/systemone", json=body)
            except httpx.TransportError as e:
                last = e
                continue
            if r.status_code in _RETRY_STATUS:
                last = JevError(f"Jev HTTP {r.status_code}")
                continue
            if r.status_code >= 400:
                raise JevError(f"Jev HTTP {r.status_code}: {r.text[:300]}")
            data = r.json()
            tokens = int((data.get("usage") or {}).get("input_tokens") or 0)
            self.input_tokens += tokens
            self.run_cost += tokens * USD_PER_INPUT_TOKEN
            self.calls += 1
            return data.get("answers") or {}
        raise JevError(f"Jev failed after {self._max_attempts} attempts: {last}")

    def ask_choice(self, state: str | dict, name: str, question: dict) -> ChoiceAnswer:
        a = self.ask(state, {name: question}).get(name) or {}
        if a.get("type") != "choice" or "choice" not in a:
            raise JevError(f"Jev returned no choice for {name!r}")
        return ChoiceAnswer(choice=a["choice"], confidence=float(a.get("confidence") or 0.0),
                            probabilities={k: float(v) for k, v in
                                           (a.get("probabilities") or {}).items()})

    def close(self) -> None:
        self._http.close()
