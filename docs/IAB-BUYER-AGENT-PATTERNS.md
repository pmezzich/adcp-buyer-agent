# What to lift from `IABTechLab/buyer-agent` for the v2 buyer brain

Architecture inspiration only — IAB's repo is Apache-2.0 and speaks OpenDirect, not AdCP.
No code is copied; we adapt patterns and write original Python over the DBOS executor.

## Headline

IAB's buyer is built on **CrewAI** (`portfolio_manager.py:8`), not the Claude SDK or
pydantic-ai — so their *delegation mechanism* doesn't transfer. But their single most
valuable, portable idea is: **the LLM only ever emits a typed, clamped recommendation, and
a deterministic layer runs the spend ceiling and does the money commit.** That validates
our `executor.py` design ("LLM never touches the wire"). The concrete work is (a) formalize
a 2-tier Opus-planner → cheap-ranker in pydantic-ai, and (b) add an **upper** spend-ceiling
gate + plan-field clamp as pre-dispatch checks right before `create_media_buy_workflow` fires.

## 1. Delegation model (what it is, why it doesn't port)

- Three tiers via CrewAI `Process.hierarchical`: L1 **Portfolio Manager (Opus)** owns brief →
  typed budget split (`output_pydantic=BudgetAllocationOutput`, `portfolio_crew.py:96-118`);
  L2 **Channel Specialists (Sonnet)**, each the manager of its own channel crew
  (`channel_crews.py:294-325`); L3 **Research/Execution** leaves (`allow_delegation=False`)
  that hold the real tools.
- The real conductor is a **deterministic CrewAI `Flow`** (`deal_booking_flow.py`): it kicks
  off the crews to get *typed recommendations*, then **parses, clamps, spend-checks, and
  books in plain Python** — the money commit is deterministic, not an LLM tool call. Their
  architecture already obeys our "LLM never touches the wire" rule.
- Prompt discipline to copy in spirit: every agent backstory ends with "NEVER estimate,
  assume, or fabricate CPM pricing" (`portfolio_manager.py:53-56`).

## 2. The booking guards (pure, deterministic, no-LLM — the LLM↔money seam)

- **`spend_ceiling.py`** — hard gate: reject if `final_cpm > max_cpm` OR `total_cost > budget`
  (`:103-119`). Two hardening choices to steal: the exception does **not** subclass
  `ValueError/RuntimeError` (so broad handlers can't swallow a rejection, `:37-50`); fail-open
  only when *no* limit was supplied (`:90-101`). Fires before a deal is minted and before
  booked lines are created.
- **`recommendation_guard.py`** — parse-time clamp: rejects non-dict/uncoercible items,
  clamps `cpm→[0,max_cpm]`, `cost→[0,max_cost]`, `impressions→≥0`. The sharp insight
  (`:20-26`): the downstream ceiling is later derived from the approved recommendation's own
  numbers, so an **unclamped inflated CPM is self-authorizing** — clamp-at-parse (soft) +
  reject-at-commit (hard) closes the loop.
- **`quote_normalizer.py`** — ranker (not a reject gate): computes an **effective CPM** per
  seller quote (guarantee adjustment + fees) so quotes are comparable, scores 0-100
  (0.60 CPM / 0.20 deal-type / 0.20 fill), and ranks unpriced quotes last instead of
  crashing. `pricing.py` carries a `PricingSource {SELLER_QUOTED, NEGOTIATED, UNAVAILABLE}`
  enum to prevent silently using fabricated CPMs.

## 3. What to lift for adcp-buyer-agent

### 3a. Decomposition — replicate tiers with pydantic-ai, NOT CrewAI
Each tier = a separate `pydantic_ai.Agent` with its own prompt/model/`result_type`, sequenced
by deterministic Python (our §3.10 pipeline IS their Flow). **Start 2-tier**, not 3: AdCP
`create_media_buy` takes `packages` against one sales agent — no OpenDirect channel fan-out.
- **L1 → `planner.py`**: Opus (`claude-opus-4-8`), `result_type=CampaignPlan`, effort=high.
  Use pydantic `result_type` the way they use `output_pydantic`.
- **L2/L3 → `select.py` + the deterministic executor**: a cheap Sonnet/Haiku ranker with the
  mandatory `_fallback_rank`, then the DBOS `create_media_buy`. Our discover/execute are
  deterministic (not LLM) — strictly better than IAB; keep it.
- **Do NOT** wire tiers as sub-agents-as-tools on the money path. Sequence
  `planner.run()` → deterministic preflight+guards → `executor.create_media_buy()`, so the
  guards can fire and the `--no-llm` fallback + zero-token CI still work.

### 3b. Guards to add before `create_media_buy` fires (in `executor.py` or a new `guard.py`)
Between `jcs_key(plan)` and `create_media_buy_workflow(body)` (`executor.py:46-49`):
1. **Spend ceiling (biggest gap).** Port `enforce_spend_ceiling` onto AdCP: reject if a
   package's selected `pricing_option` cpm > `brief.max_cpm`, or `sum(package.budget) >
   brief.budget`. Copy the non-`ValueError` exception + fail-open-only-if-absent choices.
   **Polarity note:** our `check_budgets` clamps the *floor* (`BUDGET_TOO_LOW`); IAB clamps
   the *ceiling*. We're missing the upper bound — add it.
2. **Plan-field clamp.** At LLM-output → `CampaignPlan`, clamp each numeric field to
   brief-derived bounds and reject uncoercible ones (pydantic field validators keyed to
   `brief.max_cpm`/allocation). Clamp at parse, hard-reject at dispatch.
3. **Effective-CPM normalization for the rank step.** Feed the ranker a deterministically
   normalized, pre-sorted candidate list so it can't be swayed by raw-CPM framing. Lift the
   shape; re-derive the table for AdCP `delivery_type` (guaranteed vs non-guaranteed).
4. **Pricing provenance.** Tag every price entering `CampaignPlan` with a
   `PricingSource`-style enum — a typed field, not a prompt hope.

### 3c. What does NOT transfer
CrewAI itself; the entire OpenDirect wire (order/line/reserve/book state machine); deal
taxonomies (PG/PD/PA, Deal-ID minting, DSP activation strings); the pricing tier table
(Public/Seat/Agency/Advertiser + volume thresholds); `MultiSellerOrchestrator`. Re-derive the
PA-clearing 20% markup against AdCP's `delivery_type` instead.

## 4. License
Apache-2.0 (`license.spdx_id`), files headed "Green Mountain Systems AI Inc. / Donated to IAB
Tech Lab". Apache would permit reuse with attribution, but this is inspiration, not reuse — no
IAB files copied, no CrewAI imported, OpenDirect taxonomies left behind.
