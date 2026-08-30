"""Tests that drive a REAL salesagent. Skipped unless one is reachable.

Everything else in this suite runs offline against stubs, which is what makes it fast and
what makes it unable to notice that the seller changed. These are the counterweight: they
assert the handful of claims that are only meaningful against a live seller, and they are
the ones that would catch the seller drifting away from us.

Stand the seller up first (from the salesagent checkout):

    docker compose -f docker-compose.e2e.yml -f docker-compose.e2e.ports.yml \\
                   -f ../adcp-buyer-agent/compose/docker-compose.fuzz.yml \\
                   up -d --build postgres adcp-server proxy
    docker compose -f docker-compose.e2e.yml exec -T adcp-server \\
                   python scripts/setup/init_database_ci.py

Point elsewhere with SELLER_BASE / SELLER_TOKEN / SELLER_TENANT.
"""

from __future__ import annotations

import os
import uuid

import httpx
import pytest

from adcp_buyer.buyer.brief import CampaignBrief
from adcp_buyer.buyer.campaign import ensure_account
from adcp_buyer.core.idempotency import jcs_key
from adcp_buyer.core.transports import make_transport

BASE = os.environ.get("SELLER_BASE", "http://localhost:8092")
TOKEN = os.environ.get("SELLER_TOKEN", "ci-test-token")
TENANT = os.environ.get("SELLER_TENANT", "ci-test")


def _seller_is_up() -> bool:
    try:
        return httpx.get(f"{BASE}/health", timeout=3.0).status_code == 200
    except (httpx.HTTPError, OSError):
        return False


pytestmark = pytest.mark.skipif(not _seller_is_up(), reason=f"no salesagent reachable at {BASE}")


@pytest.fixture
def rest():
    t = make_transport("rest", BASE, TOKEN, TENANT)
    yield t
    t.close()


@pytest.fixture
def brief():
    # A fresh brand per test so reruns never collide on the seller's natural upsert key.
    return CampaignBrief(
        brand_domain=f"buyer-{uuid.uuid4().hex[:10]}.com",
        brief="premium display advertising",
        budget=4_000.0,
        max_cpm=30.0,
        flight_end="2026-09-30T23:59:59Z",
    )


def _buy_body(brief: CampaignBrief, account_id: str) -> dict:
    return {
        "brand": {"domain": brief.brand_domain},
        "account": {"account_id": account_id},
        "start_time": "asap",
        "end_time": brief.flight_end,
        "packages": [
            {
                "product_id": "prod_display_premium",
                "budget": 4000.0,
                "pricing_option_id": "cpm_usd_fixed",
            }
        ],
    }


def test_products_come_back_with_bookable_pricing(rest):
    ex = rest.call("get_products", {"brief": "premium display"})
    assert ex.is_success, ex.describe()
    products = (ex.wire_response or {}).get("products") or []
    assert products, "seeded seller returned no products"
    for p in products:
        assert p.get("pricing_options"), f"{p.get('product_id')} has no pricing options"


def test_sync_accounts_rejects_the_schema_required_idempotency_key(rest, brief):
    """A known salesagent defect, pinned so we notice when it is fixed.

    The pinned sync-accounts-request schema lists idempotency_key as REQUIRED, but salesagent
    rejects it over REST and MCP and ignores it on A2A (prebid/salesagent#1983, #2119, #2120).
    ensure_account therefore omits it. When this test starts failing, the seller has been
    fixed and ensure_account should start sending the key again.
    """
    body = {
        "accounts": [
            {
                "account": {"account_id": brief.brand_domain},
                "brand": {"domain": brief.brand_domain},
                "operator": brief.brand_domain,
                "billing": "operator",
            }
        ],
        "idempotency_key": "a" * 32,
    }
    ex = rest.call("sync_accounts", body)
    assert ex.is_error, "seller now ACCEPTS idempotency_key on sync_accounts -- send it again"
    assert "idempotency_key" in str(ex.wire_error_envelope)


def test_the_same_plan_twice_yields_one_media_buy(rest, brief):
    """The seller's own replay, with the durable layer entirely out of the path.

    This is the claim the repo exists to make good on, and it cannot be tested through the
    DBOS executor: DBOS caches the first result under the workflow id and would return it
    without a second request, so a broken seller would look identical to a working one.
    """
    account_id = ensure_account(rest, brief)
    body = _buy_body(brief, account_id)
    key = jcs_key(body)

    first = rest.call("create_media_buy", {**body, "idempotency_key": key})
    second = rest.call("create_media_buy", {**body, "idempotency_key": key})

    assert first.is_success, first.describe()
    assert second.is_success, second.describe()
    id1 = (first.wire_response or {}).get("media_buy_id")
    id2 = (second.wire_response or {}).get("media_buy_id")
    assert id1, "first create returned no media_buy_id"
    assert id1 == id2, f"one idempotency key produced two media buys: {id1} != {id2}"
    assert second.replayed is True, "seller did not mark the second call as a replay"


def test_a_different_plan_does_not_collapse_onto_the_same_key(rest, brief):
    """The other half of exactly-once: no FALSE collapse. A weak canonicalization that
    hashed too little would pass the replay test above and silently merge distinct buys."""
    account_id = ensure_account(rest, brief)
    body = _buy_body(brief, account_id)
    cheaper = {**body, "packages": [{**body["packages"][0], "budget": 3000.0}]}
    assert jcs_key(body) != jcs_key(cheaper)

    a = rest.call("create_media_buy", {**body, "idempotency_key": jcs_key(body)})
    b = rest.call("create_media_buy", {**cheaper, "idempotency_key": jcs_key(cheaper)})
    assert a.is_success and b.is_success
    assert (a.wire_response or {}).get("media_buy_id") != (b.wire_response or {}).get(
        "media_buy_id"
    ), "two different plans collapsed onto one media buy"


# The account-failure oracle lives in tests/test_campaign_flow.py against a stub. Driving it
# here meant depending on the seller rejecting a malformed brand, which it does not always
# do -- and a test that skips reads exactly like a test that passes.
