"""The plain-text link path: punctuation is not part of a URL, and a per-recipient endpoint is
not a job.

Both faults were found while diagnosing a real recruiter email (PR #20) and neither had reached a
stored posting, because that email carried HTML and the HTML path is unaffected by the first one.
A text-only copy of the same mail would have stored the reader's own candidate-tracking URL as the
posting's link: the line reads "If you are interested in this position, please click here (URL).",
the trailing "(" left on the label made no difference — "position" is itself an apply word — so the
link filter is what has to stop it, not the label cleanup.

All URLs below are synthetic.
"""

import pytest

from agent.extract import LinkCandidate, extract_links, is_personal_endpoint, trim_url
from agent.recruiter import pick_link


# ── trailing punctuation ──

@pytest.mark.parametrize("raw,want", [
    ("https://x.test/a).", "https://x.test/a"),
    ("https://x.test/a.", "https://x.test/a"),
    ("https://x.test/a,", "https://x.test/a"),
    ("https://x.test/a;", "https://x.test/a"),
    ("https://x.test/a!?", "https://x.test/a"),
    ("https://x.test/a]", "https://x.test/a"),
    ("https://x.test/a>", "https://x.test/a"),
    ('https://x.test/a"', "https://x.test/a"),
    # a URL that opens its own bracket keeps the matching closer
    ("https://x.test/wiki/Example_(disambiguation)", "https://x.test/wiki/Example_(disambiguation)"),
    ("https://x.test/wiki/Example_(disambiguation)).", "https://x.test/wiki/Example_(disambiguation)"),
    ("https://x.test/a", "https://x.test/a"),
])
def test_trim_url(raw, want):
    assert trim_url(raw) == want


def test_parenthesised_url_in_prose_is_clean_and_so_is_its_label():
    text = "Role below.\nFull details here (https://jobs.example.test/eng/42).\nThanks"
    (c,) = extract_links("", text)
    assert c.url == "https://jobs.example.test/eng/42"        # no ")." glued on
    assert c.text == "Full details here ."                     # no dangling "("


def test_bracket_pair_is_only_dropped_when_it_wrapped_the_url():
    text = "See the (remote) role at https://jobs.example.test/42 today"
    (c,) = extract_links("", text)
    assert c.url == "https://jobs.example.test/42"
    assert "(remote)" in c.text


# ── per-recipient endpoints ──

PERSONAL = [
    "https://mail.example.test/optout.jsp?d=abc",
    "https://mail.example.test/opt_out?u=1",
    "https://mail.example.test/unsubscribe/xyz",
    "https://mail.example.test/jobseekers/myjobs/emailmerge_response.jsp?d=2248_1_Someone",
    "https://mail.example.test/employers/open_candnocell.jsp?canid=1715745",
    "https://mail.example.test/candidate-lookup?id=9",
    "https://mail.example.test/preferences-center",
    # the address itself, plain / percent-encoded / hex-encoded in a query value
    "https://mail.example.test/t?to=jane.doe@example.test",
    "https://mail.example.test/t?to=jane.doe%40example.test",
    "https://mail.example.test/r.jsp?d=" + "jane.doe@example.test".encode().hex(),
]

NOT_PERSONAL = [
    "https://www.dice.com/jobs/detail/abc123",
    "https://boards.greenhouse.io/acme/jobs/4001?gh_src=ab12cd34ef56ab78cd90",
    "https://careers.example.test/apply/42",
    "https://jobs.example.test/portal/?a=ddjdnwlerl7psshh9c3kgnera1guuj08&compid=-1",
    "https://www.linkedin.com/in/jane-doe",
    "https://www.examplestaffing.test/",
]


@pytest.mark.parametrize("url", PERSONAL)
def test_is_personal_endpoint_true(url):
    assert is_personal_endpoint(url)


@pytest.mark.parametrize("url", NOT_PERSONAL)
def test_is_personal_endpoint_false(url):
    assert not is_personal_endpoint(url)


def test_personal_endpoints_are_dropped_from_both_paths():
    """They never reach a stored posting NOR the candidate list the model is shown."""
    url = "https://mail.example.test/jobseekers/myjobs/emailmerge_response.jsp?d=1_Jane+Doe"
    text = f"A Staff Engineer role.\nIf you are interested in this position, please click here ({url})."
    html = f'<p>A Staff Engineer role.</p><p>please click <a href="{url}">here</a>.</p>'
    assert extract_links("", text) == []
    assert extract_links(html) == []


def test_text_only_recruiter_mail_stores_no_link_rather_than_the_tracking_url():
    tracking = ("https://mail.example.test/jobseekers/myjobs/emailmerge_response.jsp"
                "?d=2248_1_Jane+Doe&t11=1794952192743")
    optout = "https://mail.example.test/optout.jsp?d=" + "jane.doe@example.test".encode().hex()
    body = ("Hi,\n\nJob Title: Staff Engineer\n\nLocations: Remote\n\n"
            f"If you are interested in this position, please click here ({tracking}).\n\n"
            f"To stop these emails, click unsubscribe ({optout}).\n")
    email = {"message_id": "<t1>", "sender": "Jane Doe <jane@agency.test>",
             "subject": "Staff Engineer", "body_text": body, "body_html": ""}
    assert pick_link(email) is None


def test_a_real_job_link_in_plain_text_is_still_picked():
    body = ("Hi,\n\nApply here: https://www.dice.com/jobs/detail/abc123.\n\n"
            "To stop these emails, click unsubscribe (https://mail.example.test/optout.jsp?u=1).\n")
    email = {"message_id": "<t2>", "sender": "Jane Doe <jane@agency.test>",
             "subject": "Staff Engineer", "body_text": body, "body_html": ""}
    assert pick_link(email) == "https://www.dice.com/jobs/detail/abc123"


def test_apply_word_link_in_plain_text_is_still_picked_when_it_is_not_personal():
    body = "Hi,\n\nFull job details at https://careers.example.test/apply/42.\n"
    email = {"message_id": "<t3>", "sender": "Jane Doe <jane@agency.test>",
             "subject": "Staff Engineer", "body_text": body, "body_html": ""}
    assert pick_link(email) == "https://careers.example.test/apply/42"


# ── "click here" is deliberately NOT an apply phrase ──

def test_click_here_alone_is_not_treated_as_an_apply_link():
    """A link whose only text is "click here" says nothing about where it goes. In this corpus
    such links were per-recipient response endpoints, not job pages, so the apply-word list is
    left as it is — see _APPLY_WORDS in agent/recruiter.py."""
    body = "Hi,\n\nFor the role, click here (https://news.example.test/x).\n"
    email = {"message_id": "<t4>", "sender": "Jane Doe <jane@agency.test>",
             "subject": "Staff Engineer", "body_text": body, "body_html": ""}
    assert pick_link(email) is None


def test_candidate_ids_stay_contiguous_after_filtering():
    optout = "https://mail.example.test/optout.jsp?u=1"
    text = (f"one https://jobs.example.test/1\nstop ({optout})\ntwo https://jobs.example.test/2\n")
    cands = extract_links("", text)
    assert [c.id for c in cands] == [1, 2]
    assert all(isinstance(c, LinkCandidate) for c in cands)
