# Revision Gate — BUILD-PLAN v2 vs. DESIGN-CRITIQUE v1

All evidence below was read directly from `C:\Users\pmezz\projects\salesagent` (source + `.venv\Lib\site-packages`) during this pass.

---

## Part 1 — Resolution of the 24 critique items

| # | Tier | Status | Evidence / what v2 says |
|---|---|---|---|
| **1** | 0 | **RESOLVED** | `WireBinding.advertised` / `.dispatchable` orthogonal + `Availability.UNDISPATCHABLE` + 4-part drift test + derived assertions 5/6. **Verified:** `skill_handlers` = exactly 18 (`src/a2a_server/adcp_a2a_server.py:1402-1428`); `create_agent_card()` = exactly 16 `AgentSkill`s (`:2131-2231`); `dispatchable − advertised == {create_creative, assign_creative}`; `advertised − dispatchable == ∅`; `_handle_get_creatives_skill` at `:1698` is absent from the map and has zero other `src/` refs. |
| **2** | 0 | **PARTIAL** | v2 rebuilds the case set and adds `TASK-GET-NULLRESULT`, but the rebuilt set is wrong on the *vocabulary* axis and wrongly excludes `tasks/get`. See **B1** below. |
| **3** | 0 | **RESOLVED** | `a2a_methods.py` as first-class arity-1 OpSpecs; 5-channel table; `PNC-DEADCHANNEL`, `PNC-SCOPE`. **Verified:** `_send_protocol_webhook` reads only `self._task_push_configs` (`:377`, dict declared `:186`, written `:582`); `on_create_task_push_notification_config` writes the DB via `PushNotificationConfigUoW.upsert` (`:1155-1166`) and nothing reads it back; `ListPushConfigs` lists by principal (`:1214-1218`) and stamps `params.task_id` onto every row (`:1227`); `CreatePushConfig` echoes `task_id or "*"` (`:1179`) with no validation. |
| **4** | 0 | **RESOLVED** | `ExtraFieldPolicy` declared per `(wire, op)`; REST = `IGNORE` unconditional; FP-6; `div-rest-extra-ignore`. **Verified:** all 11 `*Body` classes are bare `pydantic.BaseModel` with `# FIXME(#1442)` at `src/routes/api_v1.py:64,71,82,93,105,115,127,133,150,156,164`. |
| **5** | 1 | **RESOLVED** | `RunMode` is a first-class `Exchange` field, in `signature()` and `Finding.target`; WET default; P2d propagation gate. **Verified:** A2A *does* propagate — `AdCPTestContext.from_headers(headers)` at `adcp_a2a_server.py:266-267` off `context.state[AUTH_CONTEXT_STATE_KEY].headers`. Dry-run early return + `dry_run_<hex>` id at `media_buy_create.py:3493-3510`; workflow-step and PNC registration both guarded at `:1993`/`:2021`. (One residual naming collision — see **N6**.) |
| **6** | 1 | **RESOLVED** (over-delivers) | Compose overlay `DELIVERY_WEBHOOK_INTERVAL: "5"`; fresh buy per case; **new R19**. **Verified:** `delivery_webhook_scheduler.py:33` (`or "3600"`, module-import read); 24h dedupe predicate `:188-196`; status filter `["active","approved"]` `:95`; `delivery_response.notification_type = NotificationType.scheduled` set **unconditionally** at `:268`, so `force=True` really does poison the scheduled path for 24h. |
| **7** | 1 | **RESOLVED by refutation** | v2 correctly overturns the critique. **Verified:** `_create_media_buy_impl` calls `ctx_manager.update_workflow_step(step.step_id, status="completed")` at `media_buy_create.py:4061` → `_send_push_notifications` → `create_mcp_webhook_payload` (`context_manager.py:866-873`), no human in the loop. Preconditions confirmed: ≥1 active `PushNotificationConfig` row (`context_manager.py:791-796`) **and** the URL is read from `step.request_data["push_notification_config"]` (`:809-812`), both created by the same request (`media_buy_create.py:2022-2070`). `AdminDriver` covers the genuinely human-gated paths. See **N7** for a consequence v2 didn't follow through on. |
| **8** | 1 | **RESOLVED** | Literal `localhost`; `WH-SILENT-DROP`; doctor advertises the literal. **Verified:** `protocol_webhook_service.py:106-122` — `parsed.hostname.lower() == "localhost"` exact; return value discarded at every call site; `context_manager.py:899-903` calls `t.result()` on a bool-returning coroutine and prints `✅ Webhook sent successfully` even on `False`. |
| **9** | 1 | **RESOLVED** | `PROJECT_CODES`, one aggregated run-level `info` row, `op: "*"`. **Verified:** `'AUTH_TOKEN_INVALID' in STANDARD_ERROR_CODES → False`; `exceptions.py:56-57` documents the deliberate passthrough (absent from `ERROR_CODE_MAPPING`). |
| **10** | 1 | **RESOLVED** | FP-1: `ALLOWED_STATUSES` sets, INV-08 sole owner, AST guard `test_no_status_hardcoding.py`. **Verified:** `tool_error_logging.py:374-425` — highest-status-wins on shared wire codes, `INVALID_REQUEST` anchored to 400 as a `_GENERIC_CATCHALL`; typed errors bypass the table entirely (`handle_tool_error` returns `e.status_code` for `AdCPToolError`). v2's three ambiguity sets are consistent with the class walk (`AdCPPolicyViolationError` 403 / `AdCPMediaBuyRejectedError` 422 both → `POLICY_VIOLATION`, `exceptions.py:451-461, 870-875`). |
| **11** | 1 | **RESOLVED** | FP-3: structural JCS equality only. **Verified:** `exceptions.py:943-946` — `"adcp_error": dict(payload["errors"][0])`, a shallow copy of the same dict, so structural equality is exactly right and identity is meaningless. |
| **12** | 1 | **RESOLVED** | `WEBHOOK_MCP` / `WEBHOOK_A2A` wires, `decamelize` rule (P3a), `ledger.resolve_op`, dedicated `webhook_o.py`. **Verified:** the sink's discriminator is sound — `McpWebhookPayload.model_fields` = `{context_id, idempotency_key, message, notification_id, operation_id, protocol, result, status, task_id, task_type, timestamp, token}`, i.e. **no `id` and no `taskId`**, so `"taskId" in payload or "id" in payload` cannot misfire. |
| **13** | 2 | **RESOLVED** | `core/spec/canonical.py` (JCS + JSON-Pointer differ + `DiffEntry`) moved to P1; P3a/b/c genuinely parallel. |
| **14** | 2 | **RESOLVED** | `core/findings/{model,signature,store}.py`; `tests/unit/test_import_boundaries.py`; `buyer/guard.py` writes to `core/findings/store.py`. |
| **15** | 2 | **RESOLVED** | P4a serial (`registry.py`, `Violation`, `auth_disposition.py`, `taint.py`, per-op `volatile_paths`/`unordered`, `divergences.yaml` seed) → P4b fan-out. `DiffEntry` relocated to P1 rather than P4a, which is consistent. |
| **16** | 2 | **RESOLVED** | `mutating` / `idempotent` split; `carries_idempotency_key` gates `IDEM-SCOPE` (FP-4); inability = one deduped `FIELD-REACHABILITY` per `(op, wire)`. **Verified:** `IDEMPOTENT_TASKS` contains `create_media_buy, update_media_buy, sync_accounts, sync_creatives`; only `CreateMediaBuyBody` carries `idempotency_key` (`api_v1.py:71-80`). |
| **17** | 2 | **RESOLVED** | `CoverageMarker(op, "single_wire" \| "single_pair")`; report must print `parity untested for N ops`. |
| **18** | 3 | **RESOLVED** | `monitorable_on: frozenset[Wire]` added; `monitor_mode="cross_wire_readback"`. **Verified:** `api_v1.py:266-276` forwards exactly 7 buyer params (`brand, packages, start_time, end_time, po_number, account, idempotency_key`) — no `reporting_webhook`, no `push_notification_config`, no `context`, no `ext`. |
| **19** | 3 | **RESOLVED** (with a corrected rationale) | `adcp_error.field` moved to Tier B with `FIELD-POINTER-SHAPE`, compared only when both wires produced one. v2 correctly refutes the critique's *reason*: `_base.py:1529` `packages: list[PackageRequest] \| None = None` drops the library's `MinLen(1)`, so `[]` and `None` validate identically. The divergence is real for other reasons (dot-index vs bracket-index vs `None`). |
| **20** | 3 | **RESOLVED** | Enumerate `api_v1.router.routes` from the module. **Verified:** exactly 12 at `api_v1.py:178,195,202,214,254,280,297,321,336,351,362,372`. Over-count risk confirmed: `app.mount("/", admin_wsgi)` at `src/app.py:85`. |
| **21** | 3 | **RESOLVED** | New INV-36 `TASK-CANCEL-STATE`; E3 target (a); companion assertion in known-issue 8. **Verified:** `adcp_a2a_server.py:1022-1041` — `task.status.CopyFrom(TaskStatus(state=TASK_STATE_CANCELED))` with no auth check and no state check; `TaskNotCancelableError` (`-32002`, `a2a/utils/errors.py:137`) unused in `src/`. |
| **22** | 3 | **RESOLVED** | #1702 is validation entry 8, severity `critical`, TENANT arena, INV-34. **Verified:** `self.tasks: dict[str, Task] = {}` (`:185`), `on_get_task` = `self.tasks.get(task_id)` (`:1019`), no identity filter, no owner field on the proto. |
| **23** | 3 | **RESOLVED** | `NormPipeline(numeric=TOLERANT, numeric_tolerant_wires={Wire.A2A})`; mode **and** wire-set recorded in `norm_log`. |
| **24** | 3 | **RESOLVED** | FP-5: `AuthDisposition` on `(outcome, collection_emptiness)` only; advisory-`errors[]` carried as an independent dimension consumed by `ADVISORY-ERRORS-PARITY`; `list_accounts.expected_disposition[NONE] = SOFT_EMPTY`. |

