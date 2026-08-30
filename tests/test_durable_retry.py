"""A failed attempt must not permanently burn the plan. Needs DBOS Postgres + a live seller.

DBOS caches terminal outcomes, and ERROR is terminal. When the JCS key was used as BOTH the
wire idempotency_key and the DBOS workflow id, the first failure was recorded under that id
and every later attempt re-raised the cached exception without contacting the seller at all
-- so one transient outage made that campaign plan unplaceable forever.

That also inverted the contract the seller implements correctly: under AdCP, errors are never
cached and a retry re-executes. The buyer's durable layer was caching exactly what the spec
says must not be cached.

This test is the oracle for the fix. It runs DBOS, so it manages its own lifecycle and is
kept in its own module to stay away from the rest of the suite.

    docker start buyer-pg    # postgres on 127.0.0.1:5544, database adcp_buyer
"""

from __future__ import annotations

import os
import uuid

import httpx
import pytest

# DBOS does not re-export its exception types at package level.
from dbos._error import DBOSMaxStepRetriesExceeded

BUYER_DB = os.environ.get(
    "BUYER_DATABASE_URL", "postgresql://postgres:buyer@127.0.0.1:5544/adcp_buyer"
)
SELLER = os.environ.get("SELLER_BASE", "http://localhost:8092")
TOKEN = os.environ.get("SELLER_TOKEN", "ci-test-token")
TENANT = os.environ.get("SELLER_TENANT", "ci-test")
DEAD = "http://127.0.0.1:59998"  # nothing listens here


def _seller_up() -> bool:
    try:
        return httpx.get(f"{SELLER}/health", timeout=3.0).status_code == 200
    except (httpx.HTTPError, OSError):
        return False


def _buyer_db_up() -> bool:
    try:
        import psycopg

        with psycopg.connect(BUYER_DB, connect_timeout=3):
            return True
    except (ImportError, psycopg.Error, OSError):
        return False


pytestmark = pytest.mark.skipif(
    not (_seller_up() and _buyer_db_up()),
    reason=f"needs a seller at {SELLER} and the buyer DBOS database at {BUYER_DB}",
)


def test_a_failed_attempt_can_be_retried_once_the_seller_recovers():
    from adcp_buyer.buyer import dbos_app
    from adcp_buyer.buyer.brief import CampaignBrief
    from adcp_buyer.buyer.campaign import ensure_account
    from adcp_buyer.buyer.executor import create_media_buy
    from adcp_buyer.core.transports import make_transport
    from adcp_buyer.core.transports.rest import RestTransport

    brief = CampaignBrief(
        brand_domain=f"retry-{uuid.uuid4().hex[:10]}.com",
        brief="premium display",
        budget=4000.0,
        max_cpm=30.0,
        flight_end="2026-09-30T23:59:59Z",
    )
    probe = make_transport("rest", SELLER, TOKEN, TENANT)
    try:
        account_id = ensure_account(probe, brief)
    finally:
        probe.close()

    plan = {
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

    dbos_app.init_dbos(base_url=DEAD, token=TOKEN, tenant=TENANT, database_url=BUYER_DB)
    try:
        # Narrow on purpose: a bare `Exception` here would also pass if the call blew up
        # for an unrelated reason, and the point is that the SELLER was unreachable.
        with pytest.raises(DBOSMaxStepRetriesExceeded):
            create_media_buy(plan)

        # Same plan, seller now healthy.
        dbos_app._transport = RestTransport(base_url=SELLER, token=TOKEN, tenant=TENANT)
        result = create_media_buy(plan)

        assert result.is_success, f"the plan was burned by its earlier failure: {result.describe()}"
        assert (result.wire_response or {}).get("media_buy_id")
    finally:
        dbos_app.DBOS.destroy()
