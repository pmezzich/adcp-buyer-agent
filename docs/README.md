# Design record

Artifacts from the design phase, kept because they are the ground truth the implementation is
written against.

| File | What it is |
|---|---|
| `scout-digest.json` | Surface maps of the `adcp` SDK, salesagent's MCP/A2A/REST transports, its test harness + local-run story, 33 AdCP spec invariants (the fuzzer's oracles), and external reference implementations. Every entry carries source paths. |
| `BUILD-PLAN.md` | v1 build plan — synthesis of three independent architect designs (pragmatic / fuzzer-first / spec-fidelity). Superseded by v2, kept for provenance. |
| `DESIGN-CRITIQUE.md` | Adversarial review of v1. Found 24 concrete problems with `file:line` evidence, including four that would break on day one. Verdict: architecture sound, bounded revision required. |
| `BUILD-PLAN-v2.md` | The plan implementation follows, with every critique item applied. |
| `REVISION-GATE.md` | Item-by-item confirmation that v2 resolves the critique, plus the go/no-go verdict. |

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
