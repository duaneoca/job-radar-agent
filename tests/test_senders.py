"""Sender allow-list + DMARC check (runs before any LLM call)."""

from agent.senders import SenderPolicy, domain_allowed, sender_domain, split_list
from samples import auth_pass

LI = "LinkedIn Job Alerts <jobalerts-noreply@linkedin.com>"


def test_subdomains_match_but_lookalikes_do_not():
    allowed = ("monster.com", "indeed.com")
    assert domain_allowed("notifications.monster.com", allowed)
    assert domain_allowed("indeed.com", allowed)
    assert not domain_allowed("evilmonster.com", allowed)
    assert not domain_allowed("monster.com.evil.io", allowed)


def test_domain_is_taken_from_the_address_not_the_display_name():
    assert sender_domain('"linkedin.com" <phish@evil.io>') == "evil.io"
    assert SenderPolicy().check('"linkedin.com" <phish@evil.io>', auth_pass("evil.io")) is not None


def test_allowed_and_authenticated_sender_passes():
    assert SenderPolicy().check(LI, auth_pass("linkedin.com")) is None


def test_unknown_sender_is_rejected():
    reason = SenderPolicy().check("Recruiter <r@somewhere.biz>", auth_pass("somewhere.biz"))
    assert "allow-list" in reason


def test_missing_auth_rejected_unless_disabled():
    assert "no DMARC" in SenderPolicy().check(LI, [])
    assert SenderPolicy(require_auth=False).check(LI, []) is None


def test_dmarc_failure_rejected():
    ar = ["mail.protonmail.ch; dmarc=fail (p=reject dis=reject) header.from=linkedin.com"]
    assert "failed DMARC" in SenderPolicy().check(LI, ar)


def test_authenticated_domain_must_match_sender():
    assert "does not match" in SenderPolicy().check(LI, auth_pass("evil.io"))


def test_untrusted_or_lower_forged_results_are_ignored():
    forged_untrusted = ["attacker.example; dmarc=pass header.from=linkedin.com"]
    assert SenderPolicy().check(LI, forged_untrusted) is not None
    # Proton's real (top) result says fail; a forged "pass" further down must not win.
    real_then_forged = ["mail.protonmail.ch; dmarc=fail header.from=linkedin.com",
                        "mail.protonmail.ch; dmarc=pass header.from=linkedin.com"]
    assert "failed DMARC" in SenderPolicy().check(LI, real_then_forged)


def test_gmail_authserv_is_trusted_by_default():
    assert SenderPolicy().check(LI, auth_pass("linkedin.com", authserv="mx.google.com")) is None


def test_empty_allow_list_allows_any_sender():
    pol = SenderPolicy.from_values(allowed="", require_auth=False)
    assert pol.allowed_domains == () and pol.check("x <a@anything.io>", []) is None


def test_values_parse_from_env_strings_and_cloud_lists():
    assert split_list(" LinkedIn.com, @indeed.com ,,") == ("linkedin.com", "indeed.com")
    assert split_list(["Dice.com"]) == ("dice.com",)
    pol = SenderPolicy.from_values(None, None, None)
    assert pol.require_auth and "linkedin.com" in pol.allowed_domains
