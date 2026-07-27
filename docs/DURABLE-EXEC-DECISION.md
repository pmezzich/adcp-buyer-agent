# Decision Brief: Durable-Execution Layer for the AdCP Buyer Agent

**Scope:** the `buyer/executor.py` + `buyer/monitor.py` + `core/tasks/` durable boundary only. Transports, oracles, and the fuzzer are explicitly out of scope and must stay untouched.
**Context:** solo developer, Windows/PowerShell primary, Postgres already on the box (`salesagent` runs `postgres:17` on `:5435`), executor is already deterministic-by-design (LLM is kept off the wire), waits range seconds → hours (approval, poll) with a multi-day monitor tail.

---

## 1. TL;DR recommendation

**Choose DBOS Transact.** For this context the decisive factor is operational baseline: DBOS is an embedded **library** (`uv add dbos`, `DBOS.launch()` in-process) that checkpoints to a Postgres you already run, so the durable layer adds **zero always-on infrastructure** — no Service cluster, no separate worker fleet to keep alive, monitor, and version. Temporal is the stronger *platform*, but it makes durability contingent on two always-on processes (Server + Worker) and forces a determinism refactor that splits `executor.py` into a workflow shell plus activity leaves — real work whose payoff is org-scale resilience a solo dev on a single box does not need yet. The plan's own decoupling invariant (durable choice must not touch transports/oracles/fuzzer) is satisfiable by either, but DBOS's `recover_pending_workflows` natively deletes the exact hand-rolled machinery the plan is carrying (the "re-arm every non-terminal ledger row on boot" loop), and pydantic-ai ships a first-class `DBOSDurability` integration for the `claude-opus-4-8` brain. The one real cost — Python DBOS is Postgres-only, so the SQLite ledger/vault must move to Postgres — is nearly free here because Postgres is already present (provision a *separate* buyer DB; do not reuse the seller's, since `arena.reset()`/`stack down -v` would wipe the buyer's durable state).

---

## 2. Side-by-side

