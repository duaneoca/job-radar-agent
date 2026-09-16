"""
End-to-end V2 pipeline (graph) with fakes: screen → pick → verify → write/retry/escalate → finalize.
"""

import pytest

from agent.dedup import SqliteDedupStore
from agent.extract import extract_links
from agent.graph import build_graph
from agent.llm import FakeLLM, LLMParseError
from agent.nodes import Nodes
from agent.prompts import SeedPromptProvider
from agent.reader import FakeReader
from agent.schemas import LinkPicks, PickedPosting
from agent.writer import FakeWriter
from samples import (GLASSDOOR_HTML, LENNYS_HTML, LINKEDIN_HTML, MONSTER_HTML, NEWSLETTER_HTML,
                     SENDERS, make_email)

PROMPTS = SeedPromptProvider()


def _ids(html):
    return {c.text: c.id for c in extract_links(html)}


def _picks(*triples):
    return LinkPicks(postings=[PickedPosting(link_id=i, title=t, company=c) for i, t, c in triples])


def _run(email, responses, *, reviews=None, dedup=None, zero="mark_read", writer=None):
    llm = FakeLLM(responses)
    writer = writer or FakeWriter(reviews=reviews)
    reader = FakeReader()
    nodes = Nodes(llm=llm, writer=writer, reader=reader, prompts=PROMPTS, dedup=dedup,
                  zero_postings_action=zero)
    final = build_graph(nodes).invoke({"email": email})
    return final, llm, writer, reader


def _linkedin_good():
    ids = _ids(LINKEDIN_HTML)
    return _picks((ids["Forward Deployed Engineer"], "Forward Deployed Engineer", "Acme Robotics"),
                  (ids["Senior Solutions Architect"], "Senior Solutions Architect", "Globex Corporation"))


def test_happy_path_one_llm_call_writes_and_marks_read_in_place():
    final, llm, writer, reader = _run(make_email(LINKEDIN_HTML), [_linkedin_good()])
    assert len(llm.calls) == 1
    assert final["outcome"] == "processed" and final["attempts"] == 1
    (entry,) = writer.inbox_entries
    assert entry["category"] == "job_alert" and entry["message_id"] == "<m1@x>"
    assert [p["company"] for p in entry["postings"]] == ["Acme Robotics", "Globex Corporation"]
    assert entry["postings"][0]["link"].startswith("https://www.linkedin.com/comm/jobs/view/4457198691")
    assert entry["postings"][0]["dedup_key"] == "linkedin:4457198691"
    assert reader.marked_read == ["<m1@x>"] and reader.moves == []


def test_model_sees_links_not_the_raw_email():
    _, llm, _, _ = _run(make_email(LINKEDIN_HTML), [_linkedin_good()])
    user = llm.calls[0]["user"]
    assert "<links>" in user and "[1]" in user
    assert "<table" not in user and "otpToken" not in user and "SECRET" not in user


def test_retry_sends_only_corrections_never_the_wrong_values():
    ids = _ids(LINKEDIN_HTML)
    bad = _picks((ids["Forward Deployed Engineer"], "Forward Deployed Engineer", "Hallucinated LLC"))
    final, llm, writer, _ = _run(make_email(LINKEDIN_HTML), [bad, _linkedin_good()])
    assert final["outcome"] == "processed" and final["attempts"] == 2 and len(llm.calls) == 2
    retry_prompt = llm.calls[1]["user"]
    assert f"link [{ids['Forward Deployed Engineer']}]" in retry_prompt
    assert "Hallucinated" not in retry_prompt
    assert len(writer.inbox_entries) == 1


def test_three_failed_attempts_go_to_unprocessed_without_writing():
    ids = _ids(LINKEDIN_HTML)
    bad = _picks((ids["Forward Deployed Engineer"], "Made Up Title", "Acme Robotics"))
    final, llm, writer, reader = _run(make_email(LINKEDIN_HTML), [bad, bad, bad])
    assert len(llm.calls) == 3
    assert final["outcome"] == "needs_review" and "3 attempts" in final["reason"]
    assert writer.inbox_entries == []
    assert reader.moves == [("<m1@x>", "unprocessed")] and reader.marked_read == []


def test_partial_success_still_goes_to_unprocessed():
    ids = _ids(LINKEDIN_HTML)
    half = _picks((ids["Forward Deployed Engineer"], "Forward Deployed Engineer", "Acme Robotics"),
                  (ids["Senior Solutions Architect"], "Senior Solutions Architect", "Nope Inc"))
    final, _, writer, reader = _run(make_email(LINKEDIN_HTML), [half, half, half])
    assert final["outcome"] == "needs_review" and writer.inbox_entries == []
    assert reader.moves == [("<m1@x>", "unprocessed")]


