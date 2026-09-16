"""
Sender policy — deterministic checks run BEFORE any LLM call (a rejected sender costs nothing).

1. Allow-list: the sender's address domain must be an allowed domain or a subdomain of one
   (`notifications.monster.com` matches `monster.com`; `evilmonster.com` does not). An empty list
   allows every sender.
2. Authentication (optional, on by default): the receiving mail server's `Authentication-Results`
   header must show `dmarc=pass` for the sender's own domain. This is what stops a spoofed
   "jobalerts-noreply@linkedin.com" from being trusted.

Forgery resistance: anyone can put a fake `Authentication-Results` header in a message they send.
Receiving servers add their own result at the TOP of the headers, so we only trust the topmost
DMARC result issued by a known server (`mail.protonmail.ch` for Proton, `mx.google.com` for Gmail).

Where the policy comes from:
  • local agent  → `.env` (ALLOWED_SENDER_DOMAINS, REQUIRE_SENDER_AUTH, TRUSTED_AUTHSERV_IDS)
  • cloud agent  → the user's Email Agent settings in Job Radar (`email_policy` in the cloud config)
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from email.utils import parseaddr

DEFAULT_ALLOWED_DOMAINS = (
    "linkedin.com", "glassdoor.com", "monster.com", "indeed.com", "dice.com",
    "builtin.com", "trueup.io", "jobot.com", "jobright.ai",
)
DEFAULT_TRUSTED_AUTHSERV_IDS = ("mail.protonmail.ch", "mx.google.com")

_DMARC = re.compile(r"\bdmarc=(\w+)", re.IGNORECASE)
_HEADER_FROM = re.compile(r"\bheader\.from=([^\s;()]+)", re.IGNORECASE)


def split_list(value: str | list[str] | tuple[str, ...] | None) -> tuple[str, ...]:
    """Accept a comma-separated string (from .env) or a list (from the cloud config)."""
    if value is None:
        return ()
    items = value.split(",") if isinstance(value, str) else value
    return tuple(i.strip().lower().lstrip("@.") for i in items if i and i.strip())


def sender_domain(from_header: str) -> str:
    addr = parseaddr(from_header or "")[1].strip().lower()
    return addr.rsplit("@", 1)[1] if "@" in addr else ""


def domain_allowed(domain: str, allowed: tuple[str, ...]) -> bool:
    if not allowed:
        return True
    return any(domain == d or domain.endswith("." + d) for d in allowed)


@dataclass(frozen=True)
class SenderPolicy:
    allowed_domains: tuple[str, ...] = DEFAULT_ALLOWED_DOMAINS
    require_auth: bool = True
    trusted_authserv_ids: tuple[str, ...] = DEFAULT_TRUSTED_AUTHSERV_IDS

    @classmethod
    def from_values(cls, allowed=None, require_auth=None, trusted=None) -> "SenderPolicy":
        """Build from raw config values; anything missing falls back to the defaults."""
        return cls(
            allowed_domains=split_list(allowed) if allowed is not None else DEFAULT_ALLOWED_DOMAINS,
            require_auth=True if require_auth is None else bool(require_auth),
            trusted_authserv_ids=split_list(trusted) if trusted else DEFAULT_TRUSTED_AUTHSERV_IDS,
        )

    def check(self, from_header: str, auth_results: list[str]) -> str | None:
        """None if the sender is acceptable; otherwise a short reason (safe to log/store)."""
        domain = sender_domain(from_header)
        if not domain:
            return "sender has no usable email address"
        if not domain_allowed(domain, self.allowed_domains):
            return f"sender domain {domain} is not on the allow-list"
        if not self.require_auth:
            return None

        for header in auth_results or []:
            authserv = header.split(";", 1)[0].strip().split()[0].lower() if header.strip() else ""
            if authserv not in self.trusted_authserv_ids:
                continue
            m = _DMARC.search(header)
            if not m:
                continue                      # a trusted header, but not the DMARC line — keep looking
            if m.group(1).lower() != "pass":
                return f"sender failed DMARC ({m.group(1).lower()})"
            hf = _HEADER_FROM.search(header)
            if not hf or hf.group(1).lower() != domain:
                return "authenticated domain does not match the sender"
            return None                       # topmost trusted DMARC result passed for this domain
        return "no DMARC result from a trusted mail server"
