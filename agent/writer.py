"""
Writer interface — the agent's only path into Job Radar (the Writer MCP / `/agent/*` endpoints).

`JobRadarWriter` mirrors INTEGRATION_SPEC §4. Production talks to the Writer MCP over HTTPS (or,
for cloud, the in-cluster tracker-api). Tests use `FakeWriter`, which records calls and serves
canned reviews — so the graph is testable before job-radar's JR-2/JR-3 exist.
"""

from __future__ import annotations

from typing import Any, Protocol


class JobRadarWriter(Protocol):
    def get_reviews(self) -> list[dict[str, Any]]:
        """User's UserJobReview rows: [{review_id, company, title, status, url}] for matching."""
        ...

    def create_inbox_entry(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Create one inbox_emails row + N inbox_postings. Returns {inbox_email_id, posting_ids}."""
        ...

    def record_interaction(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Record a status-update email; if matched, updates the review + timeline."""
        ...

    def register_hitl(self, hitl_id: str, candidates: list[dict[str, Any]]) -> dict[str, Any]:
        """Register a pending HITL decision before posting Slack buttons."""
        ...

    def report_run(self, record: dict[str, Any]) -> dict[str, Any]:
        """Post the run heartbeat (counts + status)."""
        ...


class FakeWriter:
    """In-memory test double. Records every call; serves a configurable review set."""

    def __init__(self, reviews: list[dict[str, Any]] | None = None):
        self._reviews = reviews or []
        self.inbox_entries: list[dict[str, Any]] = []
        self.interactions: list[dict[str, Any]] = []
        self.hitl: list[dict[str, Any]] = []
        self.runs: list[dict[str, Any]] = []

    def get_reviews(self) -> list[dict[str, Any]]:
        return list(self._reviews)

    def create_inbox_entry(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.inbox_entries.append(payload)
        return {
            "inbox_email_id": f"inbox-{len(self.inbox_entries)}",
            "posting_ids": [f"p-{i}" for i in range(len(payload.get("postings", [])))],
        }

    def record_interaction(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.interactions.append(payload)
        return {"interaction_id": f"int-{len(self.interactions)}"}

    def register_hitl(self, hitl_id: str, candidates: list[dict[str, Any]]) -> dict[str, Any]:
        self.hitl.append({"hitl_id": hitl_id, "candidates": candidates})
        return {"ok": True}

    def report_run(self, record: dict[str, Any]) -> dict[str, Any]:
        self.runs.append(record)
        return {"run_id": f"run-{len(self.runs)}"}
