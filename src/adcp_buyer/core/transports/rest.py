"""Hand-rolled REST transport.

The adcp SDK's Protocol enum is MCP + A2A only — REST is not in the SDK — so this is a
byte-faithful httpx client mirroring salesagent's src/routes/api_v1.py. Reads are
POST-with-body; only /capabilities is GET; update_media_buy is the one PUT. Returns the
normalized Exchange so the buyer (and later the differential fuzzer) read one shape.
"""

from __future__ import annotations

from typing import Any

import httpx

from adcp_buyer.core.result import Exchange

# op -> (verb, path template). {id} is filled from the body's "media_buy_id".
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


class RestTransport:
    def __init__(self, base_url: str, token: str, tenant: str, *, timeout: float = 60.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.tenant = tenant
        self._client = httpx.Client(timeout=timeout, follow_redirects=True)

    def _headers(self, *, authed: bool = True) -> dict[str, str]:
        h = {"x-adcp-tenant": self.tenant}
        if authed:
            h["x-adcp-auth"] = self.token
        return h

    def call(self, op: str, body: dict[str, Any] | None = None, *, authed: bool = True) -> Exchange:
        if op not in _ROUTES:
            raise KeyError(f"unknown REST op: {op}")
        verb, path_t = _ROUTES[op]
        body = dict(body or {})
        path = path_t.format(**body) if "{" in path_t else path_t
        url = f"{self.base_url}{path}"

        if verb == "GET":
            resp = self._client.get(url, headers=self._headers(authed=authed))
        elif verb == "PUT":
            resp = self._client.put(url, headers=self._headers(authed=authed), json=body)
        else:
            resp = self._client.post(url, headers=self._headers(authed=authed), json=body)

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
            request=body,
            status_code=resp.status_code,
            wire_response=payload,
            wire_error_envelope=envelope,
            idempotency_key=body.get("idempotency_key"),
            raw_body=resp.text[:8000],
            unclassified_reason=reason,
        )

    def close(self) -> None:
        self._client.close()
