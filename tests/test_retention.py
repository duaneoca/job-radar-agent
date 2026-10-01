"""Retention: deterministic, allow-listed, Trash-only, never an MCP tool. Synthetic data only."""

import pytest

from agent.retention import RETENTION_FOLDERS, RetentionPolicy, RetentionStage
from agent.runner import run_once
from agent.writer import FakeWriter


# ── policy + stage ────────────────────────────────────────────

def test_policy_from_values_coerces_and_defaults_off():
    assert not RetentionPolicy.from_values().enabled
    p = RetentionPolicy.from_values("14", 14, None)
    assert p.days == {"social": 14, "postings": 14} and p.max_per_run == 200 and p.enabled
    assert RetentionPolicy.from_values("x", -3).days == {"social": 0, "postings": 0}


class FakeProvider:
    def __init__(self, counts=None, fail=()):
        self.counts, self.fail, self.calls = counts or {}, set(fail), []

    def trash_expired(self, folder, older_than_days, limit, dry_run=False):
        self.calls.append((folder, older_than_days, limit, dry_run))
        if folder in self.fail:
            raise RuntimeError("boom")
        return min(self.counts.get(folder, 0), limit)


ALL_PATHS = {"interaction": "R/Interaction", "postings": "R/Postings", "social": "R/Social",
             "unprocessed": "R/Unprocessed", "root": "R"}


def test_only_social_and_postings_are_ever_swept():
    assert RETENTION_FOLDERS == ("social", "postings")
    prov = FakeProvider()
    policy = RetentionPolicy(days={"social": 14, "postings": 14, "interaction": 1, "unprocessed": 1})
    RetentionStage(prov, ALL_PATHS, policy).run()
    assert [c[0] for c in prov.calls] == ["R/Social", "R/Postings"]


def test_per_run_cap_is_shared_across_folders():
    prov = FakeProvider({"R/Social": 150, "R/Postings": 150})
    res = RetentionStage(prov, ALL_PATHS, RetentionPolicy.from_values(14, 14, 200)).run()
    assert res.trashed == {"social": 150, "postings": 50}
    assert prov.calls[1][2] == 50


def test_disabled_folder_is_skipped_and_dry_run_passes_through():
    prov = FakeProvider({"R/Postings": 3})
    res = RetentionStage(prov, ALL_PATHS, RetentionPolicy.from_values(0, 14)).run(dry_run=True)
    assert prov.calls == [("R/Postings", 14, 200, True)] and res.trashed == {"postings": 3}


def test_one_folder_failing_does_not_stop_the_other():
    prov = FakeProvider({"R/Postings": 2}, fail={"R/Social"})
    res = RetentionStage(prov, ALL_PATHS, RetentionPolicy.from_values(14, 14)).run()
    assert res.trashed == {"postings": 2} and "social" in res.errors[0]


# ── IMAP (Proton Bridge / Yahoo) ──────────────────────────────

class _RetentionIMAP:
    def __init__(self, list_lines=None, move_ok=True, uids=b"3 7 9"):
        self.commands, self.selects = [], []
        self.move_ok, self.uids = move_ok, uids
        self.list_lines = list_lines or [b'(\\HasNoChildren) "/" "INBOX"',
                                         b'(\\HasNoChildren \\Trash) "/" "Trash"',
                                         b'(\\HasNoChildren) "/" "R/Social"']

    def list(self):
        return "OK", self.list_lines

    def select(self, folder, readonly=False):
        self.selects.append((folder, readonly))
        return "OK", [b"3"]

    def uid(self, command, *args):
        self.commands.append((command.upper(), args))
        if command.upper() == "SEARCH":
            return "OK", [self.uids]
        if command.upper() == "MOVE":
            return ("OK" if self.move_ok else "NO"), [b""]
        return "OK", [b""]


def _imap(fake):
    from mcp_email.providers.proton import ProtonProvider
    p = ProtonProvider("h", 1, "u", "pw")
    p._conn = fake
    return p


def test_imap_moves_old_unstarred_mail_to_special_use_trash():
    fake = _RetentionIMAP()
    assert _imap(fake).trash_expired("R/Social", 14, 2) == 2
    search = [a for c, a in fake.commands if c == "SEARCH"][0]
    assert search[1] == "BEFORE" and search[3] == "UNFLAGGED"     # server date + not starred
    assert "UNSEEN" not in search and "SEEN" not in search         # read OR unread
    assert ("MOVE", ("3,7", '"Trash"')) in fake.commands           # oldest first, capped
    assert fake.selects == [('"R/Social"', False)]


