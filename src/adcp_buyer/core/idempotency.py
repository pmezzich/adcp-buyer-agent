"""Idempotency key derivation — delegated to the SDK the seller dedupes on.

The AdCP spec defines replay-equivalence over an RFC 8785 (JCS) canonicalization of the
request with a closed exclusion list removed, hashed with SHA-256. We use that same key for
two things at once: the wire ``idempotency_key`` the seller dedupes on, and (with a per-
attempt discriminator) the DBOS workflow identity.

Because the buyer's hash IS the wire key, any disagreement with the seller's hash is a
double-book, not a mismatch: a field the buyer hashes and the seller excludes makes the
buyer mint a NEW key for a request the seller would have replayed. A hand-rolled version of
this function stripped only the three TOP-LEVEL exclusions and missed the nested
``push_notification_config.authentication.credentials`` path, so a rotated webhook
credential between an attempt and its retry booked the campaign twice.

So this module does not implement the rule; it calls the same
``adcp.server.idempotency.canonical_json_sha256`` that salesagent's own seam calls
(``src/core/idempotency_canonical.py``). Being bit-identical by construction is the only
version of this that stays correct when the exclusion list changes.

A SHA-256 hex digest is 64 chars over [0-9a-f], which satisfies the spec's key format
(length 16-255, charset ``[A-Za-z0-9_.:-]``).
"""

from __future__ import annotations

from typing import Any

from adcp.server.idempotency import EXCLUDED_FIELDS, canonical_json_sha256, strip_excluded_fields

__all__ = ["EXCLUDED_FIELDS", "canonical_payload", "jcs_key"]


def canonical_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """The payload as it is hashed: excluded fields removed, nothing else changed.

    Exposed for the differential engine, which needs to show WHY two requests hashed the
    same or differently without re-deriving the exclusion rule and drifting from it.
    """
    return strip_excluded_fields(payload)


def jcs_key(payload: dict[str, Any]) -> str:
    """Stable idempotency key: SHA-256 over the spec's canonical form of the request."""
    return canonical_json_sha256(payload)
