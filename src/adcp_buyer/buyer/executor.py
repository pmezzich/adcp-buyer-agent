"""The durable executor — the buyer's deterministic protocol steps, wrapped in DBOS.

Only this module (plus the monitor) carries @DBOS decorators; the transports and the oracle
library stay durable-exec-agnostic. The executor's job is to make mutating verbs crash-safe
and to stop a re-run of the same logical buy from dispatching a second create.

Two identifiers, deliberately not the same value
------------------------------------------------
The WIRE key must be STABLE across retries — that is precisely what makes the seller replay
instead of double-booking, and it is what the AdCP idempotency contract is written around.

The DBOS WORKFLOW ID must NOT be stable across *failed* attempts. DBOS caches terminal
outcomes, and ERROR is terminal: once an attempt is recorded as failed under an id, every
later invocation of that id re-raises the cached exception and never contacts the seller
again. Binding both to one JCS hash therefore meant a transient seller outage permanently
burned that campaign plan — and it inverted the very contract the seller implements
correctly, whose rule is that errors are never cached and a retry re-executes.

So the wire key stays the JCS hash, and the workflow id is that hash plus an attempt
suffix that advances only past attempts DBOS has already recorded as ERROR. A SUCCESS is
still short-circuited (cheap, and correct), a crash mid-flight still recovers, and a failed
attempt is retryable — with the same wire key, so the seller is still the authority on
exactly-once.
"""

from __future__ import annotations

import logging
from typing import Any

from dbos import DBOS, SetWorkflowID

from adcp_buyer.buyer.dbos_app import get_transport
from adcp_buyer.core.idempotency import jcs_key, validate_key
from adcp_buyer.core.result import Exchange

logger = logging.getLogger(__name__)

#: How many failed attempts under one plan we will step past before giving up. A plan that
#: has failed this many times is not having a transient problem.
MAX_ATTEMPTS = 25


@DBOS.step(retries_allowed=True, max_attempts=3, interval_seconds=1.0)
def _dispatch_create_media_buy(body: dict[str, Any]) -> dict[str, Any]:
    """The one non-deterministic external call, checkpointed by DBOS.

    Returns the Exchange as a dict so it crosses the durable boundary cleanly. Because the
    body already carries the stable idempotency_key, a retry of THIS step after a mid-flight
    crash re-POSTs the same key and the seller replays.
    """
    ex = get_transport().call("create_media_buy", body)
    return ex.to_dict()


@DBOS.workflow()
def create_media_buy_workflow(body: dict[str, Any]) -> dict[str, Any]:
    return _dispatch_create_media_buy(body)


def _attempt_id(key: str, attempt: int) -> str:
    """Workflow id for one attempt. Attempt 1 keeps the bare key for readability."""
    return key if attempt == 1 else f"{key}:{attempt}"


def _next_workflow_id(key: str) -> str:
    """The id to run under: reuse a SUCCESS or an in-flight attempt, step past an ERROR.

    Falls back to the bare key if DBOS cannot be queried — the previous behaviour, which is
    safe in the sense that it never double-dispatches, just occasionally unretryable.
    """
    for attempt in range(1, MAX_ATTEMPTS + 1):
        wf_id = _attempt_id(key, attempt)
        try:
            statuses = DBOS.list_workflows(
                workflow_ids=[wf_id], load_input=False, load_output=False
            )
        except Exception as exc:  # noqa: BLE001 -- see below
            # Deliberately broad: DBOS exports no public exception base (its types live in
            # the private dbos._error), and this lookup is an optimisation, not a
            # correctness step. Any failure to read status -- DBOS not launched, system DB
            # unreachable, an API change -- falls back to the bare key, which is the old
            # behaviour: never double-dispatches, only unretryable after a failure. Failing
            # the buy because we could not read a status would be strictly worse.
            logger.warning("could not read DBOS status for %s (%s); using it as-is", wf_id, exc)
            return wf_id
        if not statuses:
            return wf_id  # never run
        status = getattr(statuses[0], "status", None)
        if status != "ERROR":
            return wf_id  # SUCCESS (short-circuits) or still running (DBOS will attach)
        logger.info("workflow %s previously failed; retrying under a fresh id", wf_id)
    raise RuntimeError(
        f"create_media_buy has failed {MAX_ATTEMPTS} times for this plan (key {key[:16]}...); "
        "refusing to keep retrying"
    )


def create_media_buy(plan: dict[str, Any], *, idempotency_key: str | None = None) -> Exchange:
    """Run a media buy durably. Two calls with the same logical plan collapse to one buy.

    ``plan`` is the create_media_buy request WITHOUT an idempotency_key. By DEFAULT the key is
    derived from its canonical form — the same canonicalization the seller hashes — so a retry
    of the same plan replays instead of double-booking, with no bookkeeping from the caller.

    Pass ``idempotency_key`` to override that. The default answers "is this the same REQUEST?",
    which is the right question for a retry and the WRONG one for a deliberate repeat: two
    campaigns that are legitimately the same shape (a monthly re-run, two identical flights)
    hash identically, so the seller replays and the caller receives one media buy while
    believing it placed two — a silent under-buy. AdCP makes the key client-generated exactly
    so that call belongs to the caller; supply a distinct key per intended buy.

    The key is validated against the pinned format before anything is sent, so a malformed one
    is a local error rather than a seller rejection mid-flight.
    """
    key = validate_key(idempotency_key) if idempotency_key is not None else jcs_key(plan)
    body = {**plan, "idempotency_key": key}
    with SetWorkflowID(_next_workflow_id(key)):
        result = create_media_buy_workflow(body)
    return Exchange.from_dict(result)
