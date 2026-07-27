"""DBOS wiring for the buyer's durable-execution layer.

DBOS is an embedded library: it checkpoints workflow/step state to Postgres in-process,
so there is no separate server or worker fleet. It uses a DEDICATED buyer database — never
the seller's — because the fuzz arena resets the seller's DB between runs and that would
wipe the buyer's durable state.

The REST transport is a non-durable dependency (an httpx client), so it is held as a
module singleton that steps read, rather than being serialized into workflow state.
"""

from __future__ import annotations

import os

from dbos import DBOS, DBOSConfig

from adcp_buyer.core.transports.rest import RestTransport

# Dedicated buyer Postgres (separate from the seller's postgres:17 on :5435).
DEFAULT_BUYER_DB = "postgresql://postgres:buyer@127.0.0.1:5544/adcp_buyer"

_transport: RestTransport | None = None


def get_transport() -> RestTransport:
    if _transport is None:
        raise RuntimeError("transport not initialized; call init_dbos() first")
    return _transport


def init_dbos(
    *,
    base_url: str,
    token: str,
    tenant: str,
    database_url: str | None = None,
    launch: bool = True,
) -> None:
    """Configure DBOS + the REST transport. Idempotent within a process."""
    global _transport
    _transport = RestTransport(base_url=base_url, token=token, tenant=tenant)

    config: DBOSConfig = {
        "name": "adcp-buyer",
        "database_url": database_url or os.environ.get("BUYER_DATABASE_URL", DEFAULT_BUYER_DB),
    }
    DBOS(config=config)
    if launch:
        DBOS.launch()
