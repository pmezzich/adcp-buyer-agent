"""Three ways a transport can hand back an answer that is not the answer.

None of these produce an error. Each returns a well-formed payload that belongs to a
different request, a different auth posture, or a different artifact -- which is why they
survived until something drove `.call()` and looked at what came back.
"""

from __future__ import annotations

import json

import httpx

from adcp_buyer.core.transports.a2a import A2aTransport, _extract_data_part
from adcp_buyer.core.transports.mcp import McpTransport, _parse_sse


def _mcp(handler):
    t = McpTransport("http://seller", "tok", "ten")
    t._client = httpx.Client(transport=httpx.MockTransport(handler))
    return t


def _a2a(handler):
    t = A2aTransport("http://seller", "tok", "ten")
    t._client = httpx.Client(transport=httpx.MockTransport(handler))
    return t


# ---------------------------------------------------------------------------
# MCP: the answer must belong to THIS request
# ---------------------------------------------------------------------------


def test_parse_sse_picks_the_frame_answering_our_id():
    body = (
        'data: {"jsonrpc":"2.0","id":99,"result":{"structuredContent":{"WRONG":1}}}\n'
        'data: {"jsonrpc":"2.0","id":2,"result":{"structuredContent":{"RIGHT":1}}}\n'
    )
    assert _parse_sse(body, want_id=2)["result"]["structuredContent"] == {"RIGHT": 1}


def test_parse_sse_returns_nothing_when_no_frame_answers_us():
    body = 'data: {"jsonrpc":"2.0","id":99,"result":{"structuredContent":{"WRONG":1}}}\n'
    assert _parse_sse(body, want_id=2) == {}


def test_a_reply_carrying_someone_elses_id_is_not_read_as_our_payload():
    """A stream carries progress frames and, on a reused connection, other requests' answers.
    Taking the first id-bearing frame attributed another call's media_buy_id to this one."""

    def handler(request):
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": 4242,
                "result": {"structuredContent": {"media_buy_id": "SOMEONE_ELSES"}},
            },
        )

    t = _mcp(handler)
    t._session, t._session_authed = "s", True
    ex = t.call("create_media_buy", {})
    assert ex.is_success is False
    assert ex.wire_response is None
    assert "id" in (ex.unclassified_reason or "")


# ---------------------------------------------------------------------------
# MCP: a session must not carry across auth postures
# ---------------------------------------------------------------------------


def test_an_unauthenticated_call_does_not_ride_an_authenticated_session():
    """The server may authorize on the identity established at initialize, so reusing the
    session would make `authed=False` a false pass for an auth-boundary probe."""
    seen: list[tuple[str, str | None, str | None]] = []

    def handler(request: httpx.Request):
        method = json.loads(request.content).get("method") if request.content else None
        seen.append(
            (
                method or "",
                request.headers.get("x-adcp-auth"),
                request.headers.get("mcp-session-id"),
            )
        )
        rpc_id = json.loads(request.content).get("id") if request.content else None
        if method == "initialize":
            auth = request.headers.get("x-adcp-auth")
            return httpx.Response(
                200,
                headers={"mcp-session-id": "sess-AUTHED" if auth else "sess-ANON"},
                json={"jsonrpc": "2.0", "id": rpc_id, "result": {}},
            )
        return httpx.Response(
            200, json={"jsonrpc": "2.0", "id": rpc_id, "result": {"structuredContent": {"ok": 1}}}
        )

    t = _mcp(handler)
    t.call("get_products", {}, authed=True)
    t.call("create_media_buy", {}, authed=False)

    tool_calls = [row for row in seen if row[0] == "tools/call"]
    unauthed = [row for row in tool_calls if row[1] is None]
    assert unauthed, "the unauthenticated call was never made"
    assert unauthed[0][2] != "sess-AUTHED", "unauthenticated call rode the authenticated session"


def test_a_4xx_invalidates_the_cached_session():
    """Otherwise a dead mcp-session-id is replayed forever and every later call is
    indeterminate for a reason that a fresh initialize would have cleared."""
    t = _mcp(lambda r: httpx.Response(400, text="Missing session ID"))
    t._session, t._session_authed = "stale", True
    t.call("get_products", {})
    assert t._session is None


# ---------------------------------------------------------------------------
# A2A: which artifact is authoritative
# ---------------------------------------------------------------------------


def _task(artifacts, state=None):
    task: dict = {"artifacts": artifacts}
    if state:
        task["status"] = {"state": state}
    return {"jsonrpc": "2.0", "id": "1", "result": task}


def test_an_error_artifact_wins_over_an_earlier_success_artifact():
    """The seller appends one artifact per skill result in invocation order, so a task can
    carry a success payload AND an error envelope. Returning the first made every error
    after a success unreachable -- a FAILED task reported success."""
    artifacts = [
        {"artifactId": "skill_result_1", "parts": [{"data": {"products": []}}]},
        {
            "artifactId": "skill_result_2",
            "parts": [{"data": {"adcp_error": {"code": "POLICY_VIOLATION"}, "errors": []}}],
        },
    ]
    assert _extract_data_part(_task(artifacts)["result"])["adcp_error"]["code"] == (
        "POLICY_VIOLATION"
    )

    ex = _a2a(lambda r: httpx.Response(200, json=_task(artifacts, state="failed"))).call(
        "create_media_buy", {}
    )
    assert ex.is_success is False
    assert ex.error_code == "POLICY_VIOLATION"
    assert ex.extra["artifact_count"] == 2
    assert ex.extra["task_state"] == "failed"


def test_a_single_success_artifact_is_still_the_payload():
    artifacts = [{"artifactId": "skill_result_1", "parts": [{"data": {"products": [1]}}]}]
    ex = _a2a(lambda r: httpx.Response(200, json=_task(artifacts, state="completed"))).call(
        "get_products", {}
    )
    assert ex.is_success is True
    assert ex.wire_response == {"products": [1]}
    assert ex.extra["artifact_count"] == 1