| Dimension | **DBOS Transact (recommended)** | Temporal |
|---|---|---|
| **Python maturity (2026)** | `dbos` 2.28.0 (2026-07-21), weekly/bi-weekly cadence, well past 0.x. Pin exact version (mirrors the plan's `adcp==5.7.0` discipline). | `temporalio` 1.30.0 (2026-07-02), mature stable 1.x, reference-quality SDK, Python 3.10–3.14. The more battle-tested of the two. |
| **Async webhook / poll waits** | Sink resolves `operation_id→workflow_id` and `DBOS.send(wfid, payload, topic)`; workflow blocks on `DBOS.recv(topic, timeout)`. `recv(timeout=poll_interval)` **collapses the plan's dual-armed rendezvous into one idiom**: returns early on webhook, else times out and polls. Message persisted to a Postgres notifications table → survives a crash while parked. | Webhook → FastAPI sink calls `client.signal`; workflow sits in `workflow.wait_condition(..., timeout)`. Poll → a polling Activity loop with a durable Timer. Race poll-loop vs signal via `wait_condition` + cancellable task. Both crash-proof; two patterns instead of one. |
| **Human-approval step** | Park on `DBOS.recv` with long/no timeout; `AdminDriver.approve_media_buy` fires the seller webhook → sink `send` wakes it. Survives arbitrarily long human delay, no polling. | Flagship demo. `wait_condition(lambda: self.approved, timeout=timedelta(days=5))` — zero compute while parked, instant resume on signal, auto-timeout. Slightly more idiomatic here. |
| **Exactly-once vs the JCS idempotency_key** | Two layers: (1) `SetWorkflowID(JCS-key)` → exactly-once *workflow*; (2) the deterministic JCS key is stamped on the wire and **stable across step re-runs by construction**, so a mid-step crash re-POST carries the same key → seller returns `replayed=true`. Directly retires plan risk **R6** (SDK auto-mints fresh UUID4 on retry). | Same two layers, same discipline: Workflow-ID = JCS key with `RejectDuplicate`; activities are **at-least-once** and WILL double-fire, so the wire key stays load-bearing. Equivalent guarantee; identical caveat. Neither framework gives exactly-once *side effects* for free. |
| **Days-long delivery monitoring** | One long-lived durable workflow per buy looping `DBOS.sleep(1 day)` + delivery step, **or** `@DBOS.scheduled(cron)` daily sweep over active buys. Survives restarts with no external scheduler and **no history-cap discipline required**. | Long-running monitor Workflow with durable Timers — but **must** call Continue-As-New on a bound (event history degrades past ~10K events, hard cap 50K/50MB). Correct-but-deliberate code the implementer must design in from day one. A real trap DBOS doesn't have. |
| **What must RUN (ops)** | Nothing extra. Durable runtime + recovery loop run **in-process** at buyer startup. | A **Temporal Service** (frontend/history/matching/internal-worker roles) + its DB + **≥1 always-on Worker**. Nothing durable advances while the Worker is down. |
| **Local dev (Windows + Postgres)** | Runs on Windows; point `DBOS(config=...)` at local/Docker Postgres — the one you already have. Single datastore. | `temporal server start-dev` (single `temporal.exe`, no Docker) is genuinely easy — but uses its **own** datastore, so dev runs **two** stores (Temporal SQLite + salesagent postgres:17) unless you stand up a Postgres-backed Service. |
| **Deploy footprint** | Buyer process + one Postgres schema (`workflow_status`, `operation_outputs`, `notifications`). | Temporal Service + persistence DB (+ Elasticsearch for advanced visibility) + worker process(es). |
| **Lock-in / exit cost** | Library is open-source, self-hostable; Conductor console (RBAC/metrics) is optional paid cloud, **not required** → no lock-in for core durable exec. Exit = strip decorators. | Open-source Service, self-hostable; no forced cloud. Exit cost higher — determinism-partitioned code (workflow vs activity split) is a structural refactor to unwind. |

---

## 3. The fold-in (DBOS delta to BUILD-PLAN-v2)

**Durable boundary:** sits exactly at the **executor ↔ transport seam** (plan §3.10's `vault → encode → call → rendezvous → decode → guard`). Every `@DBOS` decorator is confined to `buyer/executor.py`, `buyer/monitor.py`, and `core/tasks/`. Keep the DBOS import **lazy/optional** in `core/tasks/` and re-run `tests/unit/test_import_boundaries.py` to prove `fuzz/` (which reaches `core/` via sink/ledger) stays DBOS-free.

**Becomes a WORKFLOW:**
- `buyer/executor.py` execute pipeline → `@DBOS.workflow`, invoked under `SetWorkflowID(JCS-key)`.
- `buyer/monitor.py` → `@DBOS.scheduled(cron)` daily sweep **or** one long-lived per-buy workflow looping `DBOS.sleep`. Its pydantic-ai `PacingVerdict` call becomes durable for free via `DBOSDurability` (the `agent.run()` runs as a step inside the workflow). `monitor_mode="cross_wire_readback"` for REST is unchanged — it's just which step the workflow calls.

**Becomes a STEP / primitive in `core/tasks/`** (`C:\Users\pmezz\projects\adcp-buyer-agent\src\adcp_buyer\core\tasks\`):
- `poller.py` `get_task`/`GetTask` → `@DBOS.step`.
- `rendezvous.py::await_terminal` → rewritten as `recv(timeout) ⊕ sleep+poll` loop, **keeping the plan's §5.6 per-wire priority** (MCP poll-primary, A2A webhook-primary, REST cross-wire readback) and the `RunMode.DRY_RUN → RuntimeError` guard. The hand-rolled `asyncio.Event` race is replaced by DBOS primitives.
- `sink.py` → stays a thin FastAPI adapter; swap `Event.set` for `DBOSClient.send` (a DBOS **Client** lets the separate sink process publish into the workflow's Postgres without being the full app).
- `ledger.py` → the **"re-arm every non-terminal row on boot" loop is DELETED** (plan §5.6 line: *"On boot every non-terminal row is re-armed"*) — DBOS `recover_pending_workflows` does this natively at launch. `ledger.py` shrinks to a pure `operation_id ↔ task_handle ↔ wire` correlation index; **`operation_id` becomes the DBOS workflow ID**.
- `lifecycle.py` → **untouched**. The 9-member `AdcpTaskStatus` spec enum is orthogonal to DBOS workflow status.
- `core/idempotency.py` → keep the JCS key derivation (deterministic → ideal `SetWorkflowID`); one value serves as both workflow ID and wire-stamped key. Vault's wire-dedup role stays; storage moves **SQLite → Postgres**.

**Stays UNCHANGED (the plan's hard decoupling invariant):**
- `core/transport/{mcp,a2a,rest}.py` — plain, undecorated; invoked *from inside* a step, never decorated themselves.
- `core/oracles/` (all 36 invariants) and `core/findings/` — the guard-mode oracle library is unaffected.
- `fuzz/` entirely — E1/E2/E3 engines, corpus, shrink stay DBOS-free.

**Known edge (plan §5.4):** `WEBHOOK_MCP` fires carry a *fresh* UUID4 idempotency_key per fire, so `DBOS.send`'s `idempotency_key` can't dedupe them — rely on workflow-level idempotency of the downstream effect plus ledger correlation, exactly as the plan already handles the WEBHOOK-UNCORRELATABLE path.

**Tiny sketch — `create_media_buy` → await-completion:**

```python
from dbos import DBOS, SetWorkflowID

@DBOS.step(retries_allowed=True, max_attempts=4)
async def _call_create_media_buy(sent: dict, wire: Wire, jcs_key: str) -> Exchange:
    # transport invoked FROM inside the step; key stamped on the wire (stable across re-runs)
    return await client.call(spec("create_media_buy"), sent, wire, idempotency_key=jcs_key)

@DBOS.workflow()
async def create_media_buy_wf(plan: CampaignPlan, wire: Wire, jcs_key: str) -> Exchange:
    if plan.run_mode is RunMode.DRY_RUN:          # §5 disabled under dry-run
        return await _call_create_media_buy(encode(plan), wire, jcs_key)  # no rendezvous
    sent = encode(plan)                                   # deterministic
    ex   = await _call_create_media_buy(sent, wire, jcs_key)
    ledger.record(op_id=DBOS.workflow_id, wire=wire, handle=ex.task_handle)
    # rendezvous: race webhook-arrival vs poll cadence, per-wire priority (§5.6)
    while ex.status not in TERMINAL:
        msg = await DBOS.recv(topic="webhook", timeout=poll_interval)   # durable park
        ex  = msg or await poll_get_task(ex.task_handle, wire)          # @DBOS.step
    return decode(ex)                                     # guard oracles run in LOG mode

# caller — exactly-once creation keyed on the JCS idempotency_key
with SetWorkflowID(jcs_key):
    handle = await DBOS.start_workflow(create_media_buy_wf, plan, wire, jcs_key)
```

---

## 4. Honest case for Temporal

Pick Temporal instead if any of these hold:
- **The org already runs a Temporal cluster / standardizes on it.** The entire ops-cost argument for DBOS evaporates — you'd be adopting shared infrastructure, not standing up your own, and you gain the mature UI (`:8233`), visibility/search, and a team that already knows the failure modes.
- **The system is expected to scale past a solo buyer into a fleet of long-lived agents.** Temporal's Child-Workflows (one monitor child per media-buy), signal-with-start dedup, heartbeating, and async activity completion are a richer, more proven toolkit at scale, and Anthropic-agent / human-in-the-loop patterns are first-class documented use cases in 2026.
- **You want durability decoupled from your app's uptime.** Temporal's Service persists history independently of your Worker; a Worker crash loses zero progress and simply resumes. DBOS durability lives in-process — fine for one box, less isolated than a dedicated Service.
- **The monitor's history-cap discipline (Continue-As-New) is acceptable overhead** and you value Temporal's deterministic replay as a stronger correctness model than step-checkpointing.

The determinism refactor Temporal forces (no `httpx`/LLM/`random`/wall-clock in workflow code) is *less* painful here than usual, because the plan **already** keeps the LLM off the wire and the executor deterministic — so if the org context above applies, Temporal's tax is smaller than it looks.

---

## 5. What to confirm with Chris (any one flips the call)

1. **Does the org already run a Temporal cluster, or standardize on Temporal for durable execution?** If yes → use it; the DBOS ops-cost advantage disappears and you inherit tooling + expertise. *(This is the single most likely flip.)*
2. **Is this buyer agent staying a solo/single-box tool, or is it the seed of a multi-agent fleet with an SLA?** Fleet + SLA tilts toward Temporal's platform primitives and independent-of-uptime durability.
3. **Can we provision a separate Postgres database for the buyer's durable state** (same `postgres:17` server is fine, distinct DB) so `arena.reset()` / `stack down -v` never wipes it? If a dedicated buyer DB is off the table for some reason, re-weigh — DBOS's Postgres-only requirement is its one hard cost, and it must not co-locate with the system-under-test's DB.