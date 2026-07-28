"""The three byte-faithful transports behind one interface.

Each exposes ``.call(op, body, *, authed=True) -> Exchange`` and ``.close()``. The buyer
uses one; the differential fuzzer drives the same request through all three and compares
the normalized Exchanges.
"""

from __future__ import annotations

from adcp_buyer.core.transports.a2a import A2aTransport
from adcp_buyer.core.transports.mcp import McpTransport
from adcp_buyer.core.transports.rest import RestTransport

Wire = str  # "rest" | "mcp" | "a2a"

_TRANSPORTS = {"rest": RestTransport, "mcp": McpTransport, "a2a": A2aTransport}


def make_transport(wire: Wire, base_url: str, token: str, tenant: str):
    """Construct a transport by wire name."""
    try:
        cls = _TRANSPORTS[wire]
    except KeyError:
        raise ValueError(f"unknown wire {wire!r}; expected one of {sorted(_TRANSPORTS)}") from None
    return cls(base_url=base_url, token=token, tenant=tenant)


__all__ = ["A2aTransport", "McpTransport", "RestTransport", "Wire", "make_transport"]
