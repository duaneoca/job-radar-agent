"""Agency mail whose boilerplate outvoted the pitch (the Nov 2026 "Tanisha Systems" report).

Two independent faults produced one bad posting from a real recruiter email:

  * the posting said "On-site" though the pitch said "Locations: Remote", because the agency's
    About blurb says it "combines local on-site and off-site resources" and work_mode() scanned
    the whole text with on-site outranking remote;
  * the posting's company was a street address, because the signature window was the last 15
    non-empty lines and this mail ends in ~14 lines of EEO notice, unsubscribe and
    candidate-lookup boilerplate — so the real sign-off fell outside it and the only pickable
    fragment left was "Address: …".

The fixture below is SYNTHESIZED to that shape (labelled remote + on-site boilerplate; a real
signature above a long legal tail). The reported email carried the user's own candidate-tracking
and unsubscribe URLs, which are not reproduced here.
"""

import pytest

from agent.jev import JevError
from agent.pay import work_mode
from agent.recruiter import (build_card_and_posting, clean_name, fragments, head_fragments,
                             head_lines, looks_like_address, signature_lines)

SUBJECT = "Urgent role || Staff Platform Engineer - Payments & AI || Remote"
SENDER = '"Riya Example (#EXA3005)" <riya.example@examplestaffing.test>'

BODY = """Hi Duane,

I hope this message finds you well.

We have an exciting job opportunity with one of our clients, below are the details of the position:

Job Title:    Staff Platform Engineer - Payments & AI

Locations: Remote

Mode of Hiring : Full-time roles

Job Responsibilities:

We are seeking a hands-on platform engineer to work with Payments leaders and technical teams.

Build and operate services that settle and reconcile high-volume payment flows.

About Example Staffing, Inc.

Example Staffing, founded in 2002, is a leading provider of Custom Application Development and
end-to-end IT Services to clients globally. We use a client-centric engagement model that combines
local on-site and off-site resources with the cost, scale and quality advantages of global delivery.

Example Staffing services clients in Government, Banking, Insurance, Healthcare and Retail.

Want to read more about Example Staffing? Visit us at www.examplestaffing.test

Thanks again.

Thanks & Regards

Riya Example

Sr. Recruiter

Example Staffing Inc

Phone : +1 555 010 4341

Email: riya.example@examplestaffing.test

LinkedIn: https://www.linkedin.com/in/riya-example-803020149/

www.examplestaffing.test

Address: 99 Wood Ave South Suite # 308, Iselin, NJ 08830

Example Staffing, Inc. is an Equal Employment Opportunity Employer. All qualified applicants will
receive consideration for employment without regard to race, colour, religion, sex, sexual
orientation, national origin, age, physical or mental disability, or any other status protected by
applicable federal, state, or local law.

Website (https://www.examplestaffing.test/)   Open Jobs (https://jobs.examplestaffing.test/portal)

If you are qualified, available, interested, planning to make a change, or know of a friend who
might have the required qualifications and interest, please call me ASAP at (555) 010-4341 Ext.761,
even if we have spoken recently about a different position.

Note: Please allow me to reiterate that I chose to contact you either because your resume had been
posted to one of the internet job sites to which we subscribe, or you had previously submitted your
resume to our career section page on our website.

If you are not currently seeking employment, or if you would prefer I contact you at some later
date, please indicate your date of availability so that I may honor your request.

Note : "Example Staffing, Inc. is an Equal Employment Opportunity Employer. All qualified applicants
will receive consideration for employment without regard to race, color, religion or sex.

If you are interested in this position, please click here (https://apply.examplestaffing.test/r/1).

You have received this message because either you have contacted our Company to express interest in
employment, or you have posted your Resume in an area accessible to recruiters.

If you no longer wish to receive any emails from our Company, you can withdraw your consent at any
time by clicking unsubscribe (https://apply.examplestaffing.test/optout).

Lookup Candidate (https://apply.examplestaffing.test/c/1)
"""


def _email():
    return {"message_id": "<agency1>", "sender": SENDER, "subject": SUBJECT, "body_text": BODY,
            "received_at": "2026-11-17T16:41:06+00:00"}


class FakeJev:
    """Picks by fragment TEXT. A pick the question does not offer raises — which is the assertion
    for options we withhold."""

    def __init__(self, picks):
        self.picks, self.calls = picks, []
        self.run_cost = 0.0

    def ask(self, state, questions):
        self.calls.append((state, questions))
        out = {}
        for name, q in questions.items():
            if q["type"] == "noul":
                out[name] = {"type": "noul", "noul": self.picks.get(name, 0.9)}
                continue
            want = self.picks.get(name, "none")
            key = ("none" if want == "none"
                   else next(k for k, v in q["criteria"].items() if v == want))
            out[name] = {"type": "choice", "choice": key, "confidence": 0.9,
                         "probabilities": {key: 0.9}}
        return out