def test_imap_dry_run_is_readonly_and_moves_nothing():
    fake = _RetentionIMAP()
    assert _imap(fake).trash_expired("R/Social", 14, 10, dry_run=True) == 3
    assert fake.selects == [('"R/Social"', True)]
    assert not [c for c, _ in fake.commands if c in ("MOVE", "COPY", "STORE", "EXPUNGE")]


def test_imap_fallback_expunges_only_the_copied_set():
    fake = _RetentionIMAP(move_ok=False)
    _imap(fake).trash_expired("R/Social", 14, 10)
    cmds = [(c, a[0]) for c, a in fake.commands if c in ("COPY", "STORE", "EXPUNGE")]
    assert cmds == [("COPY", "3,7,9"), ("STORE", "3,7,9"), ("EXPUNGE", "3,7,9")]


@pytest.mark.parametrize("folder", ["INBOX", "Trash", "trash"])
def test_imap_refuses_inbox_and_trash(folder):
    with pytest.raises(ValueError):
        _imap(_RetentionIMAP()).trash_expired(folder, 14, 10)


def test_imap_trash_by_name_when_no_special_use_and_error_when_none():
    named = _RetentionIMAP(list_lines=[b'() "/" "Trash"', b'() "/" "R/Social"'])
    assert _imap(named)._trash_folder() == "Trash"
    with pytest.raises(LookupError):
        _imap(_RetentionIMAP(list_lines=[b'() "/" "R/Social"']))._trash_folder()


def test_imap_zero_days_is_a_noop():
    fake = _RetentionIMAP()
    assert _imap(fake).trash_expired("R/Social", 0, 10) == 0 and fake.commands == []


# ── Gmail ─────────────────────────────────────────────────────

class _GmailRetention:
    def __init__(self):
        self.listed, self.trashed = [], []

    def users(self):
        return self

    def messages(self):
        return self

    def labels(self):
        return self

    def list(self, **kw):
        if "labelIds" in kw:
            self.listed.append(kw)
            return _Exec({"messages": [{"id": "a"}, {"id": "b"}]})
        return _Exec({"labels": [{"name": "R/Social", "id": "SOC"}]})

    def trash(self, userId, id):
        self.trashed.append(id)
        return _Exec({})


class _Exec:
    def __init__(self, r):
        self.r = r

    def execute(self):
        return self.r


def _gmail():
    from mcp_email.providers.gmail import GmailProvider
    p = GmailProvider(root_folder="R/Postings", creds_info={})
    p._svc = _GmailRetention()
    return p


def test_gmail_trashes_old_unstarred_label_mail():
    p = _gmail()
    assert p.trash_expired("R/Social", 14, 10) == 2
    (kw,) = p._svc.listed
    assert kw["labelIds"] == ["SOC"] and kw["q"] == "older_than:14d -is:starred"
    assert p._svc.trashed == ["a", "b"]


def test_gmail_dry_run_and_refusals():
    p = _gmail()
    assert p.trash_expired("R/Social", 14, 10, dry_run=True) == 2 and p._svc.trashed == []
    with pytest.raises(ValueError):
        p.trash_expired("INBOX", 14, 10)


# ── never an AI-facing tool ───────────────────────────────────

def test_mcp_server_exposes_no_trash_or_retention_tool():
    import pathlib
    src = pathlib.Path("mcp_email/server.py").read_text()
    tools = [ln for ln in src.splitlines() if ln.startswith("def ")]
    assert not any("trash" in t or "retention" in t or "delete" in t for t in tools)
    assert "trash_expired" not in src


# ── runner ────────────────────────────────────────────────────

class _NoMail:
    def get_unread(self):
        return []


def test_run_once_runs_retention_last_and_reports():
    prov = FakeProvider({"R/Social": 4})
    stage = RetentionStage(prov, ALL_PATHS, RetentionPolicy.from_values(14, 0))
    res = run_once(reader=_NoMail(), writer=FakeWriter(), llm=object(), prompts=None,
                   use_lock=False, retention_stage=stage, dry_run=True)
    assert res.trashed == {"social": 4} and prov.calls[0][3] is True


def test_cloud_bundle_retention_defaults_off():
    from agent.config import retention_from_config_bundle
    assert not retention_from_config_bundle({}).enabled
    p = retention_from_config_bundle({"retention": {"social_days": 14, "postings_days": 14}})
    assert p.days == {"social": 14, "postings": 14}
