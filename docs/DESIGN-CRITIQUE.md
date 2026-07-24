# Completeness Critique — adcp-buyer-agent build plan

Verified against `scout-digest.json` plus direct reads of the salesagent clone. Every claim below carries file:line evidence.

---

## Tier 0 — Wrong facts that break a gate or the registry on day one

### 1. The A2A surface numbers are wrong; `test_registry_drift.py` fails immediately
`adcp_a2a_server.py:1402-1428` — `skill_handlers` has **18** entries. `create_agent_card()` (`:2083`) declares **16** `AgentSkill`s. These are different sets, and the plan collapses them into one "18 A2A skills (agent card)" row.

Concretely:
- **`get_creatives` is not dispatchable.** `_handle_get_creatives_skill` exists at `:1712` and raises `UnsupportedOperationError`, but the name is absent from `skill_handlers` — invoking it yields `MethodNotFoundError` (→ `-32601`, flattened to `-32603`), never `UNSUPPORTED_FEATURE`. The plan lists it as one of the 6 stubs.
- **Reachable stubs are 5**, not 6: `create_creative` (:1696), `assign_creative` (:1741), `approve_creative` (:1745), `get_media_buy_status` (:1751), `optimize_media_buy` (:1755).
- **Only 3 of those 5 are on the agent card** (`approve_creative`, `get_media_buy_status`, `optimize_media_buy`). `create_creative` and `assign_creative` are dispatchable but *unadvertised*.

So `CAPABILITY-TRUTHFULNESS` does not "fall out for free" over 6 — it covers 3.

**Fix.** `WireBinding` needs two orthogonal facts for A2A: `advertised: bool` (agent card) and `dispatchable: bool` (handler map). `Availability` gains `UNDISPATCHABLE`. The drift test asserts card == 16 and handler map == 18 as *separate* assertions. Add the mirror oracle `CAPABILITY-CONCEALMENT` (dispatchable-but-unadvertised) — it is free and it catches the two the plan misses.

### 2. Half the #1670 validation suite cannot fire — `tasks/get` never raises `TaskNotFoundError`
`adcp_a2a_server.py:1005-1020`:
```python
task_id = params.id
return self.tasks.get(task_id)
```
No raise. A random id returns `None`, so there is no typed `A2AError` for the v0.3 adapter to flatten. The plan's `corpus/known/gh-1670_tasks_get_missing.json` is a dead case, and P5 — "★ THE MILESTONE" — is gated on it.

**Fix.** Replace with cases that actually raise a typed `A2AError` through the compat adapter: `tasks/list` → `UnsupportedOperationError` (`:1049`); `tasks/pushNotificationConfig/set` with no `url` → `InvalidParamsError` (`:1147`); `message/send` without auth on a non-discovery skill → `InvalidRequestError`; unknown skill → `MethodNotFoundError` (`:1431`). Keep unknown-skill as the primary. Separately: `tasks/get` returning a null result where the spec wants `TaskNotFoundError` is *its own* finding — add an oracle for it.

### 3. A2A has **three** push-notification registries, not two — and the spec-canonical one is dead
- (a) payload `push_notification_config` → DB (media_buy_create)
- (b) `message/send` → `params.configuration.task_push_notification_config` → `self._task_push_configs[task_id]` (`:582`)
- (c) JSON-RPC `tasks/pushNotificationConfig/set` → `on_create_task_push_notification_config` (`:1122`) → **DB via `PushNotificationConfigUoW`**

`_send_protocol_webhook` reads **only (b)** (`:377`). So (c) — the A2A-spec-canonical registration method — writes to a store nothing ever reads. The plan's ops registry omits the entire `tasks/pushNotificationConfig/{set,get,list,delete}` method surface (`:1060`, `:1122`, `:1197`, `:1255`), plus `tasks/get`, `tasks/cancel`, `tasks/list`, `tasks/resubscribe`.

**Fix.** Model A2A JSON-RPC methods as first-class ops in `OPERATIONS` (arity-1 only — no parity peer). Register a webhook via all three channels and assert delivery parity; (a) and (c) being silently dead is a **high** finding the plan currently has no path to generate.

