# adcp-buyer-agent — Canonical Build Plan

**Status:** hand-off ready. Synthesis of three architect designs (pragmatic / fuzzer-first / spec-fidelity) against the scout digest.
**Repo:** `adcp-buyer-agent` — private, `uv`-managed, Python 3.12, Windows/PowerShell primary.
**Target under test:** `C:\Users\pmezz\projects\salesagent` running in Docker, over real HTTP.
**Hard pin:** `adcp==5.7.0` (AdCP spec `3.1.0-beta.3`) — must match the seller exactly.

---

## 1. Architecture overview

### 1.1 The three decisions everything else follows from

**Decision 1 — The SDK supplies models, not wire.** `adcp==5.7.0` gives us ~400 generated pydantic models, `STANDARD_ERROR_CODES`, `INTERNAL_CODES`, `MEDIA_BUY_STATE_MACHINE`, `valid_actions_for_status()`, `IDEMPOTENT_TASKS`, and RFC-8785 canonicalization. All of that is imported, never retyped. But the SDK's *transport* layer is lossy in ways that are fatal to a fuzzer:

- `TaskResult` carries only `adcp_error`; the `errors[]` mirror layer is **dropped** (`adcp/types/core.py:178`, `protocols/mcp.py:557-596` reconstructs `DebugInfo.response` rather than capturing wire bytes). Bug #1449 is literally "MCP ships advisory `errors[]` that A2A/REST drop" — an SDK-based fuzzer is structurally blind to it.
- The a2a-sdk client raises typed exceptions, hiding the JSON-RPC `error.code`, so #1670's `-32603` flattening is invisible; its protobuf round-trip re-coerces numerics, masking #1583.
- SDK `TaskStatus` has 5 members; the AdCP 3.1 lifecycle has 9. A2A `working`/`input-required`/`auth-required` all collapse to `SUBMITTED` (`protocols/a2a.py:673-694`).
- `_idempotency.resolve_key()` **auto-mints a fresh UUID4** for any mutating tool when the key is absent — a naive retry through the SDK double-books.
- `Protocol` enum is `{MCP, A2A}` only. REST does not exist in the SDK at all.

So: **three hand-rolled, byte-faithful raw transports** (~300 LOC total) are the single dispatch layer for both sides. The SDK's `ADCPClient` appears exactly once, as an optional **cross-check adapter** used only to diff SDK-emitted bytes against ours in a contract test.

**Decision 2 — Two strata over one transport layer.** The interchange currency is a plain `dict`.

- **Wire stratum** — `dict → bytes → dict`, no validation, no coercion. The fuzzer's home; it must be able to send `buying_mode: 17`, a 300-char idempotency key, a 12-deep envelope, and a duplicate JSON key.
- **Typed stratum** — `codec.encode(op, **kwargs) -> dict` and `codec.decode(op, body, status) -> BaseModel`, bolted onto the ends. The buyer's home.

Typed models are never a mandatory chokepoint. This is what lets one core serve both consumers.

**Decision 3 — One observation type, one oracle library, three engines, two consumers.** Every call on every wire produces an `Exchange`. Every oracle takes an `Exchange` (arity 1) or an `ExchangeSet` (arity N) — never a live client — so oracles are unit-testable against hand-written envelopes and are themselves testable. The fuzzer's three engines all funnel into the same oracle library and the same finding pipeline. The buyer runs **the same oracle library in `guard` mode**, logging violations instead of raising: every line written for the fuzzer is a line the buyer needs in order to trust seller responses.

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
 │  corpus · shrink · findings  │                     │  monitor                      │
 └────────┬─────────────────────┘                     └────────────────┬──────────────┘
          │                ┌───────────────────────────────┐           │ guard mode
          └───────────────►│ core/oracles/  — 33 invariants│◄──────────┘
                           │ arity-1 + arity-N (parity)    │
                           └───────────────┬───────────────┘
                                           │  Exchange / ExchangeSet
                           ┌───────────────▼───────────────┐
                           │ core/normalize/               │ two-tier, norm_log,
                           │ pipeline · rules · canonical  │ JCS differential_key
                           └───────────────┬───────────────┘
   TYPED STRATUM  ─────────────────────────┼───────────────────── WIRE STRATUM
   core/codec.py (adcp models)  ┌──────────▼──────────┐  raw dicts, byte-faithful
   core/capabilities.py         │  core/client.py     │  .call()  .call_all()
   core/idempotency.py (vault)  │  core/ops.py        │
   core/tasks/ (lifecycle,      │  core/session.py    │
     ledger, sink, poller,      └──┬───────┬───────┬──┘
     rendezvous)     ┌─────────────▼─┐ ┌───▼─────┐ ┌▼──────────────┐
                     │ transport/mcp │ │ tr./a2a │ │ transport/rest│
                     │ fastmcp+httpx │ │ httpx   │ │ httpx         │
                     │ tap           │ │ JSON-RPC│ │ /api/v1       │
                     └───────────────┴─┴─────────┴─┴───────────────┘
                                   │ real HTTP
                    ┌──────────────▼──────────────────────────────┐
                    │ harness/ : docker stack, seed, arena, doctor │
                    │ salesagent  :8000 (dev) | :8092 (e2e)        │
                    └──────────────────────────────────────────────┘
```

### 1.3 The asymmetry map (ground truth — drift-tested, not hardcoded from memory)

| Surface | Count | Source of truth |
|---|---|---|
| MCP tools | 16 | `src/core/main.py:305-332` `_register_tool` |
| A2A skills | 18 (6 stubs) | `src/a2a_server/adcp_a2a_server.py:1404-1426` `skill_handlers` |
| REST routes | 12 | `src/routes/api_v1.py` |

- `get_media_buys`: MCP + A2A, **no REST route** → #1651 is a 2-way differential.
- `list_tasks` / `get_task` / `complete_task`: **MCP only**, return a **bare dict** (not `ToolResult`, no response model), and hard-require a non-anonymous principal.
- 6 A2A skills (`create_creative`, `get_creatives`, `assign_creative`, `approve_creative`, `get_media_buy_status`, `optimize_media_buy`) validate params then raise `UnsupportedOperationError` — the agent card advertises them as functional. **Capability-truthfulness oracle falls out for free.**
- `GET /api/v1/capabilities` is the only GET. `PUT /api/v1/media-buys/{id}` is the only PUT. Everything else is POST-with-body.
- REST cannot express `get_products.buying_mode`, cannot carry `push_notification_config` on `create_media_buy`, has no task endpoints, and has no `idempotency_key` field on `UpdateMediaBuyBody` / `SyncCreativesBody` despite both being in `IDEMPOTENT_TASKS`. **REST async is a dead end** — encoded as `OpSpec.async_capable_on = {MCP, A2A}`.

---

## 2. Repo tree

```
adcp-buyer-agent/
├── pyproject.toml                  # py>=3.12; adcp==5.7.0 (HARD pin), httpx, fastmcp~=3.2,
│                                   #   pydantic~=2, pydantic-ai-slim[anthropic], rfc8785,
│                                   #   typer, rich, pyyaml, sqlalchemy, fastapi+uvicorn (sink),
│                                   #   hypothesis(dev), respx(dev), pytest, pytest-asyncio
├── .env.example                    # ANTHROPIC_API_KEY, ADCP_SELLER_BASE, ADCP_TOKEN,
│                                   #   ADCP_TENANT, ADCP_WEBHOOK_PORT, ADCP_WEBHOOK_SECRET
├── README.md                       # 10-line quickstart: doctor → stack up → probe → fuzz → buy
├── src/adcp_buyer/
│   ├── config.py                   # pydantic-settings Settings + AgentTarget(base_url,token,tenant)
│   ├── cli.py                      # typer app: doctor|stack|seed|probe|buy|fuzz|replay|sink|report
│   ├── core/                       # ══════════ SHARED PROTOCOL CORE (no LLM, no hypothesis) ══════
│   │   ├── ops.py                  # OpSpec registry — the single source of per-op truth
│   │   ├── matrix.py               # Availability{EXISTS,ABSENT,STUB} + field_gaps + drift test data
│   │   ├── exchange.py             # Exchange, ExchangeSet, LogicalRequest, NormEvent, Wire enum
│   │   ├── envelope.py             # two-layer envelope parse/validate; TASK_ENVELOPE_FIELDS
│   │   ├── session.py              # AgentSession: creds, base urls, transport factory, context_id
│   │   ├── identity.py             # BuyerIdentity: header assembly (x-adcp-auth / Bearer / tenant)
│   │   ├── client.py               # Client.call() / .call_all() — the ONE front door
│   │   ├── codec.py                # typed encode/decode + CompatProfile application
│   │   ├── compat.py               # CompatProfile: SALESAGENT vs SPEC_LITERAL field rewrites
│   │   ├── capabilities.py         # CapabilityGate over all 3 wires (FeatureResolver reuse)
│   │   ├── idempotency.py          # IdempotencyVault (SQLite) + JCS hash (SDK canonicalize)
│   │   ├── transport/
│   │   │   ├── base.py             # RawTransport Protocol
│   │   │   ├── rest.py             # hand-rolled httpx mirroring api_v1.py (12 routes)
│   │   │   ├── mcp.py              # fastmcp Client over StreamableHttp + httpx wiretap
│   │   │   ├── a2a.py              # hand-rolled JSON-RPC message/send over httpx
│   │   │   ├── sdk_crosscheck.py   # adcp.ADCPClient adapter — contract-test only, never in a run
│   │   │   └── wiretap.py          # httpx.AsyncBaseTransport that records exact request/response bytes
│   │   ├── normalize/
│   │   │   ├── pipeline.py         # ordered NormRule chain + norm_log
│   │   │   ├── rules.py            # unwrap_frame, split_envelope, strip_a2a_synthetic,
│   │   │   │                       #   numeric_policy, drop_volatile, sort_unordered
│   │   │   └── canonical.py        # rfc8785 JCS wrapper + JSON-Pointer structural differ
│   │   ├── spec/
│   │   │   ├── codes.py            # re-export STANDARD_ERROR_CODES / INTERNAL_CODES / recovery table
│   │   │   ├── state_machine.py    # re-export MEDIA_BUY_STATE_MACHINE / valid_actions_for_status
│   │   │   ├── enums.py            # closed enums from adcp.types.generated_poc.enums
│   │   │   ├── status_map.py       # wire-code → HTTP status table (REST oracle)
│   │   │   └── divergences.yaml    # documented salesagent-vs-SDK deltas → severity: info
│   │   ├── oracles/
│   │   │   ├── registry.py         # Oracle ABC, @oracle decorator, Violation, applicability
│   │   │   ├── envelope_o.py       # INV-01..05, 08, 31
│   │   │   ├── auth_o.py           # INV-07, 30  (+ AuthDisposition)
│   │   │   ├── lifecycle_o.py      # INV-09..16, 32, 33
│   │   │   ├── idempotency_o.py    # INV-17..22 + cross-transport replay
│   │   │   ├── budget_o.py         # INV-23..26
│   │   │   ├── isolation_o.py      # INV-27, 28 (+ timing side-channel)
│   │   │   ├── listing_o.py        # INV-29
│   │   │   ├── parity_o.py         # INV-06 family — arity N, the strongest oracle
│   │   │   ├── capability_o.py     # advertised-vs-implemented (agent card vs stubs)
│   │   │   └── taint.py            # canary corpus + whole-response scanner (INV-02 details)
│   │   └── tasks/
│   │       ├── lifecycle.py        # AdcpTaskStatus (9-member SPEC enum) + TERMINAL/PAUSED sets
│   │       ├── ledger.py           # TaskLedger (SQLite): operation_id ↔ server task handle
│   │       ├── sink.py             # FastAPI webhook receiver (host-side, port 8788)
│   │       ├── poller.py           # per-wire polling fallback (MCP get_task / A2A tasks/get)
│   │       └── rendezvous.py       # await_terminal(): webhook ⊕ poll race
│   ├── fuzz/                       # ══════════ SIDE 1: THE FUZZER ══════════
│   │   ├── engines/
│   │   │   ├── differential.py     # E1 — cross-transport; every other engine feeds it
│   │   │   ├── schema.py           # E2 — hypothesis boundary/mutation over SDK models
│   │   │   └── sequence.py         # E3 — stateful media-buy walks + idempotency probes
│   │   ├── strategies/
│   │   │   ├── from_model.py       # model → valid()/boundary()/mutate() strategies
│   │   │   ├── boundaries.py       # numeric/string/enum/datetime ladders
│   │   │   ├── idempotency.py      # key length × charset ladders
│   │   │   ├── malicious.py        # canary × injection cross-product
│   │   │   └── mutators.py         # JSON-level structural mutators (post-serialization)
│   │   ├── arena.py                # NAMESPACE (fast) | TENANT (hermetic) isolation + ledger
│   │   ├── corpus.py               # coverage tokens, novelty scoring, seed/discovered/failing
│   │   ├── shrink.py               # ddmin over traces + per-field shrink ladder
│   │   ├── findings/
│   │   │   ├── model.py            # Finding + signature
│   │   │   ├── store.py            # dedupe, occurrence counting, run diffing
│   │   │   ├── known_issues.py     # declarative matcher evaluation
│   │   │   └── report.py           # markdown / JSON / JUnit emitters, NOVEL-first
│   │   ├── known_issues.yaml       # the 7 required rediscoveries
│   │   └── repro.py                # generate a standalone repro.py per finding
│   ├── buyer/                      # ══════════ SIDE 2: THE LLM BUYER ══════════
│   │   ├── models.py               # Brief, CampaignPlan, PackagePlan, StepRecord, CampaignRun
│   │   ├── select.py               # LLM product ranker + MANDATORY deterministic fallback
│   │   ├── planner.py              # brief + ranked → CampaignPlan (budget split)
│   │   ├── policy.py               # preflight gates: budget math, state machine, available_actions
│   │   ├── creative.py             # format → creative manifest assembly
│   │   ├── executor.py             # deterministic plan → wire (vault → encode → call → rendezvous)
│   │   ├── monitor.py              # delivery polling + LLM pacing verdict
│   │   ├── guard.py                # runs core/oracles in guard mode over live buyer traffic
│   │   ├── prompts.py              # all prompt text, with <seller_data> untrusted delimiters
│   │   └── agent.py                # BuyerAgent.run_campaign() — the explicit state machine
│   ├── harness/                    # ══════════ LOCAL-RUN ══════════
│   │   ├── stack.py                # docker compose up/down, health-wait, profile dev|e2e
│   │   ├── seed.py                 # init_database_ci.py invocation + post-seed invariant asserts
│   │   ├── fixtures.py             # second tenant, budget-boundary products (raw psycopg SQL)
│   │   └── doctor.py               # docker/ports/clone/webhook-reachability preflight
│   └── obs/
│       ├── recorder.py             # JSONL wire log (every request/response, every run)
│       └── redact.py               # token + idempotency-key redaction (8-char prefix)
├── corpus/
│   ├── seeds/                      # hand-written high-value cases
│   ├── known/                      # the 7 known-issue reproducers
│   ├── discovered/                 # auto-saved novel-coverage-token cases
│   └── failing/<finding_id>/       # case.json · trace.jsonl · wire.jsonl · shrunk.json · repro.py
├── scripts/
│   ├── stack_up.ps1 / stack_down.ps1 / arena_reset.ps1
│   └── seed.ps1
└── tests/
    ├── unit/                       # oracles on SYNTHETIC envelopes; codec; JCS; vault; ledger
    ├── oracle_mutation/            # every oracle must FIRE on its deliberately-violating fixture
    ├── contract/                   # live-stack: op × wire matrix, drift tests, SDK byte-diff
    ├── validation/                 # THE GATE: test_rediscovers_known_issues.py
    └── e2e/                        # buyer end-to-end over the live stack
