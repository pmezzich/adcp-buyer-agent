"""Unit tests for the pure delivery-summary logic (no DBOS, no infra).

The durable poll loops (await_active, monitor_delivery) are exercised by
scripts/run_monitor.py against the live stack.
"""

from adcp_buyer.buyer.monitor import delivery_summary
from adcp_buyer.core.result import Exchange


def test_delivery_summary_reads_per_buy_totals():
    ex = Exchange(
        op="get_media_buy_delivery",
        wire="rest",
        request={},
        wire_response={
            "media_buy_deliveries": [
                {
                    "media_buy_id": "b1",
                    "status": "active",
                    "totals": {"impressions": 100.0, "spend": 1.5},
                }
            ]
        },
    )
    assert delivery_summary(ex, "b1") == {"status": "active", "impressions": 100.0, "spend": 1.5}


def test_delivery_summary_falls_back_to_aggregate_totals():
    ex = Exchange(
        op="get_media_buy_delivery",
        wire="rest",
        request={},
        wire_response={
            "aggregated_totals": {"impressions": 5.0, "spend": 0.5},
            "media_buy_deliveries": [],
        },
    )
    summary = delivery_summary(ex, "not-present")
    assert summary["impressions"] == 5.0 and summary["spend"] == 0.5
