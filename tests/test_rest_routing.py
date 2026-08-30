"""How the REST transport builds a request: path params, encoding, and redirects.

media_buy_id is the only path parameter in the route table, and it was being interpolated
into the URL *and* left in the JSON body. The seller's UpdateMediaBuyBody does not declare
it and forbids extras outside production, so every buyer PUT was an INVALID_REQUEST against
a dev or CI seller and worked only in production -- the inverse of what a fuzzer needs.

It was also interpolated unencoded, and ids are seller-supplied data the buyer echoes back.
"""

from __future__ import annotations

import httpx
import pytest

from adcp_buyer.core.transports.rest import RestRequestError, RestTransport


def _capture():
    """A transport whose requests are recorded instead of sent."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"ok": True})

    t = RestTransport("http://seller", "tok", "ten")
    t._client = httpx.Client(
        transport=httpx.MockTransport(handler),
        follow_redirects=t._client.follow_redirects,  # inherit, never restate
    )
    return t, seen


def test_a_path_parameter_rides_in_the_url_and_not_in_the_body():
    t, seen = _capture()
    t.call("update_media_buy", {"media_buy_id": "mb_abc", "budget": 100.0})
    (req,) = seen
    assert req.url.path == "/api/v1/media-buys/mb_abc"
    import json

    body = json.loads(req.content)
    assert "media_buy_id" not in body, "the seller forbids it in the body outside production"
    assert body == {"budget": 100.0}


def test_the_logical_request_still_records_the_path_parameter():
    """The differential engine compares one logical request across wires, where the field
    lives in the URL on REST and inside the arguments on MCP and A2A."""
    t, _ = _capture()
    ex = t.call("update_media_buy", {"media_buy_id": "mb_abc", "budget": 100.0})
    assert ex.request["media_buy_id"] == "mb_abc"
    assert "media_buy_id" not in ex.extra["sent_body"]


@pytest.mark.parametrize(
    "raw, encoded",
    [
        ("mb/../../admin", "mb%2F..%2F..%2Fadmin"),
        ("mb?x=1", "mb%3Fx%3D1"),
        ("mb#frag", "mb%23frag"),
        ("mb 1", "mb%201"),
    ],
)
def test_a_hostile_id_cannot_rewrite_the_request_target(raw, encoded):
    """Asserted on raw_path -- the bytes actually sent. httpx's .url.path DECODES for
    display, so checking it would pass an unencoded id straight through.
    """
    t, seen = _capture()
    t.call("update_media_buy", {"media_buy_id": raw, "budget": 1.0})
    (req,) = seen
    raw_target = req.url.raw_path.decode()
    assert raw_target == f"/api/v1/media-buys/{encoded}", raw_target
    # The id stays ONE segment: no separator it contained survives unencoded.
    segment = raw_target.rsplit("/", 1)[1]
    assert not any(ch in segment for ch in "/?#"), segment


def test_a_missing_path_parameter_raises_a_typed_deterministic_error():
    """A bare KeyError escaped into the durable step, which then burned three retries on an
    error that could never resolve."""
    t, _ = _capture()
    with pytest.raises(RestRequestError, match="media_buy_id"):
        t.call("update_media_buy", {"budget": 100.0})


def test_an_unknown_op_raises_the_same_typed_error():
    t, _ = _capture()
    with pytest.raises(RestRequestError):
        t.call("no_such_op", {})


def test_a_redirect_is_recorded_as_an_error_not_followed():
    """httpx re-issues a followed 302 on POST as a GET, so a failed money POST came back as
    a 200 carrying the redirect target's HTML -- and the seller mounts a Flask app at "/"
    behind the API routes, so this is reachable rather than theoretical."""
    hops: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hops.append((request.method, request.url.path))
        if request.url.path == "/api/v1/media-buys":
            return httpx.Response(302, headers={"location": "http://seller/login"})
        return httpx.Response(200, text="<!doctype html><title>Log In</title>")

    t = RestTransport("http://seller", "tok", "ten")
    assert t._client.follow_redirects is False, (
        "the transport must not follow redirects: httpx re-issues a followed 302 on POST as "
        "a GET, so a failed money POST comes back as a 200 with another page's body"
    )
    t._client = httpx.Client(
        transport=httpx.MockTransport(handler),
        follow_redirects=t._client.follow_redirects,
    )

    ex = t.call("create_media_buy", {"idempotency_key": "k"})
    assert hops == [("POST", "/api/v1/media-buys")], f"the redirect was followed: {hops}"
    assert ex.is_success is False
    assert ex.error_code == "UNEXPECTED_REDIRECT"
    assert ex.wire_error_envelope["adcp_error"]["location"] == "http://seller/login"
