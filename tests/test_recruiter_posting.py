"""The role a recruiter is pitching becomes ONE posting, from the same Jev call as the card.
Synthetic data only."""

from agent.dedup import dedup_key
from agent.jev import JevError
from agent.recruiter import build_card, build_card_and_posting, head_fragments, head_lines, pick_link
from agent.sort_stage import SortConfig, SortStage
from agent.writer import FakeWriter

SUBJECT = "Forward Deployed Engineer | Mountain View, CA / NYC | On-site"
BODY = """Hi Duane,

We have an urgent opening with our client for the role below.
Client: Acme Robotics
Location: Mountain View, CA
Apply here: https://www.dice.com/jobs/detail/abc123
My profile: https://www.linkedin.com/in/sonali-example

Thanks,
Sonali Example
Technical Recruiter | Nityo Infotech Corp.
Phone: +1 (555) 010-9988 | sonali@nityo.example

On Mon, Sep 1, 2026 at 9:00 AM Someone <s@x.com> wrote:
> older quoted text with Other Title | Other Corp
"""
SENDER = "Sonali Example <sonali@nityo.example>"


class FakeJev:
    """Picks by fragment TEXT, so tests don't depend on fragment numbering."""

    def __init__(self, picks=None, p=0.9, fail=False):
        self.picks, self.p, self.fail, self.calls = picks or {}, p, fail, []
        self.run_cost = 0.0

    def ask(self, state, questions):
        self.calls.append((state, questions))
        if self.fail:
            raise JevError("down")
        out = {}
        for name, q in questions.items():
            if q["type"] == "noul":
                out[name] = {"type": "noul", "noul": self.picks.get(name, 0.9)}
                continue
            if name == "category":
                cat = self.picks["category"]
                out[name] = {"type": "choice", "choice": cat, "confidence": 0.97,
                             "probabilities": {cat: 0.97, "other": 0.03}}
                continue
            want = self.picks.get(name, "none")
            key = "none" if want == "none" else next(k for k, v in q["criteria"].items() if v == want)
            out[name] = {"type": "choice", "choice": key, "confidence": self.p, "probabilities": {key: self.p}}
        return out

    def ask_choice(self, state, name, question):
        from agent.jev import ChoiceAnswer
        a = self.ask(state, {name: question})[name]
        return ChoiceAnswer(a["choice"], a["confidence"], a["probabilities"])


def _email(sender=SENDER, subject=SUBJECT, body=BODY, mid="<r1>"):
    return {"message_id": mid, "sender": sender, "subject": subject, "body_text": body,
            "received_at": "2026-10-22T10:00:00+00:00"}


PICKS = {"job_title": "Forward Deployed Engineer", "hiring_company": "Acme Robotics",
         "title": "Technical Recruiter", "employer": "Nityo Infotech Corp."}


# ── fragments and links ───────────────────────────────────────

def test_head_fragments_split_the_subject_and_the_first_lines():
    frags = head_fragments(SUBJECT, head_lines(BODY), "Sonali Example")
    assert frags[:4] == ["Forward Deployed Engineer", "Mountain View, CA", "NYC", "On-site"]
    assert "Acme Robotics" in frags and "Client" in frags
    assert not any("http" in f or "@" in f for f in frags)
    assert not any("Other Corp" in f for f in frags)          # quoted history excluded


def test_pick_link_prefers_a_job_board_link_over_the_recruiters_profile():
    assert pick_link(_email()) == "https://www.dice.com/jobs/detail/abc123"
    body = "Please apply here: https://careers.example.com/apply/42\nMe: https://www.linkedin.com/in/x"
    assert pick_link(_email(body=body)) == "https://careers.example.com/apply/42"
    assert pick_link(_email(body="No links in this one.\nhttps://www.linkedin.com/in/only-me")) is None


# ── one call, card + posting ──────────────────────────────────

def test_card_and_posting_come_from_one_jev_call():
    jev = FakeJev(PICKS)
    card, posting = build_card_and_posting(jev, _email())
    assert len(jev.calls) == 1
    state, questions = jev.calls[0]
    assert {"is_agency", "title", "employer", "job_title", "hiring_company"} <= set(questions)
    assert state["body_head"].startswith("Hi Duane")
    assert card["name"] == "Sonali Example" and card["employer"] == "Nityo Infotech Corp."
    assert card["is_agency"] is True
    assert {k: posting[k] for k in ("company", "role", "link", "action_required")} == {
        "company": "Acme Robotics", "role": "Forward Deployed Engineer",
        "link": "https://www.dice.com/jobs/detail/abc123", "action_required": False}


