"""Run the buyer brain end-to-end against the live salesagent (localhost:8092).

Two briefs, deterministic planner (no API key needed):
  * a normal brief buys the products that fit
  * a tighter brief (max_cpm below the $15/$25 products) correctly refuses to buy

    uv run python scripts/run_campaign.py
"""

from __future__ import annotations

from adcp_buyer.buyer import dbos_app
from adcp_buyer.buyer.brief import CampaignBrief
from adcp_buyer.buyer.campaign import run_campaign

BASE = "http://localhost:8092"
TOKEN = "ci-test-token"
TENANT = "ci-test"

NORMAL = CampaignBrief(
    brand_domain="example-buyer.com",
    brief="premium display and video advertising for a consumer tech launch",
    budget=10_000.0,
    max_cpm=30.0,
    flight_end="2026-09-30T23:59:59Z",
)

TIGHT = CampaignBrief(
    brand_domain="example-buyer.com",
    brief="premium display and video advertising for a consumer tech launch",
    budget=10_000.0,
    max_cpm=12.0,  # below both products ($15 / $25) — should refuse
    flight_end="2026-09-30T23:59:59Z",
)


def _report(label: str, result) -> None:
    print(f"\n=== {label} (budget=${result.plan.total_budget:g}, status={result.status}) ===")
    for p in result.plan.packages:
        print(
            f"    buy {p.product_id} @ ${p.cpm:g} cpm  budget ${p.budget:g}  [{p.pricing_source.value}]"
        )
    if result.status == "bought":
        print(f"    -> media_buy_id={result.media_buy_id}")
    elif result.status in ("no_buy", "refused", "error"):
        print(f"    -> {result.status}: {result.reason}")


def main() -> None:
    dbos_app.init_dbos(base_url=BASE, token=TOKEN, tenant=TENANT)
    _report("NORMAL brief (max_cpm $30)", run_campaign(NORMAL, use_llm=False))
    _report("TIGHT brief  (max_cpm $12)", run_campaign(TIGHT, use_llm=False))
    dbos_app.DBOS.destroy()


if __name__ == "__main__":
    main()
