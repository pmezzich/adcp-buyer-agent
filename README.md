# adcp-buyer-agent

Two things that share one AdCP protocol core:

1. **Buyer agent** — an LLM-driven (Claude) buyer that runs a real campaign flow against an
   AdCP sales agent: brief → discover products → allocate budget → create media buy →
   sync creatives → monitor delivery. A deterministic protocol core does the wire work; the
   LLM does the judgment, always with a deterministic fallback.
2. **Fuzzer** — flushes bugs out of an AdCP sales agent (targeting
   [prebid/salesagent](https://github.com/prebid/salesagent)) via three engines: cross-transport
   differential (MCP vs A2A vs REST parity), schema/boundary property fuzzing, and stateful
   media-buy sequence fuzzing — checked against the AdCP spec's wire invariants.

Transports: MCP and A2A via the `adcp` SDK; REST is a hand-rolled `httpx` client (the SDK has no
REST protocol). All three normalize to one result type so the differential engine can compare them.

Status: scaffolding. Architecture and build plan are produced by the design phase.