**Tally: 23 RESOLVED, 1 PARTIAL, 0 UNRESOLVED.**

---

## Part 2 — New problems introduced by v2

### B1 (BLOCKER) — The #1670 vocabulary axis is inverted; 8 of v2's 12 declared cases cannot fire

v2 records `a2a_vocabulary` (good) but assigns viability to the **wrong** vocabulary throughout §3.2.2 and §7.1. The flattening lives in one place:

- `a2a/compat/v0_3/jsonrpc_adapter.py:141-144` — `handle_request`'s outer `except Exception as e:` returns `_generate_error_response(request_id, CoreInternalError(message=str(e)))`. There is **no** `except A2AError` anywhere in that file, so *every* typed `A2AError` raised inside `_process_non_streaming_request` becomes **-32603**.
- `a2a/server/routes/jsonrpc_dispatcher.py:338-339` — the v1.0 dispatcher has `except A2AError as e: return self._generate_error_response(request_id, e)`, and `:181-182` preserves it. **v1.0 emits the correct code.**
- Dispatch order confirmed: v0.3 is tried first (`jsonrpc_dispatcher.py:274-278`), v1.0 second (`:285`).

Consequences, each a dead validation case as written:

| v2 claim | Reality |
|---|---|
| `ListTasks` (v1.0 name) ⟹ #1670 viable | **Never.** `ListTasks` has no v0.3 alias (v0.3 `METHOD_TO_MODEL`, `jsonrpc_adapter.py:53-64`, has no `tasks/list`), so it can only travel the *preserving* v1.0 path → clean `-32004`. Oracle passes. |
| `SubscribeToTask` viable "via v1.0 name only" | **Exactly inverted.** v1.0 gives the clean `-32004`; `tasks/resubscribe` degrades to SSE (which v2 itself excludes). Net: **no viable case at all.** |
| `GetExtendedAgentCard` viable "both vocabularies" | Only `agent/getAuthenticatedExtendedCard` (v0.3) flattens. |
| `CreatePushConfig` / `GetPushConfig` / `ListPushConfigs` / `DeletePushConfig` — 9 cases | Viable only via the `tasks/pushNotificationConfig/*` v0.3 aliases. The v1.0 protobuf names preserve. |
| §3.3: `_vocabulary` "defaulting to `v1.0` for deterministic error shapes" | v1.0 **is** the non-buggy path. This default makes the P5 milestone silently green. |

