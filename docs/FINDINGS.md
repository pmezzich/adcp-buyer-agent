# Findings from driving a live seller

What the buyer turned up on its first real campaign runs against
[prebid/salesagent](https://github.com/prebid/salesagent) `1d93e372` (upstream/main),
`adcp==6.6.0`, spec **3.1.1**, on the canonical e2e stack (`http://localhost:8092`, tenant
`ci-test`, seeded by `scripts/setup/init_database_ci.py`, mock adapter, `ADCP_TESTING=true`).

Everything is **[observed]** unless tagged otherwise. Nothing here has been posted upstream.

Findings against THIS repo are not listed — they were fixed; see the git log.

---

## F1 — `get_media_buy_delivery` synthesizes package ids (one line, three symptoms)

**Status: appears unfiled. One of its three symptoms is #2012, whose open question this
answers.**

`src/core/tools/media_buy_delivery.py:428`

```python
package_id = pkg_data.get("package_id") or f"pkg_{pkg_data.get('product_id', 'unknown')}_{i}"
```

`pkg_data` iterates `buy.raw_request["packages"]` — the *buyer's* request. A buyer never
sends `package_id`; the seller mints it at `media_buy_create.py:2957`. So the left operand is
always `None` on the buyer-created path and the synthetic fallback always fires. Confirmed in
the database for a two-package buy: `raw_request->'packages'` carries only
`product_id`/`budget`/`pricing_option_id`, while `media_packages` holds
`pkg_prod_display_premium_ab4f1994_1` and `pkg_prod_video_premium_03187adf_2`.

**Symptom 1 — the wire carries a package id that does not exist.**

```
create_media_buy   : ['pkg_prod_display_premium_ab4f1994_1', 'pkg_prod_video_premium_03187adf_2']
get_media_buy_delivery: ['pkg_prod_display_premium_0',       'pkg_prod_video_premium_1']
```

A buyer cannot join per-package delivery back to the packages it created. Pacing or
optimization keyed on `package_id` matches nothing, silently — both sides are well-formed.

**Symptom 2 — this is the root cause of #2012.** `:432` looks up `package_pricing_map` (built
at `:417`, keyed by the *real* id) with the synthetic key, so `pricing_info` is permanently
`None` and `:487-495` emit `pricing_model=None, rate=None, currency=None` on every
`by_package` entry. #2012 asks whether the data exists and says "worth confirming before
scoping": it does, and it is correctly persisted —

```
pkg_prod_display_premium_ab4f1994_1 | {"rate": 15.0, "currency": "USD", "pricing_model": "cpm", ...}
pkg_prod_video_premium_03187adf_2   | {"rate": 25.0, "currency": "USD", "pricing_model": "cpm", ...}
```

so this is a wrong lookup key, not a threading or serialization gap, and not a migration.

**Symptom 3 — real per-package metrics are discarded.** `:442` misses on the same key, so
`:452-454` divides the buy total equally. Observable: a $3,000 and a $2,000 package reported
identical spend. [inferred that a non-mock adapter populates `adapter_package_metrics` more
richly; only the mock adapter was exercised]

**Fix direction.** Resolve the package id from the persisted `MediaPackage` rows — already
batch-fetched into `packages_by_buy` at `:412`. Iterating those instead of `raw_request`
makes all three lookups key-correct by construction. Keep the `f"pkg_..._{i}"` fallback only
if some path genuinely has no persisted package, and make it loud: a synthesized id that
reaches a buyer is indistinguishable from a real one.

**Could not verify:** only the mock adapter and only REST were driven (MCP/A2A share the
`_impl`, so the same id is [inferred] to reach them). Whether an `update_media_buy` that adds
packages writes `package_id` back into `raw_request` — which would make the fallback correct
on that path — is unchecked.

---

## F2 — sync-op `idempotency_key`: a known-bug rediscovery, not a new bug

**Status: already filed, comprehensively. Do not file.** Kept because BUILD-PLAN-v2 §7.1 wants
a known-bug rediscovery suite as the project's proof of teeth, and this is one — found live,
cross-transport, on the first campaign run, before any fuzzer existed.

| issue | what it already says |
|---|---|
| #1983 | "**No sync transport accepts the schema-required `idempotency_key`.**" Also the prod `extra="ignore"` silent-drop vs dev/CI 400 split. |
| #2119 | `accounts.py:719` `idempotency_key=str(uuid.uuid4())` — the server manufactures a client-generated value, so it gives zero replay protection. |
| #2120 | The same guarantee failed two other ways: dropped on A2A `update_media_buy`, un-offerable on `sync_creatives`. |
| #1984 | One schema failure emitting different codes at different boundaries. |
| #1075, #1470 | Feature/decision tickets for key support on `update_media_buy` / `sync_creatives`. |

