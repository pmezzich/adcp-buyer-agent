# adcp-buyer-agent

Two things that share one AdCP protocol core:

1. **Buyer agent** — an LLM-driven (Claude) buyer that runs a real campaign flow against an
   AdCP sales agent: brief → discover products → allocate budget → create media buy. A
   deterministic protocol core does the wire work; the LLM does the judgment, always with a
   deterministic fallback, and never the money commit.
2. **Fuzzer** — will flush bugs out of an AdCP sales agent (targeting
   [prebid/salesagent](https://github.com/prebid/salesagent)) via three engines: cross-transport
   differential (MCP vs A2A vs REST parity), schema/boundary property fuzzing, and stateful
   media-buy sequence fuzzing — checked against the AdCP spec's wire invariants.

**Transports are hand-rolled `httpx` clients for all three wires** — MCP (streamable-http
JSON-RPC), A2A (JSON-RPC `message/send` carrying a skill DataPart), and REST. The `adcp` SDK
is used for its pinned schemas and its canonicalization, not for the wire: its transport
layer is lossy in ways fatal to a fuzzer (see `docs/README.md`). All three normalize to one
`Exchange` so the differential engine can compare them.

## Status

The buyer half runs end to end against a live salesagent: it discovers products, plans and
guards a budget, registers an account, and creates a media buy, with the money commit going
through a durable (DBOS) executor. The fuzzer half is designed but not built — see
`docs/BUILD-PLAN-v2.md` for the phase plan and `docs/REVISION-GATE.md` for the three
blockers still open against it.

Not yet built, despite being in the plan: `sync_creatives` is not part of the campaign
pipeline (the buyer creates a media buy and stops at `pending_creatives`), and the delivery
monitor in `buyer/monitor.py` is reachable only through `scripts/run_monitor.py`, not from
`run_campaign`.

## Running it

Stand up a seller (from a `salesagent` checkout next to this one):

```bash
docker compose -f docker-compose.e2e.yml -f docker-compose.e2e.ports.yml \
               -f ../adcp-buyer-agent/compose/docker-compose.fuzz.yml \
               up -d --build postgres adcp-server proxy
docker compose -f docker-compose.e2e.yml exec -T adcp-server \
               python scripts/setup/init_database_ci.py
```

That serves on `http://localhost:8092`, tenant `ci-test`, token `ci-test-token`, seeded with
two products. The e2e compose (not the plain dev one) is what sets `ADCP_TESTING=true`, which
is what makes the mock adapter count as a configured ad server — without it every
`create_media_buy` fails the setup checklist.

The durable executor needs its own Postgres, kept separate from the seller's because the fuzz
arena resets the seller's database between runs:

```bash
docker run -d --name buyer-pg -e POSTGRES_PASSWORD=buyer -e POSTGRES_DB=adcp_buyer \
           -p 127.0.0.1:5544:5432 postgres:17-alpine
```

Then:

```bash
uv sync --extra dev
uv run python scripts/run_campaign.py
```

Two briefs, deterministic planner, no API key needed: one buys, and one with a CPM ceiling
below both products correctly refuses.

## Tests

```bash
uv run pytest tests/            # everything reachable
uv run pytest tests/ -q -k "not live and not durable"   # offline only
```

Most of the suite runs offline against stubbed transports. `test_live_seller.py` and
`test_durable_retry.py` drive a real seller and real DBOS respectively and **skip** when
those are not up — so a green run without them proves less than it looks. The claims that
are only decidable against a live seller live there on purpose: exactly-once, in particular,
cannot be tested through the durable executor, because DBOS answers from its own cache and a
broken seller would look identical to a working one.

Fixes on the money path are expected to be mutation-verified — reverting the production line
should redden a named test. Two checks written during the last audit survived their mutation
because they were dead as written; they were made load-bearing or removed rather than kept
as decoration.
