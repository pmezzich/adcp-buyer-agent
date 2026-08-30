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
        parts = _data_parts(task) if isinstance(task, dict) else []
        payload = _extract_data_part(task)
        # Recorded so a multi-artifact response is visible rather than silently collapsed.
        meta: dict[str, Any] = {"artifact_count": len(parts)}
        if task_state:
            meta["task_state"] = task_state
        if isinstance(payload, dict) and "adcp_error" in payload:
            return Exchange(
                op=op,
                wire="a2a",
                request=body,
                status_code=resp.status_code,
                wire_error_envelope=payload,
                idempotency_key=body.get("idempotency_key"),
                raw_body=resp.text[:8000],
                extra=meta,
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
                extra=meta,
            )
        return Exchange(
            op=op,
            wire="a2a",
            request=body,
            status_code=resp.status_code,
            wire_response=payload,
            idempotency_key=body.get("idempotency_key"),
            raw_body=resp.text[:8000],
            extra=meta,
        )

    def close(self) -> None:
        self._client.close()


def _data_parts(result: dict[str, Any]) -> list[Any]:
    """Every structured DataPart in the Task, in artifact order."""
    out = []
    for artifact in result.get("artifacts") or []:
        for part in artifact.get("parts") or []:
            # A2A DataParts arrive as {kind:'data', data:{...}} or a bare {data:{...}}
            if "data" in part and part.get("kind", "data") == "data":
                out.append(part["data"])
    return out


def _extract_data_part(result: dict[str, Any]) -> Any:
    """The authoritative DataPart of a Task: an error envelope if there is one, else the first.

    The seller appends one artifact per skill result in invocation order, so a task can carry
    a success payload AND an error envelope. Returning the first match made every error
    artifact after a success unreachable, and a FAILED task reported success. An envelope
    anywhere in the task is the answer.
    """
    parts = _data_parts(result)
    for data in parts:
        if isinstance(data, dict) and "adcp_error" in data:
            return data
    return parts[0] if parts else None