Pinned schema `_schemas/3.0/account/sync-accounts-request.json` lists
`required: ["idempotency_key", "accounts"]`, and its description says the key exists so that
onboarding webhooks, billing setup and audit events "do not fire twice on retry".

| transport | `sync_accounts` **with** the key (spec-conformant) | **without** it (spec-violating) |
|---|---|---|
| REST | **400** `INVALID_REQUEST` "Extra inputs are not permitted" | **200 OK** |
| MCP | **`VALIDATION_ERROR`** "Unexpected keyword argument" | **200 OK** |
| A2A | **200 OK** — accepted, then never read | **200 OK** |

`sync_creatives` behaves identically. The polarity is inverted in both directions.

The part worth keeping is the **internal control**: `create_media_buy`, same run, gets it
right on all three transports — key required, key accepted. So the rule is already
implemented uniformly for one op family, which is a sharper oracle than the spec citation
alone. This repo's `ensure_account` therefore omits a field the schema marks required;
`tests/test_live_seller.py` pins the rejection so we notice when the seller is fixed.

---

## F3 — three transports, three unknown-field policies

One request, `get_products` with two fields the schema does not define:

```
REST: 400 INVALID_REQUEST  "Extra inputs are not permitted"
MCP : VALIDATION_ERROR     "Unexpected keyword argument"
A2A : 200 OK, 2 products   (fields silently ignored)
```

A2A's mechanism is `_handle_get_products_skill` (`adcp_a2a_server.py:1551-1562`) plucking
named keys out of the raw `parameters` dict — no model is constructed, so nothing unknown is
ever rejected. Verified with raw curl, independent of this repo's transports.

**This is not filed as a defect here**, for two reasons. Dropping a *non-spec* field is
defensible forward-compatibility; the divergence is the finding, not the tolerance. And
dropping a *spec-declared* field is already #1983 ("A2A create handler drops `ext`"), which
is the same mechanism. What this does establish is that `ExtraFieldPolicy` must be declared
per `(wire, op)` as BUILD-PLAN-v2 §3.2.2 says — with these values, not the recorded ones.

A hand-run differential over 16 cases × 3 wires found 5 divergences; the other two were the
`INVALID_REQUEST` vs `VALIDATION_ERROR` split for one cause (#1984), and MCP validating the
payload *before* checking auth, so an unauthenticated caller learns the schema requirements.
That last one is unconfirmed against the seller's source and is not written up.


---

## F4 — a creative missing `assets` is per-item on A2A/REST and whole-request on MCP

**Status: appears unfiled.** #2011 names this exact contract for A2A and was fixed there by
#1802; MCP was not in its scope and still diverges.

Measured on the live seller (`fd90b69a`, e2e stack, tenant `ci-test`), one creative carrying a
REGISTERED format (`display_970x250_html`) and no `assets` map:

| transport | outcome |
|---|---|
| A2A | HTTP 200, request accepted, `creatives[0].action = "failed"` |
| REST | HTTP 200, request accepted, `creatives[0].action = "failed"` |
| MCP | **whole request rejected** — `VALIDATION_ERROR: "Field required"` |

#2011 states the contract plainly: *"A creative with no assets, or no `format_id`, is a
per-creative failure in the AdCP contract: `_sync_creatives_impl` returns `action='failed'` for
that entry and the request succeeds overall."* It filed that as a defect **against A2A**, where
`CreativeAsset(**c)` raised at the boundary. #1802 removed that construction, and the three
cases #2011 names (`test_no_format_action_failed`, `test_no_preview_no_url_fails`,
`test_generative_no_gemini_key_fails`) now pass on a2a — 12 passed, no xpass, verified this run.
The three xfails still in that file are unrelated ("Async lifecycle not implemented").

MCP does the same thing A2A used to: its typed wrapper requires the field, so the request never
reaches `_sync_creatives_impl` and no per-item verdict is produced. Nothing tests it —
`grep` for a missing-assets case in `tests/integration/test_creative_sync_transport.py` returns
nothing.

**Why this matters beyond symmetry.** A batch is best-effort per item, so a buyer sending five
creatives where one lacks assets should get four synced and one `failed`. On MCP it gets a
whole-request rejection instead.

**[inferred, not measured]** — that consequence could not be demonstrated on this stack. Every
creative sync here fails at the creative-agent hop with `CONFIGURATION_ERROR: "The configured
endpoint for the creative agent is not reachable under this deployment's egress policy"`, so no
creative syncs regardless of shape. The MCP/A2A/REST divergence above IS measured, because it
happens at the request-shape boundary before that hop — MCP rejects without ever reaching it.

**Relationship to #1671.** That PR proposed the opposite: making A2A refuse at the request level
for this shape. #2011 shows request-level was the bug and per-item is the contract, so the PR
was pinning the defect as correct behaviour. Its A2A change and the 77-line test asserting it
were removed during the upstream catch-up. The residue is that MCP still behaves the way #1671
wanted A2A to.
