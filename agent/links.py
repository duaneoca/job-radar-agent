"""
Link cleaning for extracted posting URLs.

Links come from attacker-controlled email, so we scheme-allowlist them agent-side BEFORE sending to
Job Radar (defense in depth — job-radar also validates at write + render, C2). Only absolute
http(s) survive; javascript:/data:/file:/mailto:/relative/bare-domain are dropped to None.

We NEVER dereference, fetch, expand, or follow these URLs (M2) — only string-validate them.
The single safe coercion: a leading bare `www.` host gets `https://` (very likely a real URL).
"""

from __future__ import annotations

_ALLOWED = ("http://", "https://")


def clean_link(raw: str | None) -> str | None:
    if not raw:
        return None
    s = raw.strip().strip("<>\"'")
    if not s or any(ws in s for ws in (" ", "\t", "\n", "\r")):
        return None                       # URLs have no whitespace — reject junk
    low = s.lower()
    if low.startswith(_ALLOWED):
        return s
    if low.startswith("www."):
        return "https://" + s             # the one safe coercion
    return None                           # scheme-less / relative / javascript: / data: / mailto:
