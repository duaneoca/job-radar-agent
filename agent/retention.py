"""
Retention — the agent's only path to removing mail (INTEGRATION_SPEC §3.8, security [R1]).

Deterministic by construction: a message is eligible when its folder is allow-listed, it arrived more
than N days ago (the mail server's own date), and it is not starred. Read or unread does not matter.
No message id is ever passed in, so nothing an LLM or Jev outputs can select a message. "Remove" means
MOVE TO TRASH (recoverable until the provider empties Trash) — never a permanent delete.

Allow-list (hard-coded, not configurable): Social and Postings. Interaction, Unprocessed, the root,
the Inbox and anything else are never touched. Days come from `.env` locally and from the user's
`retention` settings in the cloud config; 0 (the default) disables a folder.

Runs last in a pass (after sorting and the link picker), inside the same lock. Not an MCP tool.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# The ONLY logical folders retention may ever sweep.
RETENTION_FOLDERS = ("social", "postings")
DEFAULT_MAX_PER_RUN = 200


@dataclass(frozen=True)
class RetentionPolicy:
    days: dict[str, int] = field(default_factory=dict)     # logical folder → days (0 = off)
    max_per_run: int = DEFAULT_MAX_PER_RUN

    @property
    def enabled(self) -> bool:
        return any(d > 0 for f, d in self.days.items() if f in RETENTION_FOLDERS)

    @classmethod
    def from_values(cls, social_days=None, postings_days=None, max_per_run=None) -> "RetentionPolicy":
        def _days(v) -> int:
            try:
                return max(0, int(v or 0))
            except (TypeError, ValueError):
                return 0
        return cls(days={"social": _days(social_days), "postings": _days(postings_days)},
                   max_per_run=max(0, int(max_per_run or DEFAULT_MAX_PER_RUN)))


@dataclass
class RetentionResult:
    trashed: dict[str, int] = field(default_factory=dict)  # logical folder → count
    errors: list[str] = field(default_factory=list)


class RetentionStage:
    def __init__(self, provider, folder_paths: dict[str, str], policy: RetentionPolicy):
        self.provider = provider
        # Only allow-listed logical folders survive, whatever the caller passed.
        self.paths = {k: v for k, v in folder_paths.items() if k in RETENTION_FOLDERS and v}
        self.policy = policy

    def run(self, *, dry_run: bool = False) -> RetentionResult:
        res = RetentionResult()
        budget = self.policy.max_per_run
        for name in RETENTION_FOLDERS:
            days = self.policy.days.get(name, 0)
            path = self.paths.get(name)
            if days <= 0 or not path or budget <= 0:
                continue
            try:
                n = self.provider.trash_expired(path, days, budget, dry_run=dry_run)
            except Exception as exc:     # one folder failing must not stop the other
                res.errors.append(f"retention {name}: {type(exc).__name__}: {exc}")
                continue
            res.trashed[name] = n
            budget -= n
        return res
