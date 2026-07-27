"""Idempotency key derivation.

The AdCP spec defines replay-equivalence over an RFC 8785 (JCS) canonicalization of
the request with a closed exclusion list removed, hashed with SHA-256. We reuse that
same canonicalization for two things at once:

  1. the wire ``idempotency_key`` the seller dedupes on, and
  2. the DBOS workflow ID (via ``SetWorkflowID``), which makes the *executor* itself
     exactly-once — a re-run of the same logical media buy returns the cached workflow
     result instead of dispatching a second create.

A SHA-256 hex digest is 64 chars over [0-9a-f], which satisfies the spec's key format
(length 16-255, charset ``[A-Za-z0-9_.:-]``).
"""

from __future__ import annotations

import hashlib
from typing import Any

import rfc8785

# Top-level fields excluded from the replay hash per the AdCP idempotency contract.
# `idempotency_key` obviously can't hash itself; `context`/`governance_context` are
# envelope metadata, not request identity.
_EXCLUDED_TOP_LEVEL = ("idempotency_key", "context", "governance_context")


def canonical_bytes(payload: dict[str, Any]) -> bytes:
    """RFC 8785 canonical JSON of the payload with excluded fields removed."""
    scrubbed = {k: v for k, v in payload.items() if k not in _EXCLUDED_TOP_LEVEL}
    return rfc8785.dumps(scrubbed)


def jcs_key(payload: dict[str, Any]) -> str:
    """Stable idempotency key: SHA-256 of the canonicalized request."""
    return hashlib.sha256(canonical_bytes(payload)).hexdigest()
