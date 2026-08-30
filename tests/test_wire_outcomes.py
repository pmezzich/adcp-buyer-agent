"""Drive .call() on all three transports and pin how each response shape is classified.

The audit these tests answer: nothing exercised `.call()` at all, so the Exchange-construction
code -- where every false success lived -- had no coverage. A transport that reports
``is_success`` for an HTTP 500, an HTML 502, an empty body, or an A2A Task with no artifact
makes a failed money call read as a completed one, and the durable executor then checkpoints
that lie under the plan's idempotency key.

Each case asserts the FULL trichotomy (success / error / indeterminate), because asserting
only ``not is_success`` would pass for a wrong reason.
"""

from __future__ import annotations

import json

import httpx
import pytest

from adcp_buyer.core.transports.a2a import A2aTransport
from adcp_buyer.core.transports.mcp import McpTransport
from adcp_buyer.core.transports.rest import RestTransport


def _wire(transport, handler):
    """Point a transport's httpx client at a stub responder."""
    transport._client = httpx.Client(transport=httpx.MockTransport(handler))
    return transport


def _rest(handler):
    return _wire(RestTransport("http://seller", "tok", "ten"), handler)


def _mcp(handler):
    t = _wire(McpTransport("http://seller", "tok", "ten"), handler)
    # Skip the initialize handshake; these cases are about tools/call. _session_authed must
    # match, or the transport (correctly) re-initializes for a different auth posture.
    t._session = "sess-1"
    t._session_authed = True
    return t


def _echo(build):
    """Wrap a stub so its JSON-RPC frame answers the id the transport actually minted.

    Hardcoding an id here would make every case depend on the transport's internal counter,
    and would quietly defeat the id-correlation the transport now does.
    """

    def handler(request):
        import json as _json

        rpc_id = _json.loads(request.content).get("id")
        return build(rpc_id)

    return handler


def _a2a(handler):
    return _wire(A2aTransport("http://seller", "tok", "ten"), handler)


def _rpc_ok(payload: dict):
    return _echo(
        lambda i: httpx.Response(
            200, json={"jsonrpc": "2.0", "id": i, "result": {"structuredContent": payload}}
        )
    )


# ---------------------------------------------------------------------------
# REST
# ---------------------------------------------------------------------------


def test_rest_2xx_payload_is_success():
    ex = _rest(lambda r: httpx.Response(200, json={"products": []})).call("get_products", {})
    assert (ex.is_success, ex.is_error, ex.is_indeterminate) == (True, False, False)


def test_rest_adcp_envelope_is_error_and_keeps_the_code():
    env = {"adcp_error": {"code": "INVALID_REQUEST", "message": "nope"}, "errors": []}
    ex = _rest(lambda r: httpx.Response(400, json=env)).call("get_products", {})
    assert (ex.is_success, ex.is_error, ex.is_indeterminate) == (False, True, False)
    assert ex.error_code == "INVALID_REQUEST"


@pytest.mark.parametrize(
    "status, body",
    [
        (500, {"detail": "internal server error"}),  # FastAPI-shaped, not an AdCP envelope
        (403, {"message": "forbidden"}),
        (404, {}),
    ],
)
def test_rest_non_envelope_http_failure_is_error_not_success(status, body):
    """The regression: is_success was 'no envelope recognized', so these read as success."""
    ex = _rest(lambda r: httpx.Response(status, json=body)).call("create_media_buy", {})
    assert ex.is_success is False, f"HTTP {status} must never be a success"
    assert ex.is_error is True


def test_rest_html_502_from_a_proxy_is_error():
    ex = _rest(lambda r: httpx.Response(502, text="<html>502 Bad Gateway</html>")).call(
        "create_media_buy", {}
    )
    assert (ex.is_success, ex.is_error) == (False, True)
    assert ex.wire_response is None
    assert "502" in (ex.raw_body or "")


def test_rest_200_with_non_object_json_is_indeterminate_not_success():
    ex = _rest(lambda r: httpx.Response(200, json=[1, 2, 3])).call("get_products", {})
    assert (ex.is_success, ex.is_error, ex.is_indeterminate) == (False, False, True)
    assert "list" in (ex.unclassified_reason or "")