def test_unnamed_client_falls_back_to_the_agency_marked_as_such():
    _, posting = build_card_and_posting(FakeJev({**PICKS, "hiring_company": "none"}), _email())
    assert posting["company"] == "Nityo Infotech Corp. (agency)"
    assert posting["role"] == "Forward Deployed Engineer"


def test_in_house_recruiter_without_a_named_client_is_the_employer_itself():
    jev = FakeJev({**PICKS, "hiring_company": "none", "employer": "Acme Robotics", "is_agency": 0.1,
                   "title": "Talent Partner"})
    body = BODY.replace("Technical Recruiter | Nityo Infotech Corp.", "Talent Partner | Acme Robotics")
    _, posting = build_card_and_posting(jev, _email(body=body))
    assert posting["company"] == "Acme Robotics"


def test_no_signal_at_all_is_an_unknown_company_not_a_guess():
    jev = FakeJev({"job_title": "Forward Deployed Engineer"})
    _, posting = build_card_and_posting(jev, _email(sender="Pat <pat@gmail.com>", body="Interested?\n"))
    assert posting["company"] == "Unknown company"


def test_no_title_means_no_posting_but_still_a_card():
    card, posting = build_card_and_posting(FakeJev({**PICKS, "job_title": "none"}), _email())
    assert posting is None and card["name"] == "Sonali Example"


def test_a_low_confidence_title_is_not_a_posting():
    _, posting = build_card_and_posting(FakeJev(PICKS, p=0.4), _email())
    assert posting is None


def test_company_equal_to_the_title_is_not_a_company():
    _, posting = build_card_and_posting(FakeJev({**PICKS, "hiring_company": "Forward Deployed Engineer"}), _email())
    assert posting["company"] == "Nityo Infotech Corp. (agency)"


def test_jev_down_keeps_the_header_card_and_no_posting():
    card, posting = build_card_and_posting(FakeJev(fail=True), _email())
    assert card["name"] == "Sonali Example" and "phone" in card and posting is None


def test_build_card_is_unchanged_for_callers():
    assert build_card(FakeJev(PICKS), _email())["employer"] == "Nityo Infotech Corp."


# ── through the sort stage ────────────────────────────────────

class Reader:
    def __init__(self, emails):
        self.emails, self.moves = emails, []

    def get_unread(self):
        return list(self.emails)

    def move_and_mark(self, message_id, destination, mark_read=True):
        self.moves.append((message_id, destination, mark_read))


class MemDedup:
    def __init__(self):
        self.keys = set()

    def seen(self, key):
        return key in self.keys

    def add(self, keys):
        self.keys.update(keys)


def _stage(jev, dedup=None):
    return SortStage(Reader([_email()]), jev, SortConfig(), dedup=dedup)


def test_recruiter_email_is_written_with_its_posting():
    writer = FakeWriter()
    res = _stage(FakeJev({**PICKS, "category": "recruiter_outreach"})).run(writer)
    (entry,) = writer.inbox_entries
    assert entry["category"] == "recruiter_outreach"
    (p,) = entry["postings"]
    assert p["company"] == "Acme Robotics" and p["role"] == "Forward Deployed Engineer"
    assert p["possible_duplicate"] is False and p["matched_review_id"] is None
    assert p["dedup_key"] == dedup_key(p["link"], p["company"], p["role"])
    assert entry["raw_extracted_json"]["recruiter_posting"] == {"role": "Forward Deployed Engineer",
                                                                 "company": "Acme Robotics"}
    assert entry["raw_extracted_json"]["recruiter_contact"]["name"] == "Sonali Example"
    assert res.postings == 1 and res.duplicates_skipped == 0
    assert res.details[0]["posting"] == "Forward Deployed Engineer"


def test_a_pitch_already_tracked_is_flagged_as_a_possible_duplicate():
    writer = FakeWriter(reviews=[{"review_id": "r-1", "company": "Acme Robotics",
                                  "title": "Forward Deployed Engineer"}])
    _stage(FakeJev({**PICKS, "category": "recruiter_outreach"})).run(writer)
    (p,) = writer.inbox_entries[0]["postings"]
    assert p["possible_duplicate"] is True and p["matched_review_id"] == "r-1"


