"""Buy a campaign, then durably await it going active and monitor its delivery.

uv run python scripts/run_monitor.py
"""

from __future__ import annotations

from adcp_buyer.buyer import dbos_app
from adcp_buyer.buyer.brief import CampaignBrief
from adcp_buyer.buyer.campaign import run_campaign
from adcp_buyer.buyer.monitor import await_active, monitor_delivery

BASE, TOKEN, TENANT = "http://localhost:8092", "ci-test-token", "ci-test"

BRIEF = CampaignBrief(
    brand_domain="example-buyer.com",
    brief="premium display and video advertising for a consumer tech launch",
    budget=10_000.0,
    max_cpm=30.0,
    flight_end="2026-09-30T23:59:59Z",
)


def main() -> None:
    dbos_app.init_dbos(base_url=BASE, token=TOKEN, tenant=TENANT)

    result = run_campaign(BRIEF, use_llm=False)
    print(f"\n[buy] status={result.status} media_buy_id={result.media_buy_id}")
    if not result.media_buy_id:
        dbos_app.DBOS.destroy()
        return

    print("\n[await_active] durably polling until the buy is live/terminal...")
    print(f"  -> {await_active(result.media_buy_id, max_polls=5, interval_seconds=2.0)}")

    print("\n[monitor_delivery] durable delivery snapshots:")
    for i, snap in enumerate(monitor_delivery(result.media_buy_id, polls=3, interval_seconds=2.0)):
        print(f"  poll {i}: {snap}")

    dbos_app.DBOS.destroy()


if __name__ == "__main__":
    main()
