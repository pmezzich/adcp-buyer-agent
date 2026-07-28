"""Durable delivery monitoring — the async tail of a media buy.

Two DBOS workflows over the poll path (get_media_buy_delivery is a REST route, so no MCP
needed here):
  * await_active — durably poll until the buy reaches a live/terminal status (the poll half
    of the rendezvous; the webhook half lives in sink.py).
  * monitor_delivery — durably poll delivery N times, returning impressions/spend snapshots.

Both use DBOS.sleep between polls, so a crash mid-monitor resumes from the last completed
poll rather than restarting or double-counting. The production form is a @DBOS.scheduled
daily sweep over all active buys — same _poll_delivery step, cron-driven instead of bounded.
"""

from __future__ import annotations

from typing import Any

from dbos import DBOS

from adcp_buyer.buyer.dbos_app import get_transport
from adcp_buyer.core.result import Exchange

_LIVE_OR_TERMINAL = {"active", "completed", "paused", "canceled", "rejected"}


@DBOS.step(retries_allowed=True, max_attempts=3, interval_seconds=1.0)
def _poll_delivery(media_buy_ids: list[str]) -> dict[str, Any]:
    return (
        get_transport().call("get_media_buy_delivery", {"media_buy_ids": media_buy_ids}).to_dict()
    )


def delivery_summary(exchange: Exchange, media_buy_id: str) -> dict[str, Any]:
    """Pull {status, impressions, spend} for one buy out of a delivery response."""
    body = exchange.wire_response or {}
    status = None
    for d in body.get("media_buy_deliveries") or []:
        if d.get("media_buy_id") == media_buy_id:
            totals = d.get("totals") or {}
            return {
                "status": d.get("status"),
                "impressions": totals.get("impressions"),
                "spend": totals.get("spend"),
            }
    totals = body.get("aggregated_totals") or {}
    return {
        "status": status,
        "impressions": totals.get("impressions"),
        "spend": totals.get("spend"),
    }


@DBOS.workflow()
def await_active(media_buy_id: str, *, max_polls: int = 10, interval_seconds: float = 2.0) -> dict:
    """Durably poll until the buy is live/terminal, or max_polls is exhausted."""
    last: dict[str, Any] = {}
    for i in range(max_polls):
        if i:
            DBOS.sleep(interval_seconds)
        ex = Exchange.from_dict(_poll_delivery([media_buy_id]))
        last = delivery_summary(ex, media_buy_id)
        if last.get("status") in _LIVE_OR_TERMINAL:
            return last
    return last


@DBOS.workflow()
def monitor_delivery(
    media_buy_id: str, *, polls: int = 3, interval_seconds: float = 2.0
) -> list[dict]:
    """Durably poll delivery `polls` times; return the impressions/spend snapshots."""
    snapshots: list[dict[str, Any]] = []
    for i in range(polls):
        if i:
            DBOS.sleep(interval_seconds)
        ex = Exchange.from_dict(_poll_delivery([media_buy_id]))
        snapshots.append(delivery_summary(ex, media_buy_id))
    return snapshots
