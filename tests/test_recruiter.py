"""Recruiter card: every value is copied from the email; Jev only picks fragments. Synthetic data."""

from agent.jev import JevError
from agent.recruiter import build_card, clean_name, fragments, signature_lines

BODY = """Hi Duane,

We have a Data Architect role (Remote) with our client. Please share your resume.

Thanks & Regards,
Pat Example
Senior Technical Recruiter | Example Staffing Inc
Phone: +1 (555) 010-2233 | pat@examplestaffing.com
https://www.linkedin.com/in/pat-example-123

On Mon, Sep 1, 2026 at 9:00 AM Someone <s@x.com> wrote:
> older quoted text with Other Title | Other Corp
"""


class FakeJev:
    def __init__(self, answers=None, fail=False):
        self.answers, self.fail, self.calls = answers or {}, fail, []

    def ask(self, state, questions):
        self.calls.append((state, questions))
        if self.fail:
            raise JevError("down")
        return self.answers


def _choice(key, p=0.9):
    return {"type": "choice", "choice": key, "probabilities": {key: p}, "confidence": p}


def _email(sender="Pat Example <pat@examplestaffing.com>", body=BODY):
    return {"sender": sender, "subject": "Data Architect", "body_text": body}


def test_clean_name_strips_relay_and_id_decorations():
    assert clean_name('"Nukul (via LinkedIn)" <messages-noreply@linkedin.com>') == "Nukul"
    assert clean_name('"Harish k (TSI2208)" <harish@x.com>') == "Harish k"
    assert clean_name('"Niharika Ruhela (#TSI2946)" <n@x.com>') == "Niharika Ruhela"
    assert clean_name('"Dice Recruiter" <a@user.dice.com>') is None
    assert clean_name("bare@x.com") is None


def test_signature_excludes_quoted_history():
    sig = signature_lines(BODY)
    assert sig[-1].startswith("https://www.linkedin.com/in/")
    assert not any("Other Corp" in ln for ln in sig)


def test_fragments_drop_urls_emails_phones_and_name():
    frags = fragments(signature_lines(BODY), "Pat Example")
    assert "Senior Technical Recruiter" in frags and "Example Staffing Inc" in frags
    assert "Pat Example" not in frags
    assert not any("@" in f or "linkedin" in f or "555" in f for f in frags)


def test_card_copies_values_and_uses_jev_picks():
    frags = fragments(signature_lines(BODY), "Pat Example")
    t, e = frags.index("Senior Technical Recruiter") + 1, frags.index("Example Staffing Inc") + 1
    jev = FakeJev({"title": _choice(f"f{t}"), "employer": _choice(f"f{e}"),
                   "is_agency": {"type": "noul", "noul": 0.92}})
    card = build_card(jev, _email())
    assert card == {
        "name": "Pat Example", "email": "pat@examplestaffing.com", "phone": "+1 (555) 010-2233",
        "linkedin_url": "https://www.linkedin.com/in/pat-example-123",
        "title": "Senior Technical Recruiter", "employer": "Example Staffing Inc", "is_agency": True,
    }
    # Jev sees only header + the signature region (newest message's last lines), no quoted history
    state = jev.calls[0][0]
    assert set(state) == {"from", "subject", "signature"} and "Other Corp" not in state["signature"]


def test_low_probability_none_and_out_of_range_picks_are_omitted():
    jev = FakeJev({"title": _choice("f1", 0.4), "employer": _choice("f99"),
                   "is_agency": {"type": "noul", "noul": 0.5}})
    card = build_card(jev, _email())
    assert "title" not in card and "employer" not in card and "is_agency" not in card


def test_relay_sender_address_is_not_the_recruiters():
    jev = FakeJev({"is_agency": {"type": "noul", "noul": 0.1}})
    card = build_card(jev, _email(sender='"Kyle Stock" <inmail-hit-reply@linkedin.com>',
                                  body="Hi,\nCloud role?\nKyle Stock\nAcme Corp"))
    assert card["name"] == "Kyle Stock" and "email" not in card and card["is_agency"] is False


def test_jev_failure_still_returns_header_fields():
    card = build_card(FakeJev(fail=True), _email())
    assert card["name"] == "Pat Example" and card["email"] == "pat@examplestaffing.com"
    assert "title" not in card


def test_no_name_no_card():
    assert build_card(FakeJev(), _email(sender="noreply@example.com")) is None


def test_values_are_capped_and_stripped_of_angle_brackets():
    long = "X" * 500
    card = build_card(FakeJev(), _email(sender=f'"<b>{long}</b>" <a@b.com>'))
    assert len(card["name"]) == 200 and "<" not in card["name"]


def test_name_drops_company_suffix_matching_the_sender_domain():
    assert clean_name('"Pavan - PRIMUS" <pavank@primusglobal.com>') == "Pavan"
    assert clean_name('"Mary-Jo Smith - Recruiter" <mj@acme.com>') == "Mary-Jo Smith - Recruiter"


def test_html_entities_and_parenthesised_area_codes():
    body = "Hi\nNoor Mohammad\nSenior IT Recruiter&nbsp;| Kaash Tech\n(551) 248-5087"
    frags = fragments(signature_lines(body), "Noor Mohammad")
    assert "Senior IT Recruiter" in frags
    card = build_card(FakeJev(), _email(sender="Noor Mohammad <noor@kaash.com>", body=body))
    assert card["phone"] == "(551) 248-5087"


def test_pick_that_repeats_the_subject_is_the_role_not_the_sender():
    body = "Hi\nSarah\nGTM Recruiter\nSenior SA, AWS Industries"
    frags = fragments(signature_lines(body), "Sarah")
    jev = FakeJev({"title": _choice(f"f{frags.index('GTM Recruiter') + 1}"),
                   "employer": _choice(f"f{frags.index('Senior SA, AWS Industries') + 1}")})
    card = build_card(jev, {"sender": "Sarah <s@amazon.com>", "body_text": body,
                            "subject": "Solve the hardest cloud problems | Senior SA, AWS Industries"})
    assert card["title"] == "GTM Recruiter" and "employer" not in card
