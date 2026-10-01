"""Deterministic verification of the model's picks (V2 stage 3)."""

from agent.extract import extract_links
from agent.schemas import LinkPicks, PickedPosting
from agent.verify import verify_picks
from samples import LINKEDIN_HTML, MONSTER_HTML


def _cands(html=LINKEDIN_HTML):
    return extract_links(html)


def _id(cands, text):
    return next(c.id for c in cands if c.text == text)


def _picks(*triples):
    return LinkPicks(postings=[PickedPosting(link_id=i, title=t, company=c) for i, t, c in triples])


def test_good_picks_verify_and_take_url_from_the_email():
    c = _cands()
    fde, arch = _id(c, "Forward Deployed Engineer"), _id(c, "Senior Solutions Architect")
    v = verify_picks(_picks((fde, "Forward Deployed Engineer", "Acme Robotics"),
                            (arch, "senior solutions architect", "Globex Corporation")), c)
    assert v.ok
    assert [p.company for p in v.postings] == ["Acme Robotics", "Globex Corporation"]
    assert v.postings[0].url == next(x.url for x in c if x.id == fde)


def test_unknown_link_number_is_flagged_without_echoing_it():
    v = verify_picks(_picks((9999, "Forward Deployed Engineer", "Acme Robotics")), _cands())
    assert not v.ok and "not in the list" in v.issues[0]
    assert "9999" not in " ".join(v.issues)


def test_invented_title_and_company_are_flagged_without_echoing_them():
    c = _cands()
    fde = _id(c, "Forward Deployed Engineer")
    v = verify_picks(_picks((fde, "Chief Hallucination Officer", "Fabricated Inc")), c)
    text = " ".join(v.issues)
    assert len(v.issues) == 2 and f"[{fde}]" in text
    assert "Hallucination" not in text and "Fabricated" not in text
    assert v.postings == []


def test_company_must_not_just_be_the_title():
    c = _cands()
    fde = _id(c, "Forward Deployed Engineer")
    v = verify_picks(_picks((fde, "Forward Deployed Engineer", "Forward Deployed Engineer")), c)
    assert not v.ok and "employer" in v.issues[0]


def test_company_from_a_different_posting_is_rejected():
    c = _cands()
    fde = _id(c, "Forward Deployed Engineer")
    # "Globex" appears in the email, but not near this link
    v = verify_picks(_picks((fde, "Forward Deployed Engineer", "Globex Corporation")), c)
    assert not v.ok


def test_duplicates_collapse_silently_without_a_retry():
    c = _cands(MONSTER_HTML)
    title = _id(c, "Forward Deployed Engineer")
    view = [x.id for x in c if x.text == "VIEW JOB"][0]
    v = verify_picks(_picks((title, "Forward Deployed Engineer", "Initech"),
                            (title, "Forward Deployed Engineer", "Initech"),
                            (view, "Forward Deployed Engineer", "Initech")), c)
    assert v.ok and len(v.postings) == 1 and v.postings[0].link_id == title


def test_empty_picks_are_valid():
    v = verify_picks(LinkPicks(), _cands())
    assert v.ok and v.postings == []


def test_truncation_is_not_an_error():
    html = "".join(f'<p><a href="https://a.example/{i}">Engineer {i}</a> Co{i} Inc</p>' for i in range(40))
    c = extract_links(html)
    v = verify_picks(_picks(*[(x.id, x.text, f"Co{i} Inc") for i, x in enumerate(c)]), c,
                     max_postings=30)
    assert v.ok and v.truncated and len(v.postings) == 30
