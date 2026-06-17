"""McpReaderClient result-parsing unit tests (no subprocess) — covers structuredContent + TextContent."""

import json
from types import SimpleNamespace

from agent.reader_mcp import _parse_tool_result


def _text_result(obj):
    return SimpleNamespace(structuredContent=None,
                           content=[SimpleNamespace(text=json.dumps(obj))])


def test_parses_structured_list_under_result_key():
    # FastMCP wraps a list return as {"result": [...]}
    r = SimpleNamespace(structuredContent={"result": [{"message_id": "<1>"}]}, content=[])
    assert _parse_tool_result(r) == [{"message_id": "<1>"}]


def test_parses_structured_dict_directly():
    r = SimpleNamespace(structuredContent={"moved": "<1>", "to": "X", "marked_read": False},
                        content=[])
    assert _parse_tool_result(r)["marked_read"] is False


def test_falls_back_to_text_content_json():
    assert _parse_tool_result(_text_result([{"a": 1}])) == [{"a": 1}]


def test_returns_none_when_empty():
    assert _parse_tool_result(SimpleNamespace(structuredContent=None, content=[])) is None