### 4. REST does **not** fail loudly on unknown fields — R1's premise is wrong for one of three wires
`api_v1.py:64,71,82,93,105,115,127,133,150,156,164` — every `*Body` is bare `pydantic.BaseModel`, so `extra` defaults to `'ignore'` **unconditionally**, independent of `ENVIRONMENT`. `ENVIRONMENT` only flips extra-policy for MCP/A2A (`SalesAgentBaseModel`).

Consequences:
- R1's "a spec-correct request fails loudly on all three wires" is false. On REST, `buying_mode` is silently swallowed.
- `UNKNOWN-FIELD-CONSISTENCY`'s clause "or disagrees with the server's probed `ENVIRONMENT`" produces a **false positive on every REST case**.
- D1 (CompatProfile) and `FIELD-REACHABILITY` are testing the same REST behavior by two mechanisms.

**Fix.** Extra-field policy belongs in the registry per `(wire, op)`, not probed once per server. REST's expected policy is `ignore` until #1442 lands — put that in `divergences.yaml` and have the oracle assert *against the declared policy*, so when #1442 lands the test goes red.

---

## Tier 1 — Unsound assumptions (false positives / untestable gates)

### 5. `x-dry-run` is not a no-op — it is a different code path
`testing_hooks.py:549` `is_testing = dry_run or test_session_id or simulated_spend`; `:591` rewrites `media_buy_id` when `dry_run` and the id doesn't start with `test_`; `:614` routes to simulated delivery. E1's `SEQUENTIAL_ISOLATED` default ("`x-dry-run` unless `--wet`") therefore fuzzes the testing-hooks path and attributes its divergences to the protocol.

Worse: it is **unverified whether A2A propagates `x-dry-run`** into `ServerCallContext` at all. If it doesn't, dry-run mutations are real writes on A2A and simulated on MCP/REST — a guaranteed spurious parity finding *plus* uncontrolled state.

**Fix.** Make per-wire `x-dry-run` propagation an explicit P2 gate assertion. Make testing-mode a tagged run dimension (`testing_mode` in `Exchange`, in `signature`, in `Finding.target`), never a silent default. Default mutating differential runs to arena-isolated **wet** with reset between cases; dry-run becomes a separate labelled campaign.

### 6. The reporting-webhook gate in P6a hangs for up to an hour, then never fires again
`delivery_webhook_scheduler.py:33` `SLEEP_INTERVAL_SECONDS = int(os.getenv("DELIVERY_WEBHOOK_INTERVAL") or "3600")`; `:72-75` "sends immediately on startup … then hourly"; `_send_report_for_media_buy` carries a **24h duplicate suppression**. A media buy created *after* startup waits for the next hourly tick; once reported, it is suppressed for 24h.

**Fix.** `harness/stack.py` must inject `DELIVERY_WEBHOOK_INTERVAL` (≈10s) via the compose overlay, and the arena must mint a **fresh media buy per webhook case** to dodge the 24h dedupe. Document that reporting webhooks are scheduler-driven, not create-time — the plan's `monitor.py` currently implies the latter.

### 7. There is no MCP protocol webhook on the `create_media_buy` path
`create_mcp_webhook_payload` is called only from `context_manager.py:868` (workflow-step transitions), `admin/blueprints/operations.py:501,595` (**human** approve/reject in the Admin UI), `admin/blueprints/creatives.py:257`, and the delivery scheduler. Nothing fires an MCP webhook synchronously off `create_media_buy`.

So "MCP submitted → webhook completes it" requires a human clicking Approve. The plan's `rendezvous.await_terminal`, R11, and the P6a gate all assume otherwise.

**Fix.** On MCP, polling (`get_task`) is the *primary* path and webhook is opportunistic — invert the rendezvous priority per-wire. Add a harness affordance that drives the approval transition programmatically (Admin API or direct workflow-step advance) or the async lifecycle is simply unreachable in a headless run. Split P6a's gate into (i) reachability proven by a synthetic POST from inside the container, and (ii) a real async completion webhook — only (i) is a hard blocker.

