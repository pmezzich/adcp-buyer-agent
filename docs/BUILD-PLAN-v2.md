# adcp-buyer-agent — Canonical Build Plan **v2**

**Status:** hand-off ready. Supersedes BUILD-PLAN v1 in full. v1 is retained only for archaeology; every A2A row, the entire async/webhook section, the dry-run treatment, three module boundaries, and eleven oracles are changed. Where v1 and v2 disagree, **v2 is correct** — v1's A2A and webhook sections were built from a digest summary that has since been refuted against source.
**Repo:** `adcp-buyer-agent` — private, `uv`-managed, Python 3.12, Windows/PowerShell primary.
**Target under test:** `C:\Users\pmezz\projects\salesagent` running in Docker, over real HTTP.
**Hard pin:** `adcp==5.7.0` (AdCP spec `3.1.0-beta.3`) — must match the seller exactly.

---

## 0. What changed from v1, and why you must not skip this

Nine v1 premises were verified false against salesagent source. Every one of them would have shipped an oracle that fires on correct-by-design behavior, or a gate that cannot pass.

| # | v1 said | Source says | Consequence for v2 |
|---|---|---|---|
| A | "18 A2A skills (agent card)" | 18 **dispatchable** (`skill_handlers`, `adcp_a2a_server.py:1402-1429`); **16 advertised** (`create_agent_card()`, `:2129-2234`). Different sets. | `advertised` and `dispatchable` become orthogonal registry fields; drift test asserts 18 and 16 separately |
| B | "`get_creatives` is one of 6 stubs" | `_handle_get_creatives_skill` (`:1698`) is **orphaned** — not a `skill_handlers` value, zero references in `src/`. Invoking it hits the guard at `:1431` → `MethodNotFoundError` **-32601** | New `Availability.UNDISPATCHABLE`; delete the `get_creatives` UNSUPPORTED_FEATURE case |
| C | "6 stubs, capability oracle free over 6" | **5** reachable stubs; only **3** advertised (`approve_creative`, `get_media_buy_status`, `optimize_media_buy`). `create_creative`/`assign_creative` are dispatchable-but-**unadvertised** | `CAPABILITY-TRUTHFULNESS` covers 3; new mirror oracle `CAPABILITY-CONCEALMENT` covers 2 |
| D | "`tasks/get` on a random id ⟹ `-32001`, a #1670 case" | `on_get_task` (`:1005-1020`) is `return self.tasks.get(task_id)` — **no raise**. The -32001 is manufactured by the SDK (`jsonrpc_dispatcher.py:428`). Not a salesagent typed error. | #1670 case set rebuilt from 12 methods that *do* raise; `tasks/get` gets its own oracle `TASK-GET-NULLRESULT` |
| E | "REST fails loudly on unknown fields in dev" | All 11 `*Body` models are bare `pydantic.BaseModel` (`api_v1.py:64-170`), `extra=None` under **both** `ENVIRONMENT` values | Extra-field policy is declared per `(wire, op)` in the registry, never probed per server |
| F | "REST `buying_mode` drop is a FIELD-REACHABILITY finding" | `buying_mode` is unreachable on **all three** wires — `create_get_products_request()` (`schema_helpers.py:164-204`) accepts only brief/brand/filters/property_list/context | Removed from the known-issue gate; replaced by A2A `list_authorized_properties` dropping `property_tags`/`publisher_domains` |
| G | "REST `packages=[]` vs SDK `min_length=1` is a divergence" | salesagent's override (`_base.py:1529`) **drops** the library's `MinLen(1)`. `[]` and `None` validate identically | Case deleted. The real divergence is A2A's presence check at `:1565-1571` |
| H | "INV-07 asserts `AUTH_TOKEN_INVALID → 401`" | Three wire codes are status-**ambiguous** after `ERROR_CODE_MAPPING` translation | No oracle hardcodes a status; INV-08 owns status as an allowed **set** |
| I | "MCP `create_media_buy` completes via webhook" | `_send_protocol_webhook` reads **only** the in-memory `_task_push_configs` dict (A2A `message/send` only). MCP's webhook comes from `ContextManager._send_push_notifications` and requires a DB `PushNotificationConfig` row **created by the same request** | Rendezvous priority inverted per wire; §5 rewritten end to end |

Preserved unchanged from v1 (the critique affirmed all four):

1. **Three hand-rolled byte-faithful transports** as the single dispatch layer; the adcp SDK supplies models/constants/JCS **only**, never wire.
2. **Wire stratum (raw dicts) + typed stratum (codec)** over one core, so the fuzzer and the buyer share it.
3. **One `Exchange` observation type → one oracle library (arity-1 + arity-N parity) → two consumers**, with the buyer running the same oracles in guard mode.
4. **Two-tier normalization with `norm_log` in every finding**, and the **verify-then-report gate**.

---

## 1. Architecture overview

### 1.1 The three decisions everything else follows from

**Decision 1 — The SDK supplies models, not wire.** `adcp==5.7.0` gives us ~400 generated pydantic models, `STANDARD_ERROR_CODES`, `INTERNAL_CODES`, `MEDIA_BUY_STATE_MACHINE`, `valid_actions_for_status()`, `IDEMPOTENT_TASKS`, `to_wire_dict`, and RFC-8785 canonicalization. All of that is imported, never retyped. But the SDK's *transport* layer is lossy in ways that are fatal to a fuzzer:

- `TaskResult` carries only `adcp_error`; the `errors[]` mirror layer is **dropped** (`adcp/types/core.py:178`; `protocols/mcp.py:557-596` reconstructs `DebugInfo.response` rather than capturing wire bytes). Bug #1449 is literally "MCP ships advisory `errors[]` that A2A/REST drop" — an SDK-based fuzzer is structurally blind to it.
- The a2a-sdk client raises typed exceptions, hiding the JSON-RPC `error.code`, so #1670's flattening is invisible; its protobuf round-trip re-coerces numerics, masking #1583.
- SDK `TaskStatus` has 5 members; the AdCP 3.1 lifecycle has 9. A2A `working`/`input-required`/`auth-required` all collapse to `SUBMITTED` (`protocols/a2a.py:673-694`).
- `_idempotency.resolve_key()` **auto-mints a fresh UUID4** for any mutating tool when the key is absent — a naive retry through the SDK double-books.
- `Protocol` enum is `{MCP, A2A}` only. REST does not exist in the SDK at all.
- The SDK client cannot address the A2A JSON-RPC *method* surface at all (`ListTasks`, `tasks/pushNotificationConfig/*`, `GetExtendedAgentCard`), which is where four of the twelve viable #1670 cases live.

So: **three hand-rolled, byte-faithful raw transports** (~350 LOC total) are the single dispatch layer for both sides. The SDK's `ADCPClient` appears exactly once, as an optional **cross-check adapter** used only to diff SDK-emitted bytes against ours in a contract test.

**Decision 2 — Two strata over one transport layer.** The interchange currency is a plain `dict`.

- **Wire stratum** — `dict → bytes → dict`, no validation, no coercion. The fuzzer's home; it must be able to send `buying_mode: 17`, a 300-char idempotency key, a 12-deep envelope, and a duplicate JSON key.
- **Typed stratum** — `codec.encode(op, **kwargs) -> dict` and `codec.decode(op, body, status) -> BaseModel`, bolted onto the ends. The buyer's home.

Typed models are never a mandatory chokepoint. This is what lets one core serve both consumers.

**Decision 3 — One observation type, one oracle library, three engines, two consumers.** Every call on every wire — and every inbound webhook — produces an `Exchange`. Every oracle takes an `Exchange` (arity 1) or an `ExchangeSet` (arity N) — never a live client — so oracles are unit-testable against hand-written envelopes and are themselves testable. The fuzzer's three engines all funnel into the same oracle library and the same finding pipeline, which now lives in **`core/findings/`** so the buyer never imports `fuzz/`. The buyer runs **the same oracle library in `guard` mode**, logging violations instead of raising.

### 1.2 Layer diagram

```
                    ┌──────────────────────────────────────────────────┐
                    │ CLI (typer): doctor · stack · seed · probe       │
                    │              buy · fuzz · replay · sink · report │
                    └───────────────────┬──────────────────────────────┘
          ┌─────────────────────────────┴──────────────────────────────┐
 ┌────────▼─────────────────────┐                     ┌────────────────▼──────────────┐
 │ fuzz/                        │                     │ buyer/                        │
 │  E1 differential (cross-wire)│                     │  brain (Claude/pydantic-ai)   │
 │  E2 schema/boundary (hypoth.)│                     │  planner · preflight          │
 │  E3 stateful sequence        │                     │  executor (deterministic)     │
 │  corpus · shrink             │                     │  monitor · guard              │
 │  known_issues · report ·repro│                     │                               │
 └────────┬─────────────────────┘                     └────────────────┬──────────────┘
          │                ┌───────────────────────────────┐           │ guard mode
          ├───────────────►│ core/oracles/  — 36 invariants│◄──────────┤
          │                │ arity-1 + arity-N (parity)    │           │
          │                └───────────────┬───────────────┘           │
          │                ┌───────────────▼───────────────┐           │
          └───────────────►│ core/findings/ Finding·sig·   │◄──────────┘
                           │  Violation·DiffEntry·store    │
                           └───────────────┬───────────────┘
                                           │  Exchange / ExchangeSet
                           ┌───────────────▼───────────────┐
                           │ core/normalize/               │ two-tier, norm_log,
                           │ pipeline · rules              │ (canonical lives in spec/)
                           └───────────────┬───────────────┘
   TYPED STRATUM  ─────────────────────────┼───────────────────── WIRE STRATUM
   core/codec.py (adcp models)  ┌──────────▼──────────┐  raw dicts, byte-faithful
   core/capabilities.py         │  core/client.py     │  .call()  .call_all()
   core/idempotency.py (vault)  │  core/ops.py        │
   core/tasks/ (lifecycle,      │  core/session.py    │
     ledger, sink, poller,      └─┬──────┬──────┬───┬─┘
     rendezvous)     ┌────────────▼┐ ┌───▼───┐ ┌▼───────────┐ │
                     │transport/mcp│ │tr./a2a│ │transport/  │ │ inbound
                     │fastmcp+httpx│ │httpx  │ │  rest      │ │ webhooks
                     │  tap        │ │JSONRPC│ │  /api/v1   │ │   ▲
                     └─────────────┴─┴───────┴─┴────────────┘ │   │
                                   │ real HTTP                 └───┤
                    ┌──────────────▼──────────────────────────────┐│
                    │ harness/ : docker stack, seed, arena, doctor ││
                    │ salesagent  :8000 (dev) | :8092 (e2e)        ├┘
                    │ DELIVERY_WEBHOOK_INTERVAL=5 overlay          │
                    └──────────────────────────────────────────────┘
```

### 1.3 The asymmetry map — rebuilt from source

**All three counts are drift-tested against the live server, never trusted from this document.**

| Surface | Count | Source of truth | Drift assertion |
|---|---|---|---|
| MCP tools | **unpinned at P0 — pinned by P2b** | `src/core/main.py:305-332` `_register_tool` | `list_tools()` enumeration captured live; the count is written into `ops.py` **by the P2b gate**, not from memory |
| A2A dispatchable skills | **18** | `adcp_a2a_server.py:1402-1429` `skill_handlers` | separate assertion, exact set |
| A2A advertised skills | **16** | `adcp_a2a_server.py:2129-2234` `create_agent_card()` | separate assertion, exact set |
| A2A JSON-RPC methods | **21** (11 v1.0 + 10 v0.3 aliases) | `jsonrpc_dispatcher.py:115-127` + `compat/v0_3/jsonrpc_adapter.py:53-64`, both live because `enable_v0_3_compat=True` (`src/app.py:298-303`) | probe-based |
| REST routes | **12** | `src/routes/api_v1.py` `APIRouter(prefix="/api/v1")` | enumerate from the router, **not** from `/openapi.json` |

**MCP count hazard (must be resolved at P2b, hard gate).** v1 asserted "16 MCP tools" while simultaneously listing 13 REST-shared ops + `get_media_buys` + `list_tasks`/`get_task`/`complete_task` = 17 MCP-reachable names. These cannot both be true. P2b's gate is: enumerate `list_tools()` live, write the exact tuple into `core/ops.py::MCP_TOOL_NAMES`, and fail if any registry row claims an MCP binding not in that tuple. **Do not hardcode 16.**

Load-bearing asymmetries, all verified:

- `get_media_buys`: MCP + A2A, **no REST route** → #1651 is a 2-way differential and must carry a `coverage: single_pair` marker.
- `list_tasks` / `get_task` / `complete_task`: **MCP only**, return a **bare dict** (not `ToolResult`, no response model), hard-require a non-anonymous principal. Zero parity coverage — must be reported as such (§3.7, `coverage:single_wire`).
- **A2A card is a strict subset of dispatch.** Dispatchable-but-unadvertised = `{create_creative, assign_creative}`. Advertised-but-undispatchable = **∅**. One handler (`get_creatives`) is neither.
- **Five reachable stubs** raise `UnsupportedOperationError` → JSON-RPC **-32004** at the top level (it is re-raised untouched through `_handle_explicit_skill:1441-1443` and `on_message_send:632-638`), **not** a failed-Task envelope. Two of them (`create_creative` `:1688-1691`, `assign_creative` `:1720-1727`) validate params first; three raise unconditionally.
- The **natural-language** `create_media_buy` path (`:863-870` → `:2061-2080`) always raises `AdCPCapabilityNotSupportedError` and surfaces as **-32603 with a failed-Task artifact** — a different shape from the -32004 stubs. Never conflate them.
- `GET /api/v1/capabilities` is the only GET. `PUT /api/v1/media-buys/{id}` is the only PUT. Everything else is POST-with-body.
- **REST async is a dead end**: `CreateMediaBuyBody` carries neither `push_notification_config` nor `reporting_webhook` (`api_v1.py:71-79`), and the route forwards only 7 of the raw wrapper's 11 buyer params (`:266-276`). Encoded as `async_capable_on = {MCP, A2A}` **and** `monitorable_on = {MCP, A2A}`.
- **Two Flask admin blueprints also answer under `/api/v1`** (`/api/v1/sync/*` 9 routes, `/api/v1/tenant-management/*` 6 routes) via `app.mount("/", admin_wsgi)` (`src/app.py:85`). They are Admin-UI, not AdCP buyer surface. Any live-probe enumeration will see 27 paths, not 12. **Enumerate from the APIRouter.**

---

## 2. Repo tree

