"""
Prompt provider seam.

Langfuse owns the live, versioned prompts (D4). For now a `SeedPromptProvider` loads the seed
files in `prompts/` so the agent runs before Langfuse is wired. Swapping to a LangfusePromptProvider
later means implementing the same `.get()` method — nodes don't change.

V2 never sends the email body to the model — only a numbered list of extracted links, fenced in
<links>…</links> with angle brackets stripped from email text (see agent/extract.py). [C1]
"""

from __future__ import annotations

from importlib.resources import files
from typing import Protocol


class PromptProvider(Protocol):
    def get(self, name: str) -> str:
        ...


class SeedPromptProvider:
    """Loads the seed prompts shipped inside the package (`agent/seed_prompts/*.md`) via
    importlib.resources — so they're found whether running from the repo or a pip/pipx install."""

    def __init__(self):
        self._cache: dict[str, str] = {}

    def get(self, name: str) -> str:
        if name not in self._cache:
            self._cache[name] = (files("agent.seed_prompts") / f"{name}.md").read_text(encoding="utf-8")
        return self._cache[name]