### 8. The `localhost → host.docker.internal` rewrite is real but narrower than claimed
Confirmed at `protocol_webhook_service.py:106`, called at `:163`, and it *does* cover both A2A and MCP/delivery payloads because everything funnels through `ProtocolWebhookService.send_notification`. Good — R11 survives. But `:110` matches `parsed.hostname.lower() == "localhost"` **exactly**: `127.0.0.1` is not rewritten, and `send_notification` returns a bool most callers ignore, so the failure is silent.

**Fix.** `doctor` and `sink` must advertise the literal string `localhost`. Add a fuzz case registering `127.0.0.1` and assert the delivery failure is *surfaced*, not swallowed — silent webhook drop is a legitimate high finding.

### 9. `CODE-MEMBERSHIP` will fire on essentially every auth error in the corpus
`AUTH_TOKEN_INVALID` ∉ `STANDARD_ERROR_CODES`, so INV-04 fires arity-1 for *every* auth-required op × every wire × every invalid-token case. `divergences.yaml` is keyed `(op, code)`, but this divergence is op-independent. Even at `info`, it dominates occurrence counts, pollutes the fingerprint-similarity tier of the known-issue matcher, and buries genuine membership violations.

**Fix.** Allow `op: "*"` in divergence entries. Add an explicit `PROJECT_CODES = {"AUTH_TOKEN_INVALID"}` that `CODE-MEMBERSHIP` consults *before* firing, emitting **one aggregated run-level `info` row** rather than N findings. Parity oracles keep firing normally — those carry the real signal.

### 10. INV-07 and INV-08 are constructed to contradict each other
INV-07 hardcodes `AUTH_TOKEN_INVALID → 401`. INV-08 derives status from `_build_error_code_to_status`, where **the highest status wins for a shared wire code** (digest, MCP gotchas). Any subclass sharing that code with a higher `_default_status_code` makes one of the two oracles a guaranteed false positive.

**Fix.** No oracle may hardcode an HTTP status. INV-07 asserts code + recovery only; status is INV-08's sole jurisdiction, from the imported table.

### 11. `adcp_error is not errors[0]` is a meaningless assertion over the wire
The "defensive COPY" property is an **in-process REST** invariant of `build_two_layer_error_envelope`. Over MCP and A2A the envelope is serialized to JSON and re-parsed, so `is not` is trivially true and carries zero information — and on REST the plan reads it back through `r.json()` too, so it's trivially true there as well.

**Fix.** Delete identity checks from wire oracles. Assert structural equality of the two layers. If the aliasing property matters, it's a server-side unit test, not a fuzzer oracle.

### 12. Webhook payloads never enter the normalization pipeline
A2A webhook bodies are camelCase protobuf `MessageToDict` output with A2A-0.3 lowercase enums (`protocol_webhook_service.py:92-94`, `_to_wire_dict` + `_normalize_a2a_task_state_to_v03`); MCP bodies are `McpWebhookPayload.model_dump`. The plan says "the sink normalizes both to one `Exchange`," but `normalize/rules.py` has no decamelize rule, and `NormPipeline.run(wire, op, raw)` is keyed on `(wire, op)` — neither of which the sink knows for an inbound webhook.

**Fix.** Add `Wire.WEBHOOK_A2A` / `Wire.WEBHOOK_MCP` as distinct pipeline inputs with a `decamelize` rule and a webhook→op resolver via the `TaskLedger`. Webhook `Exchange`s get their own arity-1 oracle set (no parity peer exists).

---

## Tier 2 — Module boundaries and build-order hazards

### 13. P3b is declared parallel to P3a but depends on it
`tests/contract/test_divergences.py` proves D1/D2/D3 by comparing behavior *across wires*; that needs `differential_key` / `structural_diff` from `normalize/canonical.py` (P3a), or P3b reimplements them. `capabilities.py` has the same problem — it reconciles `/capabilities` across three wires.