def test_rest_200_with_unparseable_body_is_indeterminate():
    ex = _rest(lambda r: httpx.Response(200, text="not json at all")).call("create_media_buy", {})
    assert ex.is_indeterminate is True
    assert ex.wire_response is None


# ---------------------------------------------------------------------------
# MCP
# ---------------------------------------------------------------------------


def test_mcp_structured_content_is_success():
    ex = _mcp(_rpc_ok({"products": []})).call("get_products", {})
    assert (ex.is_success, ex.is_error, ex.is_indeterminate) == (True, False, False)


def test_mcp_is_error_envelope_is_error():
    env = {"adcp_error": {"code": "VALIDATION_ERROR"}, "errors": []}
    ex = _mcp(
        _echo(
            lambda i: httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": i,
                    "result": {
                        "isError": True,
                        "content": [{"type": "text", "text": json.dumps(env)}],
                    },
                },
            )
        )
    ).call("get_products", {})
    assert ex.is_error is True and ex.error_code == "VALIDATION_ERROR"


@pytest.mark.parametrize(
    "status, text",
    [
        (401, "Unauthorized"),
        (500, "<html>500</html>"),
        (400, "Missing session ID"),
        (200, ""),
    ],
)
def test_mcp_response_without_a_jsonrpc_frame_is_never_success(status, text):
    """These previously became {"content": None} in wire_response, i.e. a success."""
    ex = _mcp(lambda r: httpx.Response(status, text=text)).call("create_media_buy", {})
    assert ex.is_success is False, f"HTTP {status} {text!r} must never be a success"
    assert ex.wire_response is None


def test_mcp_result_with_no_readable_payload_is_indeterminate():
    ex = _mcp(
        _echo(
            lambda i: httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": i,
                    "result": {"content": [{"type": "image", "data": "x"}]},
                },
            )
        )
    ).call("get_products", {})
    assert (ex.is_success, ex.is_error, ex.is_indeterminate) == (False, False, True)


# ---------------------------------------------------------------------------
# A2A
# ---------------------------------------------------------------------------


def _task(parts, state=None):
    task: dict = {"artifacts": [{"artifactId": "a1", "parts": parts}]}
    if state:
        task["status"] = {"state": state}
    return {"jsonrpc": "2.0", "id": "1", "result": task}


def test_a2a_data_part_is_success():
    ex = _a2a(lambda r: httpx.Response(200, json=_task([{"data": {"products": []}}]))).call(
        "get_products", {}
    )
    assert (ex.is_success, ex.is_error, ex.is_indeterminate) == (True, False, False)


def test_a2a_envelope_data_part_is_error():
    part = [{"data": {"adcp_error": {"code": "AUTH_REQUIRED"}, "errors": []}}]
    ex = _a2a(lambda r: httpx.Response(200, json=_task(part))).call("get_products", {})
    assert ex.is_error is True and ex.error_code == "AUTH_REQUIRED"


def test_a2a_submitted_task_with_no_artifact_is_not_a_success():
    """The seller's manual-approval shape: state=submitted, artifacts cleared.

    This previously became {"_payload": None} in wire_response -- a pending buy reported as
    a completed one, which is the worst possible reading on a money path.
    """
    resp = {
        "jsonrpc": "2.0",
        "id": "1",
        "result": {"artifacts": [], "status": {"state": "submitted"}},
    }
    ex = _a2a(lambda r: httpx.Response(200, json=resp)).call("create_media_buy", {})
    assert ex.is_success is False
    assert ex.is_indeterminate is True
    assert ex.extra.get("task_state") == "submitted"
    assert "submitted" in (ex.unclassified_reason or "")


def test_a2a_jsonrpc_error_is_error():
    resp = {"jsonrpc": "2.0", "id": "1", "error": {"code": -32603, "message": "boom"}}
    ex = _a2a(lambda r: httpx.Response(200, json=resp)).call("create_media_buy", {})
    assert ex.is_error is True and ex.error_code == "JSONRPC_ERROR"


def test_a2a_non_json_body_is_indeterminate():
    ex = _a2a(lambda r: httpx.Response(200, text="<html>proxy</html>")).call("create_media_buy", {})
    assert ex.is_indeterminate is True and ex.wire_response is None