def test_unparseable_reply_is_retried():
    def garbage(*_):
        raise LLMParseError("not json")
    final, llm, _, _ = _run(make_email(LINKEDIN_HTML), [garbage, _linkedin_good()])
    assert final["outcome"] == "processed" and len(llm.calls) == 2
    assert "JSON" in llm.calls[1]["user"]


def test_llm_infrastructure_errors_propagate_so_the_email_is_left_alone():
    class RateLimitError(Exception):
        pass

    def limited(*_):
        raise RateLimitError("429")
    with pytest.raises(RateLimitError):
        _run(make_email(LINKEDIN_HTML), [limited])


def test_rejected_sender_costs_no_llm_call():
    email = make_email(LINKEDIN_HTML, sender="Phish <jobs@linkedin-careers.io>")
    final, llm, writer, reader = _run(email, [])           # FakeLLM would fail if called
    assert llm.calls == [] and writer.inbox_entries == []
    assert final["outcome"] == "needs_review" and "allow-list" in final["reason"]
    assert reader.moves == [("<m1@x>", "unprocessed")]


def test_spoofed_sender_without_dmarc_costs_no_llm_call():
    final, llm, _, reader = _run(make_email(LINKEDIN_HTML, auth=[]), [])
    assert llm.calls == [] and reader.moves == [("<m1@x>", "unprocessed")]


def test_email_with_no_links_skips_the_llm():
    email = make_email(text="Thanks for being a member!", sender=SENDERS["builtin"])
    final, llm, writer, reader = _run(email, [])
    assert llm.calls == [] and final["outcome"] == "no_postings"
    assert reader.marked_read == ["<m1@x>"] and writer.inbox_entries == []


def test_newsletter_with_no_postings_follows_the_zero_postings_setting():
    email = make_email(NEWSLETTER_HTML, sender=SENDERS["builtin"])
    final, _, writer, reader = _run(email, [LinkPicks()])
    assert final["outcome"] == "no_postings" and reader.marked_read == ["<m1@x>"]
    final, _, writer, reader = _run(email, [LinkPicks()], zero="unprocessed")
    assert reader.moves == [("<m1@x>", "unprocessed")] and writer.inbox_entries == []


def test_duplicates_are_skipped_on_a_later_email(tmp_path):
    store = SqliteDedupStore(tmp_path / "d.sqlite")
    _run(make_email(LINKEDIN_HTML, message_id="<a@x>"), [_linkedin_good()], dedup=store)
    final, _, writer, reader = _run(make_email(LINKEDIN_HTML, message_id="<b@x>"),
                                    [_linkedin_good()], dedup=store)
    assert final["outcome"] == "duplicates_only" and final["duplicates_skipped"] == 2
    assert writer.inbox_entries == [] and reader.marked_read == ["<b@x>"]


def test_failed_write_does_not_record_duplicates(tmp_path):
    store = SqliteDedupStore(tmp_path / "d.sqlite")

    class Down(FakeWriter):
        def create_inbox_entry(self, payload):
            raise RuntimeError("503")
    with pytest.raises(RuntimeError):
        _run(make_email(LINKEDIN_HTML), [_linkedin_good()], dedup=store, writer=Down())
    final, _, writer, _ = _run(make_email(LINKEDIN_HTML), [_linkedin_good()], dedup=store)
    assert final["postings_written"] == 2


def test_tracked_job_is_flagged_possible_duplicate():
    reviews = [{"review_id": "r1", "company": "Acme Robotics", "title": "Forward Deployed Engineer"}]
    _, _, writer, _ = _run(make_email(LINKEDIN_HTML), [_linkedin_good()], reviews=reviews)
    first, second = writer.inbox_entries[0]["postings"]
    assert first["possible_duplicate"] and first["matched_review_id"] == "r1"
    assert not second["possible_duplicate"]


@pytest.mark.parametrize("html,sender,picks", [
    (GLASSDOOR_HTML, "glassdoor", [("Hex", "AI Research Engineer", "Hex")]),
    (MONSTER_HTML, "monster", [("Forward Deployed Engineer", "Forward Deployed Engineer", "Initech")]),
    (LENNYS_HTML, "lennys", [("Sr Manager, Applied Field Engineering",
                              "Sr Manager, Applied Field Engineering", "Snowflake")]),
])
def test_each_sender_layout_verifies(html, sender, picks):
    cands = extract_links(html)
    triples = [(next(c.id for c in cands if key in c.text), t, co) for key, t, co in picks]
    final, _, writer, _ = _run(make_email(html, sender=SENDERS[sender]), [_picks(*triples)])
    assert final["outcome"] == "processed", final.get("reason")
    assert writer.inbox_entries[0]["postings"][0]["company"] == picks[0][2]


def test_missing_date_still_sends_a_received_at():
    email = make_email(LINKEDIN_HTML)
    email["received_at"] = None
    _, _, writer, _ = _run(email, [_linkedin_good()])
    assert writer.inbox_entries[0]["received_at"]
