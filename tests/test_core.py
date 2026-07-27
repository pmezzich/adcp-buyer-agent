"""Unit tests for the pure core: idempotency-key derivation and the Exchange type.

The durable/integration behavior (DBOS exactly-once against a live seller) is exercised
by scripts/run_create_media_buy.py; these cover the deterministic pieces with no infra.
"""

from adcp_buyer.core.idempotency import jcs_key
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
