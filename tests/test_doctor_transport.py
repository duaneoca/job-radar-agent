"""EMAIL_READER_TRANSPORT guard in doctor.

bootstrap refuses to start the sorter or retention under the stdio transport
(agent/bootstrap.py). Doctor builds the provider directly, so it has to say so
itself or it reports green on a config where every run exits immediately.
"""

import pytest

from agent.doctor import transport_verdict


@pytest.mark.parametrize("transport", ["direct", "DIRECT", "", "Direct"])
def test_direct_transport_has_nothing_to_report(transport):
    assert transport_verdict(transport, sorter=True, retention=True) is None


def test_mcp_is_fine_for_the_link_picker_alone():
    passed, detail = transport_verdict("mcp", sorter=False, retention=False)
    assert passed is True
    assert "link picker only" in detail


@pytest.mark.parametrize(
    "sorter,retention,expected",
    [
        (True, False, "sorter"),
        (False, True, "retention"),
        (True, True, "sorter and retention"),
    ],
)
def test_mcp_fails_and_names_what_needs_direct(sorter, retention, expected):
    passed, detail = transport_verdict("mcp", sorter=sorter, retention=retention)
    assert passed is False
    assert expected in detail
    assert "EMAIL_READER_TRANSPORT=direct" in detail


def test_transport_match_is_case_insensitive():
    assert transport_verdict("MCP", sorter=True, retention=False)[0] is False
