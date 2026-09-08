"""Unit tests for the pure core: idempotency-key derivation and the Exchange type.

The durable/integration behavior (DBOS exactly-once against a live seller) is exercised
by scripts/run_create_media_buy.py; these cover the deterministic pieces with no infra.
"""

import pytest
from adcp.server.idempotency import canonical_json_sha256

from adcp_buyer.core.idempotency import canonical_payload, jcs_key
from adcp_buyer.core.result import Exchange


def test_jcs_key_is_key_order_independent():
    a = {"b": 1, "a": 2, "packages": [{"x": 1, "y": 2}]}
    b = {"a": 2, "packages": [{"x": 1, "y": 2}], "b": 1}
    assert jcs_key(a) == jcs_key(b)


def test_jcs_key_excludes_idempotency_and_context():
    base = {"brand": {"domain": "x.com"}, "budget": 5.0}
    k = jcs_key(base)
    assert jcs_key({**base, "idempotency_key": "anything"}) == k
    assert jcs_key({**base, "context": {"foo": "bar"}}) == k
    assert jcs_key({**base, "governance_context": {"g": 1}}) == k


def test_jcs_key_is_byte_identical_to_the_sdk_the_seller_dedupes_on():
    """The buyer's hash IS the wire key, so any disagreement with the seller is a double-book.

    salesagent hashes through adcp.server.idempotency.canonical_json_sha256
    (src/core/idempotency_canonical.py). Equality here is what makes "the seller will replay
    our retry" true rather than hoped for.
    """
    payload = {
        "brand": {"domain": "x.com"},
        "packages": [{"product_id": "p1", "budget": 5.0}],
        "context": {"ignored": True},
        "idempotency_key": "ignored-too",
    }
    assert jcs_key(payload) == canonical_json_sha256(payload)


def test_rotating_a_webhook_credential_does_not_change_the_key():
    """The spec's exclusion list is not flat: it also strips
    push_notification_config.authentication.credentials.

    A hand-rolled version stripped only the three TOP-LEVEL fields, so rotating a webhook
    credential between an attempt and its retry minted a second key for what the seller
    treats as one request -- and the campaign booked twice.
    """

    def buy(cred):
        return {
            "brand": {"domain": "x.com"},
            "packages": [{"product_id": "p1", "budget": 5.0}],
            "push_notification_config": {
                "url": "https://buyer.example/hook",
                "authentication": {"scheme": "bearer", "credentials": cred},
            },
        }

    assert jcs_key(buy("token-1")) == jcs_key(buy("token-2"))
    # ...but the surrounding webhook config is still material.
    rerouted = buy("token-1")
    rerouted["push_notification_config"]["url"] = "https://elsewhere.example/hook"
    assert jcs_key(rerouted) != jcs_key(buy("token-1"))


def test_canonical_payload_shows_what_was_excluded():
    stripped = canonical_payload({"budget": 5.0, "context": {"a": 1}, "idempotency_key": "k"})
    assert stripped == {"budget": 5.0}


def test_jcs_key_changes_on_material_field():
    assert jcs_key({"budget": 5.0}) != jcs_key({"budget": 6.0})


def test_jcs_key_satisfies_wire_format():
    # AdCP requires length 16-255 over [A-Za-z0-9_.:-]; a sha256 hex digest qualifies.
    k = jcs_key({"a": 1})
    assert 16 <= len(k) <= 255
    assert all(c in "0123456789abcdef" for c in k)


def test_exchange_success():
    ex = Exchange(
        op="create_media_buy",
        wire="rest",
        request={},
        status_code=200,
        wire_response={"media_buy_id": "buy_1", "status": "completed"},
    )
    assert ex.is_success and not ex.is_error and ex.error_code is None


def test_exchange_error_reads_two_layer_envelope():
    ex = Exchange(
        op="create_media_buy",
        wire="rest",
        request={},
        status_code=404,
        wire_error_envelope={
            "adcp_error": {"code": "ACCOUNT_NOT_FOUND"},
            "errors": [{"code": "ACCOUNT_NOT_FOUND"}],
        },
    )
    assert ex.is_error and not ex.is_success and ex.error_code == "ACCOUNT_NOT_FOUND"


def test_exchange_replayed_flag():
    assert Exchange(op="x", wire="rest", request={}, wire_response={"replayed": True}).replayed
    assert not Exchange(op="x", wire="rest", request={}, wire_response={}).replayed


def test_exchange_roundtrips_through_dict():
    # DBOS serializes the step return across the durable boundary as a dict.
    ex = Exchange(
        op="x",
        wire="rest",
        request={"a": 1},
        status_code=200,
        wire_response={"ok": True},
        idempotency_key="k",
    )
    assert Exchange.from_dict(ex.to_dict()) == ex


class TestIdempotencyKeyIdentity:
    """Who decides that two calls are "the same operation".

    Deriving the key from content answers "is this the same REQUEST?", which is right for a
    retry and wrong for a deliberate repeat: two campaigns of legitimately the same shape (a
    monthly re-run, two identical flights) hash identically, the seller replays, and the caller
    gets ONE media buy while believing it placed two. A silent under-buy is the mirror of the
    double-book the key exists to prevent. AdCP makes the key client-generated so that call
    belongs to the caller.
    """

    PLAN = {"brand": {"domain": "x.com"}, "packages": [{"product_id": "p1", "budget": 5.0}]}

    def test_the_default_makes_a_retry_of_one_plan_replay(self):
        assert jcs_key(self.PLAN) == jcs_key(dict(self.PLAN))

    def test_two_identical_plans_share_a_key_which_is_why_an_override_exists(self):
        """The hazard, stated as a test so the override's reason cannot be forgotten."""
        second_flight = dict(self.PLAN)
        assert jcs_key(self.PLAN) == jcs_key(second_flight)

    def test_distinct_supplied_keys_stay_distinct(self):
        from adcp_buyer.core.idempotency import validate_key

        a, b = validate_key("campaign-2026-09-a"), validate_key("campaign-2026-09-b")
        assert a != b

    def test_a_malformed_key_is_refused_locally_before_anything_is_sent(self):
        from adcp_buyer.core.idempotency import InvalidIdempotencyKey, validate_key

        for bad in ("short", "has spaces", "a" * 300, "no/slashes/allowed" + "x" * 10):
            with pytest.raises(InvalidIdempotencyKey):
                validate_key(bad)

    def test_a_derived_key_satisfies_the_format_it_validates(self):
        """The default path must produce a key the validator would accept -- otherwise the
        two halves disagree and only the override is usable."""
        from adcp_buyer.core.idempotency import validate_key

        assert validate_key(jcs_key(self.PLAN))
