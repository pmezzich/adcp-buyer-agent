"""The single normalized observation type every transport returns.

Modeled on salesagent's tests/harness/transport.py TransportResult so the buyer and
(later) the differential fuzzer read one shape regardless of wire. Deliberately a
plain, JSON/pickle-serializable dataclass so a DBOS step can return it across the
durable boundary without custom serialization.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Exchange:
    """One request/response on one wire.

    Exactly one of ``wire_response`` / ``wire_error_envelope`` is populated on a
    completed call. ``wire_error_envelope`` is the AdCP two-layer envelope
    ``{adcp_error: {...}, errors: [...]}`` captured verbatim from the wire.
    """

    op: str
    wire: str  # "rest" | "mcp" | "a2a"
    request: dict[str, Any]
    status_code: int | None = None
    wire_response: dict[str, Any] | None = None
    wire_error_envelope: dict[str, Any] | None = None
    idempotency_key: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def is_success(self) -> bool:
        return self.wire_error_envelope is None and self.wire_response is not None

    @property
    def is_error(self) -> bool:
        return self.wire_error_envelope is not None

    @property
    def error_code(self) -> str | None:
        if not self.wire_error_envelope:
            return None
        adcp_error = self.wire_error_envelope.get("adcp_error") or {}
        return adcp_error.get("code")

    @property
    def replayed(self) -> bool:
        """True when the seller served this as an idempotent replay."""
        body = self.wire_response or {}
        return bool(body.get("replayed"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "op": self.op,
            "wire": self.wire,
            "request": self.request,
            "status_code": self.status_code,
            "wire_response": self.wire_response,
            "wire_error_envelope": self.wire_error_envelope,
            "idempotency_key": self.idempotency_key,
            "extra": self.extra,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Exchange:
        return cls(**d)
