"""The durable executor — the buyer's deterministic protocol steps, wrapped in DBOS.

Only this module (plus the future monitor) carries @DBOS decorators; the transports and
the oracle library stay durable-exec-agnostic. The executor's job is to make mutating
verbs exactly-once and crash-safe: run a create_media_buy under its JCS-derived workflow
ID and DBOS returns the cached result on any re-run of the same logical buy, while the
same key on the wire makes the seller replay rather than double-book (retiring the SDK's
auto-mint-a-fresh-UUID hazard).
"""

from __future__ import annotations

from typing import Any

from dbos import DBOS, SetWorkflowID

from adcp_buyer.buyer.dbos_app import get_transport
from adcp_buyer.core.idempotency import jcs_key
from adcp_buyer.core.result import Exchange


@DBOS.step(retries_allowed=True, max_attempts=3, interval_seconds=1.0)
def _dispatch_create_media_buy(body: dict[str, Any]) -> dict[str, Any]:
    """The one non-deterministic external call, checkpointed by DBOS.

    Returns the Exchange as a dict so it crosses the durable boundary cleanly. Because
    the body already carries the stable idempotency_key, a retry of THIS step after a
    mid-flight crash re-POSTs the same key and the seller replays.
    """
    ex = get_transport().call("create_media_buy", body)
    return ex.to_dict()


@DBOS.workflow()
def create_media_buy_workflow(body: dict[str, Any]) -> dict[str, Any]:
    return _dispatch_create_media_buy(body)


def create_media_buy(plan: dict[str, Any]) -> Exchange:
    """Run a media buy durably and exactly-once.

    ``plan`` is the create_media_buy request WITHOUT an idempotency_key; we derive the
    key from its canonical form and use it as both the wire key and the DBOS workflow ID.
    Two calls with the same logical plan collapse to one media buy.
    """
    key = jcs_key(plan)
    body = {**plan, "idempotency_key": key}
    with SetWorkflowID(key):
        result = create_media_buy_workflow(body)
    return Exchange.from_dict(result)
