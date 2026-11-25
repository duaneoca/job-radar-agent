"""Staffing Stage A building blocks (INTEGRATION_SPEC §3.9). Synthetic strings only."""

import pytest

from agent.pay import parse_pay
from agent.staffing import (Criteria, allowed_states, clean_location, employment_type_enum,
                            hard_filter, jaccard, jd_text, labelled_location, mentions_c2c_and_w2,
                            shingles, state_of, work_mode_enum)


# ── employment type: explicit > hourly > agency > full time ──

@pytest.mark.parametrize("text,expected", [
    ("Contract to hire, 6 months", "contract_to_hire"),
    ("C2C only", "contract_c2c"),
    ("W2 role", "contract_w2"),
    ("12 month contract", "contract"),
    ("Full-time, direct hire", "full_time"),
])
def test_explicit_text_wins(text, expected):
    assert employment_type_enum(text, parse_pay("$90/hr"), True) == expected


def test_hourly_pay_means_contract():
    assert employment_type_enum("Data Architect", parse_pay("$85/hr"), False) == "contract"


def test_agency_means_contract_without_other_signal():
    assert employment_type_enum("Data Architect", None, True) == "contract"


def test_default_is_full_time_and_unsure_agency_does_not_count():
    assert employment_type_enum("Data Architect", parse_pay("$180K"), None) == "full_time"


def test_part_time_has_no_enum_and_falls_through():
    assert employment_type_enum("part-time", parse_pay("$60/hr"), None) == "contract"


def test_c2c_and_w2_both_named_is_counted():
    assert mentions_c2c_and_w2("Open to W2 or C2C")
    assert not mentions_c2c_and_w2("W2 only")


@pytest.mark.parametrize("text,expected", [
    ("Work mode: Hybrid (3 days)", "hybrid"),
    ("This role is 100% remote", "remote"),
    ("Onsite in Dallas", "onsite"),
    ("Great team", None),
])
def test_work_mode_enum(text, expected):
    assert work_mode_enum(text) == expected


# ── location ──

@pytest.mark.parametrize("cands,expected", [
    (("Dallas, TX",), "Dallas, TX"),
    (("Dallas, Texas",), "Dallas, TX"),
    (("Jersey City NJ 07302",), "Jersey City, NJ"),
    (("Hybrid Dallas, TX",), "Dallas, TX"),
    (("New York, NY",), "New York, NY"),
    (("Texas",), "TX"),
    (("TX",), "TX"),
    (("Remote", "Austin, TX"), "Austin, TX"),
    (("London, UK",), None),
    (("in",), None),
    ((None, ""), None),
])
def test_clean_location(cands, expected):
    assert clean_location(*cands) == expected


def test_labelled_location_and_state_of():
    assert labelled_location("Role: X\nLocation: Plano, TX (Hybrid)\nRate: $80") == "Plano, TX (Hybrid)"
    assert state_of("Plano, TX") == "TX" and state_of("TX") == "TX" and state_of(None) is None


def test_allowed_states_reduces_both_shapes_and_ignores_the_rest():
    assert allowed_states("Austin, TX", ["TX", "Oklahoma", "ca", "Remote", "", None]) == {"TX", "OK", "CA"}
    assert allowed_states(None, []) == set()
    assert allowed_states("Austin, TX 78701", None) == {"TX"}


# ── cleaned JD ──

PITCH = """Hi Pat,
Hope you are doing well.
This is Sam from Example Staffing.

Role: Senior Data Architect
Location: Dallas, TX (Hybrid)
Rate: $80/hr on C2C

Design and own the enterprise data platform on Snowflake and dbt.
Partner with analytics engineering on modeling standards.

Please share your updated resume if interested.

Thanks & Regards,
Sam Example
Example Staffing | sam@example-staffing.test
"""


def test_jd_text_strips_greeting_boilerplate_rate_and_signature():
    jd = jd_text(PITCH, "Sam Example")
    assert "Senior Data Architect" in jd and "Snowflake" in jd
    for gone in ("Hi Pat", "Hope you", "This is Sam", "$80", "resume", "Regards", "Sam Example",
                 "sam@"):
        assert gone not in jd


def test_jd_text_keeps_an_early_thanks():
    jd = jd_text("Thanks for connecting.\nBuild pipelines in Spark.\nShip models.\nMore.\n", None)
    assert jd.startswith("Thanks for connecting.")


# ── dedup ──

def test_same_jd_from_two_vendors_scores_high_and_different_reqs_low():
    a = jd_text(PITCH, "Sam Example")
    b = jd_text(PITCH.replace("Sam", "Lee").replace("Example Staffing", "Other Corp"), "Lee Example")
    other = ("Role: Platform SRE\nRun Kubernetes clusters on AWS and own the on-call rotation for "
             "the payments platform. Terraform everything.")
    assert jaccard(shingles(a), shingles(b)) > 0.9
    assert jaccard(shingles(a), shingles(other)) < 0.1


def test_shingles_short_and_empty_text():
    assert shingles("") == frozenset() and len(shingles("one two")) == 1
    assert jaccard(frozenset(), shingles("a b c d e")) == 0.0


# ── hard filters ──

CRIT = Criteria.from_dict({"work_style": "hybrid", "home_city": "Austin, TX", "search_locations": ["TX"],
                           "min_salary": 150000, "excluded_companies": ["Acme"]},
                          allowed_types=["full_time", "contract_w2"])


def test_missing_fields_never_fail():
    assert hard_filter({}, CRIT) is None


@pytest.mark.parametrize("posting,needle", [
    ({"work_mode": "onsite"}, "Work mode onsite"),
    ({"work_mode": "hybrid", "location": "Seattle, WA"}, "outside TX"),
    ({"employment_type": "contract_c2c"}, "contract_c2c"),
    ({"salary_max": 140000}, "$140,000"),
    ({"company": "acme", "client_named": True}, "excluded"),
])
def test_each_filter_drops_a_clear_miss(posting, needle):
    assert needle in hard_filter(posting, CRIT)


def test_passes():
    assert hard_filter({"work_mode": "remote", "location": "Seattle, WA"}, CRIT) is None
    assert hard_filter({"work_mode": "hybrid", "location": "Dallas, TX", "employment_type": "full_time",
                        "salary_max": 160000, "company": "Acme (agency)", "client_named": False},
                       CRIT) is None


def test_onsite_and_any_pass_every_mode_and_empty_states_skip_location():
    c = Criteria.from_dict({"work_style": "onsite"})
    assert hard_filter({"work_mode": "remote", "location": "Seattle, WA"}, c) is None
    assert hard_filter({"work_mode": "onsite", "location": "Seattle, WA"}, c) is None