**Fix.** Move `canonical.py` (JCS wrapper + JSON-Pointer differ — pure, ~50 LOC, no dependencies) into P1 under `core/spec/`. Then P3a/P3b/P3c are genuinely parallel. Otherwise make P3b depend on P3a and stop calling the lane parallel.

### 14. `buyer/ → fuzz/` is a real dependency the plan's invariant denies
`buyer/guard.py` "writes violations to the run's finding store" — `fuzz/findings/store.py`. The stated invariant is "`fuzz/` never imports `buyer/`," which is satisfied, but the *inverse* edge makes the fuzz package non-optional for the buyer and drags `hypothesis`-adjacent code into the buyer install path.

**Fix.** `Finding`, `Violation`, `signature`, and `store` belong in `core/findings/`. `fuzz/` and `buyer/` both depend on core; neither depends on the other. `known_issues.py` and `report.py` can stay in `fuzz/`.

### 15. P4's "6-8 way parallel, sharing only `registry.py` and `spec/`" is false
`parity_o.py` needs `AuthDisposition` (defined in `auth_o.py`) and `DiffEntry` (in `normalize/canonical.py`) and the per-op `volatile_paths` / unordered-collection declarations (in `ops.py`). `taint.py` is *consumed by* `envelope_o.py` (INV-02 `details` clause) and `idempotency_o.py` (INV-20 no-leak).

**Fix.** Split P4 into **P4a (serial, ~half a day)**: `registry.py`, `Violation`/`DiffEntry`, `AuthDisposition`, `taint.py`, per-op volatile/unordered declarations — then **P4b** fans out per oracle module.

### 16. `mutating ⇔ name ∈ IDEMPOTENT_TASKS` is not an equivalence, and `IDEM-SCOPE` will mass-false-positive on REST
Confirmed: `api_v1.py:71-80` — only `CreateMediaBuyBody` has `idempotency_key`. `UpdateMediaBuyBody` (`:82`) and `SyncCreativesBody` (`:105`) do not, yet both ops are in `IDEMPOTENT_TASKS`. `IDEM-SCOPE`'s rule — "a mutating op in `IDEMPOTENT_TASKS` with the key omitted must `VALIDATION_ERROR`" — therefore fires on **every** REST update/sync case, for a structural reason no buyer can avoid.

**Fix.** Split `mutating` and `idempotent` into separate `OpSpec` fields. Gate `IDEM-SCOPE` applicability on `WireBinding` actually being able to carry the key. The *inability* to carry it is one deduped `FIELD-REACHABILITY` finding, not N per-case violations.

### 17. Single-wire ops silently look clean
`get_media_buys` is 2-wire; `list_tasks` / `get_task` / `complete_task` are MCP-only. `run_arityN` on a 1-element `ExchangeSet` is a no-op, and the plan never says so. Four of ~17 ops get zero parity coverage while reporting green.

**Fix.** `run_arityN` emits a `coverage: single_wire` marker per op; the report must state "parity untested for N ops." A gap you can't see is worse than a gap.

---

## Tier 3 — Smaller, still concrete

18. **REST create cannot carry `reporting_webhook` either** — `CreateMediaBuyBody` (`api_v1.py:71-79`) has only `brand/packages/start_time/end_time/po_number/account/idempotency_key/adcp_version`. The plan notes the missing `push_notification_config` but not this. `buyer/monitor.py` has **no REST path at all**. Add `monitorable_on: frozenset[Wire]` alongside `async_capable_on`.

19. **`field` pointers will not match across wires.** `CreateMediaBuyBody.packages` defaults to `[]` (`:73`) while `CreateMediaBuyRequest` requires `min_length=1` — so REST's empty-packages error surfaces one layer deeper than MCP/A2A's. Codes may match; `field` will not. The plan puts `error.adcp_error.field` in **Tier A (must be identical)**. Move `field` to Tier B with its own oracle, or compare it only when both wires produced one.