```

---

## 3. Module contracts

### 3.1 `core/exchange.py` — the ONE normalized result

Merges salesagent's `TransportResult` (payload / wire_response / wire_error_envelope / is_success / is_error) with the fields a *client* needs that an in-process test harness does not.

```python
class Wire(StrEnum):
    MCP = "mcp"; A2A = "a2a"; REST = "rest"

@dataclass(frozen=True, slots=True)
class LogicalRequest:
    op: str                          # canonical AdCP op name
    args: dict[str, Any]             # transport-independent intent
    auth: AuthMode                   # VALID | NONE | GARBAGE | FOREIGN_TENANT | FOREIGN_PRINCIPAL
    context: dict | None = None
    idempotency_key: str | None = None
    dry_run: bool = False

@dataclass(frozen=True)
class NormEvent:
    rule: str; pointer: str; before: Any; after: Any

@dataclass(frozen=True, slots=True)
class Exchange:
    # ── identity ──────────────────────────────────────────────────────
    op: str
    wire: Wire
    logical: LogicalRequest
    sent_wire: dict                  # what the binder produced for THIS wire
    request_bytes: bytes | None      # exactly what went out (from wiretap)

    # ── outcome ───────────────────────────────────────────────────────
    outcome: Literal["success", "error", "transport_fault"]
    wire_response: dict | None       # RAW success body, verbatim
    wire_error_envelope: dict | None # RAW two-layer envelope, VERBATIM — never normalized
    exc: Exception | None            # transport/parse fault only

    # ── normalized (payload only; errors are never normalized) ────────
    payload: dict | None             # domain payload, protocol envelope split out
    envelope: dict | None            # TASK_ENVELOPE_FIELDS popped here
    typed: BaseModel | None          # populated only when codec.decode ran (buyer path)
    norm_log: tuple[NormEvent, ...]
    differential_key: str            # sha256(jcs(payload)) — the E1 comparison key

    # ── protocol framing (deliberately NOT normalized away) ───────────
    status: AdcpTaskStatus           # 9-member SPEC enum
    http_status: int | None          # REST + A2A
    jsonrpc_code: int | None         # A2A only — REQUIRED to see #1670
    task_handle: str | None          # SERVER-assigned
    operation_id: str | None         # BUYER-minted correlation uuid
    context_id: str | None
    idempotency_key: str | None
    replayed_raw: Any = None         # unnormalized; strict oracle flags non-bool

    latency_ms: float = 0.0
    raw: Any = None                  # httpx.Response / CallToolResult / Task

    @property
    def is_success(self) -> bool: ...
    @property
    def is_error(self) -> bool: ...
    @property
    def code(self) -> str | None:            # adcp_error.code — the parity key
    @property
    def recovery(self) -> str | None: ...
    @property
    def advisory_errors(self) -> list[dict]: # errors[] on a SUCCESS body — the #1449 surface
        return (self.wire_response or {}).get("errors") or []
    @property
    def auth_disposition(self) -> AuthDisposition: ...   # HARD_REFUSE | SOFT_EMPTY | SUCCESS

ExchangeSet = dict[Wire, Exchange]
```

Non-negotiable properties:
- `wire_error_envelope` is stored **pre-normalization, verbatim**. The wire error envelope *is* the contract; normalizing it destroys the strongest oracle.
- `advisory_errors` exists because the SDK cannot see it. This single property is the #1449 oracle.
- `jsonrpc_code` exists because without it #1670 is undetectable.
- No `synthesized_error_envelope` field (that exists in salesagent's harness only for the in-process IMPL transport, which we do not model).

### 3.2 `core/ops.py` + `core/matrix.py` — the registry

```python
class Availability(StrEnum):
    EXISTS = "exists"; ABSENT = "absent"; STUB = "stub"

@dataclass(frozen=True)
class WireBinding:
    availability: Availability
    name: str | None = None                    # mcp tool / a2a skill
    rest: tuple[str, str] | None = None        # (verb, path_template)
    field_gaps: frozenset[str] = frozenset()   # fields this wire CANNOT carry
    body_fields: frozenset[str] | None = None  # REST *Body allowlist

@dataclass(frozen=True)
class OpSpec:
    name: str
    request_model: type[BaseModel] | None
    response_model: type[BaseModel] | UnionType | None   # may be a UnionType — no .model_fields
    bindings: dict[Wire, WireBinding]
    auth: Literal["discovery", "required", "tenant_policy"]
    mutating: bool                        # ⇔ name ∈ adcp._idempotency.IDEMPOTENT_TASKS
    async_capable_on: frozenset[Wire]
    result_shape: Literal["model", "bare_dict"] = "model"

OPERATIONS: dict[str, OpSpec]

def wires_for(op: str) -> tuple[Wire, ...]        # EXISTS only — E1 dispatch set
def spec(op: str) -> OpSpec
```

Registry contents (the load-bearing rows):

| op | MCP | A2A | REST | notes |
|---|---|---|---|---|
| `get_adcp_capabilities` | ✓ | ✓ | **GET** `/api/v1/capabilities` | only GET; discovery |
| `get_products` | ✓ | ✓ | POST `/api/v1/products` | REST `field_gaps={"buying_mode"}`; tenant-policy auth |
| `list_creative_formats` | ✓ | ✓ | POST `/api/v1/creative-formats` | discovery |
| `list_authorized_properties` | ✓ | ✓ | POST `/api/v1/authorized-properties` | discovery |
| `list_accounts` | ✓ | ✓ | POST `/api/v1/accounts` | discovery (empty for unauthed, BR-RULE-055) |
| `sync_accounts` | ✓ | ✓ | POST `/api/v1/accounts/sync` | mutating |
| `create_media_buy` | ✓ | ✓ | POST `/api/v1/media-buys` | mutating; `async_capable_on={MCP,A2A}` (no pnc on REST) |
| `update_media_buy` | ✓ | ✓ | **PUT** `/api/v1/media-buys/{id}` | mutating; REST `field_gaps={"idempotency_key","account","revision","canceled",...}` |
| `get_media_buy_delivery` | ✓ | ✓ | POST `/api/v1/media-buys/delivery` | `account` enriches identity in place — #1316 |
| `sync_creatives` | ✓ | ✓ | POST `/api/v1/creatives/sync` | mutating; REST `field_gaps={"idempotency_key"}` |
| `list_creatives` | ✓ | ✓ | POST `/api/v1/creatives` | required auth; limit cap 1000 |
| `update_performance_index` | ✓ | ✓ | POST `/api/v1/performance-index` | required auth |
| `get_media_buys` | ✓ | ✓ | **ABSENT** | 2-way differential — this asymmetry *is* #1651 |
| `list_tasks`/`get_task`/`complete_task` | ✓ (`bare_dict`) | ABSENT | ABSENT | different wrapper contract |
| `create_creative`, `get_creatives`, `assign_creative`, `approve_creative`, `get_media_buy_status`, `optimize_media_buy` | ABSENT | **STUB** | ABSENT | agent card lies → capability oracle |

**`field_gaps` are not exclusions — they are targets.** The `FIELD-REACHABILITY` oracle drives the gap deliberately (send `buying_mode=guaranteed` vs `non_guaranteed`; MCP/A2A results must differ, REST's must not — *that non-difference is the finding*). If the gap closes, the oracle stops firing and the known-issue gate reports "seller fixed it."

**Drift test (`tests/contract/test_registry_drift.py`):** diff `OPERATIONS` against the seller's live registration lists — 16 MCP tools (`list_tools`), 18 A2A skills (agent card), 12 REST routes (`/openapi.json`). A registry that silently rots is a fuzzer that silently loses coverage.

### 3.3 `core/transport/` — three raw clients, one shape

```python
class RawTransport(Protocol):
    wire: Wire
    async def dispatch(self, spec: OpSpec, sent: dict, ctx: CallContext) -> Exchange: ...
    async def get_task(self, handle: str, ctx: CallContext) -> Exchange | None: ...
    async def aclose(self) -> None: ...
