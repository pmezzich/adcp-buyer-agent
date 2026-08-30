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
