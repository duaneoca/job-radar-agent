"""
Daily spend ceiling (H4) — bound LLM cost on the user's BYOK key across runs.

Per-run email/token caps already bound a single run; this adds the cross-run **daily** ceiling.
Cost accrues per (date, key) — `key` is the user id (cloud) or "local". Persisted to a small JSON
file so it survives across CronJob runs / loop iterations. Only today's bucket is kept (auto-pruned).
"""

from __future__ import annotations

import datetime
import json
import os
import threading


class DailySpendStore:
    def __init__(self, path: str = "data/spend.json"):
        self._path = path
        self._lock = threading.Lock()

    @staticmethod
    def _today() -> str:
        return datetime.date.today().isoformat()

    def _load(self) -> dict:
        try:
            with open(self._path, encoding="utf-8") as f:
                return json.load(f)
        except (FileNotFoundError, ValueError):
            return {}

    def spent_today(self, key: str) -> float:
        return float(self._load().get(self._today(), {}).get(key, 0.0))

    def add(self, key: str, cost: float) -> float:
        """Add cost to today's bucket for `key`; returns the new daily total. Prunes old dates."""
        if cost <= 0:
            return self.spent_today(key)
        with self._lock:
            today = self._today()
            data = self._load()
            bucket = data.get(today, {})            # keep only today's bucket (prune older)
            bucket[key] = float(bucket.get(key, 0.0)) + float(cost)
            os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
            tmp = f"{self._path}.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({today: bucket}, f)
            os.replace(tmp, self._path)
            return bucket[key]