Genuinely viable #1670 surface (all v0.3): `message/send` (unknown skill → `-32601`; 5 stub skills → `-32004`; no-auth non-discovery skill → `-32600`; `create_creative`/`assign_creative` missing params → `AdCPValidationError` path); `tasks/pushNotificationConfig/{set,get,list,delete}`; `agent/getAuthenticatedExtendedCard`; and `tasks/get` (see B2). `tasks/cancel` needs a probe (returns `None` → `CancelTaskSuccessResponse(result=None)` may or may not validate-fail).

**Fix:** make `_vocabulary` a required per-case field with **no default**; add `expected_code_by_vocabulary: dict[Literal["v1.0","v0.3"], int]` to each `a2a_methods` row; `PROTOCOL-ERROR-FIDELITY.applies()` returns `False` for `v1.0` on methods with a v0.3 alias, and asserts the *pair* (v1.0 preserves ∧ v0.3 flattens) as the real finding. Import `JSON_RPC_ERROR_CODE_MAP` from `a2a/utils/errors.py:132-146` (it exists; v2's `spec/jsonrpc.py` plan is otherwise correct).

### B2 (BLOCKER) — Known-issue entry #6 cannot fire; v2 replaced one dead gate row with another

v2 removed v1's refuted `buying_mode` row and replaced it with "A2A `list_authorized_properties` drops buyer filters", asserting `property_tags` "provably narrows the result on REST and MCP." It does not narrow anything anywhere:

- `src/core/tools/properties.py` — the **only** `req.*` reads in `_list_authorized_properties_impl` are `req.context` (`:55`, `:93-94`, `:154-155`). `req.property_tags` and `req.publisher_domains` are never read.
- The MCP wrapper (`:195-221`) faithfully forwards both into `ListAuthorizedPropertiesRequest`; the REST body carries both (`api_v1.py:150-153`); the model declares both (`_base.py:2292-2293`). All three wires ignore them at the impl.

So this is a **uniform hole**, structurally identical to `buying_mode` — one aggregated `FIELD-REACHABILITY` row at `medium`, not a high-severity A2A parity finding. This is the exact mistake R1′ warns against, repeated. The P5 gate currently has **7** live entries, not 8, and the "expected free win" in §7.1 loses one of its three predicted hits.

**Fix:** demote #6 to a `divergences.yaml` uniform-hole entry alongside `div-buying-mode-unreachable`. Replace the gate row with a divergence that is *verified to change behavior on at least one wire* before P5 — candidates from v2's own §7.2 that I confirmed: A2A `create_media_buy` validating `ext` then dropping it (`:1574` accepts, `:1583-1595` omits), or A2A `get_media_buys` calling bare `GetMediaBuysRequest.model_validate` outside `adcp_validation_boundary()` (`:1922`, vs the wrapped sibling at `:1573-1574`).

### B3 (BLOCKER) — `WH-DOUBLE-FIRE` "exactly 2" is a false-positive oracle that collides with v2's own NOVEL target

`context_manager.py:806` iterates **every** active `PushNotificationConfig` row for `(tenant, principal)`, and `media_buy_create.py:2039` mints a fresh `config_id` (`push_notification_config.get("id") or f"pnc_{uuid4().hex[:16]}"`) whenever the request omits `id`. Fire count therefore scales with the number of prior `create_media_buy` calls in the arena. v2 simultaneously (a) lists "N duplicate webhooks when N active rows exist" as a NOVEL target in §7.2 and (b) hard-codes `WH-DOUBLE-FIRE` to "exactly 2." In `NAMESPACE` arena mode (the default) the second create in a run makes the oracle fire on correct-by-design fan-out.

**Fix:** `expected_fires = 1 (protocol, A2A only) + count(active PNC rows for principal)`, with the count tracked in the arena ledger; assert "exactly 2" only when the ledger says exactly one active row, or when the request pins `push_notification_config.id`.

### N4 — INV-35 `TASK-GET-NULLRESULT` is vocabulary-blind and its premise is half wrong

v2: "salesagent returns `None` and the SDK manufactures -32001." True on **v1.0 only** (`jsonrpc_dispatcher.py:426-428`). On v0.3, `a2a/compat/v0_3/request_handler.py:114-124` — `RequestHandler03.on_get_task` itself does `if v10_task: return ...; raise TaskNotFoundError`, and that raise propagates to the flattening handler → **-32603**. So `tasks/get` on an unknown id *is* a live #1670 case (v1's original case file was right for the wrong reason; the critique's item-2 argument missed the SDK-side raise, and v2 inherited the mistake). Split INV-35 per vocabulary and re-admit `tasks/get` to the #1670 corpus.

### N5 — Unmeasured confounder: `@validate_version(constants.PROTOCOL_VERSION_0_3)`

Both v0.3 processing methods are decorated (`jsonrpc_adapter.py:146` and `:234`). A `VersionNotSupportedError` (`-32009`) raised by that decorator would also be flattened to `-32603` and would be indistinguishable from a #1670 hit on **every** v0.3 call. P2c must characterize what the decorator demands (headers? `protocolVersion` in the body?) and pin a known-good v0.3 request before any #1670 case is trusted. This is not mentioned anywhere in v2.

### N6 — `RunMode` collides with a real payload field

§3.1: *"there is no `dry_run: bool` anywhere else."* `SyncAccountsBody` has one: `dry_run: bool = False` (`api_v1.py:167`), a domain field distinct from the `x-dry-run` header. The binder must not conflate them, `signature()` must not merge them, and there is a free fuzz case in header-vs-body disagreement (`RunMode.WET` + `{"dry_run": true}`). One-line registry note; flag it before P1 freezes `LogicalRequest`.

### N7 — v2 keeps critique #7's remedy after refuting its premise, and mis-tiers the P6a-ii gate

§5.2 proves a single MCP `create_media_buy` carrying `push_notification_config` is self-sufficient (no admin action). §5.6 nonetheless keeps "MCP primary = polling, webhook opportunistic," and §7 marks the whole of P6a-ii as **soft / `xfail-environmental`**. Combined, the single most important async path in the system can never be exercised in CI. Recommend: once P6a-i (in-container synthetic POST) passes, the **MCP arm** of P6a-ii is a hard gate; only the A2A double-fire arm and the delivery-report arm stay soft.

### N8 — `ops.MCP_TOOL_NAMES` is an empty P1 artifact for a statically-knowable fact

`src/core/main.py:317-332` registers **exactly 16** tools: `list_accounts, sync_accounts, get_adcp_capabilities, get_products, list_creative_formats, sync_creatives, list_creatives, list_authorized_properties, create_media_buy, update_media_buy, get_media_buy_delivery, get_media_buys, update_performance_index, list_tasks, get_task, complete_task`. v1's "16" was correct; v1's *derivation* was inconsistent. v2's "do not hardcode 16 / write it at P2b" creates a P1 module with a hole that P1 and P2a consumers must tolerate. Cheaper: seed the tuple at P1 from those 16 verified names and make P2b's gate **assert** rather than **write**. (Keep the gate — the drift risk is real; just don't leave a mutable hole across a phase boundary.)

### N9 — minor, verify at P0

`AdminDriver.login()` targets `/test/auth` (`src/admin/blueprints/auth.py:770`), gated on `ADCP_AUTH_TEST_MODE` (`:801`) **and** `tenant.auth_setup_mode` (`:807-808`). v2's `stack.up()` invariant list covers both. Note however that the sibling login paths at `:225, :244, :278` additionally suppress `auth_setup_mode` when global OAuth is configured; confirm `/test/auth` has no equivalent guard against the actual e2e image env before declaring the P0 gate green.

---

## Verdict

**NOT READY TO IMPLEMENT — three bounded blockers, all in the P5 milestone's line of fire.**

The architecture is unchanged and remains correct; v2's corrections to the critique are almost uniformly right, and in two places (items 7 and 19) v2 correctly overturns the critique against source. The remaining defects are all in the same category as the ones the critique caught: **gate rows built on unverified behavioral premises.**

Blockers, in order:

1. **B1 — rebuild the #1670 case matrix on the vocabulary axis.** The flattening is a v0.3-adapter property (`jsonrpc_adapter.py:141-144`), not a method property; the v1.0 dispatcher preserves (`jsonrpc_dispatcher.py:338-339`). Remove `ListTasks` and `SubscribeToTask` from the viable set, re-admit `tasks/get` (**N4**), and make `_vocabulary` a required, defaultless per-case field. ~2 hours.
2. **B2 — replace known-issue entry #6.** `property_tags`/`publisher_domains` are ignored by the implementation on all three wires (`src/core/tools/properties.py`, only `req.context` is ever read). Demote to a uniform-hole divergence and substitute a wire-differential that has been *empirically confirmed to change behavior*. ~2 hours plus one live probe.
3. **B3 — make `WH-DOUBLE-FIRE` count-parameterized**, or it fires on the second create in any `NAMESPACE` arena. ~30 minutes.

Fold in **N5** (v0.3 version-validator confounder) as a P2c characterization task before any #1670 case is trusted, **N6** and **N8** as P1 registry edits, and **N7** as a P6a gate re-tiering. Nothing else needs to change.

Total revision cost: well under a day. Re-gate after B1/B2/B3 land and this is ready to build.