```

All three build their httpx client over `wiretap.WireTapTransport` so `request_bytes` is exact.

**`rest.py`** — POST-with-body for every read except `GET /capabilities`; `PUT` for update. Headers: `x-adcp-auth` (primary) → `Authorization: Bearer` (fallback, a fuzz case not the default), `x-adcp-tenant: <subdomain>`, `x-context-id`, `x-dry-run`. Body built by field-picking against `WireBinding.body_fields`; **every dropped field is recorded** into `sent_wire["_dropped"]` — that record is what feeds `FIELD-REACHABILITY`. Send raw bytes exactly once (`content=raw`, not `json=`) because `RestCompatMiddleware` hashes `request.state.raw_wire_payload` pre-normalization; re-serializing with different key order manufactures a false `IDEMPOTENCY_CONFLICT`. Success → `wire_response = r.json()` (bare payload, **not** `ProtocolEnvelope`-wrapped). Body containing `adcp_error` → `wire_error_envelope` + `http_status`. `follow_redirects=False`.

**`mcp.py`** — `fastmcp.Client(StreamableHttpTransport(f"{base}/mcp/", headers=...))`. **Trailing slash is mandatory** (`mcp.http_app(path="/")` mounted at `/mcp`). Success → `wire_response = result.structured_content`. `result.isError` → `json.loads(result.content[0].text)` → `wire_error_envelope`, **both layers preserved**. Also parse `content[0].text` on success and assert it mirrors `structuredContent` (a Tier-B oracle). `result_shape == "bare_dict"` ops skip response-model decode without a special case.

**`a2a.py`** — hand-rolled `httpx.post(f"{base}/a2a")` (no trailing slash — `/a2a/` 307-redirects and a redirected POST can drop the body):

```json
{"jsonrpc":"2.0","id":"<uuid-string>","method":"message/send",
 "params":{"message":{"messageId":"<uuid-string>","role":"ROLE_USER",
   "parts":[{"data":{"skill":"<op>","input":{...}}}]},
   "configuration":{"task_push_notification_config": {...}}}}
```

`messageId` and `id` are **always strings** (a middleware rewrites numerics — don't rely on it). Both `input` (spec) and `parameters` (legacy) keys are accepted by the seller; we send `input` by default and fuzz `parameters`. Then: `body["error"]["code"]` → `jsonrpc_code` (**the #1670 sensor**); `body["result"]` is a Task → walk `artifacts[].parts[]` for the last `data` part → `wire_response`, or if `status.state` is FAILED and the DataPart carries `adcp_error` → `wire_error_envelope`. `status.state` → `AdcpTaskStatus` via `strip("TASK_STATE_").lower().replace("_","-")`. Auth is `Authorization: Bearer`. Raw JSON parsing preserves `1.0` as float and `1` as int — exactly what `TYPE-PARITY` asserts on for #1583.

**`sdk_crosscheck.py`** — `adcp.ADCPClient` with `AgentConfig(agent_uri=f"{base}/mcp/", protocol=Protocol.MCP, auth_header="x-adcp-auth", auth_type="token", extra_headers={"x-adcp-tenant": ...}, timeout=120.0, validate_features=False, strict_idempotency=False, validation=ValidationHookConfig(requests="off", responses="off"))`. **Never used in a fuzz run or a buyer run.** Its only job is `tests/contract/test_sdk_byte_parity.py`: for the same logical call, our bytes vs SDK bytes must match modulo documented deltas. `validation="off"` is load-bearing — the SDK's default strict response validator converts *seller drift* into `TaskStatus.FAILED` with a synthesized message, which would destroy the comparison.

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
           THE parity primitive. ~20 lines. Hoists salesagent's BaseTestEnv.call_via
           to a real HTTP client."""

    async def aclose(self) -> None: ...
```

`validate=False` is what lets the fuzzer send deliberately-invalid payloads through the same front door the buyer uses.

`binder.bind(req, wire)` is **generated from `ops.py`, never hand-written per case** — otherwise the binder becomes the place where the fuzzer accidentally "fixes" a request and hides the bug.

### 3.5 `core/codec.py` + `core/compat.py` — the typed stratum

```python
def encode(op: OpSpec, *, profile: CompatProfile = SALESAGENT, **kw) -> tuple[dict, BaseModel | None]:
    """Typed construct → validate → model_dump(mode='json', exclude_none=True)
       → inject adcp_version='3.1' → apply profile rewrites. Returns (dict, model)."""

def decode(op: OpSpec, body: dict, status: AdcpTaskStatus) -> BaseModel | None:
    """Status-FIRST variant selection (response models are UnionTypes with no
       .model_fields), then model_validate. Never raises into the transport —
       a decode failure lands on Exchange.exc with the raw body intact."""
```

**`CompatProfile` is the highest-value artifact in the typed stratum.** Three verified structural divergences between spec-correct SDK requests and what salesagent's wrappers accept. In dev/e2e (`ENVIRONMENT != production`) models are `extra='forbid'`, so **a spec-correct request fails loudly on all three wires**:

| # | Divergence | SALESAGENT profile action | Fuzzer action |
|---|---|---|---|
| D1 | `GetProductsRequest.buying_mode` is **required** by the SDK; absent from salesagent's MCP wrapper signature *and* from `GetProductsBody` *and* from `create_get_products_request()` | `pop("buying_mode")` | send it unmodified on every wire; the divergence **is** the finding |
| D2 | `create_media_buy` wrapper accepts only `{brand, packages, start_time, end_time, po_number, reporting_webhook, push_notification_config, context, ext, account, idempotency_key}`. `plan_id`, `proposal_id`, `total_budget`, `advertiser_industry`, `invoice_recipient`, `io_acceptance`, `agency_estimate_number`, `artifact_webhook` are unknown | restrict to the accepted set | send each unsupported field **individually** to map the gap precisely |
| D3 | `update_media_buy` wrapper has **no** `account`, `revision`, `canceled`, `cancellation_reason`, `new_packages`; carries non-spec `flight_start_date`/`flight_end_date`, scalar `budget`+`currency`, `daily_budget`, `pacing`. **Cancellation has no expressible request shape.** | emit salesagent's shape from a spec-typed intent | drive both shapes; report the delta |

Every profile rule ships with a test in `tests/contract/test_divergences.py` **asserting the divergence still exists**. When salesagent fixes one, that test goes red — the profile can never silently rot into a lie.

`compat.py` also defines `SPEC_LITERAL` (no rewrites) so a single flag flips the buyer between "works against this seller" and "conforms to the spec."

### 3.6 `core/normalize/` — see §4 in full. Public surface:

```python
class NormPipeline:
    def __init__(self, *, numeric: NumericPolicy = NumericPolicy.STRICT,
                 rules: Sequence[NormRule] = DEFAULT_RULES): ...
    def run(self, wire: Wire, op: OpSpec, raw: dict) -> tuple[dict, dict, tuple[NormEvent, ...]]:
        """returns (payload, envelope, norm_log)"""

def differential_key(payload: dict) -> str          # sha256(rfc8785.dumps(payload))
def structural_diff(a: dict, b: dict) -> list[DiffEntry]   # JSON-Pointer level
```

### 3.7 `core/oracles/` — the 33-invariant library

```python
class Oracle(ABC):
    id: str                          # e.g. "INV06-AUTH-DISPOSITION-PARITY"
    invariant_ids: tuple[str, ...]
    arity: Literal[1, "N"]
    severity: Severity
    def applies(self, op: str, wire: Wire | None, phase: Phase) -> bool: ...
    def check(self, obs: Exchange | ExchangeSet) -> list[Violation]: ...

@dataclass(frozen=True)
class Violation:
    oracle_id: str; invariant_ids: tuple[str, ...]
    severity: Severity; title: str
    expectation: str                 # the invariant text, verbatim
    observed: dict                   # per-wire observation
    diff: list[DiffEntry]
    evidence_paths: tuple[str, ...]

ORACLES: dict[str, Oracle]
def run_arity1(obs: Exchange, phase: Phase) -> list[Violation]
def run_arityN(obs_set: ExchangeSet, phase: Phase) -> list[Violation]
```

**Spec tables are imported, never retyped**: `STANDARD_ERROR_CODES` / `INTERNAL_CODES` / recovery table from `adcp/server/helpers.py`; `MEDIA_BUY_STATE_MACHINE` and `valid_actions_for_status()` from the same; closed enums from `adcp/types/generated_poc/enums/`; canonicalization from `adcp/server/idempotency/canonicalize.py`. **Where salesagent disagrees with its own SDK, that disagreement is automatically a finding** — unless listed in `spec/divergences.yaml`, in which case it is downgraded to `info` and still counted.

#### Arity-1 oracles