20. **`/openapi.json` over-counts.** `app = FastAPI(...)` (`app.py:114`) has no `docs_url`/`openapi_url` override, and the app also carries `/health` and `/debug/*` routes. The drift test must filter to the `/api/v1` prefix and assert exactly 12 (verified: 12 routes at `api_v1.py:178,195,202,214,254,280,297,321,336,351,362,372`).

21. **`on_cancel_task` is an unauthenticated, unchecked state flip.** `adcp_a2a_server.py:1022-1040` sets any task to `CANCELED` with no auth check and no state validation. The plan's `MB-CANCEL-SHAPE`/`NOT-CANCELLABLE` oracles target AdCP media-buy cancel and will never reach this. Add it to E3 as a distinct isolation/authorization target.

22. **#1702 is missing from `known_issues.yaml` and will read as a false positive.** `on_get_task` (`:1019`) is `self.tasks.get(task_id)` — no identity filter, confirming the digest. The isolation oracle will class it `critical NOVEL` on the very first run. It is one of the cheapest, highest-confidence rediscoveries available. Add it as an 8th validation entry.

23. **`--mask gh-1583` masks too much.** Setting `NumericPolicy.TOLERANT` globally coerces whole floats to int on *every* wire, hiding genuine int/float divergence on MCP↔REST. Scope numeric tolerance to `wire == A2A`.

24. **`AUTH-DISPOSITION-PARITY`'s `SOFT_EMPTY` is defined circularly against #1449.** `SOFT_EMPTY` requires "advisory `errors[]` containing `AUTH_*`" — but #1449 *is* "A2A/REST drop advisory `errors[]`." Detecting the disposition on A2A/REST depends on the field the bug removes, biasing #1651. Also note `list_accounts` returns empty-for-unauthed **by design** (BR-RULE-055) on both MCP and A2A — that's an expected `SOFT_EMPTY`, not a finding. **Fix:** define disposition on `(outcome, collection_emptiness)` only; carry advisory-errors presence as an independent dimension consumed by `ADVISORY-ERRORS-PARITY`; add `list_accounts` as an expected-`SOFT_EMPTY` registry annotation.

---

## Verdict

**Not ready to implement as-is — but the architecture is sound and the required revision is bounded.**

The three load-bearing decisions (hand-rolled byte-faithful transports; wire/typed strata over one `dict`; one `Exchange` → one oracle library → two consumers) are correct and well-motivated by the evidence. The two-tier normalization contract with `norm_log` in every finding is the right answer to R4, and the verify-then-report gate is the right answer to R9. None of that needs to change.

What needs to change before code is written, in order:

1. **Rebuild the A2A rows of `core/matrix.py` from source, not from the digest summary** (#1, #3). The current registry is wrong on skill count, stub set, dispatchability, and omits the entire A2A protocol-method surface. This is the single highest-leverage correction because P2d, P4, P5 and the capability oracle all read from it.
2. **Repair the #1670 validation cases** (#2). P5 is *the* milestone and it is currently gated on a case that cannot fire.
3. **Rewrite the async/webhook section** (#6, #7, #8, #12). The local async story is materially different from what the plan describes: MCP has no create-time webhook, reporting webhooks are hourly with 24h dedupe, and webhook bodies bypass normalization. R11 survives, but the P6a gate and `rendezvous` priority do not.
4. **Demote dry-run from silent default to tagged dimension** (#5), and make per-wire header propagation a P2 gate.
5. **Restructure three module boundaries**: `canonical.py` → P1 (#13), `findings/` → `core/` (#14), P4 → P4a-serial + P4b-fanout (#15).
6. **Apply the false-positive fixes before P4 ships**: #4, #9, #10, #11, #16, #24. Every one of these is an oracle that fires on correct-by-design behavior; shipping them means P5's first run drowns in noise and the "NOVEL findings are the product" claim collapses.

Items 18-23 can be folded in during their owning phase.

Estimated cost of the revision: roughly one day of registry/oracle rework plus a half-day re-verifying the A2A and webhook surfaces against source. That is far cheaper than discovering #1 and #2 at the P5 gate, which is where the current plan would find them.