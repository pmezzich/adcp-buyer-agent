"""Unit tests for the transport response parsers (no infra).

Live three-wire behavior is exercised by scripts/probe_smoke.py; here we pin the pure
parsing so a wire-shape change can't silently mis-read a response.
"""

from adcp_buyer.core.transports.a2a import _extract_data_part
from adcp_buyer.core.transports.mcp import _first_text_json, _parse_sse


def test_parse_sse_extracts_result_from_data_line():
    body = 'event: message\ndata: {"jsonrpc":"2.0","id":3,"result":{"structuredContent":{"ok":true}}}\n'
    assert _parse_sse(body)["result"]["structuredContent"] == {"ok": True}


def test_parse_sse_falls_back_to_plain_json():
    assert _parse_sse('{"result":{"x":1}}')["result"] == {"x": 1}


def test_parse_sse_empty_on_garbage():
    assert _parse_sse("not json at all") == {}


def test_first_text_json_parses_error_envelope():
    result = {"content": [{"type": "text", "text": '{"adcp_error":{"code":"X"}}'}]}
    assert _first_text_json(result)["adcp_error"]["code"] == "X"


def test_extract_data_part_finds_the_data_part():
    result = {
        "artifacts": [
            {"parts": [{"kind": "text", "text": "hi"}, {"kind": "data", "data": {"products": []}}]}
        ]
    }
    assert _extract_data_part(result) == {"products": []}


def test_extract_data_part_none_when_only_text():
    assert _extract_data_part({"artifacts": [{"parts": [{"kind": "text", "text": "hi"}]}]}) is None