```
adcp-buyer-agent/
├── pyproject.toml                  # py>=3.12; adcp==5.7.0 (HARD pin), httpx, fastmcp~=3.2,
│                                   #   pydantic~=2, pydantic-ai-slim[anthropic], rfc8785,
│                                   #   typer, rich, pyyaml, psycopg[binary], fastapi+uvicorn (sink),
│                                   #   hypothesis(dev), respx(dev), pytest, pytest-asyncio
├── .env.example                    # ANTHROPIC_API_KEY, ADCP_SELLER_BASE, ADCP_TOKEN, ADCP_TENANT,
│                                   #   ADCP_WEBHOOK_PORT, ADCP_WEBHOOK_SECRET, ADCP_WEBHOOK_HOST
├── README.md                       # 10-line quickstart: doctor → stack up → probe → fuzz → buy
├── src/adcp_buyer/
│   ├── config.py                   # pydantic-settings Settings + AgentTarget(base_url,token,tenant)
│   ├── cli.py                      # typer app: doctor|stack|seed|probe|buy|fuzz|replay|sink|report
│   ├── core/                       # ══════════ SHARED PROTOCOL CORE (no LLM, no hypothesis) ══════
│   │   ├── ops.py                  # OpSpec registry — single source of per-op truth (business ops)
│   │   ├── a2a_methods.py          # A2A JSON-RPC protocol methods as first-class arity-1 OpSpecs
│   │   ├── matrix.py               # Availability + WireBinding + field_gaps + drift fixtures
│   │   ├── exchange.py             # Exchange, ExchangeSet, LogicalRequest, NormEvent, Wire, RunMode
│   │   ├── envelope.py             # two-layer envelope parse/validate; TASK_ENVELOPE_FIELDS
│   │   ├── session.py              # AgentSession: creds, base urls, transport factory, context_id
│   │   ├── identity.py             # BuyerIdentity: header assembly (x-adcp-auth / Bearer / tenant)
│   │   ├── client.py               # Client.call() / .call_all() — the ONE front door
│   │   ├── codec.py                # typed encode/decode + CompatProfile application
│   │   ├── compat.py               # CompatProfile: SALESAGENT vs SPEC_LITERAL field rewrites
│   │   ├── capabilities.py         # CapabilityGate over all 3 wires + agent card reconciliation
│   │   ├── idempotency.py          # IdempotencyVault (SQLite) + JCS hash (SDK canonicalize)
│   │   ├── transport/
│   │   │   ├── base.py             # RawTransport Protocol
│   │   │   ├── rest.py             # hand-rolled httpx mirroring api_v1.py (12 routes)
│   │   │   ├── mcp.py              # fastmcp Client over StreamableHttp + httpx wiretap
│   │   │   ├── a2a.py              # hand-rolled JSON-RPC: message/send AND the 21-method surface
│   │   │   ├── sdk_crosscheck.py   # adcp.ADCPClient adapter — contract-test only, never in a run
│   │   │   └── wiretap.py          # httpx.AsyncBaseTransport recording exact request/response bytes
│   │   ├── normalize/
│   │   │   ├── pipeline.py         # ordered NormRule chain + norm_log
│   │   │   └── rules.py            # unwrap_frame, split_envelope, strip_a2a_synthetic,
│   │   │                           #   decamelize (webhook wires), numeric_policy,
│   │   │                           #   drop_volatile, sort_unordered
│   │   ├── spec/
│   │   │   ├── canonical.py        # ◀ MOVED TO P1: rfc8785 JCS wrapper + JSON-Pointer differ +
│   │   │   │                       #   DiffEntry. Pure, ~60 LOC, zero deps. P3a/b/c now truly ‖
│   │   │   ├── codes.py            # STANDARD_ERROR_CODES / INTERNAL_CODES / RECOVERY_BY_CODE /
│   │   │   │                       #   PROJECT_CODES / ERROR_CODE_MAPPING re-exports
│   │   │   ├── status_map.py       # wire-code → ALLOWED HTTP STATUS SET (never a scalar)
│   │   │   ├── state_machine.py    # MEDIA_BUY_STATE_MACHINE / valid_actions_for_status re-export
│   │   │   ├── enums.py            # closed enums from adcp.types.generated_poc.enums
│   │   │   ├── jsonrpc.py          # A2AError class → JSON-RPC code map (from a2a.utils.errors)
│   │   │   └── divergences.yaml    # documented salesagent-vs-SDK deltas; supports op:"*"
│   │   ├── findings/               # ◀ MOVED OUT OF fuzz/: buyer must never import fuzz
│   │   │   ├── model.py            # Finding, Violation, Severity, Confidence
│   │   │   ├── signature.py        # signature() — includes run_mode
│   │   │   └── store.py            # dedupe, occurrence counting, run diffing (SQLite)
│   │   ├── oracles/
│   │   │   ├── registry.py         # Oracle ABC, @oracle decorator, applicability, Phase
│   │   │   ├── taint.py            # canary corpus + whole-response scanner
│   │   │   ├── auth_disposition.py # AuthDisposition — needed by both auth_o and parity_o
│   │   │   ├── envelope_o.py       # INV-01..05, 08, 31
│   │   │   ├── auth_o.py           # INV-07, 30
│   │   │   ├── lifecycle_o.py      # INV-09..16, 32, 33
│   │   │   ├── idempotency_o.py    # INV-17..22 + cross-transport replay
│   │   │   ├── budget_o.py         # INV-23..26
│   │   │   ├── isolation_o.py      # INV-27, 28, 34 (task-store isolation, #1702)
│   │   │   ├── listing_o.py        # INV-29
│   │   │   ├── parity_o.py         # INV-06 family — arity N
│   │   │   ├── capability_o.py     # CAPABILITY-TRUTHFULNESS + CAPABILITY-CONCEALMENT
│   │   │   ├── protocol_o.py       # PROTOCOL-ERROR-FIDELITY, TASK-GET-NULLRESULT,
│   │   │   │                       #   TASK-CANCEL-STATE, PNC-SCOPE
│   │   │   └── webhook_o.py        # arity-1 oracle set for WEBHOOK_* wires
│   │   └── tasks/
│   │       ├── lifecycle.py        # AdcpTaskStatus (9-member SPEC enum) + TERMINAL/PAUSED sets
│   │       ├── ledger.py           # TaskLedger (SQLite): operation_id ↔ task handle ↔ op ↔ wire
│   │       ├── sink.py             # FastAPI webhook receiver (host-side, port 8788)
│   │       ├── poller.py           # per-wire polling (MCP get_task / A2A GetTask)
│   │       └── rendezvous.py       # await_terminal(): PER-WIRE priority, poll ⊕ webhook
│   ├── fuzz/                       # ══════════ SIDE 1: THE FUZZER ══════════
│   │   ├── engines/{differential,schema,sequence}.py
│   │   ├── strategies/{from_model,boundaries,idempotency,malicious,mutators}.py
│   │   ├── arena.py                # NAMESPACE | TENANT isolation + reset + ledger
│   │   ├── corpus.py               # coverage tokens, novelty scoring
│   │   ├── shrink.py               # ddmin over traces + per-field ladder
│   │   ├── known_issues.py         # declarative matcher evaluation (imports core/findings)
│   │   ├── known_issues.yaml       # the 8 required rediscoveries
│   │   ├── report.py               # markdown / JSON / JUnit emitters, NOVEL-first
│   │   └── repro.py                # standalone repro.py generator per finding
│   ├── buyer/                      # ══════════ SIDE 2: THE LLM BUYER ══════════
│   │   ├── models.py select.py planner.py policy.py creative.py
│   │   ├── executor.py monitor.py guard.py prompts.py agent.py
│   ├── harness/
│   │   ├── stack.py                # docker compose up/down + DELIVERY_WEBHOOK_INTERVAL overlay
│   │   ├── seed.py                 # init_database_ci.py + post-seed invariant asserts
│   │   ├── fixtures.py             # second tenant, budget-boundary products (raw psycopg SQL)
│   │   ├── admin.py                # ◀ NEW: headless Flask session + programmatic approval driver
│   │   └── doctor.py               # docker/ports/clone/webhook-reachability preflight
│   └── obs/{recorder,redact}.py
├── compose/
│   └── docker-compose.fuzz.yml     # ◀ NEW overlay: DELIVERY_WEBHOOK_INTERVAL=5,
│                                   #   ADCP_AUTH_TEST_MODE=true, host port publication
├── corpus/{seeds,known,discovered,failing/<finding_id>/}
├── scripts/{stack_up,stack_down,arena_reset,seed}.ps1
└── tests/
    ├── unit/                       # oracles on SYNTHETIC envelopes; codec; JCS; vault; ledger
    ├── oracle_mutation/            # every oracle must FIRE on its violating fixture
    ├── contract/                   # live-stack: op × wire matrix, drift, SDK byte-diff
    ├── validation/                 # THE GATE: test_rediscovers_known_issues.py (8 entries)
    └── e2e/                        # buyer end-to-end over the live stack
```

**Package dependency invariant (enforced by `tests/unit/test_import_boundaries.py`):**
`core/` imports nothing from `fuzz/` or `buyer/`. `fuzz/` imports `core/`. `buyer/` imports `core/`. **`buyer/` never imports `fuzz/`** — satisfied because `Finding`/`Violation`/`signature`/`store` now live in `core/findings/`.

---

## 3. Module contracts

### 3.1 `core/exchange.py` — the ONE normalized observation

```python
class Wire(StrEnum):
    MCP = "mcp"; A2A = "a2a"; REST = "rest"
    WEBHOOK_MCP = "webhook_mcp"        # inbound; McpWebhookPayload shape
    WEBHOOK_A2A = "webhook_a2a"        # inbound; protobuf camelCase Task/TaskStatusUpdateEvent

REQUEST_WIRES = frozenset({Wire.MCP, Wire.A2A, Wire.REST})
WEBHOOK_WIRES = frozenset({Wire.WEBHOOK_MCP, Wire.WEBHOOK_A2A})

class RunMode(StrEnum):
    """The testing-mode dimension. NEVER a silent default — it is tagged
       into Exchange, into signature(), and into Finding.target."""
    WET     = "wet"        # real writes; arena-isolated; the DEFAULT for mutating runs
    DRY_RUN = "dry_run"    # x-dry-run: true — a DIFFERENT server code path, not a no-op

@dataclass(frozen=True, slots=True)
class LogicalRequest:
    op: str                          # canonical AdCP op name, or "a2a.<Method>" for protocol ops
    args: dict[str, Any]             # transport-independent intent
    auth: AuthMode                   # VALID | NONE | GARBAGE | FOREIGN_TENANT | FOREIGN_PRINCIPAL
    context: dict | None = None
    idempotency_key: str | None = None
    run_mode: RunMode = RunMode.WET  # ◀ explicit; there is no `dry_run: bool` anywhere else

@dataclass(frozen=True)
class NormEvent:
    rule: str; pointer: str; before: Any; after: Any

@dataclass(frozen=True, slots=True)
class Exchange:
    # ── identity ──────────────────────────────────────────────────────
    op: str
    wire: Wire
    run_mode: RunMode                # ◀ tagged dimension, participates in signature()
    logical: LogicalRequest | None    # None for inbound webhook Exchanges
    sent_wire: dict                  # what the binder produced for THIS wire (incl. "_dropped")
    request_bytes: bytes | None      # exactly what went out (from wiretap)

    # ── outcome ───────────────────────────────────────────────────────
    outcome: Literal["success", "error", "transport_fault"]
    wire_response: dict | None       # RAW success body, verbatim
    wire_error_envelope: dict | None # RAW two-layer envelope, VERBATIM — never normalized
    exc: Exception | None            # transport/parse fault only

    # ── normalized (payload only; errors are never normalized) ────────
    payload: dict | None
    envelope: dict | None            # TASK_ENVELOPE_FIELDS popped here
    typed: BaseModel | None          # populated only when codec.decode ran (buyer path)
    norm_log: tuple[NormEvent, ...]
    differential_key: str            # sha256(jcs(payload)) — the E1 comparison key

    # ── protocol framing (deliberately NOT normalized away) ───────────
    status: AdcpTaskStatus           # 9-member SPEC enum
    http_status: int | None          # REST + A2A
    jsonrpc_code: int | None         # A2A only — REQUIRED to see #1670
    jsonrpc_method: str | None       # ◀ NEW: which of the 21 method names was used
    a2a_vocabulary: Literal["v1.0","v0.3"] | None   # ◀ NEW: shapes are vocabulary-dependent
    task_handle: str | None          # SERVER-assigned
    operation_id: str | None         # BUYER-minted correlation uuid
    context_id: str | None
    idempotency_key: str | None
    replayed_raw: Any = None         # unnormalized; strict oracle flags non-bool

    latency_ms: float = 0.0
    raw: Any = None

    @property
    def is_success(self) -> bool: ...
    @property
    def is_error(self) -> bool: ...
    @property
    def code(self) -> str | None:                # adcp_error.code — the parity key
    @property
    def recovery(self) -> str | None: ...
    @property
    def field_pointer(self) -> str | None:       # adcp_error.field — TIER B, see §4.2
    @property
    def advisory_errors(self) -> list[dict]:     # errors[] on a SUCCESS body — the #1449 surface
        return (self.wire_response or {}).get("errors") or []
    @property
    def collection_emptiness(self) -> Literal["empty","nonempty","n/a"]:
        """Sole payload input to AuthDisposition. Derived from OpSpec.collection_pointer."""
    @property
    def auth_disposition(self) -> AuthDisposition: ...   # see §3.7

ExchangeSet = dict[Wire, Exchange]
```

Non-negotiable properties:

