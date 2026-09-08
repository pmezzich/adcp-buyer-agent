"""The pipeline's failure paths, driven offline against a stub seller.

These were previously reachable only through a live seller, and the live version of the
account-failure test SKIPPED whenever the seller happened to accept the malformed input --
so the oracle was dormant, which reads exactly like passing. Stubbing the transport makes
the failure deterministic and the test always-run.
"""

from __future__ import annotations

import pytest

from adcp_buyer.buyer.brief import CampaignBrief
from adcp_buyer.buyer.campaign import AccountRegistrationFailed, ensure_account
from adcp_buyer.core.result import Exchange

BRIEF = CampaignBrief(
    brand_domain="b.com",
    brief="display",
    budget=1_000.0,
    max_cpm=30.0,
    flight_end="2026-09-30T23:59:59Z",
)


class StubTransport:
    """Returns a canned Exchange for every call and records what it was asked."""

    def __init__(self, exchange: Exchange):
        self.exchange = exchange
        self.calls: list[tuple[str, dict]] = []

    def call(self, op, body=None, *, authed=True):
        self.calls.append((op, dict(body or {})))
        return self.exchange


def _envelope(code="INVALID_REQUEST", message="Extra inputs are not permitted"):
    return {"adcp_error": {"code": code, "message": message}, "errors": [{"code": code}]}


def test_a_rejected_registration_raises_and_keeps_the_wire_reason():
    """The reason must survive. It previously became a bare "could not register account"
    on the path that leads to a buy -- the seller's own message discarded."""
    ex = Exchange(
        op="sync_accounts",
        wire="rest",
        request={},
        status_code=400,
        wire_error_envelope=_envelope(),
    )
    with pytest.raises(AccountRegistrationFailed) as exc:
        ensure_account(StubTransport(ex), BRIEF)
    assert exc.value.exchange is ex
    assert "INVALID_REQUEST" in str(exc.value)
    assert "Extra inputs" in str(exc.value)


def test_an_indeterminate_registration_is_not_treated_as_success():
    ex = Exchange(
        op="sync_accounts",
        wire="mcp",
        request={},
        status_code=200,
        unclassified_reason="no JSON-RPC frame in the response body",
    )
    with pytest.raises(AccountRegistrationFailed):
        ensure_account(StubTransport(ex), BRIEF)


def test_a_success_carrying_no_account_is_still_a_failure():
    """A 200 with accounts: [] is not an account. Reading it as one produced a None
    account_id that flowed onward into the media buy."""
    ex = Exchange(
        op="sync_accounts", wire="rest", request={}, status_code=200, wire_response={"accounts": []}
    )
    with pytest.raises(AccountRegistrationFailed):
        ensure_account(StubTransport(ex), BRIEF)


def test_a_failed_response_that_still_carries_an_account_is_rejected():
    """The case that makes the is_success check load-bearing rather than decorative.

    Reading the body first and the outcome second (or never) means any response whose SHAPE
    looks right is accepted regardless of what the wire said -- a 502 from a gateway that
    echoed a cached body, or a 4xx returning a partial result. The outcome decides; the
    shape only supplies the value once the outcome permits reading it.
    """
    ex = Exchange(
        op="sync_accounts",
        wire="rest",
        request={},
        status_code=502,
        wire_response={"accounts": [{"account_id": "acc_from_a_failed_call"}]},
    )
    with pytest.raises(AccountRegistrationFailed):
        ensure_account(StubTransport(ex), BRIEF)


def test_a_good_registration_returns_the_seller_minted_id():
    ex = Exchange(
        op="sync_accounts",
        wire="rest",
        request={},
        status_code=200,
        wire_response={"accounts": [{"account_id": "acc_123"}]},
    )
    assert ensure_account(StubTransport(ex), BRIEF) == "acc_123"


def test_ensure_account_does_not_send_an_idempotency_key():
    """salesagent rejects the schema-required key on sync_accounts over REST and MCP
    (prebid/salesagent#1983, #2119, #2120), so sending it made every call a 400. Pinned here
    so the omission stays deliberate rather than becoming folklore."""
    ex = Exchange(
        op="sync_accounts",
        wire="rest",
        request={},
        status_code=200,
        wire_response={"accounts": [{"account_id": "acc_1"}]},
    )
    stub = StubTransport(ex)
    ensure_account(stub, BRIEF)
    ((_, body),) = stub.calls
    assert "idempotency_key" not in body


def test_a_malformed_supplied_idempotency_key_never_reaches_the_seller():
    """Refused locally, so a bad key is a caller error rather than a money-path rejection.

    Lives here rather than beside the durable tests because it needs no stack -- it raises
    before any I/O -- and a module-level stack skipif would have made it dormant, which reads
    exactly like passing.

    It is also the executing half of the override's oracle: if create_media_buy stopped
    honouring `idempotency_key` and fell back to the content-derived key, this stops raising.
    """
    from adcp_buyer.buyer.executor import create_media_buy
    from adcp_buyer.core.idempotency import InvalidIdempotencyKey

    with pytest.raises(InvalidIdempotencyKey):
        create_media_buy({"brand": {"domain": "x.com"}}, idempotency_key="too-short")