| INV | Oracle id | Assertable check |
|---|---|---|
| 01 | `ENV-TWO-LAYER` | `error` is dict; `adcp_error` present; `errors` is a list, `len>=1`; `errors[0].code == adcp_error.code`; `adcp_error is not errors[0]` (defensive copy per the REST invariant); both codes ∈ `STANDARD_ERROR_CODES` |
| 02 | `ENV-FIELDS` | required `{code,message,recovery}`; optional ⊆ `{field,suggestion,retry_after,details}` (unknown key → finding); `recovery ∈ {transient,correctable,terminal}`; **`details` taint scan** |
| 03 | `CTX-ECHO` | request `context` ≤64KB JCS ⟹ echoed JCS-equal on success **and** error; >64KB ⟹ absent **and** not an error (dropped, not truncated, not 500) |
| 04 | `CODE-MEMBERSHIP` | code ∈ `STANDARD_ERROR_CODES` **and** ∉ `INTERNAL_CODES`. An `INTERNAL_CODES` member on the wire = **critical** |
| 05 | `CODE-RECOVERY-TABLE` | `recovery == RECOVERY_BY_CODE[code]` unless `(op,code)` ∈ divergences → `info`. Plus **global consistency**: one wire code never carries two recoveries in one run |
| 08 | `REST-STATUS-MAP` | REST: `http_status == STATUS_BY_CODE[code]` (highest-wins table from `_build_error_code_to_status`). Plus cross-case consistency |
| 07 | `AUTH-CODE` | present-but-invalid token → `AUTH_TOKEN_INVALID` + 401 (note: also fires `CODE-MEMBERSHIP` — both are correct; one reports parity, one reports spec membership) |
| 09/14/15/16 | `*-STATUS-ENUM` | media-buy ∈ 7-set; creative ∈ 5-set; approval ∈ 3-set (never conflated); task ∈ 9-set; proposal ∈ 2-set; account ∈ 6-set |
| 11 | `MB-TERMINAL` | mutation on `completed/rejected/canceled` ⟹ `INVALID_STATE` + REST 410. **Guard:** non-spec statuses (`draft`) are exempt — salesagent allows all actions there |
| 12 | `MB-VALID-ACTIONS-ECHO` | every mutation/dry-run/cancel response carries `valid_actions == valid_actions_for_status(resulting_status)` compared as **sets** against the imported SDK function; terminal ⟹ `[]` |
| 13 | `MB-CANCEL-SHAPE` | `status=="canceled"`, `valid_actions==[]`, `canceled_by ∈ {buyer,seller}`, `canceled_at` present and tz-aware |
| 17-22 | `IDEM-*` | see §3.9 |
| 23-26 | `BUDGET-*` | min package (create **and** update), max campaign, max daily (incl. date-shrink attack), non-negativity |
| 29 | `LIST-CAP` | `limit` ladder; `>1000` **silently capped, not errored**; default 50; principal+tenant scoped |
| 31 | `ERROR-NORMALIZATION` | value-shaped ⟹ `VALIDATION_ERROR/400/correctable`; permission-shaped ⟹ `AUTH_REQUIRED/403`; induced fault ⟹ `INTERNAL_ERROR` translated to `SERVICE_UNAVAILABLE`. Asserted identically on all 3 |
| 32 | `NOT-CANCELLABLE` | emits `NOT_CANCELLABLE` (correctable), not a generic failure |
| 33 | `CONTEXT-ID-RESOLUTION` | unresolvable `x-context-id`/`context_id` ⟹ `SESSION_NOT_FOUND` (404, correctable) — **not** `INVALID_STATE`/`GONE` |
| — | `CAPABILITY-TRUTHFULNESS` | every feature the seller advertises (agent card skills, `/capabilities` features) must not return `UNSUPPORTED_FEATURE` when exercised |
| — | `PROTOCOL-ERROR-FIDELITY` | **A2A-only, arity 1** (no parity peer exists): `jsonrpc_code == JSON_RPC_ERROR_CODE_MAP[expected_a2a_error]`. Driven by cases engineered to provoke each typed `A2AError`: unknown skill ⟹ `-32601`; `tasks/get` on a random id ⟹ `-32001`; missing auth on a non-discovery skill ⟹ `-32600`; malformed params ⟹ `-32602`. **This is #1670.** |

#### Arity-N (parity) oracles — INV-06 family

| Oracle | Fires when | Rediscovers |
|---|---|---|
| `ERROR-CODE-PARITY` | `adcp_error.code` differs across wires | generic |
| `RECOVERY-PARITY` | `recovery` differs for equal codes | generic |
| `ADVISORY-ERRORS-PARITY` | on **success**, the set of `errors[].code` differs | **#1449** |
| `TYPE-PARITY` | JSON *type* at any payload pointer differs (runs **before** value comparison so a type mismatch is never mis-reported as a value mismatch) | **#1583** |
| `PAYLOAD-SHAPE-PARITY` | normalized payload JCS differs | generic |
| `AUTH-DISPOSITION-PARITY` | disposition tuple differs | **#1651** |
| `ACCOUNT-SCOPE-PARITY` | returned account-id set differs, or a foreign `account` refuses differently | **#1316** |
| `FIELD-REACHABILITY` | a distinguishing field changes results on some wires but not others | **REST `buying_mode` drop** |
| `IDEM-CROSS-TRANSPORT` | create on one wire, replay same key+payload on another ⟹ must replay, not conflict | capture-point drift |
| `ENVELOPE-WRAPPING` | REST returns bare payload while MCP/A2A wrap in `ProtocolEnvelope` — asserted to remain **exactly** that documented Tier-B difference; any drift is a finding | generic |
| `UNKNOWN-FIELD-CONSISTENCY` | unknown-field handling differs across wires, **or** disagrees with the server's probed `ENVIRONMENT` | REST `*Body` bare-`BaseModel` gap (#1442) |

**`AuthDisposition` is the key abstraction for INV-07/30**: `HARD_REFUSE` (error envelope, no payload) | `SOFT_EMPTY` (success + empty collection + advisory `errors[]` containing `AUTH_*`) | `SUCCESS`. For a given `(op, auth_mode)` the disposition must be identical across wires. Token-less `get_media_buys` → A2A `HARD_REFUSE`, MCP `SOFT_EMPTY` ⟹ **#1651**.

