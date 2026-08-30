# Design record

Artifacts from the design phase, kept because they are the ground truth the implementation is
written against.

| File | What it is |
|---|---|
| `scout-digest.json` | Surface maps of the `adcp` SDK, salesagent's MCP/A2A/REST transports, its test harness + local-run story, 33 AdCP spec invariants (the fuzzer's oracles), and external reference implementations. Every entry carries source paths. |
| `BUILD-PLAN.md` | v1 build plan — synthesis of three independent architect designs (pragmatic / fuzzer-first / spec-fidelity). Superseded by v2, kept for provenance. |
| `DESIGN-CRITIQUE.md` | Adversarial review of v1. Found 24 concrete problems with `file:line` evidence, including four that would break on day one. Verdict: architecture sound, bounded revision required. |
| `BUILD-PLAN-v2.md` | The plan implementation follows, with every critique item applied. |
| `REVISION-GATE.md` | Item-by-item confirmation that v2 resolves the critique, plus the go/no-go verdict. Its verdict is NOT READY: three blockers (B1/B2/B3) are still open against the fuzzer half. |
| `FINDINGS.md` | What driving a live seller has turned up, and which of it was already filed upstream. |

## What is actually built

These files are the DESIGN record. They describe the finished system, in the present tense,
including an oracle library, `ExchangeSet`, and three fuzz engines that do not exist yet.
Read them as the plan, not as a description of `src/`.

Built today: the three transports, one normalized `Exchange`, the JCS idempotency key, the
DBOS executor, the planner + money guards, the delivery monitor, and the webhook sink.
Not built: `core/oracles/`, `core/normalize/`, `core/findings/`, `fuzz/`, `harness/`.

Two claims in these documents have since been overtaken by the code and are corrected here
rather than silently:

* The asymmetry map records REST's extra-field policy as IGNORE. salesagent's REST bodies now
  inherit `SalesAgentBaseModel`, which is `extra="forbid"` outside production, so REST
  REJECTS unknown fields in dev/CI. Measured on a live seller: REST rejects with
  `INVALID_REQUEST`, MCP rejects with `VALIDATION_ERROR`, and A2A silently ignores them —
  three policies, one request. `ExtraFieldPolicy` needs to be declared per `(wire, op)` as
  planned, but with those values, not the recorded ones.
* The idempotency section is right that the exclusion list is closed and includes the nested
  `push_notification_config.authentication.credentials`. The implementation missed the nested
  path; it now delegates to the SDK function the seller itself hashes with, rather than
  restating the rule.

## The three load-bearing decisions

1. **Hand-rolled, byte-faithful transports.** The `adcp` SDK is used for models, constants, and
   JCS canonicalization — never for the wire. Its transport layer is lossy in ways fatal to a
   fuzzer: `TaskResult` drops the `errors[]` mirror layer, the a2a-sdk client hides JSON-RPC
   codes behind typed exceptions, its protobuf round-trip re-coerces numerics, and
   `resolve_key()` auto-mints an idempotency key so a naive retry double-books.
2. **Two strata over one core.** A wire stratum (raw dicts, no coercion) for the fuzzer and a
   typed stratum (codec over adcp models) for the buyer, sharing one dispatch layer.
3. **One observation, one oracle library, two consumers.** Every call produces an `Exchange`;
   oracles take `Exchange`/`ExchangeSet`, never a live client, so they are unit-testable. The
   buyer runs the same oracles in guard mode.
