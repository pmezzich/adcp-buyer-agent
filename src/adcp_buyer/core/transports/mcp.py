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


def _parse_sse(text: str) -> dict[str, Any]:
    """Return the JSON-RPC object from a streamable-http (SSE or plain) response body."""
    for line in text.splitlines():
        if line.startswith("data: "):
            try:
                obj = json.loads(line[6:])
                if isinstance(obj, dict) and ("result" in obj or "error" in obj or "id" in obj):
                    return obj
            except json.JSONDecodeError:
                continue
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {}


class McpTransport:
    def __init__(self, base_url: str, token: str, tenant: str, *, timeout: float = 60.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.tenant = tenant
        self._client = httpx.Client(timeout=timeout, follow_redirects=True)
        self._session: str | None = None
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
        if self._session is not None:
            return
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
        if self._session:
            self._client.post(
                self._url,
                headers=self._headers(authed=authed),
                json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            )

    def call(self, op: str, body: dict[str, Any] | None = None, *, authed: bool = True) -> Exchange:
        body = dict(body or {})
        self._ensure_session(authed=authed)
        req = {
            "jsonrpc": "2.0",
            "id": self._rpc_id(),
            "method": "tools/call",
            "params": {"name": op, "arguments": body},
        }
        resp = self._client.post(self._url, headers=self._headers(authed=authed), json=req)
        rpc = _parse_sse(resp.text)
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
            )
        payload = result.get("structuredContent")
        if payload is None:
            payload = _first_text_json(result) or {"content": result.get("content")}
        return Exchange(
            op=op,
            wire="mcp",
            request=body,
            status_code=resp.status_code,
            wire_response=payload,
            idempotency_key=body.get("idempotency_key"),
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
