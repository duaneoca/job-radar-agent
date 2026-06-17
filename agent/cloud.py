"""
Multi-user cloud runner (in-cluster, the k8s CronJob's job).

Enumerates enabled users via the internal cloud endpoints, then processes each ONE AT A TIME —
fetch that user's config → build per-user reader/writer/LLM → run → **discard creds** → next. Only
one user's secrets are in memory at any moment (H6). Per-user error isolation + a circuit breaker
(stop on systemic failure) + a total-email budget per cloud run (H4 cost guard).

Cloud writes use X-Internal-Token + user_id (SPEC §2.1b). Per-user *notifications* are deferred —
each user's run uses a NullNotifier; the cloud run sends one admin summary. (Per-user notification
routing needs per-user notifier config — future.)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

import httpx

from .config import llm_from_config_bundle, make_critic_llm
from .reader import ProviderReader
from .runner import run_once
from .writer_rest import RestWriter
from notifications.base import Notification, NullNotifier

CIRCUIT_BREAK = 3   # consecutive per-user failures ⇒ assume systemic, stop


class CloudConfigClient:
    """Reads the internal in-cluster enumeration endpoints (X-Internal-Token)."""

    def __init__(self, base_url: str, internal_token: str, timeout: float = 30.0):
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={"X-Internal-Token": internal_token, "Content-Type": "application/json"},
            timeout=timeout,
        )

    def close(self) -> None:
        self._client.close()

    def users(self) -> list[dict[str, Any]]:
        r = self._client.get("/agent/cloud/users"); r.raise_for_status()
        data = r.json()
        return data["items"] if isinstance(data, dict) and "items" in data else data

    def config(self, user_id: str) -> dict[str, Any]:
        r = self._client.get(f"/agent/cloud/config/{user_id}"); r.raise_for_status()
        return r.json()


@dataclass
class _UserComponents:
    reader: Any
    writer: Any
    llm: Any
    critic_llm: Any
    close: Callable[[], None]


def build_user_components(cfg: dict, user_id: str, *, base_url: str, internal_token: str,
                          since_days: int | None, limit: int | None) -> _UserComponents:
    f = cfg["folders"]
    ec = cfg["email_credentials"]
    provider_kind = ec.get("provider", "gmail")
    if provider_kind == "gmail":
        from mcp_email.providers.gmail import GmailProvider
        provider = GmailProvider(root_folder=f["root"], creds_info=ec)
    else:
        # No cloud-IMAP provider yet (cloud = Gmail-only). Fail loudly per user, isolated.
        raise NotImplementedError(f"cloud provider '{provider_kind}' not supported yet")

    reader = ProviderReader(
        provider, root=f["root"],
        dest_folders={"interaction": f["interaction"], "postings": f["postings"],
                      "social": f["social"], "unprocessed": f["unprocessed"]},
        since_days=since_days, limit=limit,
    )
    writer = RestWriter(base_url, internal_token=internal_token, user_id=user_id)
    llm = llm_from_config_bundle(cfg)
    if llm is None:
        raise ValueError("no LLM key in config for user")

    def _close():
        for obj in (provider, writer):
            if hasattr(obj, "close"):
                try:
                    obj.close()
                except Exception:
                    pass

    return _UserComponents(reader=reader, writer=writer, llm=llm,
                           critic_llm=make_critic_llm(), close=_close)


def cloud_run(config_client: CloudConfigClient, *, base_url: str, internal_token: str,
              prompts, notifier=None, since_days: int | None = 14, limit: int | None = 100,
              max_total_emails: int = 1000, dry_run: bool = False,
              daily_ceiling: float | None = None, spend_store=None,
              run_once_fn=run_once) -> dict[str, Any]:
    notifier = notifier or NullNotifier()
    summary = {"users": 0, "processed": 0, "escalations": 0, "errors": [], "stopped": None}
    consecutive_errors = 0

    for u in config_client.users():
        if not u.get("enabled"):
            continue
        uid = u["user_id"]
        summary["users"] += 1
        try:
            cfg = config_client.config(uid)
            comp = build_user_components(cfg, uid, base_url=base_url, internal_token=internal_token,
                                         since_days=since_days, limit=limit)
            try:
                res = run_once_fn(
                    reader=comp.reader, writer=comp.writer, llm=comp.llm,
                    critic_llm=comp.critic_llm, prompts=prompts, notifier=NullNotifier(),
                    environment="cloud", dry_run=dry_run, use_lock=False,
                    spend_key=uid, daily_ceiling=daily_ceiling, spend_store=spend_store,
                )
            finally:
                comp.close()                       # discard this user's creds before the next
            summary["processed"] += res.emails_processed
            summary["escalations"] += res.escalations
            consecutive_errors = 0
        except Exception as exc:
            consecutive_errors += 1
            summary["errors"].append(f"{uid}: {type(exc).__name__}: {exc}")
            if consecutive_errors >= CIRCUIT_BREAK:
                summary["stopped"] = "circuit breaker (consecutive failures)"
                break
        if summary["processed"] >= max_total_emails:   # H4 cost guard for the whole cloud run
            summary["stopped"] = "max_total_emails budget reached"
            break

    # one admin summary for the whole cloud run
    if summary["errors"] or summary["stopped"]:
        notifier.send(Notification(
            "admin", f"Cloud run: {summary['processed']} processed across {summary['users']} users",
            body=("stopped: " + summary["stopped"] + "\n" if summary["stopped"] else "")
                 + "\n".join(summary["errors"][:10]),
            fields={"users": str(summary["users"]), "processed": str(summary["processed"]),
                    "escalations": str(summary["escalations"]), "errors": str(len(summary["errors"]))},
        ))
    return summary
