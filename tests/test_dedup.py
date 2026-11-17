"""Posting dedup keys + the local SQLite store."""

import os
import stat

from agent.dedup import NullDedupStore, ReadOnlyDedupStore, SqliteDedupStore, dedup_key


def test_stable_ids_from_known_job_urls():
    assert dedup_key("https://www.linkedin.com/comm/jobs/view/4457198691/?trackingId=a", "A", "B") \
        == "linkedin:4457198691"
    assert dedup_key("https://linkedin.com/jobs/view/42", "", "") == "linkedin:42"
    assert dedup_key("https://www.glassdoor.com/partner/jobListing.htm?pos=1&jobListingId=1009",
                     "A", "B") == "glassdoor:1009"
    assert dedup_key("https://www.indeed.com/viewjob?jk=ABC123&from=x", "A", "B") == "indeed:abc123"
    assert dedup_key("https://jobs.ashbyhq.com/snowflake/639b/?utm_source=x", "A", "B") \
        == "url:jobs.ashbyhq.com/snowflake/639b"


def test_tracking_redirects_fall_back_to_company_and_title():
    k1 = dedup_key("http://click.monster.com/f/a/AAA", "Initech, Inc.", "Forward Deployed Engineer")
    k2 = dedup_key("http://click.monster.com/f/a/ZZZ", "initech inc", "forward  deployed engineer")
    assert k1 == k2 == "ct:initech inc|forward deployed engineer"


def test_lookalike_hosts_are_not_trusted_for_ids():
    assert dedup_key("https://evil-linkedin.com/jobs/view/1", "A", "B").startswith("ct:")


class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t


def test_store_remembers_within_window_and_forgets_after(tmp_path):
    clock = Clock()
    s = SqliteDedupStore(tmp_path / "d.sqlite", window_days=30, now=clock)
    assert not s.seen("k")
    s.add(["k"])
    assert s.seen("k")
    clock.t += 31 * 86400
    assert not s.seen("k")


def test_resighting_refreshes_the_window(tmp_path):
    clock = Clock()
    s = SqliteDedupStore(tmp_path / "d.sqlite", window_days=30, now=clock)
    s.add(["k"])
    clock.t += 20 * 86400
    s.add(["k"])                       # re-sent posting seen again
    clock.t += 20 * 86400              # 40 days after first sighting, 20 after the last
    assert s.seen("k")


def test_store_persists_prunes_and_is_private(tmp_path):
    path = tmp_path / "d.sqlite"
    clock = Clock()
    s = SqliteDedupStore(path, window_days=1, now=clock)
    s.add(["a", "b"])
    s.close()
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert SqliteDedupStore(path, window_days=1, now=clock).seen("a")
    clock.t += 2 * 86400
    s2 = SqliteDedupStore(path, window_days=1, now=clock)      # prunes on open
    assert s2._conn.execute("SELECT COUNT(*) FROM seen_postings").fetchone()[0] == 0


def test_corrupt_store_is_set_aside_not_fatal(tmp_path):
    path = tmp_path / "d.sqlite"
    path.write_bytes(b"this is not a sqlite database" * 100)
    s = SqliteDedupStore(path)
    s.add(["x"])
    assert s.seen("x")
    assert any(p.name.startswith("d.sqlite.corrupt-") for p in tmp_path.iterdir())


def test_null_and_read_only_stores(tmp_path):
    assert not NullDedupStore().seen("x")
    inner = SqliteDedupStore(tmp_path / "d.sqlite")
    inner.add(["seen-before"])
    ro = ReadOnlyDedupStore(inner)
    ro.add(["new"])
    assert ro.seen("seen-before") and not inner.seen("new")


# ── key length cap (INTEGRATION_SPEC §3.6: server column is String(300)) ──

def test_normal_keys_are_untouched_by_the_cap():
    from agent.dedup import dedup_key
    assert dedup_key("https://x.test/a", "Acme", "Forward Deployed Engineer") == \
        "ct:acme|forward deployed engineer"
    assert dedup_key("https://www.linkedin.com/jobs/view/4457198691/", "", "") == \
        "linkedin:4457198691"


def test_long_key_is_capped_to_the_server_column_width():
    from agent.dedup import MAX_DEDUP_KEY, dedup_key
    key = dedup_key("", "C" * 400, "T" * 400)
    assert len(key) == MAX_DEDUP_KEY
    assert key.startswith("ct:ccc")


def test_capping_keeps_distinct_long_keys_distinct():
    """A plain truncation would collide here: both share the first 300 chars.
    Phase B skips a conflicting posting, so a collision silently drops a real job."""
    from agent.dedup import dedup_key
    a = dedup_key("", "C" * 400, "T" * 400)
    b = dedup_key("", "C" * 400, "T" * 401)
    assert a[:280] == b[:280]          # same prefix
    assert a != b                      # different key


def test_cap_is_deterministic():
    from agent.dedup import dedup_key
    assert dedup_key("", "C" * 400, "T" * 400) == dedup_key("", "C" * 400, "T" * 400)


def test_long_ats_url_key_is_capped_too():
    from agent.dedup import MAX_DEDUP_KEY, dedup_key
    key = dedup_key("https://boards.greenhouse.io/" + "seg/" * 200, "Acme", "Engineer")
    assert len(key) <= MAX_DEDUP_KEY
    assert key.startswith("url:boards.greenhouse.io")