- `wire_error_envelope` is stored **pre-normalization, verbatim**. The wire error envelope *is* the contract.
- `advisory_errors` exists because the SDK cannot see it. This single property is the #1449 oracle.
- `jsonrpc_code` + `jsonrpc_method` + `a2a_vocabulary` exist because #1670's observable shape **differs by vocabulary**: `SubscribeToTask` yields a clean -32004 JSON-RPC error, while `tasks/resubscribe` degrades to an SSE `InternalError` event (`jsonrpc_adapter.py:264-270`). An oracle that does not record which vocabulary was used cannot be trusted.
- `run_mode` is a first-class field, not a header detail. `signature()` includes it, so a WET finding and a DRY_RUN finding are never deduped together.
- No `synthesized_error_envelope` field (that exists in salesagent's harness only for the in-process IMPL transport, which we do not model).

### 3.2 `core/ops.py` + `core/a2a_methods.py` + `core/matrix.py` — the registry

```python
class Availability(StrEnum):
    EXISTS        = "exists"          # handler present and reachable, real implementation
    STUB          = "stub"            # reachable, always raises UnsupportedOperationError
    UNDISPATCHABLE= "undispatchable"  # ◀ NEW: handler code exists but no route reaches it
    ABSENT        = "absent"          # no handler at all

class ExtraFieldPolicy(StrEnum):
    IGNORE          = "ignore"           # silently dropped, unconditionally
    REJECT_PROTOCOL = "reject_protocol"  # FastMCP ToolError — NOT an AdCP envelope
    ENV_MODEL       = "env_model"        # forbid in dev/CI, ignore in prod (SalesAgentBaseModel)
    ALLOW           = "allow"            # extra='allow' (adcp SDK CreativeAsset)

@dataclass(frozen=True)
class WireBinding:
    availability: Availability
    name: str | None = None                    # mcp tool / a2a skill
    rest: tuple[str, str] | None = None        # (verb, path_template)

    # ── A2A: two ORTHOGONAL facts, never collapsed ──────────────────
    advertised: bool = False        # appears in create_agent_card() AgentSkills
    dispatchable: bool = False      # is a key in skill_handlers

    # ── carriage ────────────────────────────────────────────────────
    field_gaps: frozenset[str] = frozenset()   # fields this wire CANNOT carry
    body_fields: frozenset[str] | None = None  # REST *Body allowlist
    carries_idempotency_key: bool = False      # ◀ gates IDEM-SCOPE applicability

    # ── validation policy, DECLARED not probed ──────────────────────
    extra_top_level: ExtraFieldPolicy = ExtraFieldPolicy.IGNORE
    validation_mechanism: str = ""             # "pick_list" | "model_validate" | "fastmcp_sig"
                                               # | "bare_basemodel" | "raw_ctor_no_boundary"
    field_pointer_style: Literal["dot_index","bracket_index","none"] = "none"

@dataclass(frozen=True)
class OpSpec:
    name: str
    request_model: type[BaseModel] | None
    response_model: type[BaseModel] | UnionType | None
    bindings: dict[Wire, WireBinding]
    auth: Literal["discovery", "required", "tenant_policy"]

    mutating: bool                        # ◀ SPLIT from idempotent
    idempotent: bool                      # ⇔ name ∈ adcp._idempotency.IDEMPOTENT_TASKS
    async_capable_on: frozenset[Wire]     # can register a push_notification_config
    monitorable_on: frozenset[Wire]       # ◀ NEW: can register a reporting_webhook
    result_shape: Literal["model", "bare_dict"] = "model"

    collection_pointer: str | None = None      # JSON-Pointer to the op's collection, for
                                               #   AuthDisposition.collection_emptiness
    expected_disposition: dict[AuthMode, AuthDisposition] | None = None   # by-design annotations
    volatile_paths: frozenset[str] = frozenset()
    unordered: dict[str, str] = field(default_factory=dict)   # pointer → sort key

OPERATIONS: dict[str, OpSpec]        # business ops
A2A_METHODS: dict[str, OpSpec]       # A2A JSON-RPC protocol methods, arity-1 only
ALL_OPS: dict[str, OpSpec]           # the union; the binder dispatches over this

def wires_for(op: str) -> tuple[Wire, ...]   # EXISTS only — the E1 dispatch set
def spec(op: str) -> OpSpec
```

#### 3.2.1 Business-op registry (the load-bearing rows)

| op | MCP | A2A (dispatch / advert) | REST | mut | idem | key carriable | notes |
|---|---|---|---|---|---|---|---|
| `get_adcp_capabilities` | ✓ | ✓ / ✓ | **GET** `/capabilities` | – | – | – | REST cannot carry `protocols` (GET, no body) |
| `get_products` | ✓ | ✓ / ✓ | POST `/products` | – | – | – | REST gaps `{property_list, context}`. `buying_mode` unreachable on **all 3** — an all-wire hole, **not** a REST divergence |
| `list_creative_formats` | ✓ | ✓ / ✓ | POST `/creative-formats` | – | – | – | **REST-ONLY** fields `{disclosure_positions, disclosure_persistence}` — reverse gap |
| `list_authorized_properties` | ✓ | ✓ / ✓ | POST `/authorized-properties` | – | – | – | **A2A DROPS** `{property_tags, publisher_domains}` (`:1860`) — known-issue #6 |
| `list_accounts` | ✓ | ✓ / ✓ | POST `/accounts` | – | – | – | `expected_disposition[NONE] = SOFT_EMPTY` (BR-RULE-055) |
| `sync_accounts` | ✓ | ✓ / ✓ | POST `/accounts/sync` | ✓ | ✓ | **none** | key not carriable on ANY wire; `accounts.py:473,720` mint uuid4 server-side. **REST-ONLY** `push_notification_config` |
| `create_media_buy` | ✓ | ✓ / ✓ | POST `/media-buys` | ✓ | ✓ | MCP,A2A,REST | REST gaps `{reporting_webhook, push_notification_config, context, ext}`. A2A validates `ext` then drops it. `async_capable_on={MCP,A2A}`, `monitorable_on={MCP,A2A}` |
| `update_media_buy` | ✓ | ✓ / ✓ | **PUT** `/media-buys/{id}` | ✓ | ✓ | **MCP only** | REST gaps 10 fields; A2A gaps 10 fields. No replay store even on MCP — key is only folded into request_params (`media_buy_update.py:1328`) |
| `get_media_buy_delivery` | ✓ | ✓ / ✓ | POST `/media-buys/delivery` | – | – | – | A2A uses `model_validate` ⟹ `extra_top_level=ENV_MODEL` |
| `sync_creatives` | ✓ | ✓ / ✓ | POST `/creatives/sync` | ✓ | ✓ | **none** | no `idempotency_key` param exists on any wire. Nested `CreativeAsset` is `extra='allow'` everywhere |
| `list_creatives` | ✓ | ✓ / ✓ | POST `/creatives` | – | – | – | A2A drops `{media_buy_ids, fields, include_performance, include_assignments, include_sub_assets}` |
| `update_performance_index` | ✓ | ✓ / ✓ | POST `/performance-index` | ✓ | – | – | A2A `model_validate` ⟹ `ENV_MODEL` |
| `get_media_buys` | ✓ | ✓ / ✓ | **ABSENT** | – | – | – | 2-way differential = #1651. A2A `model_validate` at `:1922` **not** wrapped in `adcp_validation_boundary()` — a distinct defect target |
| `list_tasks` `get_task` `complete_task` | ✓ (`bare_dict`) | ABSENT | ABSENT | – | – | – | `coverage: single_wire` — zero parity coverage, must be reported |
| `create_creative` | ABSENT | **STUB** / **NOT advertised** | ABSENT | – | – | – | validates `{format_id, content_uri, name}` then -32004. Target of `CAPABILITY-CONCEALMENT` |
| `assign_creative` | ABSENT | **STUB** / **NOT advertised** | ABSENT | – | – | – | validates `{media_buy_id, package_id, creative_id}` then -32004. `CAPABILITY-CONCEALMENT` |
| `approve_creative` | ABSENT | **STUB** / advertised | ABSENT | – | – | – | unconditional -32004. `CAPABILITY-TRUTHFULNESS` |
| `get_media_buy_status` | ABSENT | **STUB** / advertised | ABSENT | – | – | – | unconditional -32004. `CAPABILITY-TRUTHFULNESS` |
| `optimize_media_buy` | ABSENT | **STUB** / advertised | ABSENT | – | – | – | unconditional -32004. `CAPABILITY-TRUTHFULNESS` |
| `get_creatives` | ABSENT | **UNDISPATCHABLE** / not advertised | ABSENT | – | – | – | orphan handler `:1698`. Invoking ⟹ **-32601 MethodNotFound**, never -32004. Its own oracle `ORPHAN-HANDLER` |

**Extra-field policy, declared per (wire, op) — never probed.** This replaces v1's `UNKNOWN-FIELD-CONSISTENCY` "disagrees with the probed `ENVIRONMENT`" clause, which produced a false positive on every REST case.

| Wire | Top-level policy | Mechanism |
|---|---|---|
| REST, all 12 ops | `IGNORE`, **unconditional** | bare `pydantic.BaseModel`, no `model_config` (`api_v1.py:64-170`; 11 entries formally allowlisted at `tests/unit/test_architecture_no_bare_basemodel.py:46-56`) |
| MCP, all ops | `REJECT_PROTOCOL`, **unconditional** | FastMCP validates against the wrapped signature before invocation → plain `ToolError: unexpected_keyword_argument`. **No AdCP envelope.** Asserting `adcp_error.code` on this path is a false positive |
| A2A `create_media_buy`, `get_media_buy_delivery`, `update_performance_index`, `get_media_buys` | `ENV_MODEL` | `model_validate(params)` on a `SalesAgentBaseModel` subclass |
| A2A — the other 8 | `IGNORE` | `parameters.get(...)` pick-lists |
| **Nested** (`PackageRequest`, all `*Request` sub-models) — **all wires incl. REST** | `ENV_MODEL` | REST's `dict[str, Any]`-typed body fields feed `SalesAgentBaseModel` downstream |
| Nested `CreativeAsset` (`sync_creatives.creatives[]`) — all wires | `ALLOW` | adcp SDK `CreativeAsset1`, `extra='allow'` in both environments |

**`get_pydantic_extra_mode()` is resolved at class-definition (import) time** (`_base.py:247`, `config.py:172-178`). Flipping `ENVIRONMENT` mid-run does nothing. The harness sets it before container start and records it in `Finding.target.environment`.

**`field_gaps` are targets, not exclusions.** `FIELD-REACHABILITY` drives each gap deliberately with a *distinguishing* value and asserts the result changes on wires that carry it and does not on wires that don't — *that non-difference is the finding*. When a gap closes, the oracle stops firing and the known-issue gate reports "seller fixed it."

#### 3.2.2 `core/a2a_methods.py` — the A2A protocol surface as first-class ops

Modelled as `OpSpec`s bound only to `Wire.A2A`, `arity-1 only` (no parity peer exists). **Both vocabularies are live** because `create_jsonrpc_routes(..., enable_v0_3_compat=True)` (`src/app.py:298-303`); v0.3 is checked first, then v1.0.

| op name | v1.0 method | v0.3 alias | Auth? | Validates? | Typed error raised **by salesagent** | #1670 viable |
|---|---|---|---|---|---|---|
| `a2a.GetTask` | `GetTask` | `tasks/get` | **NO** | **NO** | none — the -32001 is SDK-manufactured | **NO** → `TASK-GET-NULLRESULT` |
| `a2a.CancelTask` | `CancelTask` | `tasks/cancel` | **NO** | **NO** — overwrites any state | none; `TaskNotCancelableError` unused in `src/` | **NO** → `TASK-CANCEL-STATE` |
| `a2a.ListTasks` | `ListTasks` | **none** — literal `tasks/list` is MethodNotFound in both maps | n/a | n/a | `UnsupportedOperationError` **-32004** | **YES** — send `ListTasks` |
| `a2a.SubscribeToTask` | `SubscribeToTask` | `tasks/resubscribe` | n/a | n/a | `UnsupportedOperationError` **-32004** | **YES** via v1.0 name only; v0.3 degrades to an SSE `InternalError` |
| `a2a.GetExtendedAgentCard` | `GetExtendedAgentCard` | `agent/getAuthenticatedExtendedCard` | n/a | n/a | `UnsupportedOperationError` **-32004** | **YES**, both vocabularies |
| `a2a.CreatePushConfig` | `CreateTaskPushNotificationConfig` | `tasks/pushNotificationConfig/set` | **YES** | `url` required; **`task_id` never checked**; `config_id` auto-generated | `InvalidRequestError` **-32600**, `InvalidParamsError` **-32602** | **YES** (missing token; missing url) |
| `a2a.GetPushConfig` | `GetTaskPushNotificationConfig` | `.../get` | **YES** | `id` required; tenant+principal scoped read | -32600, -32602, `TaskNotFoundError` **-32001** | **YES** (3 cases) |
| `a2a.ListPushConfigs` | `ListTaskPushNotificationConfigs` | `.../list` | **YES** | none; **`task_id` echoed but not used as a filter** | -32600 | **YES** (missing token only) |
| `a2a.DeletePushConfig` | `DeleteTaskPushNotificationConfig` | `.../delete` | **YES** | `id` required; scoped soft-delete | -32600, -32602, -32001 | **YES** (3 cases) |

`transport/a2a.py` must be able to emit **any** of these method names, in **either** vocabulary, selected by `LogicalRequest.args["_vocabulary"]` defaulting to `v1.0` for deterministic error shapes.

#### 3.2.3 Drift test — `tests/contract/test_registry_drift.py`

Four **separate** assertions, each failing independently:

1. `set(live list_tools()) == ops.MCP_TOOL_NAMES` (tuple written by the P2b gate, not hardcoded).
2. `{k for k in skill_handlers} == ops.A2A_DISPATCHABLE` — **exactly 18**, enumerated by name.
3. `{s.id for s in agent_card.skills} == ops.A2A_ADVERTISED` — **exactly 16**, enumerated by name.
4. REST: enumerate `api_v1.router.routes` **from the module, not `/openapi.json`** — exactly 12. `/openapi.json` and live path probing over-count by 15 admin blueprint routes plus 8 unprefixed health/debug routes.

Plus two derived assertions that encode the current defects and go red when fixed:
5. `A2A_DISPATCHABLE - A2A_ADVERTISED == {"create_creative", "assign_creative"}`
6. `A2A_ADVERTISED - A2A_DISPATCHABLE == set()`

### 3.3 `core/transport/` — three raw clients, one shape

```python
class RawTransport(Protocol):
    wire: Wire
    async def dispatch(self, spec: OpSpec, sent: dict, ctx: CallContext) -> Exchange: ...
    async def get_task(self, handle: str, ctx: CallContext) -> Exchange | None: ...
    async def aclose(self) -> None: ...
```

All three build their httpx client over `wiretap.WireTapTransport` so `request_bytes` is exact.

**`rest.py`** — POST-with-body for every read except `GET /capabilities`; `PUT` for update. Headers: `x-adcp-auth` (primary) → `Authorization: Bearer` (fallback, a fuzz case not the default), `x-adcp-tenant: <subdomain>`, `x-context-id`, and `x-dry-run` **only when `run_mode is RunMode.DRY_RUN`**. Body built by field-picking against `WireBinding.body_fields`; **every dropped field is recorded** into `sent_wire["_dropped"]` — that record feeds `FIELD-REACHABILITY`. Send raw bytes exactly once (`content=raw`, never `json=`) because `RestCompatMiddleware` stashes `request.state.raw_wire_payload` pre-normalization; re-serializing with different key order manufactures a false `IDEMPOTENCY_CONFLICT`. Note the middleware only rewrites POST bodies for `/products`, `/media-buys`, `/creatives/sync` (`rest_compat_middleware.py:22-26,38`) — **`PUT /media-buys/{id}` gets no deprecated-field normalization and no raw-payload capture**, so deprecated-alias cases are valid only on those three paths. Success → `wire_response = r.json()` (bare payload, **not** `ProtocolEnvelope`-wrapped). Body containing `adcp_error` → `wire_error_envelope` + `http_status`. `follow_redirects=False`.

**`mcp.py`** — `fastmcp.Client(StreamableHttpTransport(f"{base}/mcp/", headers=...))`. **Trailing slash is mandatory** (`mcp.http_app(path="/")` mounted at `/mcp`). Success → `wire_response = result.structured_content`. `result.isError` → attempt `json.loads(result.content[0].text)`; if it parses to a two-layer envelope, that's `wire_error_envelope`; **if it does not parse, it is a FastMCP protocol error** (the `unexpected_keyword_argument` class) and is recorded as `outcome="error"` with `wire_error_envelope=None` and `sent_wire["_mcp_protocol_error"]=text`. Oracles that assert on `adcp_error` MUST skip that case. Also parse `content[0].text` on success and assert it mirrors `structuredContent` (a Tier-B oracle). `result_shape == "bare_dict"` ops skip response-model decode without a special case.

**`a2a.py`** — hand-rolled `httpx.post(f"{base}/a2a")` (no trailing slash — `/a2a/` 307-redirects and a redirected POST can drop the body). Skill invocation:

```json
{"jsonrpc":"2.0","id":"<uuid-string>","method":"message/send",
 "params":{"message":{"messageId":"<uuid-string>","role":"ROLE_USER",
   "parts":[{"data":{"skill":"<op>","input":{...}}}]},
   "configuration":{"pushNotificationConfig":{"url":"...","authentication":{"schemes":["HMAC-SHA256"],"credentials":"..."}}}}}
```

`messageId` and `id` are **always strings**. Both `input` (spec) and `parameters` (legacy) keys are accepted by the seller; we send `input` by default and fuzz `parameters`. The push-notification config key on `message/send` is the **v0.3 spec key `pushNotificationConfig`** — the compat adapter translates it into the v1.0 protobuf `task_push_notification_config` (`compat/v0_3/conversions.py:314-319`). Protobuf `AuthenticationInfo` uses singular `scheme`; salesagent translates it to AdCP's plural `schemes` at `:1376-1385` — no action needed on our side.

Response handling: `body["error"]["code"]` → `jsonrpc_code` (**the #1670 sensor**); `body["result"]` is a Task → walk `artifacts[].parts[]` for the last `data` part → `wire_response`, or if `status.state` is FAILED and the DataPart carries `adcp_error` → `wire_error_envelope`. `status.state` → `AdcpTaskStatus` via `strip("TASK_STATE_").lower().replace("_","-")`. Auth is `Authorization: Bearer`. Raw JSON parsing preserves `1.0` as float and `1` as int — exactly what `TYPE-PARITY` asserts on for #1583.

The transport also exposes `call_method(method: str, params: dict, *, vocabulary)` for the nine protocol ops, and records `jsonrpc_method` + `a2a_vocabulary` on the resulting `Exchange`. For `tasks/resubscribe` it must handle an **SSE stream response** and surface the first event's error as `sent_wire["_sse_error"]`, not as `jsonrpc_code`.

**Auth gating is skill-dependent.** `DISCOVERY_SKILLS = {get_adcp_capabilities, list_accounts, list_creative_formats, list_authorized_properties, get_products}` (`:138-146`). Auth is required only if the requested skill set minus discovery is non-empty (`:592-603` → `InvalidRequestError`, message "Missing authentication token - Bearer token required in Authorization header"), and `_handle_explicit_skill` enforces identity a **second** time at `:1395-1396` with a *different* message ("Authentication required for skill invocation"). Auth-required negative tests must target a non-discovery skill, and the oracle must accept either message (the two paths are distinguishable and both legal).

**`sdk_crosscheck.py`** — `adcp.ADCPClient` with `AgentConfig(agent_uri=f"{base}/mcp/", protocol=Protocol.MCP, auth_header="x-adcp-auth", auth_type="token", extra_headers={"x-adcp-tenant": ...}, timeout=120.0, validate_features=False, strict_idempotency=False, validation=ValidationHookConfig(requests="off", responses="off"))`. **Never used in a fuzz run or a buyer run.** Its only job is `tests/contract/test_sdk_byte_parity.py`.

### 3.4 `core/client.py` — the single front door

```python
class Client:
    def __init__(self, session: AgentSession, *, default_wire: Wire = Wire.MCP,
                 profile: CompatProfile = SALESAGENT, guard: GuardMode = GuardMode.OFF): ...

    async def call(self, req: LogicalRequest, *, wire: Wire | None = None,
                   validate: bool = True) -> Exchange:
        """resolve OpSpec → (optional) codec.encode → vault key for mutating ops
           → binder.bind(req, wire) → dispatch → normalize → parse envelope
           → (optional) codec.decode → (optional) guard-mode oracles → Exchange"""

    async def call_all(self, req: LogicalRequest, *,
                       wires: Sequence[Wire] | None = None) -> ExchangeSet:
        """asyncio.gather over wires_for(op) with ONE identical logical request.
           THE parity primitive. ~20 lines."""

    async def aclose(self) -> None: ...
```

`validate=False` is what lets the fuzzer send deliberately-invalid payloads through the same front door the buyer uses. `binder.bind(req, wire)` is **generated from `ops.py`, never hand-written per case** — otherwise the binder becomes the place where the fuzzer accidentally "fixes" a request and hides the bug.

### 3.5 `core/codec.py` + `core/compat.py` — the typed stratum

```python
def encode(op: OpSpec, *, profile: CompatProfile = SALESAGENT, **kw) -> tuple[dict, BaseModel | None]
def decode(op: OpSpec, body: dict, status: AdcpTaskStatus) -> BaseModel | None
```

`decode` selects the response variant **status-first** (response models are `UnionType`s with no `.model_fields`) and never raises into the transport — a decode failure lands on `Exchange.exc` with the raw body intact.

**`CompatProfile` — corrected divergence set.** v1's D1 is refuted; the replacement set is:

| # | Divergence | SALESAGENT profile action | Fuzzer action |
|---|---|---|---|
| **D1′** | `GetProductsRequest` declares `buying_mode`, `refine`, `catalog`, `account`, `preferred_delivery_types`, `fields`, `time_budget`, `pagination`, `required_policies`, `ext`, `push_notification_config`, `product_selectors` — **none reachable on any wire** because `create_get_products_request()` (`schema_helpers.py:164-204`) accepts only `{brief, brand, filters, property_list, context}` | drop all 12 | send each individually on all 3 wires; expect **uniform** silent-drop (REST/A2A) or `REJECT_PROTOCOL` (MCP). A per-wire *difference* here is the finding; a uniform hole is one aggregated `FIELD-REACHABILITY` row at `medium`, **not** a parity finding |
| **D2** | `create_media_buy` wrapper accepts `{brand, packages, start_time, end_time, po_number, reporting_webhook, push_notification_config, context, ext, account, idempotency_key}`. `plan_id`, `proposal_id`, `total_budget`, `advertiser_industry`, `invoice_recipient`, `io_acceptance`, `agency_estimate_number`, `artifact_webhook` are unknown | restrict to the accepted set | send each unsupported field **individually** to map the gap |
| **D3** | `update_media_buy` wrapper has **no** `account`, `revision`, `canceled`, `cancellation_reason`, `new_packages`; carries non-spec `flight_start_date`/`flight_end_date`, scalar `budget`+`currency`, `daily_budget`, `pacing`. **Cancellation has no expressible request shape.** | emit salesagent's shape from a spec-typed intent | drive both shapes; report the delta |
| **D4** (new) | salesagent's `CreateMediaBuyRequest` **overrides** `packages` (`_base.py:1529`) dropping the library's `MinLen(1)`, and relaxes `account` from required to optional (`:1525`) | accept both | assert the relaxation persists; **do not** generate "missing account ⟹ VALIDATION_ERROR" cases |

Every profile rule ships with a test in `tests/contract/test_divergences.py` **asserting the divergence still exists**. `compat.py` also defines `SPEC_LITERAL` (no rewrites) so one flag flips the buyer between "works against this seller" and "conforms to the spec."

### 3.6 `core/normalize/` — see §4. Public surface:

```python
class NormPipeline:
    def __init__(self, *, numeric: NumericPolicy = NumericPolicy.STRICT,
                 numeric_tolerant_wires: frozenset[Wire] = frozenset(),   # ◀ scoped, see §4.3
                 rules: Sequence[NormRule] = DEFAULT_RULES): ...
    def run(self, wire: Wire, op: OpSpec, raw: dict) -> tuple[dict, dict, tuple[NormEvent, ...]]:
        """returns (payload, envelope, norm_log). Accepts WEBHOOK_* wires."""
```

`differential_key()` and `structural_diff()`/`DiffEntry` live in **`core/spec/canonical.py`** (P1), not here — that is what makes the P3 lane genuinely parallel.

### 3.7 `core/oracles/` — the 36-invariant library

```python
class Oracle(ABC):
    id: str
    invariant_ids: tuple[str, ...]
    arity: Literal[1, "N"]
    severity: Severity
    def applies(self, op: str, wire: Wire | None, phase: Phase, run_mode: RunMode) -> bool: ...
    def check(self, obs: Exchange | ExchangeSet) -> list[Violation]: ...

ORACLES: dict[str, Oracle]
def run_arity1(obs: Exchange, phase: Phase) -> list[Violation]
def run_arityN(obs_set: ExchangeSet, phase: Phase) -> list[Violation | CoverageMarker]
```

**Spec tables are imported, never retyped**: `STANDARD_ERROR_CODES` / `INTERNAL_CODES` / recovery table from `adcp/server/helpers.py`; `MEDIA_BUY_STATE_MACHINE` and `valid_actions_for_status()` likewise; closed enums from `adcp/types/generated_poc/enums/`; canonicalization from `adcp/server/idempotency/canonicalize.py`; JSON-RPC codes from `a2a/utils/errors.py`. **Where salesagent disagrees with its own SDK, that disagreement is automatically a finding** — unless listed in `spec/divergences.yaml`, in which case it is downgraded to `info` and still counted.

#### 3.7.1 The six false-positive fixes, stated as binding rules

**FP-1 — No oracle hardcodes an HTTP status.** `spec/status_map.py` exports `ALLOWED_STATUSES: dict[str, frozenset[int]]`, derived by replicating `_build_error_code_to_status()`'s walk **plus** the ambiguity sets. Three wire codes are genuinely multi-valued after `ERROR_CODE_MAPPING` translation:

| Wire code | Derived table says | Actually observable | Raising classes |
|---|---|---|---|
| `SERVICE_UNAVAILABLE` | 503 | **{500, 502, 503}** | `AdCPConfigurationError` 500; `AdCPAdapterError`/`AdCPActivationWorkflowError`/`AdCPBulkUpdateError`/`AdCPGamUpdateError`/`AdCPLineItemError`/`AdCPWorkflowError` 502; `AdCPServiceUnavailableError` 503 |
| `INVALID_REQUEST` | 400 | **{400, 404}** | `AdCPInvalidRequestError` 400; `AdCPNotFoundError`/`AdCPCreativeNotFoundError`/`AdCPFormatNotFoundError`/`AdCPTaskNotFoundError` 404 |
| `POLICY_VIOLATION` | 422 | **{403, 422}** | `AdCPPolicyViolationError` 403; `AdCPMediaBuyRejectedError` 422 |

`INV-08 REST-STATUS-MAP` asserts `http_status ∈ ALLOWED_STATUSES[code]` and is the **sole** owner of status. `INV-07 AUTH-CODE` asserts **code + recovery only**. Note the derived table is consulted only for plain-`ToolError` fallbacks (`tool_error_logging.py:456`); typed `AdCPError` on REST uses `exc.status_code` directly (`app.py:150-153`), which is exactly why a scalar assertion is unsound.

**FP-2 — `PROJECT_CODES` aggregation.** `AUTH_TOKEN_INVALID ∉ STANDARD_ERROR_CODES`. `spec/codes.py` exports `PROJECT_CODES = frozenset({"AUTH_TOKEN_INVALID"})`. `INV-04 CODE-MEMBERSHIP` consults it **before** firing and emits **one aggregated run-level `info` row** (`PROJECT-CODE-USAGE`, with an occurrence count), never N per-case findings. `divergences.yaml` supports `op: "*"` for op-independent entries. Parity oracles keep firing normally — those carry the real signal.

**FP-3 — No identity checks over the wire.** `INV-01 ENV-TWO-LAYER` asserts **structural equality** of `adcp_error` and `errors[0]` (JCS-equal). `adcp_error is not errors[0]` is an in-process server invariant, trivially true after any JSON round-trip, and is deleted.

**FP-4 — `mutating` ≠ `idempotent`, and `IDEM-SCOPE` is carriage-gated.** `IDEM-SCOPE.applies()` returns `False` unless `spec.idempotent and spec.bindings[wire].carries_idempotency_key`. The *inability* to carry the key is **one deduped `FIELD-REACHABILITY` finding per (op, wire)**, not N per-case violations. Concretely: only `(REST, create_media_buy)`, `(MCP, create_media_buy)`, `(A2A, create_media_buy)` and `(MCP, update_media_buy)` may be graded for buyer-supplied idempotency behavior; `sync_creatives` and `sync_accounts` are suppressed entirely on every wire.

**FP-5 — `AuthDisposition` is defined on `(outcome, collection_emptiness)` only.**

```python
class AuthDisposition(StrEnum):
    HARD_REFUSE = "hard_refuse"   # outcome == "error"
    SOFT_EMPTY  = "soft_empty"    # outcome == "success" and collection_emptiness == "empty"
    SUCCESS     = "success"       # outcome == "success" and collection_emptiness != "empty"
```

Advisory-`errors[]` presence is an **independent dimension**, carried separately on the `Violation` and consumed only by `ADVISORY-ERRORS-PARITY`. This breaks v1's circularity, where detecting `SOFT_EMPTY` on A2A/REST depended on the very field #1449 removes. `list_accounts` carries `expected_disposition[AuthMode.NONE] = SOFT_EMPTY` (BR-RULE-055) and is therefore **not** a finding.

**FP-6 — Extra-field policy comes from the registry.** `UNKNOWN-FIELD-CONSISTENCY` asserts observed behavior against `WireBinding.extra_top_level`, never against a probed `ENVIRONMENT`. When #1442 lands and REST's `*Body` models gain `SalesAgentBaseModel`, the declared policy stops matching and the test goes red — which is the point.

#### 3.7.2 Arity-1 oracles

| INV | Oracle id | Assertable check |
|---|---|---|
| 01 | `ENV-TWO-LAYER` | `error` is dict; `adcp_error` present; `errors` is a list, `len>=1`; `errors[0]` JCS-**equal** to `adcp_error`; both codes ∈ `STANDARD_ERROR_CODES ∪ PROJECT_CODES` |
| 02 | `ENV-FIELDS` | required `{code,message,recovery}`; optional ⊆ `{field,suggestion,retry_after,details}` (unknown key → finding); `recovery ∈ {transient,correctable,terminal}`; **`details` taint scan** |
| 03 | `CTX-ECHO` | request `context` ≤64KB JCS ⟹ echoed JCS-equal on success **and** error; >64KB ⟹ absent **and** not an error |
| 04 | `CODE-MEMBERSHIP` | code ∈ `STANDARD_ERROR_CODES`, ∉ `INTERNAL_CODES`, with `PROJECT_CODES` aggregation (FP-2). An `INTERNAL_CODES` member on the wire = **critical** |
| 05 | `CODE-RECOVERY-TABLE` | `recovery == RECOVERY_BY_CODE[code]` unless `(op,code)` ∈ divergences → `info`. Plus **global consistency**: one wire code never carries two recoveries in one run |
| 07 | `AUTH-CODE` | present-but-invalid token → expected code + recovery. **No status assertion** (FP-1) |
| 08 | `REST-STATUS-MAP` | `http_status ∈ ALLOWED_STATUSES[code]`. Sole owner of status. Plus cross-case consistency within a run |
| 09/14/15/16 | `*-STATUS-ENUM` | media-buy ∈ 7-set; creative ∈ 5-set; approval ∈ 3-set (never conflated); task ∈ 9-set; proposal ∈ 2-set; account ∈ 6-set |
| 11 | `MB-TERMINAL` | mutation on `completed/rejected/canceled` ⟹ `INVALID_STATE`. **Guard:** non-spec statuses (`draft`) are exempt |
| 12 | `MB-VALID-ACTIONS-ECHO` | `valid_actions == valid_actions_for_status(resulting_status)` compared as **sets** against the imported SDK function; terminal ⟹ `[]` |
| 13 | `MB-CANCEL-SHAPE` | `status=="canceled"`, `valid_actions==[]`, `canceled_by ∈ {buyer,seller}`, `canceled_at` present and tz-aware |
| 17-22 | `IDEM-*` | see §3.9; `IDEM-SCOPE` carriage-gated per FP-4 |
| 23-26 | `BUDGET-*` | min package (create **and** update), max campaign, max daily (incl. date-shrink attack), non-negativity |
| 29 | `LIST-CAP` | `limit` ladder; `>1000` **silently capped, not errored**; default 50; principal+tenant scoped |
| 31 | `ERROR-NORMALIZATION` | value-shaped ⟹ `VALIDATION_ERROR`/`correctable`; permission-shaped ⟹ `AUTH_REQUIRED`; induced fault ⟹ translated to `SERVICE_UNAVAILABLE`. **Code only — no status** |
| 32 | `NOT-CANCELLABLE` | emits `NOT_CANCELLABLE` (correctable), not a generic failure |
| 33 | `CONTEXT-ID-RESOLUTION` | unresolvable `x-context-id`/`context_id` ⟹ `SESSION_NOT_FOUND` (correctable) — **not** `INVALID_STATE` |
| 34 | `TASK-STORE-ISOLATION` | **NEW (#1702).** A task created under principal A must not be readable via `GetTask` by principal B, nor unauthenticated. `self.tasks` is a process-global dict (`:185`) keyed by task_id alone; the a2a `Task` proto has **no** tenant/principal field (`a2a_pb2.pyi:60-61`) and metadata (`:567-572`) carries only `request_text`/`invocation_type`/`skills_requested`. Severity **critical** |
| 35 | `TASK-GET-NULLRESULT` | **NEW.** `GetTask` on an unknown id: the spec wants a typed `TaskNotFoundError` **from the agent**. salesagent returns `None` and the SDK manufactures -32001. Assert the code is present *and* record that no salesagent-side typed error was raised. Severity **medium** |
| 36 | `TASK-CANCEL-STATE` | **NEW.** `CancelTask` on a task in a terminal state must yield `TaskNotCancelableError` (**-32002**). salesagent unconditionally `CopyFrom`s `TASK_STATE_CANCELED` over any state (`:1022-1041`) and `-32002` is never used in `src/`. Severity **high**. Combine with the missing auth: unauthenticated terminal-state rewrite is **critical** |
| — | `CAPABILITY-TRUTHFULNESS` | every **advertised** capability (agent card `AgentSkill`, `/capabilities` features) must not return `UNSUPPORTED_FEATURE`/-32004 when exercised. Current expected hits: `approve_creative`, `get_media_buy_status`, `optimize_media_buy` |
| — | `CAPABILITY-CONCEALMENT` | **NEW mirror oracle.** Every **dispatchable** skill must be advertised. Current expected hits: `create_creative`, `assign_creative`. Severity **medium** — a card-driven client can never reach them |
| — | `ORPHAN-HANDLER` | **NEW.** A skill name whose handler exists in source but is not in `skill_handlers` must not be silently unreachable. Drives `get_creatives` and asserts **-32601**, explicitly *not* -32004 |
| — | `PNC-SCOPE` | **NEW.** `CreatePushConfig` with a `task_id` that does not exist must not silently succeed (`:1178` echoes `task_id or "*"`; nothing validates it). `ListPushConfigs` with `task_id=A` must not return configs registered for task B (`:1216-1218` lists by principal, `:1227` stamps A onto every row — the response is *affirmatively wrong*, not merely over-broad). Severity **high** |
| — | `PROTOCOL-ERROR-FIDELITY` | **A2A-only, arity 1.** `jsonrpc_code == spec.jsonrpc.CODE_FOR[expected_error_class]`. **This is #1670.** Case set in §7.1. Must record `a2a_vocabulary`; `tasks/resubscribe` is excluded because its error degrades to SSE |

#### 3.7.3 Arity-N (parity) oracles — INV-06 family

| Oracle | Fires when | Rediscovers |
|---|---|---|
| `ERROR-CODE-PARITY` | `adcp_error.code` differs across wires (with the schema-failure class excepted — see divergences) | generic |
| `RECOVERY-PARITY` | `recovery` differs for equal codes | generic |
| `ADVISORY-ERRORS-PARITY` | on **success**, the set of `errors[].code` differs | **#1449** |
| `TYPE-PARITY` | JSON *type* at any payload pointer differs (runs **before** value comparison) | **#1583** |
| `PAYLOAD-SHAPE-PARITY` | normalized payload JCS differs | generic |
| `AUTH-DISPOSITION-PARITY` | `(outcome, collection_emptiness)` tuple differs, excluding `expected_disposition` annotations | **#1651** |
| `ACCOUNT-SCOPE-PARITY` | returned account-id set differs, or a foreign `account` refuses differently | **#1316** |
| `FIELD-REACHABILITY` | a distinguishing field changes results on some wires but not others. Deduped per `(op, field, wire)` | **A2A `list_authorized_properties` filter drop** |
| `FIELD-POINTER-SHAPE` | **MOVED FROM TIER A.** `adcp_error.field` compared **only when both wires produced one**, and only after normalizing style per `WireBinding.field_pointer_style` | see below |
| `IDEM-CROSS-TRANSPORT` | create on one wire, replay same key+payload on another ⟹ must replay, not conflict. Applicability gated by `carries_idempotency_key` on **both** wires | capture-point drift |
| `ENVELOPE-WRAPPING` | REST returns bare payload while MCP/A2A wrap — asserted to remain **exactly** that documented Tier-B difference | generic |
| `UNKNOWN-FIELD-CONSISTENCY` | observed extra-field behavior ≠ `WireBinding.extra_top_level` | #1442 regression detector |

**`FIELD-POINTER-SHAPE` is a proven divergence, so it must be Tier B.** For the identical payload `{"media_buy_ids":[123]}`: REST emits `field="media_buy_ids.0"` (dot + numeric segment, `app.py:234-236`), A2A emits `field="media_buy_ids[0]"` (bracket index, `validation_helpers.py:118-137`), MCP produces no envelope at all. For a package with a bad budget: REST/MCP builder gives `field=None` (`media_buy_create.py:4264` omits `field=`) while A2A gives `packages[0].budget`. The oracle normalizes `[i]` ↔ `.i` before comparing and skips any wire whose declared style is `none`.

**`run_arityN` on a 1-element `ExchangeSet` emits a `CoverageMarker(op, "single_wire")`**, and on a 2-element set for a 3-wire-capable op emits `CoverageMarker(op, "single_pair")`. The report **must** print `parity untested for N ops: [...]`. Currently that is `list_tasks`, `get_task`, `complete_task` (single-wire), all 5 A2A stubs + `get_creatives` (single-wire), all 9 A2A protocol methods (single-wire by construction), and `get_media_buys` (single-pair). A gap you can't see is worse than a gap.

**Taint oracle (INV-02's `details` clause, made mechanical).** Every string request field gets canary `ADCPFZ-<8hex>-<field_tag>` (≤24 chars ASCII, survives truncation and NFKC), combined with `{{7*7}}`, `${jndi:ldap://x}`, `<script>`, `'; DROP TABLE --`, `../../../etc/passwd`, `\u202e`, embedded NUL, lone surrogate. The scanner walks the entire response plus the JSONL wire log with casefold+NFKC+strip matching: canary in `adcp_error.details` → **high**; in `field` → expected, `info`; in `message`/`suggestion` → `medium`; in a **different request's** response → **critical**; in an idempotency-conflict message → INV-20 violation.

**Isolation oracles carry a side-channel check**: INV-27 asserts a cross-tenant resource returns the entity's `NOT_FOUND` code **and** that code, message, and latency are indistinguishable from a genuinely nonexistent id in the requesting tenant (latency delta > 3σ is a finding).

**Oracle-mutation tests are mandatory.** `tests/oracle_mutation/` ships, for every oracle, ≥1 fixture it must pass and ≥1 deliberately-violating fixture it must fire on exactly once.

### 3.8 The three fuzz engines

```python
class DifferentialEngine:                      # E1
    async def run(self, case: LogicalRequest, *,
                  mode: DispatchMode = DispatchMode.SHARED_STATE) -> list[Violation]:
        """1. wires = matrix.wires_for(case.op)
           2. sent  = binder.bind(case, wire)  per wire
           3. dispatch: SHARED_STATE (reads, concurrent) |
                        SEQUENTIAL_ISOLATED (mutations, serial, own arena slot,
                        distinct idempotency_key, run_mode=WET, arena.reset() between cases)
           4. run_arity1 on EACH
           5. run_arityN over all C(n,2) pairs, emitting CoverageMarkers for n<3"""
```

**`SEQUENTIAL_ISOLATED` defaults to `RunMode.WET`, not dry-run.** v1's dry-run default fuzzed the testing-hooks path and attributed its divergences to the protocol. Verified: `x-dry-run` propagates on all three wires (`auth_middleware.py:39-61` → `context_builder.py:37-46` → `adcp_a2a_server.py:260-267` → `testing_hooks.py:96-101`), but on `create_media_buy` it causes an **early return at `media_buy_create.py:3510`** with `media_buy_id = "dry_run_<hex>"` — no workflow step, no push-config DB row, no adapter call, and **no webhook of any kind**. Dry-run is therefore a *different feature*, run as its own labelled campaign (`--run-mode dry_run`), tagged into `Exchange.run_mode`, `signature()`, and `Finding.target`.

```python
class SchemaEngine:                            # E2 — hypothesis
    def strategies(self, op: OpSpec) -> tuple[Strategy, Strategy, Strategy]:
        """valid() | boundary() | mutate(valid) — DERIVED from adcp pydantic model_fields."""
    async def run(self, op: str, *, examples: int = 500, seed: int) -> list[Violation]:
        """every generated case whose op exists on >1 wire is dispatched THROUGH E1."""
```

**E2 boundary ladders**: `ge=0` → `{-1,-0.0,0,1e-9,1e308,NaN,Infinity,"0",2**63}`; `min_length=16,max_length=255` → `{0,1,15,16,17,254,255,256,4096}` chars × charset ladder; enums → `{member, MEMBER.upper(), member+"\x00", "not_a_member", 0, null}`; datetimes → `{naive, aware, 0001-01-01, 9999-12-31, DST fold, "2024-06-30T23:59:60Z", "not-a-date"}` (naive `end_time` must be rejected — `AwareDatetime`; `start_time` is a `StartTiming` **object**). Structural mutators applied **post-serialization**: drop required, duplicate JSON key (REST/A2A raw bodies only), type-swap, unknown field at every nesting level, deep-nest `{1,8,9,16,64,1024}`, inflate, unicode abuse.

**Nested-extra targeting is env-aware and wire-uniform.** Because `PackageRequest` is `ENV_MODEL` on *all three* wires including REST, nested-extra cases are graded against `ENVIRONMENT`, and `sync_creatives.creatives[]` nested-extra cases are **suppressed entirely** (`CreativeAsset` is `extra='allow'`).

```python
class SequenceEngine:                          # E3
    async def run(self, *, steps: int = 20, seed: int,
                  arena: Arena) -> tuple[Trace, list[Violation]]:
        """Custom scheduler — NOT hypothesis RuleBasedStateMachine."""
```

**E3 walk**: weighted 0.6 legal / 0.4 illegal. Illegal families: action disallowed for status; any action on a terminal buy; action on nonexistent/foreign-tenant/foreign-principal id; stale-status race; duplicate action; out-of-order; a legal action re-sent on a different wire mid-sequence. **New E3 targets from the verified A2A surface**: (a) `CancelTask` on a COMPLETED task (INV-36); (b) `GetTask` for a task minted under a foreign principal (INV-34); (c) `CreatePushConfig` for a nonexistent `task_id` then `ListPushConfigs` for a different `task_id` (`PNC-SCOPE`); (d) register a push config via `CreatePushConfig` (the spec-canonical channel), then `message/send` **without** an inline config, and assert a webhook arrives — it will not (§5.1), which is a **high** finding.

**`arena.py`** — `NAMESPACE` (distinct `buyer_ref`/idempotency-key prefixes on a shared stack; fast; default) or `TENANT` (fresh tenant + principal + `CurrencyLimit(USD)` + `PropertyTag('all_inventory')` + products; slow; **required** for INV-27/28/34). Hermetic reset via TRUNCATE mirroring salesagent's `_reset_e2e_db`. **E3 runs strictly serial.**

**Coverage & corpus** — black-box response-shape token:

```
coverage_token = sha256((op, wire, run_mode, status_class, wire_error_code, recovery,
                         sorted(envelope_key_set),
                         sorted(payload_key_pointers_at_depth<=3),
                         http_status, jsonrpc_code, jsonrpc_method))
```

**`shrink.py`** — E2 stateless: hypothesis's shrinker; corpus-replayed cases fall back to ddmin. E3 stateful: `delta_debug(trace, finding_signature)` replaying each reduction **in a fresh arena**, keeping it iff the *same* `signature` still fires; budget ≤60 replays; record `shrink_quality`.

**Finding schema (`core/findings/model.py`)**:

```jsonc
{
  "finding_id": "F-a91c3e77b2",
  "signature": "INV06-AUTH-DISPOSITION-PARITY|get_media_buys|a2a,mcp|wet|-|/",
  "oracle_id": "...", "invariant_ids": ["INV-06","INV-30"],
  "engine": "differential", "severity": "high", "confidence": "confirmed",
  "title": "...", "op": "...", "wires": ["a2a","mcp"],
  "expectation": "<invariant text verbatim>",
  "observed": { "a2a": {...}, "mcp": {...} },
  "diff": [{"path": "/_disposition", "left": "HARD_REFUSE", "right": "SOFT_EMPTY"}],
  "advisory_errors_present": {"a2a": false, "mcp": true},
  "norm_log": [{"rule":"strip_a2a_synthetic","path":"/message"},
               {"rule":"numeric_policy","mode":"STRICT","wires":["a2a"]}],
  "coverage": null,
  "evidence": {"request_wire":{...}, "response_wire":{...},
               "http_status": null, "jsonrpc_code": -32603,
               "jsonrpc_method": "message/send", "a2a_vocabulary": "v0.3",
               "wire_log": "corpus/failing/F-.../wire.jsonl"},
  "repro": {"case_path":"...", "script":"corpus/failing/F-.../repro.py",
            "shrunk": true, "shrink_quality": 0.86, "verified": true},
  "known_issue": {"match":"gh-1651","confidence":"high",
                  "matched_by":["oracle_id","op","wire_set","disposition_pair"]},
  "run": {"run_id":"...","seed":918273645,"occurrences":4,"first_seen":"..."},
  "target": {"adcp_sdk":"5.7.0","spec":"3.1.0-beta.3","server_git_sha":"...",
             "environment":"development", "run_mode":"wet",
             "stack":"e2e@localhost:8092","tenant":"ci-test"}
}
```

`signature = oracle_id | op | sorted(wires) | run_mode | wire_code | normalized_json_pointer`. **`run_mode` is in the signature** so a dry-run finding never dedupes against a wet one.

**Severity rubric:** `critical` = tenant/principal isolation break, cross-request leak, canary in `details`, `INTERNAL_CODES` on the wire, unauthenticated terminal-state mutation. `high` = wire-contract break that would break a conforming buyer. `medium` = recovery drift, status-map miss, ordering instability, capability concealment. `low` = cosmetic. `info` = `divergences.yaml` entries — **still detected, still counted, never hidden.**

**Auto-verification gate:** every `critical`/`high` finding is replayed once from its generated `repro.py` in a **clean arena** before reporting. Reproduces ⟹ `confidence: confirmed`. Doesn't ⟹ `probable` + `flaky`, sorted below confirmed.

**Known-issue matching** — declarative, three-tier: all `expect` predicates pass → `high`; fingerprint cosine similarity over `(oracle_id, op, wire_codes, wire_set, severity)` above threshold → `medium`; no match → **`NOVEL`, sorted to the top.** Masking is **downgrade-only and always counted**; the report header always prints `masked: {gh-1583: 214}`.

### 3.9 `core/idempotency.py` — the vault

```python
class IdempotencyVault:
    def __init__(self, db_path: Path): ...
    def acquire(self, agent_id: str, op: str, payload: dict) -> str:
        """hash = adcp.server.idempotency.canonicalize.canonical_json_sha256(payload)
           same payload → SAME key; diff payload → NEW key"""
    def record_terminal(self, key: str, exchange: Exchange) -> None: ...
    def replayed(self, key: str) -> bool: ...
```

- Key format: UUID4 str (36 chars) — inside `[16,255]`, matches `^[A-Za-z0-9_.:-]{16,255}$`. **`idempotency_key` is REQUIRED on `CreateMediaBuyRequest`** (inherited unchanged from the library); omitting it yields `VALIDATION_ERROR` with `field=None` on REST and MCP (the builder omits-when-absent at `media_buy_create.py:4255-4259`) but `field="idempotency_key"` on A2A. Encode that as a `FIELD-POINTER-SHAPE` expectation, not a code divergence.
- Canonicalization is the **SDK's own module**. **Closed exclusion list**: top-level `idempotency_key`, `context`, `governance_context`; nested `push_notification_config.authentication.credentials`. **`ext` participates.**
- `client.use_idempotency_key()` is **not** used. The vault stamps the field explicitly.
- Keys are never logged in full: 8-char prefix; `ADCP_LOG_IDEMPOTENCY_KEYS=1` gates full logging.
- **Only `create_media_buy` has an end-to-end replay path** (`raw_wire_payload` threaded from `api_v1.py:233-251` through `media_buy_create.py:4388`). `update_media_buy` accepts the key on MCP but only folds it into `request_params` — **no replay store lookup**. Replay/conflict oracles are scoped to `create_media_buy`.

Idempotency oracles: `IDEM-SCOPE` (carriage-gated per FP-4); `IDEM-KEY-FORMAT` (`""` treated as absent, not an error); `IDEM-REPLAY-EQUIV`; `IDEM-CONFLICT` (**taint-scanned** no-leak); `IDEM-EXPIRED`; `IDEM-REPLAYED-FLAG` (`replayed_raw` unnormalized so `"true"`/`1` is flagged); `IDEM-CROSS-TRANSPORT` — probe with a **deprecated field name** on `/products`, `/media-buys`, or `/creatives/sync` (the only three paths `RestCompatMiddleware` rewrites) to put capture-point drift in play.

### 3.10 `buyer/` — the LLM brain

**Hard rule: the LLM never touches the wire.** It produces and revises a typed `CampaignPlan`; a deterministic executor turns plans into calls. Mutating ops are **not** LLM tools.

```
brief ─▶ discover (deterministic)  get_adcp_capabilities · list_creative_formats
       │                           list_authorized_properties · get_products
       ▼  rank      [LLM + MANDATORY deterministic fallback]
       ▼  plan      [LLM → CampaignPlan (pydantic, validated)]
       ▼  preflight (deterministic) budget math · state machine · available_actions · capability gate
       ▼  execute   (deterministic) vault → encode → call → rendezvous → decode → guard oracles
       ▼  monitor   get_media_buy_delivery + reporting webhook → LLM PacingVerdict → replan
```

`ProductSelector(model="claude-opus-4-8", use_llm=True)` with `_fallback_rank` **always available**: token-overlap over `name/description/formats` plus delivery-type preference. `MAX_PRODUCTS_FOR_PROMPT = 20`; strip ```` ```json ```` fences on the raw-fallback path. Every LLM failure falls back and logs `selection_mode="fallback"` — this is what lets CI and the fuzzer run the full flow with `--no-llm` at zero token spend.

Model config: `pydantic_ai.Agent` on **`claude-opus-4-8`**. Planning: `thinking={"type":"adaptive"}`, `output_config={"effort":"high"}`; ranker `effort="low"`. **Never** `budget_tokens`, `temperature`, `top_p`, `top_k`; no assistant prefill.

`Preflight` never reads `available_actions[]`/`valid_actions[]` directly — 3.1 structured `available_actions` carries mode+sla, legacy 3.0 `valid_actions` is flat; when only the legacy field is present assume `self_serve` and emit a one-shot deprecation warning. It enforces `MEDIA_BUY_STATE_MACHINE` client-side before dispatch. `check_budgets` clamps against `min_package_budget=1000.0` (the CI seed value, and **the** reason a first `create_media_buy` returns `BUDGET_TOO_LOW`).

**`monitor.py` has no REST path.** `monitorable_on = {MCP, A2A}` — `CreateMediaBuyBody` carries neither `reporting_webhook` nor `push_notification_config`. A REST campaign is monitored by re-reading over MCP, and the buyer logs `monitor_mode="cross_wire_readback"`.

**`buyer/guard.py`** — `GuardMode.{OFF, LOG, RAISE}`. In `LOG` (the default for `buy`), every seller response runs the arity-1 oracle set and violations go to `core/findings/store.py`. The buyer becomes a passive continuous fuzzer at zero cost, and **imports nothing from `fuzz/`**.

**Injection boundary:** product names, echoed briefs, error `message`/`suggestion`, and webhook bodies are **data**, rendered inside a delimited `<seller_data>` block with a standing untrusted-content instruction. Only deterministic fields (`product_id`, prices, format ids) flow into `CampaignPlan`.

### 3.11 `harness/` — local run

```python
class SalesAgentStack:
    def __init__(self, repo: Path = Path(r"C:\Users\pmezz\projects\salesagent"),
                 profile: Literal["dev","e2e"] = "e2e"): ...
    def up(self, *, seed: bool = True, timeout_s: int = 300) -> AgentTarget
    def down(self, *, volumes: bool = False) -> None
    def health(self) -> bool
    def seed(self) -> SeedResult
    def reset(self) -> None
    def environment(self) -> str      # recorded in Finding.target — NOT used by any oracle
```

Windows/PowerShell-safe: `subprocess.run` with an **argv list**, absolute paths, `cwd=repo`, health-wait polls.

**Standup gotchas, encoded in code not prose:**
- `docker compose up` alone gives an **empty database** — `db-init` runs only `scripts/ops/migrate.py`.
- `scripts/setup/init_database.py` creates Tenant + CurrencyLimit + AdapterConfig + Principal + Products but **no `PropertyTag`** — and the required chain is `Tenant → CurrencyLimit(USD) → PropertyTag('all_inventory') → Products`. `init_database_ci.py` **does** create it. **Always seed with `init_database_ci.py`.**
- **`docker-compose.e2e.yml` publishes ZERO host ports.** Overlay `-f docker-compose.e2e.ports.yml` or you get connection refused.
- Canonical target = e2e stack, `localhost:8092`, token `ci-test-token`, tenant `ci-test`.

**`compose/docker-compose.fuzz.yml` (new, always applied):**
```yaml
services:
  adcp-server:
    environment:
      DELIVERY_WEBHOOK_INTERVAL: "5"     # read at MODULE IMPORT — cannot be flipped at runtime
      ADCP_AUTH_TEST_MODE: "true"        # unlocks POST /test/auth for the approval driver
```

`stack.up()` asserts post-seed invariants and **fails loudly**: ≥1 tenant, ≥1 principal with a token, ≥1 `CurrencyLimit`, ≥1 `PropertyTag`, ≥2 products, **and** `tenants.auth_setup_mode = true` for the target tenant (required by `/test/auth`, `auth.py:800-822`).

**`harness/admin.py` (new) — the programmatic approval affordance.** Without it the human-gated async lifecycle is unreachable headlessly.

```python
class AdminDriver:
    def login(self) -> httpx.Client:
        """POST /test/auth, form {email, password, tenant_id}.
           Requires ADCP_AUTH_TEST_MODE=true AND tenants.auth_setup_mode=true
           (auth.py:770-822). Defaults test_super_admin@example.com / test123.
           Returns a COOKIE-persisting client — this is a Flask session, NOT a bearer token."""
    def approve_media_buy(self, tenant_id: str, media_buy_id: str, *, action="approve"):
        """POST /tenant/<tid>/media-buy/<mbid>/approve, form action=approve|reject.
           operations.py:290-304. THIS emits an MCP webhook (create_mcp_webhook_payload
           at operations.py:501/595).
           DO NOT use /​<tid>/workflows/<wid>/steps/<sid>/approve — workflows.py:152-256
           emits NO webhook at all."""
    def trigger_delivery_webhook(self, tenant_id: str, media_buy_id: str):
        """POST /tenant/<tid>/media-buy/<mbid>/trigger-delivery-webhook.
           operations.py:626-637 → trigger_report_for_media_buy_by_id(force=True),
           which bypasses the status filter, the frequency check, AND the 24h dedupe."""
    def force_status(self, media_buy_id: str, status: str) -> None:
        """Raw psycopg UPDATE. Needed because the scheduler filters
           status IN ('active','approved') but _determine_media_buy_status()
           NEVER returns 'approved'."""
```

The Flask admin is mounted at **both** `/admin` and `/` (`src/app.py:84-85`), so either prefix works.

`fixtures.py` adds a **second tenant** (isolation oracles) and budget-boundary products via **direct `psycopg` SQL**. We deliberately do **not** import salesagent's factory-boy factories.

`harness/doctor.py` runs before anything else: docker daemon up, clone path exists, ports free (dev 8000 / e2e 8092 / postgres 5435 / webhook 8788), `uv` present, `adcp==5.7.0` importable, `adcp.get_adcp_spec_version() == "3.1.0-beta.3"`, and — for Phase 6 — **host↔container webhook reachability advertised as the literal hostname `localhost`** (§5.4).

---

## 4. Cross-transport normalization — how differential comparison stays sound

The single biggest threat to a differential fuzzer's credibility is false positives from legitimate transport framing. The defense is an explicit two-tier contract plus a lossless audit trail.

### 4.1 Tier A — MUST be identical (compared after JCS)

- `error.adcp_error.code`, `.recovery`, `.suggestion`
- `len(error.errors)` and `{e.code for e in errors}`
- presence and value of `context`
- the normalized domain `payload`
- the JSON **type** of every payload leaf
- the **auth disposition** `(outcome, collection_emptiness)`

### 4.2 Tier B — legitimately transport-varying, excluded from equality, each owning its own oracle

| Field | Varies because | Its oracle |
|---|---|---|
| `task_id`, server `context_id` | per-call identity | must be present and stable within a call |
| top-level `message` | A2A `_serialize_for_a2a` adds `str(response)` | must appear **only** on A2A |
| `success` (bool) | A2A synthetic field | must appear **only** on A2A |
| HTTP status | REST-only carriage | INV-08, **allowed-set** semantics |
| JSON-RPC `error.code` | A2A-only carriage | `PROTOCOL-ERROR-FIDELITY` (#1670) |
| MCP `content[0].text` | human-readable mirror | must parse to the same envelope as `structuredContent` |
| `ProtocolEnvelope` wrapping | MCP/A2A wrap; **REST returns bare payload** | `ENVELOPE-WRAPPING` |
| **`adcp_error.field`** ◀ moved from Tier A | REST dot+numeric vs A2A bracket vs builder `None` — **proven** | `FIELD-POINTER-SHAPE`, compared only when both wires produced one |
| **schema-failure `code`** ◀ new Tier B | REST maps `RequestValidationError` → `INVALID_REQUEST` except `attribution_window.*` → `VALIDATION_ERROR` (`app.py:245`); A2A always `VALIDATION_ERROR` | `divergences.yaml` entry `op:"*"`, class `SCHEMA-ERROR-CODE-BY-WIRE`, `info` |
| MCP extra-field rejection | plain FastMCP `ToolError`, no envelope | `UNKNOWN-FIELD-CONSISTENCY` against the declared policy |

**#1670 forces a rule:** parity **never** asserts on `jsonrpc_code`. It is recorded and used only by the arity-1 A2A fidelity oracle.

### 4.3 The pipeline (ordered, individually toggleable, every firing logged)

1. **`unwrap_transport_frame`** — REST: body as-is. MCP: `structuredContent`, or on `isError=True` `json.loads(content[0].text)` **with a non-JSON fallback path**. A2A: last artifact DataPart, protobuf `Value` → Python. `WEBHOOK_A2A`: body as-is (already `MessageToDict` output). `WEBHOOK_MCP`: body as-is.
2. **`decamelize`** ◀ **NEW, webhook wires only.** `WEBHOOK_A2A` bodies are protobuf camelCase (`id`/`taskId`/`contextId`/`status.state`) with `TASK_STATE_*` → lowercase-hyphen and `ROLE_*` → lowercase already applied server-side (`protocol_webhook_service.py:52-73, 91-94`). The rule converts camelCase keys to snake_case and maps `id`/`taskId` → `task_id` so a webhook `Exchange` is comparable to a request `Exchange`. `WEBHOOK_MCP` bodies are already snake_case with `exclude_none=True`; the rule records "no-op" and moves on.
3. **`split_protocol_envelope`** — pop `TASK_ENVELOPE_FIELDS = {status, task_id, context_id, message, errors, adcp_version, ext, replayed, success}` into `.envelope`. **Guarded against the domain-`status` collision**: only pop top-level `status` when its value ∈ `TaskStatus` *and* the op's response model does not declare a top-level domain `status`. E2 sends a media buy whose domain `status` is `"working"` to confirm neither side conflates them.
4. **`strip_a2a_synthetic`** — remove `message`/`success`, record them.
5. **`numeric_policy`** — **default `STRICT`**: `1.0 ≠ 1`. A whole-float where a peer wire sent an int becomes a `TYPE-PARITY` finding. **This is how #1583 is rediscovered.** `TOLERANT` is **scoped**: `NormPipeline(numeric=TOLERANT, numeric_tolerant_wires={Wire.A2A})`. `--mask gh-1583` sets exactly that and nothing wider — a global TOLERANT would hide genuine int/float divergence on MCP↔REST. The mode **and the wire set** are recorded in every finding's `norm_log`.
6. **`drop_volatile`** — per-op `volatile_paths` replaced by ordinal tokens `<vol:1>` keyed by **first-seen value**, so the same id maps to the same token. Referential relationships survive.
7. **`sort_unordered`** — per-op declared unordered collections sorted by declared key. A separate oracle asserts ordering *stability*.
8. **`canonicalize`** — `rfc8785.dumps` → `differential_key = sha256(jcs(payload))`.

### 4.4 The soundness mechanism

Every rule that fires appends `(rule_id, json_pointer, before, after)` to `norm_log`, and **`norm_log` is embedded in every finding**. `adcp-buyer replay <finding_id> --no-norm` re-emits with the pipeline disabled.

**The error envelope is never normalized.** Only `payload` and framing pass through. `wire_error_envelope` is stored verbatim.

`spec/divergences.yaml` downgrades documented deltas to `info` with a one-line justification each — **downgrade, never delete**. Entries support `op: "*"`. The seed set:

```yaml
- id: div-auth-token-invalid
  op: "*"
  codes: [AUTH_TOKEN_INVALID]
  class: PROJECT_CODE
  note: "salesagent project code, not in STANDARD_ERROR_CODES. Aggregated by CODE-MEMBERSHIP."
- id: div-schema-error-code-by-wire
  op: "*"
  class: SCHEMA-ERROR-CODE-BY-WIRE
  note: "REST RequestValidationError → INVALID_REQUEST (except attribution_window.* → VALIDATION_ERROR, app.py:245); A2A always VALIDATION_ERROR (validation_helpers.py:36). Same status 400."
- id: div-rest-bare-payload
  op: "*"
  class: ENVELOPE-WRAPPING
  note: "REST returns bare payload; MCP/A2A wrap in ProtocolEnvelope. Documented Tier-B."
- id: div-rest-extra-ignore
  op: "*"
  wires: [rest]
  class: UNKNOWN-FIELD-CONSISTENCY
  note: "All 11 *Body models are bare BaseModel (FIXME #1442, allowlisted in test_architecture_no_bare_basemodel.py:46-56). extra=ignore in BOTH environments. Goes red when #1442 lands."
- id: div-mcp-protocol-extra-reject
  op: "*"
  wires: [mcp]
  class: UNKNOWN-FIELD-CONSISTENCY
  note: "FastMCP unexpected_keyword_argument ToolError — not an AdCP envelope. Oracles asserting adcp_error must skip."
- id: div-recovery-correctable
  op: "*"
  codes: [UNSUPPORTED_FEATURE, IDEMPOTENCY_CONFLICT, IDEMPOTENCY_EXPIRED]
  class: CODE-RECOVERY-TABLE
  note: "salesagent emits correctable where the SDK table says terminal."
- id: div-buying-mode-unreachable
  op: get_products
  wires: [rest, mcp, a2a]
  class: FIELD-REACHABILITY
  severity_override: medium
  note: "buying_mode + 12 other GetProductsRequest fields unreachable on ALL wires via create_get_products_request(). Uniform hole, NOT a parity divergence."
```

Default report shows NOVEL first, then confirmed non-info; `--include-info` shows everything.

---

## 5. Async / webhook / polling — locally (**fully rewritten**)

### 5.1 The five registration channels, and which ones a sender actually reads

This is the section v1 got wrong. There are **five** channels feeding **two different consumers**, and the spec-canonical one is dead.

| # | Channel | Entry point | Storage | Read by a sender? |
|---|---|---|---|---|
| 1 | tool arg `push_notification_config` (MCP / A2A skill / **not REST**) | `media_buy_create.py:2022-2075`; A2A injects at `adcp_a2a_server.py:1376-1385` | `push_notification_configs` **table** *and* `workflow_steps.request_data` (via `request_metadata` merge, `context_manager.py:193-194`) | **YES** — the DB row *gates* the loop (`context_manager.py:791-796`); the URL/auth actually used come from `request_data` (`:810-819`) |
| 2 | A2A `message/send` → `params.configuration.pushNotificationConfig` | `adcp_a2a_server.py:558-564`, stored `:581-582` | in-memory `self._task_push_configs` dict (`:186`) | **YES** — the **only** source `_send_protocol_webhook` reads (`:377`) |
| 3 | JSON-RPC `tasks/pushNotificationConfig/set` — **the A2A-spec-canonical registration** | `adcp_a2a_server.py:1122-1166` → `PushNotificationConfigUoW.upsert` | `push_notification_configs` table | **NO — DEAD.** No sender queries it. A spec-conformant buyer that registers and then sends a message receives **zero** webhooks |
| 4 | MCP HTTP headers `X-Push-Notification-Url` / `-Auth-Scheme` / `-Credentials` | `src/core/auth.py:42-67` | n/a | **NO — DEAD.** Zero callers repo-wide |
| 5 | `reporting_webhook` (application-level, **not** protocol) | create request → `media_buys.raw_request` (`repositories/media_buy.py:368,389`) | `media_buys.raw_request` JSON | **YES** — delivery scheduler `:103-107`, `:143-148`. **Not settable via REST** |

**Consequences that shape the build:**

- `transport/a2a.py` sets channel **2** inline on every `message/send` that needs a protocol webhook. Registering via channel 3 first and then sending a message produces nothing. **The harness must inline the config.**
- `_task_push_configs` is per-task, per-**process**, never evicted. The harness must run **single-process** (`src/app.py:295` constructs one module-level `AdCPRequestHandler`). Multi-worker deployments silently lose A2A webhooks — a finding in its own right.
- Channels 3 and 4 being dead are **high** findings the fuzzer must generate (`PNC-DEADCHANNEL` oracle, E3 target (d) in §3.8).

### 5.2 MCP: the webhook exists, but not where v1 said

**There IS a fully synchronous, non-human-gated MCP webhook off `create_media_buy`.** `_create_media_buy_impl` ends with `ctx_manager.update_workflow_step(step.step_id, status="completed")` (`media_buy_create.py:4062`), which calls `ContextManager._send_push_notifications` → `create_mcp_webhook_payload` → `ProtocolWebhookService.send_notification`, in-process, **no admin action**.

The only precondition is ≥1 **active** `PushNotificationConfig` row for `(tenant, principal)` — which the *same* `_impl` call creates at `media_buy_create.py:2042-2070` when the request carries `push_notification_config`. **A single MCP or A2A `create_media_buy` carrying `push_notification_config` is self-sufficient.**

Corollaries the plan must encode:
- A harness that only *inserts a DB row* without passing `push_notification_config` in the request gets nothing (the URL comes from `request_data`, not the row).
- `step.request_data["protocol"]` is `identity.protocol`, so **the transport of the original call decides the webhook body shape**: A2A-originated → `create_a2a_webhook_payload` (`context_manager.py:859-865`); MCP/REST-originated → `create_mcp_webhook_payload` (`:866-873`).
- An **A2A `create_media_buy` with an inline push config produces TWO webhook fires** for one call: the A2A protocol webhook (from `_task_push_configs`) *and* the context_manager webhook (a2a-shaped). The sink must expect and correlate both, not treat the second as a duplicate defect.
- The Admin-UI approve path (`operations.py:501/595`) is a **separate** code path that only runs when auto-create is disabled — reachable headlessly via `AdminDriver.approve_media_buy`.

**Every caller of `create_mcp_webhook_payload`:**

| Site | Human-gated? | Programmatic driver |
|---|---|---|
| `context_manager.py:868` (`_send_push_notifications`) | **NO** | just call MCP/A2A `create_media_buy` with `push_notification_config` |
| `admin/blueprints/operations.py:501` (approve) | YES | `AdminDriver.approve_media_buy(action="approve")` |
| `admin/blueprints/operations.py:595` (reject) | YES | same route, `action="reject"` |
| `admin/blueprints/creatives.py:257` (creative approval) | YES | creatives blueprint route |
| `services/delivery_webhook_scheduler.py:331` | **NO** | hourly loop **or** `AdminDriver.trigger_delivery_webhook` |

### 5.3 The delivery scheduler — timing, and how not to hang

| Item | Value | Evidence |
|---|---|---|
| Default interval | **3600 s** | `delivery_webhook_scheduler.py:33`, `int(os.getenv("DELIVERY_WEBHOOK_INTERVAL") or "3600")` |
| Env override | `DELIVERY_WEBHOOK_INTERVAL`, read **at module import** (empty string → 3600) | must be set in the container env before start — cannot be flipped at runtime |
| Send on startup | **Yes**, before the first sleep | `:77-86` |
| Dedup window | **24 h** | `:187-205` |
| Dedup predicate | `media_buy_id` = X ∧ `task_type='media_buy_delivery'` ∧ `notification_type='scheduled'` ∧ `status='success'` ∧ `created_at > now-24h` | `:190-196` |
| Status filter | `status IN ('active','approved')` | `:95` |
| Frequency filter | only `daily` (unless `force=True`) | `:170-179` |
| Force/manual bypass | skips frequency **+** dedupe **+** status filter | `:122-155` |

**Three binding harness rules:**

1. `compose/docker-compose.fuzz.yml` injects `DELIVERY_WEBHOOK_INTERVAL: "5"`. Without it a media buy created after startup waits up to an hour.
2. **Mint a fresh media buy per webhook case.** The 24h dedupe makes any second assertion on the same buy unreachable.
3. **The manual trigger poisons the scheduled path.** `trigger_report_for_media_buy_by_id(force=True)` still stamps `notification_type = scheduled` unconditionally (`:266`), writing a `WebhookDeliveryLog` row that satisfies the dedup predicate for 24 h. Never assert on a scheduled fire after a manual trigger for the same buy.

Also: `status='approved'` is a value `_determine_media_buy_status()` (`media_buy_create.py:245-299`) **never returns**, and nothing in the create/approve path writes it. The scheduled path is therefore unreachable in production for any buy not already `active`. Use `AdminDriver.force_status` (mirroring `tests/e2e/utils.py:38-76`) or, preferably, the manual trigger. `reporting_webhook` must be passed **in the create request** — there is no post-hoc attachment short of a DB write — with `frequency='daily'` or omitted.

### 5.4 Webhook reception — the local story

**The seller runs in Docker; the buyer runs on the Windows host.** `protocol_webhook_service.py:106-122` rewrites a hostname of exactly `localhost` (case-insensitive) to `host.docker.internal`, preserving userinfo, port, and query. So the buyer registers `http://localhost:8788/...` and the dockerized seller reaches the host. No tunnel, no ngrok.

**The match is exact.** `127.0.0.1`, `[::1]`, and `localhost.localdomain` are **not** rewritten — inside a container `127.0.0.1` means the container's own loopback and the request fails. `doctor` and `sink` therefore **advertise the literal string `localhost`**, and a dedicated fuzz case registers `127.0.0.1` and asserts the delivery failure is *surfaced*. It is not: `send_notification` returns a `bool` and **every call site discards it** (`adcp_a2a_server.py:435-440`; `context_manager.py:885-916`; `operations.py:510-519`, `:604-614`; `delivery_webhook_scheduler.py:340-342`). Silent webhook drop is a legitimate **high** finding.

Two further facts the sink design depends on:
- **`webhook_delivery_log` only records delivery-scheduler webhooks.** `protocol_webhook_service.py:307-312` (and four sibling guards) requires `task_type ∈ {delivery_report, media_buy_delivery}` **and** `media_buy_id`/`tenant_id`/`principal_id`; context_manager metadata (`:875-879`) has no `media_buy_id`, and `operations.py` metadata carries only `task_type`. **A harness cannot verify protocol-webhook delivery by querying the DB — it must use the HTTP sink**, plus log scraping for failures (`"Webhook failed for task ..."` at `protocol_webhook_service.py:346/416/462`).
- **No SSRF validation on the send path.** `WebhookURLValidator` (`core/webhook_validator.py:46-84`) is called only from Admin UI routes. A sink on `localhost` or a private IP is accepted with no allowlist flag.

`core/tasks/sink.py` — FastAPI on `ADCP_WEBHOOK_PORT` (default 8788):

```
POST /adcp/webhook/{task_type}/{agent_id}/{operation_id}
```

```python
raw = await request.body()                       # RAW BYTES — never re-serialize
payload = json.loads(raw)
wire = Wire.WEBHOOK_A2A if ("taskId" in payload or "id" in payload) else Wire.WEBHOOK_MCP
op   = ledger.resolve_op(operation_id, payload)  # ◀ webhook → op resolution, §5.5
ex   = build_webhook_exchange(wire, op, payload, headers=request.headers, raw=raw)
run_arity1(ex, Phase.WEBHOOK)                    # webhook oracles — no parity peer exists
ledger.deliver(operation_id, ex)                 # sets the asyncio.Event
```

**Signature verification.** HMAC mode sends `X-AdCP-Signature: sha256=<hex>` and `X-AdCP-Timestamp`, over `"{timestamp}.{json.dumps(payload)}"` with **default separators** (`protocol_webhook_service.py:186-194`; `adcp/webhooks.py:265-288`). Because `requests` `json=` produces the same bytes, raw-body HMAC also works — the sink verifies over the raw bytes and records a mismatch as `webhook_signature_mismatch`. salesagent honors exactly `"HMAC-SHA256"` and `"Bearer"`; anything else **silently sends unsigned**, so the sink **rejects** a webhook whose signing mode differs from what was registered (`webhook_mode_mismatch`) — downgrade resistance.

**Body shape per transport — what the sink must normalize:**

| Aspect | `WEBHOOK_A2A` | `WEBHOOK_MCP` |
|---|---|---|
| Serializer | `MessageToDict(payload, preserving_proto_field_name=False)` | `model_dump(mode="json", exclude_none=True)` |
| Case | camelCase | snake_case |
| Task id key | `id` (final-state `Task`) **or** `taskId` (`TaskStatusUpdateEvent`) | `task_id` |
| Context key | `contextId` | `context_id` |
| Status location | `status.state` | top-level `status` |
| Enum form | `TASK_STATE_COMPLETED`→`completed`, `TASK_STATE_INPUT_REQUIRED`→`input-required`, `ROLE_AGENT`→`agent` | plain AdCP `TaskStatus` string |
| Payload location | `artifacts[].parts[].data` (final) / `status.message.parts[].data` (intermediate) | `result` |
| Dedup key | **none in body** | `idempotency_key` (required, fresh UUID4 per fire — salesagent never pins it) |
| Null fields | present | omitted |

A2A protocol webhooks fire at **four** points in `on_message_send`: `:681` `submitted` (manual-approval early return), `:740` `failed` (all skills failed), `:942` `completed`/`submitted` (normal), `:975` `failed` (unhandled exception).

**Delivery-report task_type is deliberately divergent**: the wire carries `task_type="update_media_buy"` while the internal metadata label is `"media_buy_delivery"` (`delivery_webhook_scheduler.py:321-336`), and `task_id` there is the **media_buy_id**, not a task id. The sink asserts `update_media_buy` on delivery reports. Also, untrusted `tool_name` labels are coerced to the closed `TaskType` enum with fallback `"update_media_buy"` (`webhook_validator.py:15-43`) — the sink must not assume `task_type` round-trips the originating tool.

### 5.5 Webhook → op resolution and the webhook oracle set

`TaskLedger` gains `resolve_op(operation_id, payload) -> str`, resolving in order:
1. `operation_id` path segment → ledger row → `(op, wire, task_handle)`. This is the canonical path; we set the spec's `operation_id` field **and** the URL path segment, and the fuzzer uses the disagreement to tell a URL-parsing seller from a field-reading one.
2. `payload["task_id"]` / `payload["id"]` / `payload["taskId"]` → ledger `task_handle` column.
3. `payload["task_type"]` → best-effort op, tagged `resolution="weak"`.
4. Otherwise `op="<unresolved>"`, `severity=medium` finding `WEBHOOK-UNCORRELATABLE`.

**Webhook oracle set (`webhook_o.py`, all arity-1 — there is no parity peer):**

| Oracle | Check |
|---|---|
| `WH-SHAPE` | body matches the declared per-wire shape table above |
| `WH-STATUS-ENUM` | normalized status ∈ the 9-member spec enum |
| `WH-TOKEN-ECHO` | registered `token` echoed verbatim |
| `WH-SIGNATURE` | HMAC verifies over raw bytes; mode matches registration |
| `WH-IDEMPOTENCY-KEY` | `WEBHOOK_MCP` only: present, 16-255 chars, `^[A-Za-z0-9_.:-]+$` |
| `WH-CORRELATION` | resolves to a ledger row; `resolution != "weak"` |
| `WH-TAINT` | canaries from the originating request must not appear in webhook `details` |
| `WH-DOUBLE-FIRE` | A2A create with inline pnc yields **exactly 2** fires (protocol + context_manager); more or fewer is a finding |
| `PNC-DEADCHANNEL` | register via channel 3 (`tasks/pushNotificationConfig/set`) only, then `message/send` → **zero** webhooks ⟹ **high** finding |
| `WH-SILENT-DROP` | register `127.0.0.1`, drive a fire, assert the failure appears somewhere observable (it does not) ⟹ **high** |

### 5.6 Polling and rendezvous — **per-wire priority, inverted from v1**

`core/tasks/lifecycle.py` defines the **9-member spec enum**: `submitted, working, input-required, completed, failed, canceled, rejected, auth-required, unknown`. `TERMINAL = {completed, failed, canceled, rejected}`. `PAUSED = {input-required, auth-required}`. The SDK's 5-member enum never leaves the transport module.

| Wire | **Primary** | Secondary | Why |
|---|---|---|---|
| **MCP** | **polling** (`get_task` tool, bare dict, requires non-anonymous principal) | webhook opportunistic | the context_manager webhook fires only when the request carried `push_notification_config`; polling always works |
| **A2A** | **webhook** (channel 2, inline on `message/send`) | polling via `GetTask` | `_send_protocol_webhook` fires at 4 points; but `self.tasks` is an in-memory, non-identity-scoped, process-bound dict (#1702) so polling is unreliable by construction |
| **REST** | **none** | cross-wire readback | no task endpoints, no pnc, no reporting_webhook. `monitor_mode="cross_wire_readback"` via `get_media_buy_delivery` / `get_media_buys` over MCP |

```python
async def await_terminal(op_id: str, *, wire: Wire, timeout: float,
                         poll_interval: float = 60) -> Exchange:
    """Both arms are always armed; PRIORITY decides which is cancelled first on a tie
       and which one's absence is logged as degraded rather than failed."""
```

Rules ported verbatim from adcp-client `TaskExecutor.pollTaskCompletion`:
- Default interval 60 s; **in-band `working` wait capped at 120 s** (AdCP PR #78); exponential backoff with jitter; hard deadline.
- **Paused states never spin.** `input-required` / `auth-required` return an intermediate success `Exchange` immediately.
- **Task eviction** during polling → descriptive `failed` Exchange recommending webhooks, never an uncaught exception.
- **Never conflate handles.** `operation_id` (buyer UUID) ≠ `task_handle` (server-assigned). Separate `Exchange` fields; the ledger keys on `operation_id`.
- **Wrapper-unwrap depth bounded at 8.** E2 sends a 12-deep nested envelope and asserts graceful `unknown`.

`TaskLedger` (SQLite) survives restart: `(operation_id) → (agent_id, wire, op, task_handle, context_id, idempotency_key, checkpoint_json, status, last_result_json)`. On boot every non-terminal row is re-armed.

Sequence traces spanning an A2A `GetTask` are marked `process_bound`; the shrinker never reduces across a server restart; **a `GetTask` returning another principal's task is a `critical` isolation finding (INV-34 / #1702)**.

**`RunMode.DRY_RUN` disables all of §5.** `dry_run` guards both the workflow-step creation and the push-config DB registration (`media_buy_create.py:1993`), and `:3493-3510` returns before the adapter call, so `:4062`'s `update_workflow_step` is never reached. **A dry-run `create_media_buy` NEVER produces a webhook of any kind.** Any webhook assertion under `RunMode.DRY_RUN` is a harness bug; `rendezvous.await_terminal` raises `RuntimeError` if called with `run_mode is DRY_RUN`.

---

## 6. Build phases — dependency-ordered, with explicit parallel lanes

Two structural properties preserved from v1: **the stack is Phase 0, not Phase 8**, and **the fuzzer ships before the brain**.

| Phase | Lane | Builds | Depends on | Parallel? |
|---|---|---|---|---|
| **P0** | — | `uv init`, deps + hard pin, `config.py`, `cli.py` skeleton, `obs/`, `harness/doctor.py`, `harness/stack.py` (**incl. `compose/docker-compose.fuzz.yml`**), `harness/seed.py`, `harness/admin.py`, `scripts/*.ps1` | — | **Sequential — blocks everything** |
| **P1** | — | `core/exchange.py` (incl. `RunMode`, webhook wires), `core/envelope.py`, `core/ops.py`, `core/a2a_methods.py`, `core/matrix.py`, `core/identity.py`, `core/session.py`, **`core/spec/*` incl. `canonical.py` + `DiffEntry` + `status_map.py` (allowed-SETS) + `jsonrpc.py`**, `core/findings/model.py` + `signature.py` | P0 | Sequential (tiny, pure data, no network) |
| **P2a** | T | `transport/wiretap.py` + `transport/rest.py` + minimal `client.call()` | P1 | ← start here |
| **P2b** | T | `transport/mcp.py` + **MCP tool-set enumeration written into `ops.MCP_TOOL_NAMES`** | P1, P2a wiretap | ‖ with P2c |
| **P2c** | T | `transport/a2a.py` — `message/send` **and** the 21-method surface, both vocabularies | P1, P2a wiretap | ‖ with P2b |
| **P2d** | T | `binder`, `client.call_all()`, **4-part registry drift test**, **per-wire `x-dry-run` propagation gate** | P2a-c | after b+c |
| **P3a** | N | `core/normalize/{rules,pipeline}.py` incl. `decamelize` | P2d + golden fixtures from P2 | ‖ with P3b, P3c (**genuinely** — `canonical.py` is in P1) |
| **P3b** | C | `core/codec.py`, `core/compat.py` (D1′/D2/D3/D4 + divergence tests), `core/capabilities.py` | P1 + `canonical.py` from P1 | ‖ |
| **P3c** | H | `harness/fixtures.py`, `fuzz/arena.py` (NAMESPACE first) | P0 | ‖ |
| **P4a** | O | **SERIAL, ~½ day.** `oracles/registry.py`, `Violation`, `auth_disposition.py`, `taint.py`, per-op `volatile_paths`/`unordered` declarations in `ops.py`, `divergences.yaml` seed set | P3a | Sequential — everything in P4b imports these |
| **P4b** | O | Fan out **per oracle module**: `envelope_o`, `auth_o`, `lifecycle_o`, `idempotency_o`, `budget_o`, `isolation_o`, `listing_o`, `parity_o`, `capability_o`, `protocol_o`, `webhook_o`; `tests/unit/` + `tests/oracle_mutation/` | **P4a** | 6-8 way parallel — they now share only `registry.py`, `spec/`, and the P4a primitives |
| **P5** | ★ | `fuzz/engines/differential.py`, `core/findings/store.py`, `fuzz/known_issues.{py,yaml}`, **8** hand-written reproducer seeds, `tests/validation/` | P4b, P2d | **THE MILESTONE — sequential, nothing else ships first** |
| **P6a** | I | `core/idempotency.py` (vault + JCS), `core/tasks/*` (lifecycle, ledger, sink, poller, rendezvous), webhook oracle wiring | P3b, P4a | ‖ with P6b |
| **P6b** | F | `fuzz/strategies/*`, `fuzz/engines/schema.py`, `fuzz/corpus.py`, coverage tokens, `hypothesis.target()` | P5 | ‖ with P6a |
| **P7a** | F | `fuzz/engines/sequence.py`, `fuzz/shrink.py`, `arena` TENANT mode, idempotency replay sub-engine | P6a **and** P6b | ‖ with P7b |
| **P7b** | B | `buyer/*` | P4b (oracles) + P6a (vault/tasks) — **not** P6b/P7a | ‖ with P7a |
| **P8** | — | `fuzz/report.py`, `fuzz/repro.py`, `transport/sdk_crosscheck.py` + byte-parity test, CI nightly `fuzz all --budget 30m` | P5, P7a | ‖ sub-tasks |

**Fan-out summary:** P0→P1→P2a is a hard serial spine (~2 days). Peak parallelism is **P4b** (one implementer per oracle module) and **P6/P7** (fuzz-depth vs buyer-brain). True reconvergence points: **P2d**, **P4a→P4b**, and **P5**.

---

## 7. Verification plan

Every phase has a gate that runs against a **live salesagent in Docker**. Standard target: e2e stack, `http://localhost:8092`, token `ci-test-token`, tenant `ci-test`, seeded by `scripts/setup/init_database_ci.py`.

```powershell
# canonical standup — BOTH overlays are MANDATORY
docker compose -f docker-compose.e2e.yml -f docker-compose.e2e.ports.yml `
               -f ..\adcp-buyer-agent\compose\docker-compose.fuzz.yml up -d
docker compose -f docker-compose.e2e.yml -f docker-compose.e2e.ports.yml `
  exec adcp-server python scripts/setup/init_database_ci.py
uv run adcp-buyer doctor
```

| Phase | Gate | How it's proven |
|---|---|---|
| **P0** | `doctor` all green; `stack up` → `/health` 200; seed asserts ≥1 tenant / principal / CurrencyLimit / PropertyTag / ≥2 products **and** `auth_setup_mode=true`; `AdminDriver.login()` returns a working session | `tests/contract/test_stack.py` |
| **P1** | envelope validator round-trips golden two-layer envelopes; `adcp.get_adcp_spec_version() == "3.1.0-beta.3"`; `adcp.__version__ == "5.7.0"`; `status_map.ALLOWED_STATUSES["SERVICE_UNAVAILABLE"] == {500,502,503}` | `tests/unit/` — no network |
| **P2a** | `probe --wire rest` returns real products. All 12 routes reachable; auth-optional degrades to `identity=None`; auth-required → error envelope with `AUTH_TOKEN_INVALID` (**code only — status is INV-08's**) | `tests/contract/test_rest_routes.py` |
| **P2b** | `probe --wire mcp` green; `/mcp/` trailing slash works; **`ops.MCP_TOOL_NAMES` written from live `list_tools()`**; every registry MCP binding is in that tuple | `tests/contract/test_mcp_toolset.py` |
| **P2c** | `probe --wire a2a` green; `/a2a` asserts **non-307**; all 21 method names reachable or MethodNotFound as declared; **`ListTasks` returns -32004 while literal `tasks/list` returns -32601** | `tests/contract/test_a2a_methods.py` |
| **P2d** | `call_all("get_products")` returns 3 `Exchange`s; **4-part drift test** passes (MCP tuple / A2A 18 dispatch / A2A 16 card / REST 12 from the router); **`x-dry-run` propagation confirmed on all three wires** by observing a `dry_run_`-prefixed `media_buy_id` and the absence of any `push_notification_configs` row | `tests/contract/test_registry_drift.py`, `test_dry_run_propagation.py` |
| **P3a** | normalized `get_products` payloads are **JCS-equal** across MCP/A2A modulo a recorded, human-reviewed `norm_log`; `decamelize` round-trips a captured A2A webhook body | `tests/contract/test_normalization_parity.py` |
| **P3b** | D1′/D2/D3/D4 each have a test asserting the divergence **still exists** | `tests/contract/test_divergences.py` |
| **P4a** | `AuthDisposition` derives correctly from `(outcome, collection_emptiness)` on 12 synthetic fixtures **without reading `errors[]`**; taint scanner finds a planted canary at every nesting depth | `tests/unit/test_p4a_primitives.py` |
| **P4b** | every oracle has ≥1 passing fixture **and** ≥1 firing fixture; no oracle is silent; **no oracle imports `http_status` except INV-08** (AST guard) | `tests/oracle_mutation/`, `tests/unit/test_no_status_hardcoding.py` |
| **P5** | ★ **all 8 known bugs rediscovered from a cold stack**; report prints `parity untested for N ops` | `tests/validation/test_rediscovers_known_issues.py` |
| **P6a-i** | **HARD BLOCKER.** Webhook **reachability**: a synthetic `POST http://localhost:8788/...` issued from *inside* the container (`docker compose exec adcp-server python -c "..."`) reaches the sink, proving the `localhost → host.docker.internal` rewrite path and the Windows firewall hole | `tests/e2e/test_webhook_reachability.py` |
| **P6a-ii** | **SOFT (may be marked `xfail-environmental`).** A **real async completion webhook**: MCP `create_media_buy` with `push_notification_config` (fresh buy) → context_manager fire observed at the sink; A2A `message/send` with inline `pushNotificationConfig` → **exactly 2** fires; `AdminDriver.trigger_delivery_webhook` → one `task_type="update_media_buy"` delivery report | `tests/e2e/test_webhook_roundtrip.py` |
| **P6a-iii** | idempotency: same payload submitted twice → **one key, one media buy, `replayed=true` on the second** — `create_media_buy` only | `tests/e2e/test_idempotency.py` |
| **P6b** | 1000 hypothesis examples over `get_products` + `create_media_buy` with **zero uncaught exceptions in the fuzzer itself** + a triaged finding list | `adcp-buyer fuzz schema --examples 1000` |
| **P7a** | a **deliberately injected** bug (scratch container with a patched `valid_actions` table) is found and shrunk to ≤3 steps | `tests/validation/test_shrinker.py` |
| **P7b** | `buy --brief "..." --no-llm` completes on MCP; then with Claude; then green on all 3 wires; double-submit yields one buy; guard mode logs with no crashes; REST run reports `monitor_mode="cross_wire_readback"` | `tests/e2e/test_buy_flow.py` |
| **P8** | run-over-run diff distinguishes NEW from KNOWN; every `high` finding has a `repro.py` that reproduces in a clean arena | `adcp-buyer fuzz all --budget 30m --report md` |

### 7.1 The known-bug rediscovery suite — the project's proof of teeth (**8 entries**)

| # | Bug | Oracle | Driving case (all verified reachable) |
|---|---|---|---|
| 1 | **#1670** typed `A2AError` flattened | `PROTOCOL-ERROR-FIDELITY` | Primary: `message/send` with `skill:"no_such_skill"` ⟹ expect **-32601**. Plus, all confirmed to raise a typed error **from salesagent**: `ListTasks` ⟹ -32004; `GetExtendedAgentCard` ⟹ -32004; `SubscribeToTask` ⟹ -32004; `tasks/pushNotificationConfig/set` with no `url` ⟹ -32602; same with no token ⟹ -32600; `.../get` with unknown `id` ⟹ -32001; `.../delete` with no `id` ⟹ -32602; `message/send` on a non-discovery skill with no auth ⟹ -32600; any of the 5 stub skills ⟹ -32004. **Excluded:** `tasks/get` (no raise — see #8 below), `tasks/cancel` (no raise), `tasks/resubscribe` (degrades to SSE) |
| 2 | **#1583** protobuf int→float | `TYPE-PARITY` (numeric STRICT, tolerance scoped to A2A) | `update_media_buy` then read the revision counter on all 3 wires |
| 3 | **#1651** token-less `get_media_buys` | `AUTH-DISPOSITION-PARITY` | no-token `get_media_buys`, MCP vs A2A (REST route absent → `coverage:single_pair`). Disposition from `(outcome, collection_emptiness)` only |
| 4 | **#1316** delivery account scope on A2A | `ACCOUNT-SCOPE-PARITY` | `get_media_buy_delivery` with a foreign `account`, A2A vs REST |
| 5 | **#1449** MCP advisory `errors[]` dropped | `ADVISORY-ERRORS-PARITY` | an op emitting advisory `errors[]` on MCP success, across all 3 |
| 6 | **A2A `list_authorized_properties` drops buyer filters** (replaces v1's refuted `buying_mode` row) | `FIELD-REACHABILITY` | `list_authorized_properties` with a `property_tags` value that provably narrows the result on REST and MCP; A2A (`:1860` builds `ListAuthorizedPropertiesRequest(context=...)` only) returns the unfiltered set. Severity **high** — silent failure |
| 7 | **`AUTH_TOKEN_INVALID` vs `AUTH_REQUIRED`** | `CODE-MEMBERSHIP` (aggregated) + `AUTH-CODE` | any auth-required op with a garbage token. Lands as **one** run-level `info` row, proving the divergence/masking machinery works end-to-end |
| 8 | **#1702** A2A task store unauthenticated + unscoped | `TASK-STORE-ISOLATION` (INV-34) | Arena `TENANT` mode: principal A does `message/send` on `create_media_buy`, capture `task.id`; principal B (and an **unauthenticated** client) issue `GetTask` with that id and receive A's Task including its artifacts. Severity **critical**. Companion assertion in the same case: `CancelTask` on that COMPLETED task succeeds ⟹ INV-36 |

Failure message: *"Either the seller fixed gh-XXXX (set `status: fixed` in known_issues.yaml) or the fuzzer regressed — the oracles have lost teeth."*

**Expected free win:** P5's first run of `parity` + `envelope` over just `get_products` / `list_creatives` / `get_media_buys` / `list_authorized_properties` should surface **#1651, #6, and the `AUTH_TOKEN_INVALID` divergence with zero mutation logic**. If it doesn't, the transport layer is wrong, not the seller.

### 7.2 High-confidence NOVEL targets the plan must be able to reach

These are verified source defects with no GitHub issue. They are **not** in `known_issues.yaml` — the point is that the fuzzer finds them cold. Listed so implementers can confirm the machinery reaches each one.

| Target | Oracle | Evidence |
|---|---|---|
| Spec-canonical `tasks/pushNotificationConfig/set` is write-only/dead | `PNC-DEADCHANNEL` | `:1156-1166` writes; `_send_protocol_webhook:377` reads only `_task_push_configs` |
| `CreatePushConfig` never validates `task_id`; echoes `task_id or "*"` | `PNC-SCOPE` | `:1178` |
| `ListPushConfigs` ignores `task_id` as a filter but stamps it on every row | `PNC-SCOPE` | `:1216-1218`, `:1227` — the response is affirmatively wrong |
| `on_cancel_task` unauthenticated + state-blind | `TASK-CANCEL-STATE` (INV-36) | `:1022-1041`; `TaskNotCancelableError` unused in `src/` |
| `get_creatives` handler orphaned | `ORPHAN-HANDLER` | `:1698` vs `:1402-1429` |
| Agent card wrong in **both** directions | `CAPABILITY-TRUTHFULNESS` + `CAPABILITY-CONCEALMENT` | hides 2 implemented-signature skills; advertises 3 that always -32004 |
| REST silently drops **both** webhook channels | `FIELD-REACHABILITY` | `api_v1.py:71-79, 254-277` vs `media_buy_create.py:4380-4381` |
| A2A `create_media_buy` validates `ext` then discards it | `FIELD-REACHABILITY` | `:1574` accepts, `:1583-1595` omits |
| A2A `get_media_buys` `model_validate` not wrapped in `adcp_validation_boundary()` | `ENV-TWO-LAYER` (raw ValidationError leaks, no envelope) | `:1922` vs siblings at `:1573/:1891/:1949/:1982` |
| Four REST routes construct request models with no validation boundary → `field=None` + full pydantic repr as the buyer-facing message | `ENV-FIELDS` + `FIELD-POINTER-SHAPE` | `api_v1.py:208, 222, 367, 377` → `app.py:196-209` → `exceptions.py:965` |
| `context_manager` logs webhook **success** when `send_notification` returned `False` | `WH-SILENT-DROP` | `context_manager.py:899-903` — `t.result()` on a bool-returning coroutine |
| N duplicate webhooks when N active `PushNotificationConfig` rows exist | `WH-DOUBLE-FIRE` | `context_manager.py:791-806` iterates all configs with an **unused** loop variable; `media_buy_create.py:2039` mints a fresh `config_id` per call |
| Retry backoff can raise `ValueError` (`second` must be 0..59), swallowed | log-scrape + `WH-SILENT-DROP` | `protocol_webhook_service.py:394-395` uses `.replace(second=sec+wait)` instead of `timedelta` |
| `_task_push_configs` unbounded, never evicted, process-local | `PNC-DEADCHANNEL` note + a memory-growth soak case | `:186`, `:582` |
| `to_wire_dict` backport is dead duplication; its FIXME cites adcp 4.3.0 but 5.7.0 is installed | contract test only, `info` | `protocol_webhook_service.py:40-44` vs `adcp/webhooks.py:1135-1180` |

### 7.3 Standing run discipline

- **Serial by default.** Live e2e shares one DB with no per-test rollback; xdist races on shared rows and *silently drops transports at collection* (a false green). E1/E2 may parallelize over read-only ops with a worker pool; **E3 and all mutating cases are strictly serial.**
- **Read-only + auth ops first.** P5–P6 fuzz reads and auth only (where 6 of 8 known bugs live). Mutating oracles arrive in P7a behind `arena.reset()` between cases, in `RunMode.WET`.
- **Dry-run is a separate labelled campaign**, never mixed into a differential run.
- **Unreachable stack is a hard `RuntimeError`, never a skip.**
- Tiered budgets: `smoke` <60 s (seeds only) · `pr` 5 min · `nightly` 30–60 min.

---

## 8. Risks & open decisions (ranked)

| # | Risk | Why it's real | Mitigation | Owner phase |
|---|---|---|---|---|
| **R1′** | **A large share of `GetProductsRequest` is unreachable on every wire**, and none of it is a REST-specific divergence | v1 built a known-issue gate row on a premise that is false; repeating that mistake burns a milestone | Declared once in `divergences.yaml` as a uniform hole at `medium`; `FIELD-REACHABILITY` emits **one aggregated row**, never per-wire parity findings. Verify empirically in P2, never assume | P2/P3b |
| **R2** | **SDK cannot supply raw error-path wire** | An SDK-based fuzzer is blind to #1449, #1670, #1583 — the bugs it exists to find | **Retired by architecture**: hand-rolled raw transports; SDK for models only; `sdk_crosscheck` is contract-test-only | P2 |
| **R3** | **Dev stack seeds nothing; `init_database.py` omits `PropertyTag`** | First `get_products` returns `[]`; `create_media_buy` fails obscurely | Always seed with `init_database_ci.py`; `stack.up()` asserts the chain and **fails loudly** | P0 |
| **R4** | **False positives from legitimate transport framing** | The single biggest threat to credibility | Two-tier normalization with `field` and schema-failure-code moved to Tier B; every Tier-B field owns an oracle; `norm_log` in every finding; `--no-norm` replay | P3a/P4 |
| **R5′** | **The A2A/webhook surface drifts faster than this document** | Two of v1's three A2A sections were wrong within one release | Every count is a **separate** drift assertion against the live server; nothing is hardcoded from a digest. `ops.MCP_TOOL_NAMES` is *written by a gate*, not typed by a human | P2b/P2d |
| **R6** | **SDK auto-mints idempotency keys** | A retry through the SDK double-books | `IdempotencyVault` mints before first attempt and stamps explicitly | P6a |
| **R7** | **`update_media_buy` request shape is structurally incompatible** — no `account`/`revision`/`canceled`/`new_packages`. **Cancellation has no expressible request shape** | Spec-conformance testing of update is partly blocked | Codec emits salesagent's shape from spec-typed intent; the cancel flow is marked NOT-IMPLEMENTABLE with the gap filed as a P0 conformance finding | P3b |
| **R8** | **Shared-DB state pollution across fuzz cases** | Findings become non-reproducible | P5–P6 read-only + auth only; mutating oracles in P7a behind arena reset; **never parallelize mutating fuzz** | P7a |
| **R9** | **Fuzzer bugs reported as seller bugs** | Destroys credibility of every finding | `confidence` field; every critical/high auto-replayed from generated `repro.py` in a clean arena; oracle-mutation tests prove each oracle can both fire and not fire | P5 |
| **R10** | **`create_media_buy` is a wall of required fields**: `account` (optional in salesagent, required in the library), `brand`, `packages` (**no** `MinLen(1)` in salesagent), `start_time` (**`StartTiming` object**), `end_time` (tz-aware), `idempotency_key` (**required**, 16-255); response is a `UnionType` with no `.model_fields`; budget must clear `min_package_budget=1000` | The most common "why does nothing work" sink | Build one known-good request in P2 as a pinned fixture. Branch on `Exchange.status`, **never** `hasattr` | P2/P7b |
| **R11′** | **The whole local async story depends on `localhost` matching exactly** | `127.0.0.1` silently fails; every send-failure return value is discarded at every call site | P6a-i is a **hard blocker** proven by an in-container synthetic POST; P6a-ii (real async completion) is **soft** and may be `xfail-environmental`; poller always armed; `doctor` advertises the literal `localhost`; Windows Firewall prompt documented | P6a |
| **R12′** | **A2A task store is an in-memory, non-identity-scoped, process-global dict (#1702)** with no owner field on the `Task` proto | A2A polling is unreliable by construction, **and** it is a critical isolation break that cannot be fixed by adding an auth check alone | Prefer webhooks on A2A; poller treats not-found as descriptive failure; traces spanning `GetTask` marked `process_bound`; **#1702 is known-issue entry 8**, so it is expected, not a first-run surprise | P6a |
| **R13** | **Spec/SDK pin drift** — #1625 (migrate to adcp 6.6.0 / spec 3.1.1) is **open** | Model shapes and `STANDARD_ERROR_CODES` change under us | Hard-pin `adcp==5.7.0`; startup assert against the server's `/api/v1/capabilities`; mismatch ⟹ all spec-table oracles downgraded to `info` + loud banner | P1 |
| **R14** | **LLM nondeterminism contaminating fuzz results** | Unreproducible findings, token burn | The fuzzer is 100% deterministic — seeded PRNG, no LLM, `fuzz/` never imports `buyer/`, **and `buyer/` never imports `fuzz/`** (enforced by `test_import_boundaries.py`) | structural |
| **R15** | **Prompt injection via seller-controlled strings** incl. **webhook bodies** | The buyer subverted by the system it's testing | `<seller_data>` delimiting + standing untrusted-content instruction; only deterministic fields reach `CampaignPlan`; `WH-TAINT` on the fuzz side | P7b |
| **R16′** | **Extra-field behavior is a five-way matrix, not a server property** | v1 probed `ENVIRONMENT` once and asserted against it → false positive on every REST case | Policy is **declared per (wire, op)** in `WireBinding.extra_top_level`; `ENVIRONMENT` is recorded in `Finding.target` and consulted only for `ENV_MODEL` bindings; nested policy is separate from top-level | P1/P4b |
| **R17** | **Windows/PowerShell friction** — docker exec quoting, `.venv/Lib` not `lib`, first `uv run` sync, port collisions, **the e2e ports overlay AND the fuzz overlay** | Setup drag, wasted hours | argv-list `subprocess` (never shell strings), `pathlib` throughout, configurable ports, `doctor` first, all encoded in `scripts/*.ps1` | P0 |
| **R18** | **Seller rate-limiting turns a long run into noise** | Findings become environmental artifacts | Detect `RATE_LIMITED` (transient), back off, count as environmental — **but still assert the rate-limit envelope itself is well-formed** | P5 |
| **R19** (new) | **The manual delivery-webhook trigger poisons the scheduled path for 24 h** | A harness that uses the trigger then asserts a scheduled fire will hang forever | `AdminDriver.trigger_delivery_webhook` marks the media buy `scheduled_path_poisoned=True` in the arena ledger; any scheduled-path assertion on a poisoned buy is a **harness error**, raised immediately | P6a |
| **R20** (new) | **`_task_push_configs` is process-local**, so A2A webhooks silently vanish under multi-worker deployment | A harness that scales the container gets zero A2A webhooks and no error | `stack.up()` asserts the service runs a single worker and fails loudly otherwise | P0/P6a |

### 8.1 Open decisions (need a call before the phase that consumes them)

1. **Canonical stack profile — e2e (`:8092`/`ci-test`) or dev (`:8000`/`default`)?** Plan assumes **e2e as canonical** (`ENVIRONMENT=development` → nested `extra='forbid'`, deterministic) with dev as a secondary parity target. Decide at P0.
2. **Should `RunMode.DRY_RUN` get its own nightly campaign, or only on-demand?** Given dry-run suppresses all webhook machinery and returns early from `create_media_buy`, its coverage value is limited to the testing-hooks path itself. Recommend on-demand only. Decide at P5.
3. **Does `PyCoverage` (white-box line coverage via a locally-built server container) earn its keep?** The `CoverageProbe` interface ships day one; implementation deferred past P8.
4. **`fuzz --from-run`** (buyer run logs as mutation seeds) — high value, cheap, but only exists after P7b. Scheduled P8.
5. **Reporting `context` echo cap testing (INV-03)** needs 63/64/65 KB payloads; confirm the seller doesn't 413 at the proxy before attributing a drop to application logic. Verify in P4b.
6. **Whether to ship `SPEC_LITERAL` conformance runs as a first-class report** ("here is everywhere salesagent diverges from AdCP 3.1") separate from the bug-hunt report. Cheap given `compat.py`; likely the most externally valuable artifact the project produces. Decide at P8.
7. **Do the nine A2A protocol-method ops belong in the nightly budget?** They have no parity peer, so they consume budget for arity-1 coverage only — but they carry 12 of the viable #1670 cases and three of the highest-severity novel targets. Recommend yes, in a dedicated `fuzz protocol` sub-command. Decide at P6b.