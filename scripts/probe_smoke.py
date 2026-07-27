"""P0 gate: prove a buyer can reach and authenticate against all three wires.

Byte-faithful on purpose — no SDK, no typed models. This is the smallest thing that
proves the transport approach works against a live salesagent, and it is the harness
the B2 wire-differential probe grows out of.

Run:  uv run python scripts/probe_smoke.py
"""

from __future__ import annotations

import asyncio
import json
import uuid

import httpx

BASE = "http://localhost:8092"
TOKEN = "ci-test-token"
TENANT = "ci-test"
HEADERS = {"x-adcp-auth": TOKEN, "x-adcp-tenant": TENANT}


def _show(label: str, status: object, body: str, limit: int = 260) -> None:
    flat = " ".join(str(body).split())
    print(f"  {label:<34} {status}  {flat[:limit]}")


def probe_rest(client: httpx.Client) -> None:
    print("\n[REST]  /api/v1  (POST-with-body for reads; only /capabilities is GET)")
    r = client.get(f"{BASE}/api/v1/capabilities", headers=HEADERS)
    _show("GET capabilities", r.status_code, r.text)

    r = client.post(f"{BASE}/api/v1/products", headers=HEADERS, json={"brief": "video ads"})
    _show("POST products (authed)", r.status_code, r.text)

    # Auth-disposition probe: same call, no token. Feeds the AUTH-DISPOSITION oracle.
    r = client.post(f"{BASE}/api/v1/products", json={"brief": "video ads"})
    _show("POST products (no auth)", r.status_code, r.text)

    # Error-envelope probe: an op that requires auth. Expect the two-layer envelope.
    r = client.post(f"{BASE}/api/v1/creatives", json={})
    _show("POST creatives (no auth)", r.status_code, r.text)


def probe_mcp(client: httpx.Client) -> None:
    """Raw MCP over streamable-http. Handshake by hand so we capture wire bytes."""
    print("\n[MCP]   /mcp/  (JSON-RPC over streamable-http)")
    mcp_headers = {
        **HEADERS,
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    init = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "adcp-buyer-agent-probe", "version": "0.0.1"},
        },
    }
    r = client.post(f"{BASE}/mcp/", headers=mcp_headers, json=init)
    session = r.headers.get("mcp-session-id")
    _show("initialize", r.status_code, f"session={session} {r.text}")
    if not session:
        print("  !! no mcp-session-id; cannot continue MCP probe")
        return

    sess_headers = {**mcp_headers, "mcp-session-id": session}
    client.post(
        f"{BASE}/mcp/",
        headers=sess_headers,
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
    )

    r = client.post(
        f"{BASE}/mcp/",
        headers=sess_headers,
        json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    )
    names: list[str] = []
    for line in r.text.splitlines():
        if line.startswith("data: "):
            try:
                payload = json.loads(line[6:])
                names = [t["name"] for t in payload.get("result", {}).get("tools", [])]
            except (json.JSONDecodeError, KeyError, TypeError):
                pass
    _show("tools/list", r.status_code, f"{len(names)} tools: {sorted(names)}")

    r = client.post(
        f"{BASE}/mcp/",
        headers=sess_headers,
        json={
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "get_products", "arguments": {"brief": "video ads"}},
        },
    )
    _show("tools/call get_products", r.status_code, r.text)


def probe_a2a(client: httpx.Client) -> None:
    """Raw A2A JSON-RPC. Skills ride inside message/send as a DataPart."""
    print("\n[A2A]   /a2a  (skills carried in message/send DataParts)")
    r = client.get(f"{BASE}/.well-known/agent-card.json")
    skills: list[str] = []
    if r.status_code == 200:
        try:
            skills = [s.get("id") or s.get("name") for s in r.json().get("skills", [])]
        except (json.JSONDecodeError, AttributeError):
            pass
    _show("agent-card", r.status_code, f"{len(skills)} advertised: {sorted(skills)}")

    body = {
        "jsonrpc": "2.0",
        "id": str(uuid.uuid4()),
        "method": "message/send",
        "params": {
            "message": {
                "messageId": str(uuid.uuid4()),
                "role": "user",
                "parts": [{"data": {"skill": "get_products", "input": {"brief": "video ads"}}}],
            }
        },
    }
    r = client.post(
        f"{BASE}/a2a", headers={**HEADERS, "Content-Type": "application/json"}, json=body
    )
    _show("message/send get_products", r.status_code, r.text)

    # #1670 probe: an unknown skill should raise MethodNotFoundError (-32601). If the
    # v0.3 compat adapter flattens it to -32603, that is the bug reproducing.
    body["params"]["message"]["parts"][0]["data"]["skill"] = "no_such_skill_xyz"
    body["id"] = str(uuid.uuid4())
    r = client.post(
        f"{BASE}/a2a", headers={**HEADERS, "Content-Type": "application/json"}, json=body
    )
    _show("message/send unknown skill", r.status_code, r.text)


def main() -> None:
    print(f"probing {BASE} as tenant={TENANT}")
    with httpx.Client(timeout=60, follow_redirects=True) as client:
        r = client.get(f"{BASE}/health")
        _show("health", r.status_code, r.text)
        probe_rest(client)
        probe_mcp(client)
        probe_a2a(client)


if __name__ == "__main__":
    asyncio.run(asyncio.sleep(0))  # keep the module import-compatible with async harnesses
    main()
