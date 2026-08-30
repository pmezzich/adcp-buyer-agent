"""The single normalized observation type every transport returns.

Modeled on salesagent's tests/harness/transport.py TransportResult so the buyer and
(later) the differential fuzzer read one shape regardless of wire. Deliberately a
plain, JSON/pickle-serializable dataclass so a DBOS step can return it across the
durable boundary without custom serialization.

Three outcomes, not two. A call is a success only when the wire POSITIVELY said so;
anything we could not classify is ``indeterminate`` and must never be read as a buy that
landed. The two-state version of this type derived ``is_success`` as "no error envelope
was recognized", so an HTTP 500, an nginx HTML 502, an empty body, or an MCP response
whose JSON-RPC frame failed to parse all reported success on the money path.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Exchange:
    """One request/response on one wire.

    ``wire_error_envelope`` is the AdCP two-layer envelope ``{adcp_error, errors}``
    captured verbatim. ``raw_body`` keeps the undecoded response text so the differential
    engine can compare bytes rather than our re-parse of them.
    """

    op: str
    wire: str  # "rest" | "mcp" | "a2a"
    request: dict[str, Any]
    status_code: int | None = None
    wire_response: dict[str, Any] | None = None
    wire_error_envelope: dict[str, Any] | None = None
    idempotency_key: str | None = None
    raw_body: str | None = None
    #: Why the transport could not classify the response, when it could not. Set together
    #: with wire_response=None and wire_error_envelope=None.
    unclassified_reason: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def http_failed(self) -> bool:
        """A non-2xx status. None means the wire has no HTTP status of its own."""
        return self.status_code is not None and not 200 <= self.status_code < 300

    @property
    def is_error(self) -> bool:
        """The wire said no — either an AdCP envelope, or an HTTP-level failure.

        A non-2xx status counts even when the body is not an AdCP envelope: a 502 from a
        proxy is still a failed call, and a buyer that treats it as success double-books.
        """
        return self.wire_error_envelope is not None or self.http_failed

    @property
    def is_success(self) -> bool:
        """The wire positively returned a payload and reported no failure."""
        return not self.is_error and self.wire_response is not None

    @property
    def is_indeterminate(self) -> bool:
        """2xx, but nothing we could read as either a payload or an error.

        On a mutating verb this is the dangerous state: the seller may or may not have
        acted. Callers must treat it as "possibly committed", never as a failure to retry
        blindly and never as a success.
        """
        return not self.is_success and not self.is_error

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

    def describe(self) -> str:
        """One line for logs and error messages — never just the code, which may be None."""
        if self.is_success:
            return f"{self.op}/{self.wire} ok (http {self.status_code})"
        if self.is_error:
            envelope = (self.wire_error_envelope or {}).get("adcp_error") or {}
            code = envelope.get("code") or f"HTTP_{self.status_code}"
            return f"{self.op}/{self.wire} error {code}: {envelope.get('message') or ''}".rstrip(
                ": "
            )
        return (
            f"{self.op}/{self.wire} INDETERMINATE (http {self.status_code}): "
            f"{self.unclassified_reason or 'unrecognized response shape'}"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "op": self.op,
            "wire": self.wire,
            "request": self.request,
            "status_code": self.status_code,
            "wire_response": self.wire_response,
            "wire_error_envelope": self.wire_error_envelope,
            "idempotency_key": self.idempotency_key,
            "raw_body": self.raw_body,
            "unclassified_reason": self.unclassified_reason,
            "extra": self.extra,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Exchange:
        return cls(**d)
