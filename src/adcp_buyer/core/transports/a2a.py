"""Hand-rolled A2A transport (JSON-RPC over HTTP).

AdCP operations ride as *skills* inside a single message/send: the message carries a
DataPart {skill: <op>, input: <body>}. The op name is the skill name. Responses come back
three ways, all normalized here to one Exchange:
  * success  -> a Task whose artifact DataPart carries the payload
  * AdCP-domain failure -> a Task/artifact DataPart carrying the two-layer envelope
  * transport failure -> a JSON-RPC top-level `error` (note: salesagent's v0.3 compat adapter
    flattens typed A2AError codes to -32603, gh-1670 — captured verbatim, not masked)
"""

from __future__ import annotations

import uuid
from typing import Any

import httpx

from adcp_buyer.core.result import Exchange


class A2aTransport:
    def __init__(self, base_url: str, token: str, tenant: str, *, timeout: float = 60.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.tenant = tenant
        self._client = httpx.Client(timeout=timeout, follow_redirects=True)

    def _headers(self, *, authed: bool = True) -> dict[str, str]:
        h = {"x-adcp-tenant": self.tenant, "Content-Type": "application/json"}
        if authed:
            h["x-adcp-auth"] = self.token
        return h

    def call(self, op: str, body: dict[str, Any] | None = None, *, authed: bool = True) -> Exchange:
        body = dict(body or {})
        req = {
            "jsonrpc": "2.0",
            "id": str(uuid.uuid4()),
            "method": "message/send",
            "params": {
                "message": {
                    "messageId": str(uuid.uuid4()),
                    "role": "user",
                    "parts": [{"data": {"skill": op, "input": body}}],
                }
            },
        }
        resp = self._client.post(
            f"{self.base_url}/a2a", headers=self._headers(authed=authed), json=req
        )
        try:
            rpc = resp.json()
        except ValueError:
            rpc = {}

        if not rpc:
            return Exchange(
                op=op,
                wire="a2a",
                request=body,
                status_code=resp.status_code,
                idempotency_key=body.get("idempotency_key"),
                raw_body=resp.text[:8000],
                unclassified_reason="response body is not JSON-RPC",
            )

        if "error" in rpc:  # JSON-RPC transport/protocol error
            err = rpc["error"]
            return Exchange(
                op=op,
                wire="a2a",
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

        task = rpc.get("result") or {}
        task_state = ((task.get("status") or {}).get("state")) if isinstance(task, dict) else None
        payload = _extract_data_part(task)
        if isinstance(payload, dict) and "adcp_error" in payload:
            return Exchange(
                op=op,
                wire="a2a",
                request=body,
                status_code=resp.status_code,
                wire_error_envelope=payload,
                idempotency_key=body.get("idempotency_key"),
                raw_body=resp.text[:8000],
                extra={"task_state": task_state} if task_state else {},
            )
        if not isinstance(payload, dict):
            # A Task with no readable DataPart is NOT a success. The seller's manual-approval
            # shape (state=submitted, artifacts cleared) lands here; wrapping it as
            # {"_payload": None} previously made a pending buy report as a completed one.
            return Exchange(
                op=op,
                wire="a2a",
                request=body,
                status_code=resp.status_code,
                idempotency_key=body.get("idempotency_key"),
                raw_body=resp.text[:8000],
                unclassified_reason=(
                    f"Task carried no data part (state={task_state!r})"
                    if task_state
                    else "Task carried no data part"
                ),
                extra={"task_state": task_state} if task_state else {},
            )
        return Exchange(
            op=op,
            wire="a2a",
            request=body,
            status_code=resp.status_code,
            wire_response=payload,
            idempotency_key=body.get("idempotency_key"),
            raw_body=resp.text[:8000],
            extra={"task_state": task_state} if task_state else {},
        )

    def close(self) -> None:
        self._client.close()


def _extract_data_part(result: dict[str, Any]) -> Any:
    """Pull the structured DataPart out of a Task's latest artifact."""
    for artifact in result.get("artifacts") or []:
        for part in artifact.get("parts") or []:
            # A2A DataParts arrive as {kind:'data', data:{...}} or a bare {data:{...}}
            if "data" in part and part.get("kind", "data") == "data":
                return part["data"]
    return None