PICKS = {"job_title": "Staff Platform Engineer", "hiring_company": "none",
         "title": "Sr. Recruiter", "employer": "Example Staffing Inc",
         "job_location": "Remote", "is_agency": 0.9}


# ── cause 1: the pitch's labelled mode beats the footer's stray "on-site" ──

def test_work_mode_is_remote_despite_onsite_in_the_about_blurb():
    text = f"{SUBJECT}\n{BODY}"
    assert "local on-site and off-site" in text        # the fault is still in the fixture
    assert work_mode(text) == "Remote"


def test_posting_header_and_remote_flag_follow_the_labelled_field():
    jev = FakeJev(PICKS)
    _card, posting = build_card_and_posting(jev, _email())
    assert posting["remote"] is True
    assert posting["description"].startswith("Full-time · Remote")


# ── cause 2: the signature window is anchored on the sender, not on the tail ──

def test_signature_window_starts_at_the_senders_name():
    name = clean_name(SENDER)
    assert name == "Riya Example"
    sig = signature_lines(BODY, name)
    assert sig[0] == "Riya Example"
    assert "Sr. Recruiter" in sig and "Example Staffing Inc" in sig


def test_unanchored_window_would_have_missed_the_signature():
    """Why the anchor is needed: the old behaviour is still what you get without a name."""
    sig = signature_lines(BODY)
    assert "Sr. Recruiter" not in sig and "Example Staffing Inc" not in sig


def test_signature_fragments_now_offer_the_real_employer():
    frags = fragments(signature_lines(BODY, clean_name(SENDER)), clean_name(SENDER))
    assert "Example Staffing Inc" in frags and "Sr. Recruiter" in frags


def test_company_is_the_agency_not_the_street_address():
    jev = FakeJev(PICKS)
    card, posting = build_card_and_posting(jev, _email())
    assert card["employer"] == "Example Staffing Inc"
    assert card["title"] == "Sr. Recruiter"
    assert posting["company"] == "Example Staffing Inc (agency)"
    assert "Wood Ave" not in posting["company"] and "08830" not in posting["company"]


def test_the_address_is_never_offered_as_a_company():
    jev = FakeJev(PICKS)
    build_card_and_posting(jev, _email())
    _state, questions = jev.calls[0]
    offered = set(questions["employer"]["criteria"].values())
    assert "Example Staffing Inc" in offered
    assert not any(looks_like_address(v) for v in offered)
    # still offered where an address-like string is a legitimate answer
    assert any("Address" in v for v in questions["title"]["criteria"].values())


def test_jev_failure_still_leaves_the_header_derived_card():
    class Down(FakeJev):
        def ask(self, state, questions):
            raise JevError("down")

    card, posting = build_card_and_posting(Down(PICKS), _email())
    assert card["name"] == "Riya Example"
    assert card["email"] == "riya.example@examplestaffing.test"
    assert posting is None


# ── the address matcher itself ──

@pytest.mark.parametrize("frag", [
    "Address: 99 Wood Ave South Suite # 308, Iselin, NJ 08830",
    "99 Wood Ave South",
    "Suite # 308",
    "Iselin, NJ 08830",
    "1600 Amphitheatre Pkwy",
    "Floor 12",
])
def test_looks_like_address_true(frag):
    assert looks_like_address(frag)


@pytest.mark.parametrize("frag", [
    "Example Staffing Inc", "Sr. Recruiter", "Tanisha Systems, Inc.", "Staff Platform Engineer",
    "Mountain View, CA", "NYC Madison Square Garden", "Remote", "Full-time roles",
    "St. Jude Medical", "Street Fighter Studios",
])
def test_looks_like_address_false(frag):
    assert not looks_like_address(frag)


# ── the hiring company is not the location ──

def test_hiring_company_dropped_when_it_is_the_location():
    jev = FakeJev({**PICKS, "hiring_company": "Remote", "job_location": "Remote"})
    _card, posting = build_card_and_posting(jev, _email())
    assert posting["company"] == "Example Staffing Inc (agency)"


def test_head_fragments_still_carry_the_role():
    frags = head_fragments(SUBJECT, head_lines(BODY), clean_name(SENDER))
    assert "Staff Platform Engineer" in frags
