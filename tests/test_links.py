"""clean_link tests — scheme allowlist (C2 defense-in-depth) + the one safe www. coercion."""

import pytest

from agent.links import clean_link


@pytest.mark.parametrize("raw,expected", [
    ("https://acme.example/jobs/123", "https://acme.example/jobs/123"),
    ("http://acme.example/j", "http://acme.example/j"),
    ("  https://acme.example/x  ", "https://acme.example/x"),     # trimmed
    ("<https://acme.example/x>", "https://acme.example/x"),       # angle brackets stripped
    ("www.acme.example/jobs/1", "https://www.acme.example/jobs/1"),  # safe coercion
    ("WWW.Acme.Example/j", "https://WWW.Acme.Example/j"),
])
def test_valid_and_coerced(raw, expected):
    assert clean_link(raw) == expected


@pytest.mark.parametrize("raw", [
    None, "", "   ",
    "javascript:alert(1)",                 # XSS scheme
    "data:text/html,<script>1</script>",   # data URI
    "file:///etc/passwd",
    "mailto:recruiter@acme.example",
    "/jobs/123",                           # relative
    "acme.example/jobs/1",                 # bare domain (not www.) → reject, don't guess
    "https://acme.example/x y",            # whitespace in URL → reject
    "ftp://acme.example/x",
])
def test_rejected(raw):
    assert clean_link(raw) is None
