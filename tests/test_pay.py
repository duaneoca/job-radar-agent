"""Pay / employment / work-mode parsing from recruiter pitches. Synthetic strings."""

import pytest

from agent.pay import HOURS_PER_YEAR, employment_type, parse_pay, work_mode


@pytest.mark.parametrize("text,lo,hi", [
    ("Rate: $70-80/hr on C2C", 70 * 2080, 80 * 2080),
    ("Pay $65 – $75 per hour", 65 * 2080, 75 * 2080),
    ("$70/hr W2", 70 * 2080, 70 * 2080),
    ("Rate: $85 (flexible)", 85 * 2080, 85 * 2080),
    ("$62.50/hour", 130000, 130000),
    ("hourly: $90 hourly", 90 * 2080, 90 * 2080),
])
def test_hourly_is_annualized_at_2080_hours(text, lo, hi):
    p = parse_pay(text)
    assert (p.salary_min, p.salary_max) == (lo, hi) and p.hourly is not None
    assert HOURS_PER_YEAR == 2080


@pytest.mark.parametrize("text,lo,hi", [
    ("Salary $180K-$200K + bonus", 180000, 200000),
    ("$185,000 - $210,000", 185000, 210000),
    ("up to $175k", 175000, 175000),
    ("$150,000/yr", 150000, 150000),
])
def test_annual_figures_pass_through(text, lo, hi):
    p = parse_pay(text)
    assert (p.salary_min, p.salary_max) == (lo, hi) and p.hourly is None


@pytest.mark.parametrize("text", [
    "", "No rate mentioned", "Call me at 555-0100", "Client has 25,000 employees",
    "$5/hr", "$2000/hr", "$12k", "Opening #4471", "Ref $401k match", "$403K plan",
])
def test_no_pay_or_implausible_pay_is_none(text):
    assert parse_pay(text) is None


def test_first_figure_wins_and_a_range_beats_its_own_endpoint():
    assert parse_pay("Rate $70-80/hr. Max budget $95/hr.").salary_max == 80 * 2080


def test_header_shows_the_rate_and_the_annual_figure():
    assert parse_pay("$70-80/hr").header() == "$70–80/hr (≈ $145,600–166,400/yr at 40 h/week)"
    assert parse_pay("$70/hr").header() == "$70/hr (≈ $145,600/yr at 40 h/week)"
    assert parse_pay("$180K-$200K").header() == "$180K-$200K"


@pytest.mark.parametrize("text,label", [
    ("Contract to Hire, 6 months", "Contract-to-hire"),
    ("12 month contract, C2C only", "Contract (C2C)"),
    ("W2 only", "Contract (W2)"),
    ("Long term contract", "Contract"),
    ("Full-Time / Permanent", "Full-time"),
    ("Fulltime Position", "Full-time"),
    ("nothing here", None),
])
def test_employment_type(text, label):
    assert employment_type(text) == label


@pytest.mark.parametrize("text,mode", [
    ("100% Remote", "Remote"),
    ("Hybrid - 3 days on-site/week", "Hybrid"),
    ("Onsite in Sunnyvale", "On-site"),
    ("Santa Clara, CA", None),
    # One signal each way, with no "hybrid" to reconcile them, is a contradiction. This used to
    # answer "On-site", which is how an agency footer's "on-site and off-site resources" overrode
    # a pitch headed "Locations: Remote". Saying nothing beats asserting the wrong one.
    ("Remote OK but on-site preferred", None),
    ("fully remote; candidates must attend on-site interviews", None),
    # "Hybrid" is an explicit mix, so the words it brings with it are not a contradiction.
    ("Hybrid: 3 days remote, 2 days on-site", "Hybrid"),
])
def test_work_mode(text, mode):
    assert work_mode(text) == mode


@pytest.mark.parametrize("text,mode", [
    ("Locations: Remote\nWe combine local on-site and off-site resources", "Remote"),
    ("Location : Onsite\nRemote work is not available", "On-site"),
    ("Work mode - Hybrid\n100% remote candidates will not be considered", "Hybrid"),
    ("Workplace: Remote\non-site", "Remote"),
    ("Job Location: Austin, TX\nWork Type: Remote\nsome on-site travel", "Remote"),
    # A labelled line that names no mode is skipped, not treated as an answer.
    ("Location: Austin, TX\n100% Remote", "Remote"),
    # Not a labelled field: the label must be followed by the colon.
    ("Mode of Hiring : Full-time roles\nRemote", "Remote"),
])
def test_work_mode_labelled_field_wins(text, mode):
    """A labelled field is the pitch's own statement; free text elsewhere is incidental."""
    assert work_mode(text) == mode
