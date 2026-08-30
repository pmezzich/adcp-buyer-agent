"""Hand-rolled REST transport.

The adcp SDK's Protocol enum is MCP + A2A only — REST is not in the SDK — so this is a
byte-faithful httpx client mirroring salesagent's src/routes/api_v1.py. Reads are
POST-with-body; only /capabilities is GET; update_media_buy is the one PUT. Returns the
normalized Exchange so the buyer (and later the differential fuzzer) read one shape.

Path parameters are REST's alone. ``media_buy_id`` rides in the URL here and inside the
arguments on MCP and A2A, so the three wires do not share one body dict — the caller passes
the logical request and each transport places the fields where its wire wants them.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

import httpx

from adcp_buyer.core.result import Exchange

# op -> (verb, path template). {media_buy_id} is filled from the body and REMOVED from it.
_ROUTES: dict[str, tuple[str, str]] = {
    "get_products": ("POST", "/api/v1/products"),
    "get_capabilities": ("GET", "/api/v1/capabilities"),
    "list_creative_formats": ("POST", "/api/v1/creative-formats"),
    "list_authorized_properties": ("POST", "/api/v1/authorized-properties"),
    "create_media_buy": ("POST", "/api/v1/media-buys"),
    "update_media_buy": ("PUT", "/api/v1/media-buys/{media_buy_id}"),
    "get_media_buy_delivery": ("POST", "/api/v1/media-buys/delivery"),
    "sync_creatives": ("POST", "/api/v1/creatives/sync"),
    "list_creatives": ("POST", "/api/v1/creatives"),
    "list_accounts": ("POST", "/api/v1/accounts"),
    "sync_accounts": ("POST", "/api/v1/accounts/sync"),
}


class RestRequestError(Exception):
    """The request could not be built. Deterministic — retrying it cannot help.

    Distinct from a wire failure on purpose: this is raised before anything is sent, and the
    durable step must not burn its retries on it (a bare KeyError here previously cost three
    attempts against an error that would never resolve).
    """


def _path_params(template: str) -> list[str]:
    """The {name} placeholders in a path template, in order."""
    parts, rest = [], template
    while "{" in rest:
        name, _, rest = rest.partition("{")[2].partition("}")
        parts.append(name)
    return parts


class RestTransport:
    def __init__(self, base_url: str, token: str, tenant: str, *, timeout: float = 60.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.tenant = tenant
        # Redirects are NOT followed. httpx turns a 302 on POST into a GET of the target, so
        # a failed money POST came back as a 200 with someone else's HTML body — and the
        # seller does mount a Flask app at "/" behind the API routes, so this is reachable.
        # A redirect on an AdCP operation is itself a protocol fault worth recording.
        self._client = httpx.Client(timeout=timeout, follow_redirects=False)

    def _headers(self, *, authed: bool = True) -> dict[str, str]:
        h = {"x-adcp-tenant": self.tenant}
        if authed:
            h["x-adcp-auth"] = self.token
        return h

    def _build_url(self, template: str, body: dict[str, Any]) -> str:
        """Fill the path template, percent-encoding each value and removing it from the body.

        Encoding matters because ids are seller-supplied data the buyer echoes back: an id
        containing `/`, `..`, `?` or `#` otherwise rewrites the request target, and a
        mutating PUT lands on a different resource entirely.
        """
        path = template
        for name in _path_params(template):
            if name not in body:
                raise RestRequestError(f"{name} is required in the request body for this route")
            value = body.pop(name)
            if not isinstance(value, str) or not value:
                raise RestRequestError(f"{name} must be a non-empty string, got {value!r}")
            path = path.replace("{" + name + "}", quote(value, safe=""))
        return f"{self.base_url}{path}"

    def call(self, op: str, body: dict[str, Any] | None = None, *, authed: bool = True) -> Exchange:
        if op not in _ROUTES:
            raise RestRequestError(f"unknown REST op: {op}")
        verb, path_t = _ROUTES[op]
        logical = dict(body or {})
        sent = dict(logical)  # path params are popped out of THIS one
        url = self._build_url(path_t, sent)

        if verb == "GET":
            resp = self._client.get(url, headers=self._headers(authed=authed))
        elif verb == "PUT":
            resp = self._client.put(url, headers=self._headers(authed=authed), json=sent)
        else:
            resp = self._client.post(url, headers=self._headers(authed=authed), json=sent)

        # A redirect is never a valid AdCP response. Record it as an error rather than
        # chasing it, and keep the Location so the fault is diagnosable.
        if 300 <= resp.status_code < 400:
            return Exchange(
                op=op,
                wire="rest",
                request=logical,
                status_code=resp.status_code,
                wire_error_envelope={
                    "adcp_error": {
                        "code": "UNEXPECTED_REDIRECT",
                        "message": f"{verb} {url} returned {resp.status_code}",
                        "location": resp.headers.get("location"),
                    },
                    "errors": [{"code": "UNEXPECTED_REDIRECT"}],
                },
                idempotency_key=logical.get("idempotency_key"),
                raw_body=resp.text[:8000],
                extra={"sent_body": sent, "url": url},
            )

        try:
            parsed = resp.json()
        except ValueError:
            parsed = None

        # A two-layer AdCP error envelope carries adcp_error + errors; otherwise it's a payload.
        envelope = parsed if isinstance(parsed, dict) and "adcp_error" in parsed else None
        payload = parsed if isinstance(parsed, dict) and envelope is None else None

        # Anything we could not read as a dict payload or an envelope stays UNCLASSIFIED --
        # never a synthesized payload, which is_success would then read as a completed call.
        reason = None
        if envelope is None and payload is None:
            reason = (
                "response body is not JSON"
                if parsed is None
                else f"JSON body is {type(parsed).__name__}, not an object"
            )

        return Exchange(
            op=op,
            wire="rest",
            request=logical,
            status_code=resp.status_code,
            wire_response=payload,
            wire_error_envelope=envelope,
            idempotency_key=logical.get("idempotency_key"),
            raw_body=resp.text[:8000],
            unclassified_reason=reason,
            extra={"sent_body": sent, "url": url},
        )

    def close(self) -> None:
        self._client.close()