def test_a_re_sent_pitch_is_written_once():
    dedup = MemDedup()
    jev = FakeJev({**PICKS, "category": "recruiter_outreach"})
    w1, w2 = FakeWriter(), FakeWriter()
    r1 = _stage(jev, dedup).run(w1)
    r2 = _stage(jev, dedup).run(w2)
    assert len(w1.inbox_entries[0]["postings"]) == 1 and r1.postings == 1
    assert w2.inbox_entries[0]["postings"] == [] and r2.duplicates_skipped == 1
    assert "recruiter_contact" in w2.inbox_entries[0]["raw_extracted_json"]   # the card still goes


def test_dry_run_neither_writes_nor_remembers():
    dedup = MemDedup()
    writer = FakeWriter()
    res = _stage(FakeJev({**PICKS, "category": "recruiter_outreach"}), dedup).run(writer, dry_run=True)
    assert writer.inbox_entries == [] and dedup.keys == set() and res.postings == 1


def test_no_pitch_means_the_old_shape():
    writer = FakeWriter()
    _stage(FakeJev({"category": "recruiter_outreach"})).run(writer)
    entry = writer.inbox_entries[0]
    assert entry["postings"] == [] and "recruiter_posting" not in entry["raw_extracted_json"]


# ── job details (location, remote, pay, description) ─────────

PITCH = """Hi Duane,

Role: Sr. Agentic AI Engineer
Location: Santa Clara, CA (3 days on-site/week)
Duration: 12 months contract, C2C
Rate: $70-80/hr

Responsibilities:
- Build LangGraph agents
- Own the evals

Thanks,
Amrita Example
Technical Recruiter | Example Staffing Inc

On Mon, Oct 20, 2026 at 9:00 AM Someone <s@x.com> wrote:
> earlier thread, Rate: $40/hr
"""


def _pitch_email():
    return _email(subject="Urgent Requirement : Sr. Agentic AI Engineer--Santa Clara, CA--Contract",
                  body=PITCH, sender="Amrita Example <amrita@examplestaffing.com>")


def test_details_come_with_the_posting():
    jev = FakeJev({"job_title": "Sr. Agentic AI Engineer", "job_location": "Santa Clara, CA",
                   "hiring_company": "none", "employer": "Example Staffing Inc", "title": "Technical Recruiter"})
    _, p = build_card_and_posting(jev, _pitch_email())
    assert p["location"] == "Santa Clara, CA"
    assert p["remote"] is False
    assert (p["salary_min"], p["salary_max"]) == (70 * 2080, 80 * 2080)
    head, _, rest = p["description"].partition("\n\n")
    assert head == "Contract (C2C) · On-site · $70–80/hr (≈ $145,600–166,400/yr at 40 h/week)"
    assert rest.startswith("Hi Duane") and "Build LangGraph agents" in rest
    assert "earlier thread" not in p["description"] and "$40/hr" not in p["description"]
    assert p["company"] == "Example Staffing Inc (agency)"


def test_jev_sees_the_location_question():
    jev = FakeJev({"job_title": "Sr. Agentic AI Engineer"})
    build_card_and_posting(jev, _pitch_email())
    assert "job_location" in jev.calls[0][1]


def test_remote_role_has_no_city_and_remote_true():
    jev = FakeJev({"job_title": "Forward Deployed Engineer", "job_location": "none"})
    _, p = build_card_and_posting(jev, _email(subject="Forward Deployed Engineer | 100% Remote", body="Hi\nFully remote role.\n"))
    assert p["remote"] is True and "location" not in p
    assert p["description"].startswith("Remote\n\n")


def test_no_pay_no_salary_fields():
    jev = FakeJev({"job_title": "Forward Deployed Engineer"})
    _, p = build_card_and_posting(jev, _email())
    assert "salary_min" not in p and "salary_max" not in p


def test_description_is_capped():
    from agent.recruiter import DESCRIPTION_CAP
    jev = FakeJev({"job_title": "Forward Deployed Engineer"})
    _, p = build_card_and_posting(jev, _email(body="Forward Deployed Engineer\n" + "x" * 50_000))
    assert len(p["description"]) <= DESCRIPTION_CAP
