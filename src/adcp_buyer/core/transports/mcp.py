"""Hand-rolled MCP transport (streamable-http).

The op name IS the MCP tool name; the body is the tool arguments. One session per
instance (initialize handshake -> session id -> notifications/initialized -> tools/call).
Returns the normalized Exchange: structuredContent on success, the two-layer AdCP envelope
(carried as JSON text on an isError result) on failure.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from adcp_buyer.core.result import Exchange


def _parse_sse(text: str, want_id: Any = None) -> dict[str, Any]:
    """Return the JSON-RPC object answering ``want_id`` from an SSE or plain response body.

    A stream carries progress notifications and, on a reused connection, frames belonging to
    other requests. Taking the first id-bearing frame meant another request's answer could be
    read as this one's -- on a create, that attributes someone else's media_buy_id to this
    buy. When ``want_id`` is given, only a frame carrying that id is accepted.
    """
    frames: list[dict[str, Any]] = []
    for line in text.splitlines():
        if line.startswith("data: "):
            try:
                obj = json.loads(line[6:])
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict) and ("result" in obj or "error" in obj or "id" in obj):
                frames.append(obj)
    if not frames:
        try:
            obj = json.loads(text)
        except json.JSONDecodeError:
            return {}
        frames = [obj] if isinstance(obj, dict) else []

    if want_id is None:
        return frames[0] if frames else {}
    for obj in frames:
        if obj.get("id") == want_id:
            return obj
    return {}


class McpTransport:
    def __init__(self, base_url: str, token: str, tenant: str, *, timeout: float = 60.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.tenant = tenant
        self._client = httpx.Client(timeout=timeout, follow_redirects=True)
        self._session: str | None = None
        #: The auth posture the cached session was minted under. A session established by an
        #: authenticated initialize must not be reused for an unauthenticated call: the
        #: server may authorize on session identity, so `authed=False` would silently ride
        #: the authenticated session and an auth-boundary probe would read a false pass.
        self._session_authed: bool | None = None
        self._next_id = 0

    @property
    def _url(self) -> str:
        return f"{self.base_url}/mcp/"

    def _headers(self, *, authed: bool = True) -> dict[str, str]:
        h = {
            "x-adcp-tenant": self.tenant,
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if authed:
            h["x-adcp-auth"] = self.token
        if self._session:
            h["mcp-session-id"] = self._session
        return h

    def _rpc_id(self) -> int:
        self._next_id += 1
        return self._next_id

    def _ensure_session(self, *, authed: bool) -> None:
        if self._session is not None and self._session_authed == authed:
            return
        self._session = None
        init = {
            "jsonrpc": "2.0",
            "id": self._rpc_id(),
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "adcp-buyer-agent", "version": "0.0.1"},
            },
        }
        resp = self._client.post(self._url, headers=self._headers(authed=authed), json=init)
        self._session = resp.headers.get("mcp-session-id")
        self._session_authed = authed if self._session else None
        if self._session:
            self._client.post(
                self._url,
                headers=self._headers(authed=authed),
                json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            )

    def call(self, op: str, body: dict[str, Any] | None = None, *, authed: bool = True) -> Exchange:
        body = dict(body or {})
        self._ensure_session(authed=authed)
        rpc_id = self._rpc_id()
        req = {
            "jsonrpc": "2.0",
            "id": rpc_id,
            "method": "tools/call",
            "params": {"name": op, "arguments": body},
        }
        resp = self._client.post(self._url, headers=self._headers(authed=authed), json=req)
        rpc = _parse_sse(resp.text, want_id=rpc_id)
        if resp.status_code >= 400:
            # The session (if any) may be gone; force a fresh initialize next time rather
            # than replaying a dead mcp-session-id forever.
            self._session = None
            self._session_authed = None

        # No JSON-RPC frame at all (401 HTML, 502 from a proxy, "Missing session ID", an
        # empty body). Classifying this as anything but UNCLASSIFIED is how a failed
        # mutating call reads as a completed one.
        if not rpc:
            return Exchange(
                op=op,
                wire="mcp",
                request=body,
                status_code=resp.status_code,
                idempotency_key=body.get("idempotency_key"),
                raw_body=resp.text[:8000],
                unclassified_reason=(
                    f"no JSON-RPC frame answering request id {rpc_id} in the response body"
                ),
            )
        result = rpc.get("result") or {}

        # isError -> the content text is the two-layer AdCP envelope
        if result.get("isError"):
            envelope = _first_text_json(result) or {"adcp_error": {"code": "MCP_ERROR"}}
            return Exchange(
                op=op,
                wire="mcp",
                request=body,
                status_code=resp.status_code,
                wire_error_envelope=envelope,
                idempotency_key=body.get("idempotency_key"),
                raw_body=resp.text[:8000],
            )
        # JSON-RPC transport error (e.g. unknown tool)
        if "error" in rpc:
            err = rpc["error"]
            return Exchange(
                op=op,
                wire="mcp",
                request=body,
                status_code=resp.status_code,
                wire_error_envelope={
                    "adcp_error": {
                        "code": "JSONRPC_ERROR",
                        "jsonrpc_code": err.get("code"),
                        "message": err.get("message"),
                    },
                    "errors": [{"code": "JSONRPC_ERROR"}],
                },
                raw_body=resp.text[:8000],
            )
        payload = result.get("structuredContent")
        if payload is None:
            payload = _first_text_json(result)
        if not isinstance(payload, dict):
            # A JSON-RPC result we cannot read as a payload is not a success. The previous
            # {"content": ...} wrapper made every such response look like one.
            return Exchange(
                op=op,
                wire="mcp",
                request=body,
                status_code=resp.status_code,
                idempotency_key=body.get("idempotency_key"),
                raw_body=resp.text[:8000],
                unclassified_reason="JSON-RPC result carried no structuredContent and no JSON text part",
            )
        return Exchange(
            op=op,
            wire="mcp",
            request=body,
            status_code=resp.status_code,
            wire_response=payload,
            idempotency_key=body.get("idempotency_key"),
            raw_body=resp.text[:8000],
        )

    def close(self) -> None:
        self._client.close()


def _first_text_json(result: dict[str, Any]) -> dict[str, Any] | None:
    for item in result.get("content") or []:
        if item.get("type") == "text":
            try:
                return json.loads(item.get("text", ""))
            except json.JSONDecodeError:
                return None
    return None
