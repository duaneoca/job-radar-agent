"""Loader for the synthetic golden-set fixtures (committed, PII-free)."""

import json
from pathlib import Path

_SYNTHETIC = Path(__file__).resolve().parent / "synthetic"


def load_synthetic() -> list[dict]:
    """All committed synthetic fixtures (skips *.candidate.json scrub outputs)."""
    out = []
    for p in sorted(_SYNTHETIC.glob("*.json")):
        if p.name.endswith(".candidate.json"):
            continue
        out.append(json.loads(p.read_text(encoding="utf-8")))
    return out
