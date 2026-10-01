"""
Posting de-duplication (V2).

Job boards re-send the same posting for days, so every verified posting gets a `dedup_key`:
  • a stable job id when the URL carries one — LinkedIn `/jobs/view/<id>`, Glassdoor `jobListingId`,
    Indeed `jk`, or a direct applicant-tracking-system URL (Greenhouse, Lever, Ashby, Workday, …)
  • otherwise `company | title`. Monster, Dice, Built In and Indeed alert links are one-time
    tracking redirects that hide the real job URL, and we never follow links (M2), so the text is
    the only stable identity for those.

Two stores, same interface (dual design):
  • local agent  → `SqliteDedupStore` in `<AGENT_HOME>/data/dedup.sqlite`
  • cloud agent  → `NullDedupStore`; Job Radar enforces uniqueness on (user_id, dedup_key) in its own
                   database (INTEGRATION_SPEC §3.6). The key is always sent, so enforcement can be
                   switched on server-side without an agent change.

Keys are recorded only AFTER Job Radar accepted the write, so a failed write never hides a posting.
The window is measured from the most recent sighting, so a job re-sent every day stays suppressed.
"""

from __future__ import annotations

import os
import re
import sqlite3
import time
from pathlib import Path
from typing import Callable, Iterable, Protocol
from urllib.parse import parse_qs, urlsplit

from .extract import match_key

_LINKEDIN_JOB = re.compile(r"/jobs/view/(\d+)")
_ATS_HOSTS = (
    "greenhouse.io", "lever.co", "ashbyhq.com", "myworkdayjobs.com", "smartrecruiters.com",
    "workable.com", "bamboohr.com", "jobvite.com", "icims.com", "rippling.com", "breezy.hr",
)
_NON_ALNUM = re.compile(r"[^0-9a-z]+")


def _slug(s: str) -> str:
    return _NON_ALNUM.sub(" ", match_key(s)).strip()


def _host_is(host: str, domain: str) -> bool:
    return host == domain or host.endswith("." + domain)


def dedup_key(url: str, company: str, title: str) -> str:
    parts = urlsplit(url or "")
    host = parts.netloc.lower().split(":")[0]
    query = {k.lower(): v for k, v in parse_qs(parts.query).items()}

    if _host_is(host, "linkedin.com"):
        m = _LINKEDIN_JOB.search(parts.path)
        if m:
            return f"linkedin:{m.group(1)}"
    if _host_is(host, "glassdoor.com") and query.get("joblistingid"):
        return f"glassdoor:{query['joblistingid'][0]}"
    if _host_is(host, "indeed.com") and query.get("jk"):
        return f"indeed:{query['jk'][0].lower()}"
    if any(_host_is(host, d) for d in _ATS_HOSTS):
        return f"url:{host}{parts.path.rstrip('/').lower()}"
    return f"ct:{_slug(company)}|{_slug(title)}"


class DedupStore(Protocol):
    def seen(self, key: str) -> bool: ...
    def add(self, keys: Iterable[str]) -> None: ...


class NullDedupStore:
    """Never reports a duplicate (cloud path: Job Radar enforces uniqueness server-side)."""

    def seen(self, key: str) -> bool:
        return False

    def add(self, keys: Iterable[str]) -> None:
        return None

    def close(self) -> None:
        return None


class ReadOnlyDedupStore:
    """Dry-run wrapper: reports duplicates but records nothing."""

    def __init__(self, inner: DedupStore):
        self._inner = inner

    def seen(self, key: str) -> bool:
        return self._inner.seen(key)

    def add(self, keys: Iterable[str]) -> None:
        return None


class SqliteDedupStore:
    def __init__(self, path: str | os.PathLike | None = None, window_days: int = 30,
                 now: Callable[[], float] = time.time):
        if path is None:
            from .paths import data_dir
            path = data_dir() / "dedup.sqlite"
        self._path = Path(path)
        self._window = window_days * 86400
        self._now = now
        self._path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._conn = self._open()
        except sqlite3.DatabaseError:
            # A corrupt store must not stop the agent: set it aside and start fresh.
            self._path.replace(self._path.with_name(self._path.name + f".corrupt-{int(time.time())}"))
            self._conn = self._open()

    def _open(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path, timeout=30)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE IF NOT EXISTS seen_postings ("
                     "key TEXT PRIMARY KEY, last_seen REAL NOT NULL)")
        conn.execute("DELETE FROM seen_postings WHERE last_seen < ?", (self._cutoff(),))
        conn.commit()
        try:
            os.chmod(self._path, 0o600)
        except OSError:
            pass
        return conn

    def _cutoff(self) -> float:
        return self._now() - self._window

    def seen(self, key: str) -> bool:
        row = self._conn.execute("SELECT 1 FROM seen_postings WHERE key = ? AND last_seen >= ?",
                                 (key, self._cutoff())).fetchone()
        return row is not None

    def add(self, keys: Iterable[str]) -> None:
        now = self._now()
        self._conn.executemany(
            "INSERT INTO seen_postings (key, last_seen) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET last_seen = excluded.last_seen",
            [(k, now) for k in set(keys)])
        self._conn.commit()

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass
