"""Webhook sink — the webhook half of the async rendezvous.

The seller POSTs a protocol webhook here when an async media buy advances. The sink resolves
the operation_id (which the buyer sets equal to the buy's DBOS workflow id via
push_notification_config) and DBOS.send()s the payload into that waiting workflow, so its
DBOS.recv() returns at once instead of waiting for the next poll.

End-to-end webhook delivery needs three things the local ci-test tenant does not provide:
  (1) this sink running and reachable from the seller container (localhost is rewritten to
      host.docker.internal by salesagent),
  (2) a push_notification_config on create_media_buy pointing at it, and
  (3) a seller flow that actually returns `submitted` — ci-test auto-completes.
So this is unit-tested at the receiver level and wired for DBOS, but the poll half
(monitor.await_active) is the live-verified primary path per the design.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from adcp_buyer.buyer import dbos_app

WEBHOOK_TOPIC = "webhook"

Waker = Callable[[str, dict[str, Any]], bool]


def _wake_workflow(operation_id: str, payload: dict[str, Any]) -> bool:
    """Send a webhook payload into the workflow whose id == operation_id."""
    try:
        from dbos import DBOSClient

        client = DBOSClient(database_url=dbos_app.DEFAULT_BUYER_DB)
        client.send(operation_id, payload, topic=WEBHOOK_TOPIC)
        return True
    except Exception:  # noqa: BLE001 — a missing/idle workflow must not 500 the seller's POST
        return False


def build_sink(*, waker: Waker = _wake_workflow) -> Starlette:
    """The webhook receiver app. `waker` is injectable so the receiver is testable off-DBOS."""

    async def receive(request: Request) -> JSONResponse:
        operation_id = request.path_params["operation_id"]
        try:
            payload = await request.json()
        except Exception:  # noqa: BLE001 — tolerate a non-JSON body
            payload = {}
        woke = waker(operation_id, payload)
        return JSONResponse({"ok": True, "woke": woke})

    return Starlette(routes=[Route("/webhooks/{operation_id}", receive, methods=["POST"])])
