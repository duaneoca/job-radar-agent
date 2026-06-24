"""
RestWriter — JobRadarWriter backed by Job Radar's `/agent/*` REST endpoints (HTTPS + X-Agent-Key).

Used by the LOCAL self-host path, which runs outside the k8s cluster and therefore cannot reach the
in-cluster MCP writer. The cloud/in-cluster path uses an MCP-backed writer instead (same Protocol).

Identity is derived server-side from the X-Agent-Key (H1) — we never send a user_id.
Note: per H6a the local agent does NOT call get_config (it uses local creds); get_config is provided
for completeness / the in-cluster case but is intentionally unused locally.
"""

from __future__ import annotations

from typing import Any

import httpx


class RestWriter:
    """
    Two auth modes:
      • LOCAL: `agent_key` → `X-Agent-Key` (server derives user).
      • CLOUD: `internal_token` + `user_id` → `X-Internal-Token` + `X-User-Id` (in-cluster batch
        processor acting on behalf of a user; SPEC §2.1b).
    """

    def __init__(self, base_url: str, agent_key: str | None = None, timeout: float = 30.0,
                 internal_token: str | None = None, user_id: str | None = None):
        self._base = base_url.rstrip("/")
        headers = {"Content-Type": "application/json"}
        if internal_token:
            headers["X-Internal-Token"] = internal_token
            if user_id:
                headers["X-User-Id"] = str(user_id)
        elif agent_key:
            headers["X-Agent-Key"] = agent_key
        else:
            raise ValueError("RestWriter needs agent_key OR internal_token")
        self._client = httpx.Client(base_url=self._base, headers=headers, timeout=timeout)

    def close(self) -> None:
        self._client.close()

    def _raise_with_body(self, r) -> None:
        """raise_for_status, but fold the response body into the message — a bare 4xx hides the reason
        (e.g. a 422's FastAPI validation detail telling us WHICH field was rejected)."""
        if r.is_error:
            import httpx
            detail = (r.text or "")[:500]
            raise httpx.HTTPStatusError(
                f"{r.status_code} for {r.request.url} — {detail}", request=r.request, response=r)

    def _get(self, path: str) -> Any:
        r = self._client.get(path)
        self._raise_with_body(r)
        return r.json()

    def _post(self, path: str, payload: dict[str, Any]) -> Any:
        r = self._client.post(path, json=payload)
        self._raise_with_body(r)
        return r.json() if r.content else {}

    # ── JobRadarWriter protocol ───────────────────────────────
    def get_config(self) -> dict[str, Any]:
        return self._get("/agent/config")

    def get_reviews(self) -> list[dict[str, Any]]:
        data = self._get("/agent/reviews")
        # tolerate either a bare list or {"items": [...]}
        return data["items"] if isinstance(data, dict) and "items" in data else data

    def create_inbox_entry(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._post("/agent/inbox", payload)

    def record_interaction(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._post("/agent/interactions", payload)

    def register_hitl(self, hitl_id: str, candidates: list[dict[str, Any]]) -> dict[str, Any]:
        return self._post("/agent/hitl/register", {"hitl_id": hitl_id, "candidates": candidates})

    def report_run(self, record: dict[str, Any]) -> dict[str, Any]:
        return self._post("/agent/runs", record)