**Taint oracle (INV-02's `details` clause, made mechanical).** Every string request field gets canary `ADCPFZ-<8hex>-<field_tag>` (≤24 chars ASCII, survives truncation and NFKC), combined with `{{7*7}}`, `${jndi:ldap://x}`, `<script>`, `'; DROP TABLE --`, `../../../etc/passwd`, `\u202e`, embedded NUL, lone surrogate. The scanner walks the entire response plus the JSONL wire log with casefold+NFKC+strip matching:

- canary in `adcp_error.details` → **high** (spec: `details` MUST contain only server-generated values — the prompt-injection guard)
- canary in `field` → expected, `info`
- canary in `message`/`suggestion` → `medium`
- canary in a **different request's** response → **critical** (cross-request leakage)
- canary in an idempotency-conflict message → INV-20 no-leak violation

**Isolation oracles carry a side channel check**: INV-27 asserts a cross-tenant resource returns the entity's `NOT_FOUND` code **and** that code, message, and latency are indistinguishable from a genuinely nonexistent id in the requesting tenant (latency delta > 3σ is a finding).

**Oracle-mutation tests are mandatory.** `tests/oracle_mutation/` ships, for every oracle, ≥1 fixture it must pass and ≥1 deliberately-violating fixture it must fire on exactly once. An oracle that can never fire is indistinguishable from a clean server.

### 3.8 The three fuzz engines

```python
class DifferentialEngine:                      # E1
    async def run(self, case: LogicalRequest, *,
                  mode: DispatchMode = DispatchMode.SHARED_STATE) -> list[Violation]:
        """1. wires = matrix.wires_for(case.op)         (2 or 3)
           2. sent  = binder.bind(case, wire)  per wire
           3. dispatch: SHARED_STATE (reads, concurrent) |
                        SEQUENTIAL_ISOLATED (mutations, serial, own arena slot,
                        distinct idempotency_key, x-dry-run unless --wet)
           4. run_arity1 on EACH   (a differential run is also N conformance runs — free)
           5. run_arityN over all C(n,2) pairs"""

class SchemaEngine:                            # E2 — hypothesis
    def strategies(self, op: OpSpec) -> tuple[Strategy, Strategy, Strategy]:
        """valid() | boundary() | mutate(valid) — all DERIVED from adcp pydantic
           model_fields, never hand-written."""
    async def run(self, op: str, *, examples: int = 500, seed: int) -> list[Violation]:
        """every generated case whose op exists on >1 wire is dispatched THROUGH E1.
           That's the multiplier: boundary_cases × wire_pairs. Boundary inputs are
           exactly where wires diverge, because each wire's own validation layer runs
           there (FastAPI RequestValidationError vs FastMCP TypeAdapter vs A2A
           pre-boundary param checks — cf. #1681)."""

class SequenceEngine:                          # E3
    async def run(self, *, steps: int = 20, seed: int,
                  arena: Arena) -> tuple[Trace, list[Violation]]:
        """Custom scheduler — NOT hypothesis RuleBasedStateMachine (its shrinker
           assumes replayability; the server has persistent state). Hypothesis is
           used for PARAMETER generation only; shrink.py owns trace reduction."""
```

**E2 boundary ladders** (each with a named oracle): `ge=0` → `{-1,-0.0,0,1e-9,1e308,NaN,Infinity,"0",2**63}`; `min_length=16,max_length=255` → `{0,1,15,16,17,254,255,256,4096}` chars × charset ladder; enums → `{member, MEMBER.upper(), member+"\x00", "not_a_member", 0, null}`; datetimes → `{naive, aware, 0001-01-01, 9999-12-31, DST fold, "2024-06-30T23:59:60Z", "not-a-date"}` (naive `end_time` must be rejected — `AwareDatetime`; `start_time` is a `StartTiming` **object**, not a bare datetime — fuzz both ways). Structural mutators applied **post-serialization** so they reach the wire unchanged: drop required, duplicate JSON key (REST/A2A raw bodies only), type-swap, unknown field at every nesting level, deep-nest `{1,8,9,16,64,1024}`, inflate (10k-element list, 10MB string), unicode abuse.

**E3 walk**: weighted 0.6 legal / 0.4 illegal transitions. Illegal families, each a distinct bug class: action disallowed for status; any action on a terminal buy; action on nonexistent/foreign-tenant/foreign-principal id; **stale-status race** (read status, interleave a second action, then act on the first); duplicate action; out-of-order (resume before pause); **a legal action re-sent on a different wire mid-sequence** (transport-mixed sequences are where framing-vs-state bugs live). Per-step: INV-09, INV-11, INV-12, terminal-finality (poll again — must not transition), plus every arity-1 oracle.

**`arena.py`** — `NAMESPACE` (distinct `buyer_ref`/idempotency-key prefixes on a shared stack; fast; default) or `TENANT` (fresh tenant + principal + `CurrencyLimit(USD)` + `PropertyTag('all_inventory')` + products; slow; **required** for INV-27/28). Hermetic reset via TRUNCATE mirroring salesagent's `_reset_e2e_db`. **E3 runs strictly serial** — the digest is explicit that xdist races on shared rows and silently drops transports.

**Coverage & corpus (`corpus.py`)** — no server instrumentation over HTTP, so novelty is a black-box response-shape token:

```
coverage_token = sha256((op, wire, status_class, wire_error_code, recovery,
                         sorted(envelope_key_set),
                         sorted(payload_key_pointers_at_depth<=3),
                         http_status, jsonrpc_code))
```

Novel token → save to `corpus/discovered/<sha>.json`. `hypothesis.target(novelty_score(obs))` steers generation. A `CoverageProbe` interface ships day one with `ShapeCoverage` (default); `PyCoverage` (mount `coverage.py` into a locally-built server container via `COVERAGE_PROCESS_START`) is a later drop-in.

**`shrink.py`** — E2 stateless: hypothesis's shrinker; corpus-replayed cases fall back to ddmin. E3 stateful: `delta_debug(trace, finding_signature)` — ddmin over the step list, replaying each reduction **in a fresh arena**, keeping it iff the *same* `signature` still fires (signature equality, not merely *a* finding, prevents shrinking into a different bug); then per-field ladder shrinking on the failing step; budget ≤60 replays + wall clock; record `shrink_quality = 1 - len(shrunk)/len(original)`.

**Finding schema (`findings/model.py`)**:

```jsonc
{
  "finding_id": "F-a91c3e77b2",
  "signature": "INV06-AUTH-DISPOSITION-PARITY|get_media_buys|a2a,mcp|-|/",
  "oracle_id": "...", "invariant_ids": ["INV-06","INV-07","INV-30"],
  "engine": "differential", "severity": "high", "confidence": "confirmed",
  "title": "...", "op": "...", "wires": ["a2a","mcp"],
  "expectation": "<invariant text verbatim>",
  "observed": { "a2a": {...}, "mcp": {...} },
  "diff": [{"path": "/_disposition", "left": "HARD_REFUSE", "right": "SOFT_EMPTY"}],
  "norm_log": [{"rule":"strip_a2a_synthetic","path":"/message"},
               {"rule":"numeric_policy","mode":"STRICT"}],
  "evidence": {"request_wire":{...}, "response_wire":{...},
               "http_status": null, "jsonrpc_code": -32603,
               "wire_log": "corpus/failing/F-.../wire.jsonl"},
  "repro": {"case_path":"...", "script":"corpus/failing/F-.../repro.py",
            "shrunk": true, "shrink_quality": 0.86, "verified": true},
  "known_issue": {"match":"gh-1651","confidence":"high",
                  "matched_by":["oracle_id","op","wire_set","disposition_pair"]},
  "run": {"run_id":"...","seed":918273645,"occurrences":4,"first_seen":"..."},
  "target": {"adcp_sdk":"5.7.0","spec":"3.1.0-beta.3","server_git_sha":"...",
             "environment":"development","stack":"dev@localhost:8000","tenant":"ci-test"}
}
```

`signature = oracle_id | op | sorted(wires) | wire_code | normalized_json_pointer`. All dedupe, occurrence counting, known-issue matching, and shrink-invariance key off it. Only the smallest shrunk case is retained per signature.

**Severity rubric:** `critical` = tenant/principal isolation break, cross-request leak, canary in `details`, `INTERNAL_CODES` on the wire. `high` = wire-contract break that would break a conforming buyer. `medium` = recovery drift, status-map miss, ordering instability. `low` = cosmetic. `info` = entries in `divergences.yaml` — **still detected, still counted, never hidden.**

**Auto-verification gate:** every `critical`/`high` finding is replayed once from its generated `repro.py` in a **clean arena** before reporting. Reproduces ⟹ `confidence: confirmed`. Doesn't ⟹ `probable` + `flaky` tag, sorted below confirmed. This is the primary defense against fuzzer bugs masquerading as seller bugs.

**Known-issue matching (`known_issues.yaml`)** — declarative, three-tier:

```yaml
- id: gh-1670
  title: "A2A v0.3 compat flattens all typed A2AError to -32603"
  status: open
  expect:
    oracle_id: {equals: PROTOCOL-ERROR-FIDELITY}
    wires: {contains: a2a}
    evidence.jsonrpc_code: {equals: -32603}
    evidence.expected_jsonrpc_code: {not_equals: -32603}
  validation:
    cases: [corpus/known/gh-1670_unknown_skill.json,
            corpus/known/gh-1670_tasks_get_missing.json]
  masking: {mode: downgrade_to_info}      # downgrade only — NEVER delete
```

1. all `expect` predicates pass → `confidence: high`; 2. fingerprint cosine similarity over `(oracle_id, op, wire_codes, wire_set, severity)` above threshold → `medium`, "possibly gh-XXXX"; 3. no match → **`NOVEL`, sorted to the top of every report. Novel findings are the product.**

**Masking is downgrade-only and always counted.** `--mask gh-1583` sets `NumericPolicy.TOLERANT` and downgrades matches to `info` so a hunt isn't drowned by hundreds of protobuf-float findings — but the report header always prints `masked: {gh-1583: 214}`. There is no mechanism to make a finding disappear.

### 3.9 `core/idempotency.py` — the vault

**The SDK never mints a key.** `resolve_key()` generates a fresh UUID4 for any mutating tool when the key is absent, so a naive retry double-books. The vault inverts control:

```python
class IdempotencyVault:
    def __init__(self, db_path: Path): ...     # SQLite; survives process restart
    def acquire(self, agent_id: str, op: str, payload: dict) -> str:
        """hash = adcp.server.idempotency.canonicalize.canonical_json_sha256(payload)
           row (agent_id, op, hash) → (key, minted_at, terminal_result_json|NULL)
           same payload  → SAME key   (retry safety)
           diff payload  → NEW  key   (client-side conflict prevention)"""
    def record_terminal(self, key: str, exchange: Exchange) -> None: ...
    def replayed(self, key: str) -> bool: ...
```

- Key format: UUID4 str (36 chars) — inside `[16,255]`, matches `^[A-Za-z0-9_.:-]+$`.
- Canonicalization is the **SDK's own module**, so replay-equivalence findings are not arguable. **Closed exclusion list**: top-level `idempotency_key`, `context`, `governance_context`; nested `push_notification_config.authentication.credentials`. **`ext` participates** — changing `ext` under a reused key is a legitimate `IDEMPOTENCY_CONFLICT` (a common false assumption, and an E3 probe).
- `client.use_idempotency_key()` is **not** used (single-use, pops on first consume, warns on cross-client leak). The vault stamps the field explicitly — stronger and wire-uniform.
- Keys are never logged in full: `obs/redact.py` 8-char prefix; `ADCP_LOG_IDEMPOTENCY_KEYS=1` gates full logging.
- `replayed_raw` is stored unnormalized so `IDEM-REPLAYED-FLAG` can flag `"true"` / `1` even though the SDK tolerates them.
- **Side-effect suppression**: `if exchange.replayed: skip notifications / memory writes / downstream calls` — enforced at the executor's plan level.

Idempotency oracles: `IDEM-SCOPE` (read ops must not require or echo a key; a mutating op in `IDEMPOTENT_TASKS` with the key omitted must `VALIDATION_ERROR`, not silently create); `IDEM-KEY-FORMAT` (`""` treated as absent, **not** an error); `IDEM-REPLAY-EQUIV`; `IDEM-CONFLICT` (409 + **taint-scanned** no-leak, since the first payload carried canaries); `IDEM-EXPIRED` (409, never silent re-execution, never another buy's data — verified against the arena ledger); `IDEM-REPLAYED-FLAG`; and `IDEM-CROSS-TRANSPORT`, the highest-value one: the three wires capture `raw_wire_payload` at *different* points (MCP pre-compat-middleware, REST pre-deprecated-rewrite bytes, A2A deep copy pre-pnc-injection), so probe deliberately with a **deprecated field name** to put the compat rewrite in play — any capture-point drift turns an honest retry into a 409.

### 3.10 `buyer/` — the LLM brain

**Hard rule: the LLM never touches the wire.** It produces and revises a typed `CampaignPlan`; a deterministic executor turns plans into calls. Mutating ops are **not** LLM tools — they are executor steps gated by preflight. An LLM cannot mint a media buy by hallucinating a tool call.

```
brief ─▶ discover (deterministic)  get_adcp_capabilities · list_creative_formats
       │                           list_authorized_properties · get_products
       ▼
       rank      [LLM + MANDATORY deterministic fallback]
       ▼
       plan      [LLM → CampaignPlan (pydantic, validated)]
       ▼
       preflight (deterministic)   budget math · state machine · available_actions · capability gate
       ▼
       execute   (deterministic)   vault → encode → call → rendezvous → decode → guard oracles
       ▼
       monitor   get_media_buy_delivery + reporting webhook → LLM PacingVerdict → replan
```

```python
class ProductSelector:
    def __init__(self, model: str = "claude-opus-4-8", *, use_llm: bool = True): ...
    async def rank(self, brief: Brief, products: list[dict]) -> list[RankedProduct]: ...
    def _fallback_rank(self, brief, products) -> list[RankedProduct]: ...   # ALWAYS available
```

Transposed from signals-agent `rank_signals_with_ai`, keeping **both** of its guards: (1) `MAX_PRODUCTS_FOR_PROMPT = 20` cap before prompting (expression-tree blowup guard); (2) strip ```` ```json ```` fences before parse — even though pydantic-ai's `output_type=list[RankedProduct]` normally makes that unnecessary, keep it for the raw-fallback path. The **deterministic fallback is mandatory, not optional**: token-overlap scoring over `name/description/formats` plus delivery-type preference. Every LLM failure (parse, timeout, rate limit, no API key) falls back and logs `selection_mode="fallback"`. This is what lets CI and the fuzzer run the full flow with `--no-llm` at zero token spend.

Model config: `pydantic_ai.Agent` on **`claude-opus-4-8`**. Planning: `thinking={"type":"adaptive"}`, `output_config={"effort":"high"}`; ranker: `effort="low"`. **Never** `budget_tokens`, `temperature`, `top_p`, `top_k` (400s on 4.8); no assistant prefill. Provider is a settings string so a swap is one env var — but Claude is the default and the only configured provider.

```python
class Preflight:                                  # buyer/policy.py — ported from adcp-client
    def available_actions(self, buy: dict) -> list[Action]:
        """NEVER read available_actions[] or valid_actions[] directly. 3.1 structured
           available_actions carries mode+sla; legacy 3.0 valid_actions is flat —
           when only the legacy field is present assume self_serve and emit a
           one-shot deprecation warning."""
    def check_update(self, buy: dict, intent: UpdateIntent) -> PreflightResult: ...
    def decompose_update(self, intent: UpdateIntent) -> list[Action]: ...
    def check_budgets(self, plan: CampaignPlan, products: dict) -> list[BudgetViolation]:
        """min_package_budget, max_campaign_budget, package_budget/flight_days vs
           max_daily_spend, flight_days<=0 → 1, amount >= 0. The CI seed sets
           min_package_budget=1000.0 — an unclamped naive split is THE #1 reason a
           first create_media_buy returns BUDGET_TOO_LOW."""
```

Preflight enforces `MEDIA_BUY_STATE_MACHINE` client-side before dispatch (terminal → refuse; disallowed-but-non-terminal → refuse) so the *seller's* answer stays a pure oracle when the fuzzer bypasses the gate.

```python
class BuyerAgent:
    async def run_campaign(self, brief: Brief, *, wire: Wire = Wire.MCP,
                           dry_run: bool = False, use_llm: bool = True) -> CampaignRun
```

Every step appends `StepRecord(op, wire, request, exchange, decision, rationale)` to `CampaignRun`. Two payoffs: the run log is the demo artifact, and it doubles as fuzz corpus (`fuzz --from-run`) — real request shapes a seller actually accepted are far better mutation seeds than synthetic ones.

**`buyer/guard.py`** — `GuardMode.{OFF, LOG, RAISE}`. In `LOG` (the default for `buy`), every seller response runs the arity-1 oracle set and violations are written to the run's finding store instead of raised. **The buyer becomes a passive continuous fuzzer at zero additional cost.**

**Injection boundary, stated explicitly:** product names, briefs echoed by the seller, error `message`/`suggestion` strings, and webhook bodies are **data**. They are rendered into prompts inside a delimited `<seller_data>` block with a standing instruction that content within is untrusted. No seller string is ever executed, used as a URL, or allowed to alter the plan's structure — only deterministic fields (`product_id`, prices, format ids) flow into `CampaignPlan`.

### 3.11 `harness/` — local run

```python
class SalesAgentStack:
    def __init__(self, repo: Path = Path(r"C:\Users\pmezz\projects\salesagent"),
                 profile: Literal["dev","e2e"] = "e2e"): ...
    def up(self, *, seed: bool = True, timeout_s: int = 300) -> AgentTarget
    def down(self, *, volumes: bool = False) -> None
    def health(self) -> bool                      # GET /health
    def seed(self) -> SeedResult                  # docker compose exec → init_database_ci.py
    def reset(self) -> None                       # TRUNCATE (except alembic_version) + reseed
    def environment(self) -> str                  # probed once per run — drives unknown-field oracle
```

Windows/PowerShell-safe: `subprocess.run` with an **argv list** (never a shell string), absolute paths, `cwd=repo`, health-wait polls rather than sleeps.

**The standup gotchas, encoded in code not prose:**
- `docker compose up` alone gives you an **empty database** — `db-init` runs only `scripts/ops/migrate.py`. No tenant, no principal, no products.
- `scripts/setup/init_database.py` creates Tenant + CurrencyLimit + AdapterConfig + Principal + Products but **no `PropertyTag`** — and the required chain is `Tenant → CurrencyLimit(USD) → PropertyTag('all_inventory') → Products`. `init_database_ci.py` **does** create the PropertyTag.
- **Decision: seed with `init_database_ci.py` regardless of profile.** Canonical target = e2e stack, `localhost:8092`, token `ci-test-token`, tenant `ci-test`. Dev (`localhost:8000`, `test-token`, tenant `default`, `ADCP_AUTH_TEST_MODE=true`) is a secondary parity target.
- **`docker-compose.e2e.yml` publishes ZERO host ports.** You must overlay `-f docker-compose.e2e.ports.yml` (→ `localhost:8092` proxy, `localhost:5435` postgres) or you get connection refused. This is the single most common wasted hour.
- Both `env_file` entries on the dev stack are `required: false` — no `.env.secrets` needed.

`stack.up()` asserts post-seed invariants and **fails loudly, never silently proceeds**: ≥1 tenant, ≥1 principal with a token, ≥1 `CurrencyLimit`, ≥1 `PropertyTag`, ≥2 products.

`fixtures.py` adds what the CI seed doesn't — a **second tenant** (isolation oracle) and products with known `min_package_budget` boundaries — via **direct `psycopg` SQL** against the exposed Postgres. We deliberately do **not** import salesagent's factory-boy factories: they drag in the whole `src/` import graph and mutate global factory-session binding (`ALL_FACTORIES[*]._meta.sqlalchemy_session`), and there are two same-named `TenantFactory`s (`tests.factories` correct, `tests.fixtures` legacy dict). Plain SQL against a schema we don't own is the cheaper coupling and a clean process boundary.

`harness/doctor.py` runs before anything else: docker daemon up, clone path exists, ports free (dev 8000 / e2e 8092 / postgres 5435 / webhook 8788), `uv` present, `adcp==5.7.0` importable, `adcp.get_adcp_spec_version() == "3.1.0-beta.3"`, and — for Phase 5 — host↔container webhook reachability.

---

## 4. Cross-transport normalization — how differential comparison stays sound

The single biggest threat to a differential fuzzer's credibility is false positives from legitimate transport framing. The defense is an explicit two-tier contract plus a lossless audit trail.

### 4.1 Tier A — MUST be identical (compared byte-for-byte after JCS)

- `error.adcp_error.code`, `.recovery`, `.field`, `.suggestion`
- `len(error.errors)` and `{e.code for e in errors}`
- presence and value of `context`
- the normalized domain `payload`
- the JSON **type** of every payload leaf
- the **auth disposition** (`HARD_REFUSE` / `SOFT_EMPTY` / `SUCCESS`)

### 4.2 Tier B — legitimately transport-varying, excluded from equality, each owning its own single-wire oracle

| Field | Varies because | Its oracle |
|---|---|---|
| `task_id`, server `context_id` | per-call identity | must be present and stable within a call |
| top-level `message` | A2A `_serialize_for_a2a` adds `str(response)` | must appear **only** on A2A |
| `success` (bool) | A2A synthetic field | must appear **only** on A2A |
| HTTP status | REST-only carriage | INV-08 status map |
| JSON-RPC `error.code` | A2A-only carriage | `PROTOCOL-ERROR-FIDELITY` (#1670) |
| MCP `content[0].text` | human-readable mirror | must parse to the same envelope as `structuredContent` |
| `ProtocolEnvelope` wrapping | MCP/A2A wrap; **REST returns bare payload** | `ENVELOPE-WRAPPING` shape oracle |

**#1670 forces a rule:** parity **never** asserts on `jsonrpc_code`. It is recorded and used only by the arity-1 A2A fidelity oracle.

### 4.3 The pipeline (ordered, individually toggleable, every firing logged)

1. **`unwrap_transport_frame`** — REST: body as-is. MCP: `structuredContent`, or on `isError=True` `json.loads(content[0].text)`. A2A: last artifact DataPart, protobuf `Value` → Python.
2. **`split_protocol_envelope`** — pop `TASK_ENVELOPE_FIELDS = {status, task_id, context_id, message, errors, adcp_version, ext, replayed, success}` into `.envelope`. **Guarded against the domain-`status` collision**: only pop top-level `status` when its value ∈ `TaskStatus` *and* the op's response model does not declare a top-level domain `status`. (This is `extractAdcpTaskStatusFromPayload`; getting it wrong silently mis-classifies every media-buy response. E2 sends a media buy whose domain `status` is `"working"` to confirm neither side conflates them.)
3. **`strip_a2a_synthetic`** — remove `message`/`success`, record them; Tier-B oracle asserts A2A-exclusivity.
4. **`numeric_policy`** — **default `STRICT`**: `1.0 ≠ 1`. A whole-float where a peer wire sent an int is recorded as a `TypeDivergence` and becomes a `TYPE-PARITY` finding. **This is exactly how #1583 is rediscovered.** `TOLERANT` coerces whole floats to int and exists *solely* so #1583 does not mask every other A2A difference during a hunt. The mode is recorded in every finding.
5. **`drop_volatile`** — per-op `volatile_paths` (ids, `*_at`, UUIDs) replaced by ordinal tokens `<vol:1>`, `<vol:2>` keyed by **first-seen value**, so the *same* id maps to the *same* token. Referential relationships survive; only absolute values are lost.
6. **`sort_unordered`** — per-op declared unordered collections sorted by declared key. A separate oracle asserts ordering *stability* (same request twice → same order) where the spec requires deterministic ordering.
7. **`canonicalize`** — `rfc8785.dumps` → `differential_key = sha256(jcs(payload))`.

### 4.4 The soundness mechanism

Every rule that fires appends `(rule_id, json_pointer, before, after)` to `norm_log`, and **`norm_log` is embedded in every finding**. A reviewer can always answer "was the difference real, or did rule X erase it?" without re-running. Conversely, `adcp-buyer replay <finding_id> --no-norm` re-emits with the pipeline disabled.

**The error envelope is never normalized.** Only `payload` and framing pass through the pipeline. `wire_error_envelope` is stored verbatim, because the wire error envelope *is* the contract.

`spec/divergences.yaml` downgrades documented deltas (`AUTH_TOKEN_INVALID` project code; `UNSUPPORTED_FEATURE`/`IDEMPOTENCY_CONFLICT`/`IDEMPOTENCY_EXPIRED` emitted `correctable` against the SDK's `terminal`; several `NOT_FOUND` subclasses `correctable`; REST bare-payload vs `ProtocolEnvelope`) to `info` with a one-line justification each — **downgrade, never delete.** Default report shows NOVEL first, then confirmed non-info; `--include-info` shows everything.

---

## 5. Async / webhook / polling — locally

### 5.1 The spec lifecycle, restored

`core/tasks/lifecycle.py` defines the **9-member spec enum**: `submitted, working, input-required, completed, failed, canceled, rejected, auth-required, unknown`. `TERMINAL = {completed, failed, canceled, rejected}`. `PAUSED = {input-required, auth-required}`. The SDK's 5-member enum never leaves the transport module.

| Wire | Source of truth | Mapping |
|---|---|---|
| MCP | response body `status` (envelope position only) | direct |
| A2A raw | `pb.TaskState.Name(state)` → strip `TASK_STATE_`, lower, `_`→`-` | byte-faithful |
| A2A via SDK (cross-check only) | `TaskResult.metadata["status"]` when `.status == SUBMITTED` | `TaskResult.status` alone is lossy |
| REST | domain body only — **no task envelope exists** | `unknown`; reconcile by re-read over MCP |
| Webhook | `client.handle_webhook(...)` | the only source of in-band `input-required` |

### 5.2 Webhook reception — the local story

**The seller runs in Docker; the buyer runs on the Windows host.** The decisive fact: `src/services/protocol_webhook_service.py:106` `_normalize_localhost_for_docker` rewrites a `localhost` hostname to `host.docker.internal`, preserving userinfo and port. **So the buyer registers `http://localhost:8788/...` and the dockerized seller reaches the host.** No tunnel, no ngrok, no `extra_hosts` edit.

This is a **load-bearing assumption and Phase 5 must validate it empirically before anything depends on it.** The poller is always armed as fallback. First bind will trigger a Windows Firewall prompt — documented setup step.

`core/tasks/sink.py` — FastAPI on `ADCP_WEBHOOK_PORT` (default 8788):

```
POST /adcp/webhook/{task_type}/{agent_id}/{operation_id}
```

matching the SDK's `webhook_url_template` macros (`{agent_id}`, `{task_type}`, `{operation_id}`).

```python
raw = await request.body()                       # RAW BYTES — never re-serialize
payload = json.loads(raw)
result = verify_and_parse(payload, task_type, operation_id,
                          signature=request.headers.get("X-AdCP-Signature"),
                          timestamp=request.headers.get("X-AdCP-Timestamp"),
                          raw_body=raw)          # raw bytes avoid cross-language JSON mismatch
ledger.deliver(operation_id, result)             # sets the asyncio.Event
```

Registration per `core/push_notification_config.py`: `url` (`AnyUrl`), **`operation_id`** (`^[A-Za-z0-9_.:-]{1,255}$` — the spec's *canonical* channel; the seller **MUST NOT** parse the URL, so we set both the field and the path and let the fuzzer tell a URL-parsing seller from a field-reading one), `token` (≥16 chars, echoed verbatim — validate on receipt), `authentication: {schemes: ["HMAC-SHA256"], credentials: <48 random chars>}`. The auth block is a **switch, not a fallback**: it selects the legacy scheme over RFC 9421. salesagent honors exactly `"HMAC-SHA256"` and `"Bearer"`; anything else **silently sends unsigned**. The sink **rejects** a webhook whose signing mode differs from what was registered (`webhook_mode_mismatch`) — downgrade resistance.

**Registration-channel asymmetry (design-critical):**

| Wire | Payload `push_notification_config` | A2A protocol-level `configuration` |
|---|---|---|
| MCP | ✓ persisted to DB (`media_buy_create.py:2022`) | n/a |
| A2A | ✓ persisted to DB | ✓ `self._task_push_configs[task_id]` — **the only channel `_send_protocol_webhook` reads** |
| REST | ✗ **field does not exist on `CreateMediaBuyBody`** | n/a |

On A2A there are **two independent registries**, and the one that drives protocol webhooks is the JSON-RPC-level one the SDK does not expose. `a2a.py` sets **both**, and a dedicated oracle asserts they behave identically (they currently do not — that's a finding).

Payload shape differs by wire: A2A sends a full protobuf `Task` for terminal states and `TaskStatusUpdateEvent` for intermediate (`adcp_a2a_server.py:418-426`), serialized camelCase with A2A-0.3 lowercase enums; MCP sends `McpWebhookPayload`. The sink normalizes both to one `Exchange`.

### 5.3 Polling fallback (`poller.py`)

| Wire | Mechanism | Constraint |
|---|---|---|
| MCP | `get_task` tool (**bare dict**) | requires non-anonymous principal |
| A2A | JSON-RPC `tasks/get` | task store is an **in-memory dict on the handler instance**, not identity-scoped (#1702), lost on restart; `TaskNotFoundError` flattens to `-32603` (#1670) |
| REST | **none** | reconcile via `get_media_buy_delivery` / `get_media_buys` over MCP |

Rules ported verbatim from adcp-client `TaskExecutor.pollTaskCompletion` because each is a regression class:
- Default interval 60 s; **in-band `working` wait capped at 120 s** (AdCP PR #78); exponential backoff with jitter; hard deadline.
- **Paused states never spin.** `input-required` / `auth-required` return an intermediate success `Exchange` immediately; the buyer resubmits with input or refreshed auth. Polling them to timeout is `adcp-client#977`.
- **Task eviction** during polling → descriptive `failed` Exchange recommending webhooks, never an uncaught exception (`adcp-client#1585`).
- **Never conflate handles.** `operation_id` (buyer UUID, for webhook correlation) ≠ `task_handle` (server-assigned, for polling). Separate `Exchange` fields; the ledger keys on `operation_id` and stores `task_handle` as a column.
- **Wrapper-unwrap depth bounded at 8** (`TASKS_GET_UNWRAP_MAX_DEPTH`). E2 sends a 12-deep nested envelope and asserts graceful `unknown`, not a stack overflow.

### 5.4 Rendezvous

```python
async def await_terminal(op_id: str, *, timeout: float,
                         poll_interval: float = 60) -> Exchange:
    ev = ledger.event_for(op_id)
    poll = asyncio.create_task(poller.until_terminal(op_id, poll_interval))
    done, _ = await asyncio.wait({asyncio.create_task(ev.wait()), poll},
                                 timeout=timeout, return_when=FIRST_COMPLETED)
    # cancel the loser; on buyer abort → fire-and-forget A2A tasks/cancel,
    # return a failed Exchange, and do NOT write seller-controlled error text
    # into buyer logs (silent-on-rejection trust boundary).
```

`TaskLedger` (SQLite) survives restart: `(operation_id) → (agent_id, wire, op, task_handle, context_id, idempotency_key, checkpoint_json, status, last_result_json)`. On boot every non-terminal row is re-armed — the sink can still deliver and the poller resumes.

Sequence traces spanning an A2A `tasks/get` are marked `process_bound`; the shrinker never reduces across a server restart; a `tasks/get` returning another principal's task is a **critical** isolation finding.

---

## 6. Build phases — dependency-ordered, with explicit parallel lanes

Two structural properties, taken from the pragmatic design and kept: **the stack is Phase 0, not Phase 8** (everything downstream is validated against a real seller from day one, with no "wire it up at the end" cliff), and **the fuzzer ships before the brain** (it needs strictly less, starts finding bugs a week earlier, and hardens the layer the brain then sits on).

| Phase | Lane | Builds | Depends on | Parallel? |
|---|---|---|---|---|
| **P0** | — | `uv init`, deps + hard pin, `config.py`, `cli.py` skeleton, `obs/recorder.py`, `harness/doctor.py`, `harness/stack.py`, `harness/seed.py`, `scripts/*.ps1` | — | **Sequential — blocks everything** |
| **P1** | — | `core/exchange.py`, `core/envelope.py`, `core/ops.py`, `core/matrix.py`, `core/identity.py`, `core/session.py`, `core/spec/*` (re-exports) | P0 | Sequential (tiny, pure data, no network) |
| **P2a** | T | `transport/wiretap.py` + `transport/rest.py` + minimal `client.call()` | P1 | ← start here (simplest wire, fully specified route table, `GET /capabilities` is the cheapest probe) |
| **P2b** | T | `transport/mcp.py` | P1, P2a's `wiretap` | ‖ with P2c |
| **P2c** | T | `transport/a2a.py` | P1, P2a's `wiretap` | ‖ with P2b |
| **P2d** | T | `binder`, `client.call_all()`, registry drift test | P2a-c | after b+c |
| **P3a** | N | `core/normalize/*` (rules, pipeline, canonical) | P2d + golden fixtures captured in P2 | ‖ with P3b, P3c |
| **P3b** | C | `core/codec.py`, `core/compat.py` (D1/D2/D3 profiles + divergence tests), `core/capabilities.py` | P1 (models) — **does not need P3a** | ‖ |
| **P3c** | H | `harness/fixtures.py`, `fuzz/arena.py` (NAMESPACE first) | P0 | ‖ |
| **P4** | O | `core/oracles/*` — all 33 + taint + `divergences.yaml`; `tests/unit/` on synthetic envelopes; `tests/oracle_mutation/` | P3a | Can fan out **per oracle file** across implementers (6-8 way parallel) — they share only `registry.py` and `spec/` |
| **P5** | ★ | `fuzz/engines/differential.py`, `fuzz/findings/*`, `known_issues.yaml`, 7 hand-written reproducer seeds, `tests/validation/` | P4, P2d | **THE MILESTONE — sequential, nothing else ships first** |
| **P6a** | I | `core/idempotency.py` (vault + JCS), `core/tasks/*` (lifecycle, ledger, sink, poller, rendezvous) | P3b | ‖ with P6b |
| **P6b** | F | `fuzz/strategies/*`, `fuzz/engines/schema.py`, `fuzz/corpus.py`, coverage tokens, `hypothesis.target()` | P5 | ‖ with P6a |
| **P7a** | F | `fuzz/engines/sequence.py`, `fuzz/shrink.py`, `arena` TENANT mode, idempotency replay sub-engine | P6a **and** P6b | ‖ with P7b |
| **P7b** | B | `buyer/*` — models, `select.py` (fallback FIRST, LLM second), `planner.py`, `policy.py`, `creative.py`, `executor.py`, `monitor.py`, `agent.py`, `guard.py` | P4 (oracles) + P6a (vault/tasks) — **not** P6b/P7a | ‖ with P7a |
| **P8** | — | `fuzz/findings/report.py` (markdown/JSON/JUnit, NOVEL-first, run-over-run diff), `fuzz/repro.py`, `transport/sdk_crosscheck.py` + byte-parity test, CI nightly `fuzz all --budget 30m` | P5, P7a | ‖ sub-tasks |

**Fan-out summary for an implementation workflow:**
- P0→P1→P2a is a hard serial spine (~2 days). Everything after P2a can fan out.
- Peak parallelism is **P4** (one implementer per oracle module, 6-8 way) and **P6/P7** (two independent lanes: fuzz-depth vs buyer-brain).
- The only true reconvergence points are **P2d** (all three wires must exist before `call_all`) and **P5** (the gate).

---

## 7. Verification plan

Every phase has a gate that runs against a **live salesagent in Docker**. Standard target: e2e stack, `http://localhost:8092`, token `ci-test-token`, tenant `ci-test`, seeded by `scripts/setup/init_database_ci.py`.

```powershell
# canonical standup — the ports overlay is MANDATORY
docker compose -f docker-compose.e2e.yml -f docker-compose.e2e.ports.yml up -d
docker compose -f docker-compose.e2e.yml -f docker-compose.e2e.ports.yml `
  exec adcp-server python scripts/setup/init_database_ci.py
uv run adcp-buyer doctor          # docker, ports, clone, adcp pin, spec version
```

| Phase | Gate | How it's proven |
|---|---|---|
| **P0** | `uv run adcp-buyer doctor` all green; `stack up` → `/health` 200; `seed` asserts ≥1 tenant / principal / CurrencyLimit / PropertyTag / ≥2 products | `tests/contract/test_stack.py` |
| **P1** | envelope validator round-trips golden two-layer envelopes; `adcp.get_adcp_spec_version() == "3.1.0-beta.3"`; `adcp.__version__ == "5.7.0"` | `tests/unit/` — no network |
| **P2a** | `adcp-buyer probe --wire rest` returns real products. **First end-to-end HTTP.** All 12 routes reachable; auth-optional degrade to `identity=None`; auth-required → 401 `AUTH_TOKEN_INVALID` | `tests/contract/test_rest_routes.py` |
| **P2b/c** | `adcp-buyer probe` green on all three wires; A2A asserts **non-307** (`/a2a`, no trailing slash); MCP asserts `/mcp/` trailing slash works | `tests/contract/test_probe_matrix.py` |
| **P2d** | `call_all("get_products")` returns 3 `Exchange`s; registry drift test passes against live `list_tools` / agent card / `/openapi.json` (16/18/12) | `tests/contract/test_registry_drift.py` |
| **P3a** | normalized `get_products` payloads are **JCS-equal** across MCP/A2A modulo a recorded, human-reviewed `norm_log` | `tests/contract/test_normalization_parity.py` |
| **P3b** | each of D1/D2/D3 has a test asserting the divergence **still exists**; spec-literal `get_products` with `buying_mode` fails on dev exactly as predicted (or the test fails loudly and the profile is corrected) | `tests/contract/test_divergences.py` |
| **P4** | every oracle has ≥1 passing fixture **and** ≥1 firing fixture; no oracle is silent | `tests/oracle_mutation/` — no server needed |
| **P5** | ★ **all 7 known bugs rediscovered from a cold stack** | `tests/validation/test_rediscovers_known_issues.py` |
| **P6a** | same payload submitted twice → **one key, one media buy, `replayed=true` on the second**; webhook `localhost → host.docker.internal` rewrite empirically confirmed by a real delivered webhook | `tests/e2e/test_idempotency.py`, `test_webhook_roundtrip.py` |
| **P6b** | 1000 hypothesis examples over `get_products` + `create_media_buy` with **zero uncaught exceptions in the fuzzer itself** + a triaged finding list | `adcp-buyer fuzz schema --examples 1000` |
| **P7a** | a **deliberately injected** bug (scratch container with a patched `valid_actions` table) is found and shrunk to ≤3 steps — this validates the shrinker the way P5 validates the oracles | `tests/validation/test_shrinker.py` |
| **P7b** | `adcp-buyer buy --brief "..." --no-llm` completes on MCP; then with Claude; then green on all 3 wires; double-submit yields one buy; guard mode logs ≥0 violations with no crashes | `tests/e2e/test_buy_flow.py` |
| **P8** | run-over-run diff distinguishes NEW from KNOWN; every `high` finding has a `repro.py` that reproduces in a clean arena | `adcp-buyer fuzz all --budget 30m --report md` |

### 7.1 The known-bug rediscovery suite — the project's proof of teeth

| Bug | Oracle | Driving case |
|---|---|---|
| **#1670** typed `A2AError` → `-32603` | `PROTOCOL-ERROR-FIDELITY` | `message/send` with `skill:"no_such_skill"` (expect `-32601`); `tasks/get` on a random id (expect `-32001`) |
| **#1583** protobuf int→float | `TYPE-PARITY` (numeric STRICT) | `update_media_buy` then read the revision counter on all 3 wires |
| **#1651** token-less `get_media_buys` | `AUTH-DISPOSITION-PARITY` | no-token `get_media_buys`, MCP vs A2A (REST route absent) |
| **#1316** delivery account scope on A2A | `ACCOUNT-SCOPE-PARITY` | `get_media_buy_delivery` with a foreign `account`, A2A vs REST |
| **#1449** MCP advisory `errors[]` dropped | `ADVISORY-ERRORS-PARITY` | an op emitting advisory `errors[]` on MCP success, across all 3 |
| **REST `buying_mode` drop** | `FIELD-REACHABILITY` | `get_products` with `buying_mode` guaranteed vs non_guaranteed, REST vs MCP/A2A |
| **`AUTH_TOKEN_INVALID` vs `AUTH_REQUIRED`** | `CODE-MEMBERSHIP` + `AUTH-CODE` | any auth-required op with a garbage token |

Failure message: *"Either the seller fixed gh-XXXX (set `status: fixed` in known_issues.yaml) or the fuzzer regressed — the oracles have lost teeth."*

The last row is documented-intentional and lands as `info` — which is precisely why it's in the gate: it exercises the divergence/masking machinery end-to-end and proves `info` findings are still **detected**. When salesagent fixes #1583, that test goes red and gets flipped to `assert not found` — the suite becomes a regression detector in reverse.

**Expected free win:** P5's first run of `parity` + `envelope` over just `get_products` / `list_creatives` / `get_media_buys` should surface **#1651 and the AUTH_TOKEN_INVALID divergence with zero mutation logic**. If it doesn't, the transport layer is wrong, not the seller.

### 7.2 Standing run discipline

- **Serial by default.** Live e2e shares one DB with no per-test rollback; xdist races on shared rows and *silently drops transports at collection* (a false green). E1/E2 may parallelize over read-only ops with a worker pool; **E3 and all mutating cases are strictly serial.**
- **Read-only + auth ops first.** P5–P6 fuzz reads and auth only (which is where 5 of 7 known bugs live). Mutating oracles arrive in P7a behind `arena.reset()` between cases.
- **Unreachable stack is a hard `RuntimeError`, never a skip.**
- Tiered budgets: `smoke` <60 s (seeds only) · `pr` 5 min · `nightly` 30–60 min.

---

## 8. Risks & open decisions (ranked)

| # | Risk | Why it's real | Mitigation | Owner phase |
|---|---|---|---|---|
| **R1** | **`get_products.buying_mode` is required by the SDK and unknown to every salesagent wrapper.** In dev (`extra='forbid'`) a spec-correct call fails loudly on all three wires | The single most likely "nothing works on day one" failure | `CompatProfile.SALESAGENT` pops it; a divergence test asserts the gap persists; the fuzzer drives it unmodified as `FIELD-REACHABILITY`. **Verify empirically in P2, do not assume** | P2/P3b |
| **R2** | **SDK cannot supply raw error-path wire.** `protocols/mcp.py:583-590` reconstructs `DebugInfo.response`, dropping `errors[]`; a2a-sdk hides the JSON-RPC code | An SDK-based fuzzer is blind to #1449, #1670, #1583 — the bugs it exists to find | **Retired by architecture**, not managed: hand-rolled raw transports; SDK for models only; `sdk_crosscheck` is contract-test-only | P2 |
| **R3** | **Dev stack seeds nothing; `init_database.py` omits `PropertyTag`.** `db-init` runs only `migrate.py` | First `get_products` returns `[]`; `create_media_buy` fails obscurely | Always seed with `init_database_ci.py`; `stack.up()` asserts the full chain and **fails loudly** | P0 |
| **R4** | **False positives from legitimate transport framing** | The single biggest threat to a differential fuzzer's credibility | Two-tier normalization; every Tier-B field owns its own oracle; `norm_log` in every finding; `divergences.yaml` downgrades to `info`; `--no-norm` replay always available | P3a/P4 |
| **R5** | **Known issues mask new bugs** — #1583 alone would generate hundreds of A2A findings | Signal dies in noise | Masking is per-`signature` and **downgrade-only**; `NumericPolicy.TOLERANT` exists to see past #1583; masked counts always printed in the report header | P5 |
| **R6** | **SDK auto-mints idempotency keys** (`resolve_key()` UUID4 on absent) | A retry through the SDK double-books — a real-money bug | `IdempotencyVault` mints before first attempt and stamps explicitly; the SDK generator is never reached | P6a |
| **R7** | **`update_media_buy` request shape is structurally incompatible** — no `account`/`revision`/`canceled`/`cancellation_reason`/`new_packages`; non-spec `flight_*_date`, scalar `budget` | **Cancellation has no expressible request shape.** Spec-conformance testing of update is partly blocked | Codec emits salesagent's shape from spec-typed intent; the cancel flow is marked NOT-IMPLEMENTABLE with the gap filed as a P0 conformance finding | P3b |
| **R8** | **Shared-DB state pollution across fuzz cases** | Mutating cases leave rows that poison later cases; findings become non-reproducible | P5–P6 fuzz read-only + auth only; mutating oracles in P7a behind arena reset; **never parallelize mutating fuzz** | P7a |
| **R9** | **Fuzzer bugs reported as seller bugs** | Destroys credibility of every finding | `confidence` field; every critical/high auto-replayed from generated `repro.py` in a clean arena before reporting; oracle-mutation tests prove each oracle can both fire and not fire | P5 |
| **R10** | **`create_media_buy` is a wall of required fields**: `account` (RootModel union), `brand`, `packages` (`min_length=1`), `start_time` (**`StartTiming` object**, not a datetime), `end_time` (**tz-aware required**), `idempotency_key` (16-255, `^[A-Za-z0-9_.:-]$`); response is a `UnionType` with no `.model_fields`; budget must clear `min_package_budget=1000` | The most common "why does nothing work" sink | Build one known-good request in P2 as a pinned fixture. Branch on `Exchange.status`, **never** `hasattr`. Preflight clamps budgets against seller limits | P2/P7b |
| **R11** | **Host↔container webhook reachability** — the `localhost → host.docker.internal` rewrite is the single load-bearing assumption for local async | If it breaks, all async testing degrades to polling | Empirically validated in P6a **before** dependent work; poller always armed; Windows Firewall prompt documented | P6a |
| **R12** | **A2A task store is an in-memory, non-identity-scoped dict (#1702)**, lost on restart | A2A polling is unreliable by construction | Prefer webhooks on A2A; poller treats not-found as a descriptive failure, never the sole recovery path; traces spanning `tasks/get` marked `process_bound` | P6a |
| **R13** | **Spec/SDK pin drift** — #1625 (migrate to adcp 6.6.0 / spec 3.1.1) is **open** | Model shapes and `STANDARD_ERROR_CODES` change under us; we'd fuzz the wrong contract | Hard-pin `adcp==5.7.0`; startup assert comparing `adcp.get_adcp_spec_version()` against the server's `/api/v1/capabilities`; mismatch ⟹ all spec-table oracles downgraded to `info` + loud banner | P1 |
| **R14** | **LLM nondeterminism contaminating fuzz results** | Unreproducible findings, token burn | The fuzzer is 100% deterministic — seeded PRNG, no LLM anywhere, `fuzz/` never imports `buyer/`. An optional `--llm-mutator` profile tags findings `engine: llm` and can never gate CI | structural |
| **R15** | **Prompt injection via seller-controlled strings** (product names, `suggestion`, webhook bodies) | The buyer subverted by the system it's testing | `<seller_data>` delimiting + standing untrusted-content instruction; only deterministic fields reach `CampaignPlan`; no seller string ever becomes a URL, tool argument, or instruction. Plus the `details` taint oracle on the fuzz side | P7b |
| **R16** | **REST `*Body` models are bare `pydantic.BaseModel`** (no extra-field policy, FIXME #1442) → unknown-field behavior differs by `ENVIRONMENT` | Flaky, config-dependent findings | Probe `ENVIRONMENT` once per run; the oracle asserts both cross-wire parity **and** consistency with the probed environment — turning flaky behavior into a deterministic finding | P4 |
| **R17** | **Windows/PowerShell friction** — docker exec quoting, `.venv/Lib` not `lib`, first `uv run` sync ~minutes, port collisions with existing salesagent worktrees, **e2e ports overlay** | Setup drag, wasted hours | argv-list `subprocess` (never shell strings), `pathlib` throughout, configurable ports, `doctor` runs first, all encoded in `scripts/*.ps1` — never in prose | P0 |
| **R18** | **Seller rate-limiting turns a long run into noise** | Findings become environmental artifacts | Detect `RATE_LIMITED` (transient), back off, count as environmental — **but still assert the rate-limit envelope itself is well-formed** | P5 |

### 8.1 Open decisions (need a call before the phase that consumes them)

1. **Canonical stack profile — e2e (`:8092`/`ci-test`) or dev (`:8000`/`default`)?** Plan assumes **e2e as canonical** (strict `ENVIRONMENT=development` → `extra='forbid'`, which makes unknown-field behavior deterministic) with dev as a secondary parity target. Decide at P0.
2. **Does `PyCoverage` (white-box line coverage via a locally-built server container) earn its keep?** The `CoverageProbe` interface ships day one; the implementation is deferred past P8. Revisit if `ShapeCoverage` novelty plateaus.
3. **`fuzz --from-run`** (buyer run logs as mutation seeds) — high value, cheap, but only exists after P7b. Scheduled P8; promote earlier if the synthetic corpus proves shallow.
4. **Reporting `context` echo cap testing (INV-03)** needs 63/64/65 KB payloads; confirm the seller doesn't 413 at the proxy before attributing a drop to application logic. Verify in P4.
5. **Whether to ship `SPEC_LITERAL` conformance runs as a first-class report** ("here is everywhere salesagent diverges from AdCP 3.1") separate from the bug-hunt report. Cheap given `compat.py`; likely the most externally valuable artifact the project produces. Decide at P8.