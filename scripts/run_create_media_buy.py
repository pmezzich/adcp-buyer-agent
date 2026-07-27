"""Exercise the durable create_media_buy against the live salesagent (localhost:8092).

Proves the first durability property: exactly-once via the JCS key. Runs the same logical
plan twice; the second call must NOT create a second media buy — DBOS returns the cached
workflow result and the seller reports an idempotent replay.

    uv run python scripts/run_create_media_buy.py
"""

from __future__ import annotations

import json

from adcp_buyer.buyer import dbos_app
from adcp_buyer.buyer.executor import create_media_buy

BASE = "http://localhost:8092"
TOKEN = "ci-test-token"
TENANT = "ci-test"


def _show(label: str, obj: object) -> None:
    flat = json.dumps(obj, default=str)
    print(f"  {label:<26} {flat[:400]}")


def main() -> None:
    dbos_app.init_dbos(base_url=BASE, token=TOKEN, tenant=TENANT)
    t = dbos_app.get_transport()

    print("\n[1] probe get_products")
    prods = t.call("get_products", {"brief": "premium display and video"})
    print(f"  status={prods.status_code} success={prods.is_success}")
    products = (prods.wire_response or {}).get("products", []) if prods.is_success else []
    for p in products[:3]:
        print(f"    - {p.get('product_id')}  {p.get('name')}")
    if not products:
        _show("get_products body", prods.wire_error_envelope or prods.wire_response)
        return
    product = products[0]
    product_id = product["product_id"]

    print("\n[1b] register a buyer account (sync_accounts) — seller mints the account_id")
    from adcp_buyer.core.idempotency import jcs_key as _jcs

    acct_body = {
        "accounts": [
            {
                "account": {"account_id": "buyer-acct-1"},
                "brand": {"domain": "example-buyer.com"},
                "operator": "example-buyer.com",
                "billing": "operator",
            }
        ]
    }
    acct_body["idempotency_key"] = _jcs(acct_body)
    acct_ex = t.call("sync_accounts", acct_body)
    account_id = (acct_ex.wire_response or {}).get("accounts", [{}])[0].get("account_id")
    print(
        f"  account_id={account_id} ({(acct_ex.wire_response or {}).get('accounts', [{}])[0].get('action')})"
    )

    # Extract a pricing_option_id (PackageRequest requires it). Products carry a
    # pricing_options list; the id field is pricing_option_id (fall back to id).
    pricing_options = product.get("pricing_options") or product.get("pricing") or []
    if not pricing_options:
        print("  product keys:", list(product.keys()))
        _show("product", product)
        return
    po = pricing_options[0]
    pricing_option_id = po.get("pricing_option_id") or po.get("id")
    print(f"  using product={product_id} pricing_option={pricing_option_id}")

    print("\n[2] build a create_media_buy plan (no idempotency_key — derived from the plan)")
    plan = {
        "brand": {"domain": "example-buyer.com"},
        "account": {"account_id": account_id},
        "start_time": "asap",
        "end_time": "2026-09-30T23:59:59Z",
        "packages": [
            {
                "product_id": product_id,
                "budget": 5000.0,
                "pricing_option_id": pricing_option_id,
            }
        ],
    }
    _show("plan", plan)

    print("\n[3] first create_media_buy (durable workflow)")
    ex1 = create_media_buy(plan)
    mb1 = (ex1.wire_response or {}).get("media_buy_id")
    print(
        f"  status={ex1.status_code} success={ex1.is_success} media_buy_id={mb1} status={((ex1.wire_response or {}).get('status'))}"
    )

    print("\n[4] SAME plan again — exactly-once: DBOS returns the cached workflow, no second buy")
    ex2 = create_media_buy(plan)
    mb2 = (ex2.wire_response or {}).get("media_buy_id")
    print(f"  status={ex2.status_code} success={ex2.is_success} media_buy_id={mb2}")
    print("\n  ── exactly-once check ──")
    print(f"  same idempotency_key : {ex1.idempotency_key == ex2.idempotency_key}")
    print(f"  same media_buy_id    : {mb1 == mb2}  ({mb1})")
    print(f"  => one media buy, not two: {'PASS' if (mb1 and mb1 == mb2) else 'FAIL'}")

    dbos_app.DBOS.destroy()


if __name__ == "__main__":
    main()
