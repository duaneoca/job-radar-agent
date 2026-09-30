"""Sorter routing rule + what Jev is shown. Pure functions — synthetic data only."""

import pytest

from agent.jev import ChoiceAnswer
from agent.sorter import (BODY_CHAR_CAP, CATEGORY_FOLDER, CATEGORY_QUESTION, build_state, decide,
                          folder_probabilities, sender_domain, trim_body)


def _ans(cat, conf=0.95, probs=None):
    return ChoiceAnswer(cat, conf, probs or {cat: 0.95, "other": 0.05})


def _decide(a, sender="someone@example.com"):
    return decide(a, sender, min_confidence=0.85, min_margin=0.3)


@pytest.mark.parametrize("cat,folder,mark_read", [
    ("recruiter_outreach", "interaction", False),
    ("application_update", "interaction", False),
    ("connection_request", "interaction", False),
    ("direct_message", "interaction", False),
    ("job_alert", "postings", False),          # left unread for the link-picker stage
    ("network_social", "social", True),
])
def test_confident_answer_routes_to_its_folder(cat, folder, mark_read):
    d = _decide(_ans(cat))
    assert (d.folder, d.mark_read, d.reason) == (folder, mark_read, "")


def test_other_goes_to_unprocessed_even_when_confident():
    d = _decide(_ans("other", 0.99))
    assert d.folder == "unprocessed" and d.mark_read


def test_low_confidence_goes_to_unprocessed():
    d = _decide(_ans("job_alert", 0.6, {"job_alert": 0.6, "other": 0.4}))
    assert d.folder == "unprocessed" and "low confidence" in d.reason


def test_ambiguous_margin_goes_to_unprocessed():
    d = decide(_ans("job_alert", 0.9, {"job_alert": 0.62, "network_social": 0.38}), "",
               min_confidence=0.6, min_margin=0.3)
    assert d.folder == "unprocessed" and "ambiguous" in d.reason


def test_split_within_one_folder_is_still_confident():
    # recruiter vs connection request: both Interaction → 0.95 for the folder
    d = _decide(_ans("recruiter_outreach", 0.5,
                     {"recruiter_outreach": 0.5, "connection_request": 0.45, "other": 0.05}))
    assert (d.folder, round(d.confidence, 2)) == ("interaction", 0.95)


def test_folder_probabilities_sums_categories():
    fp = folder_probabilities(_ans("x", 0.5, {"recruiter_outreach": 0.3, "application_update": 0.2,
                                              "job_alert": 0.4, "mystery": 0.1}))
    assert fp == pytest.approx({"interaction": 0.5, "postings": 0.4, "unprocessed": 0.1})


@pytest.mark.parametrize("sender", ['"Dice Recruiter" <abc-def@user.dice.com>',
                                    "Kajal Saini <3fc-mcp@USER.DICE.COM>"])
def test_bulk_channel_recruiter_mail_goes_to_postings(sender):
    d = _decide(_ans("recruiter_outreach"), sender)
    assert (d.folder, d.rule, d.mark_read) == ("postings", "bulk_sender", False)


@pytest.mark.parametrize("cat", ["application_update", "connection_request"])
def test_bulk_rule_never_moves_non_recruiter_interaction(cat):
    assert _decide(_ans(cat), "x <a@user.dice.com>").folder == "interaction"


@pytest.mark.parametrize("sender", ["Pavan <pavank@primusglobal.com>",   # direct agency mail
                                    "x <a@dice.com.evil.io>", "x <a@notuser.dice.com>"])
def test_bulk_rule_is_exact_domain_match(sender):
    assert _decide(_ans("recruiter_outreach"), sender).folder == "interaction"


def test_bulk_rule_does_not_bypass_low_confidence():
    d = _decide(_ans("recruiter_outreach", 0.5, {"recruiter_outreach": 0.5, "job_alert": 0.5}),
                "x <a@user.dice.com>")
    assert d.folder == "unprocessed"


def test_sender_domain_parses_display_names():
    assert sender_domain('"Dice Recruiter" <A@User.Dice.com>') == "user.dice.com"
    assert sender_domain("") == ""


def test_unknown_category_goes_to_unprocessed():
    assert _decide(_ans("spam")).folder == "unprocessed"


def test_question_criteria_match_routing_table():
    assert set(CATEGORY_QUESTION["criteria"]) == set(CATEGORY_FOLDER)


def test_trim_body_drops_urls_quotes_and_reply_history():
    body = ("Hi Duane, see https://evil.example/track?id=123 for the role.\n"
            "> quoted line\n"
            "Thanks\n\n"
            "On Mon, Sep 1, 2026 at 9:00 AM Someone <s@x.com> wrote:\n"
            "older message text")
    t = trim_body(body)
    assert "https://" not in t and "[link]" in t
    assert "quoted line" not in t and "older message" not in t
    assert t.endswith("Thanks")


def test_trim_body_caps_length():
    assert len(trim_body("x" * (BODY_CHAR_CAP * 3))) == BODY_CHAR_CAP


def test_build_state_has_only_from_subject_body():
    s = build_state({"sender": "a@b.com", "subject": "Hi", "body_text": "Body",
                     "body_html": "<a href='https://x'>x</a>", "auth_results": ["dmarc=pass"]})
    assert s == {"from": "a@b.com", "subject": "Hi", "body": "Body"}
