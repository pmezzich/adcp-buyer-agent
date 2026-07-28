"""Unit tests for the buyer brain's deterministic pieces: the ranker and the money guards.

No infra, no LLM. The live end-to-end (plan -> guard -> durable buy) is exercised by
scripts/run_campaign.py.
"""

import pytest
from pydantic import ValidationError
from pydantic_ai.models.test import TestModel

from adcp_buyer.buyer.brief import CampaignBrief
from adcp_buyer.buyer.guard import (
    SpendCeilingExceeded,
    enforce_spend_ceiling,
    resolve_plan_pricing,
)
from adcp_buyer.buyer.plan import CampaignPlan, PackagePlan, PricingSource
from adcp_buyer.buyer.planner import deterministic_plan, llm_plan, plan_campaign

# Two products mirroring the ci-test seed: display $15, video $25.
PRODUCTS = [
    {
        "product_id": "prod_display_premium",
        "name": "Premium Display Advertising",
        "description": "High-impact display ads across premium content",
        "pricing_options": [
            {"pricing_option_id": "cpm_usd_fixed", "fixed_price": 15.0, "supported": True}
        ],
    },
    {
        "product_id": "prod_video_premium",
        "name": "Premium Video Advertising",
        "description": "Premium video inventory",
        "pricing_options": [
            {"pricing_option_id": "cpm_usd_fixed", "fixed_price": 25.0, "supported": True}
        ],
    },
]


def _brief(**kw):
    base = {
        "brand_domain": "example-buyer.com",
        "brief": "premium display and video advertising",
        "budget": 10_000.0,
        "flight_end": "2026-09-30T23:59:59Z",
    }
    base.update(kw)
    return CampaignBrief(**base)


def test_deterministic_plan_buys_within_ceiling():
    plan = deterministic_plan(_brief(max_cpm=30.0), PRODUCTS)
    assert len(plan.packages) == 2
    assert plan.total_budget <= 10_000.0
    assert all(p.pricing_source is PricingSource.SELLER_QUOTED for p in plan.packages)


def test_budget_split_never_exceeds_budget_on_odd_division():
    # 6 packages at $100 -> 16.666.. rounds to 16.67, *6 = 100.02 without the drift fix.
    prods = [
        {
            "product_id": f"p{i}",
            "name": "premium display advertising",
            "description": "x",
            "pricing_options": [{"pricing_option_id": "o", "fixed_price": 5.0, "supported": True}],
        }
        for i in range(6)
    ]
    plan = deterministic_plan(_brief(budget=100.0, max_cpm=30.0, max_packages=6), prods)
    assert len(plan.packages) == 6
    assert plan.total_budget <= 100.0  # would be 100.02 without the drift absorption


def test_deterministic_plan_refuses_when_all_over_ceiling():
    # $12 ceiling < both $15 and $25 products -> nothing qualifies.
    plan = deterministic_plan(_brief(max_cpm=12.0), PRODUCTS)
    assert plan.is_empty


def test_resolve_plan_pricing_restamps_fabricated_cpm():
    # A planner claims a $1 CPM on the $15 product to slip past a ceiling.
    lie = CampaignPlan(
        packages=[
            PackagePlan(
                product_id="prod_display_premium",
                pricing_option_id="cpm_usd_fixed",
                cpm=1.0,  # fabricated
                budget=5000.0,
            )
        ]
    )
    resolved = resolve_plan_pricing(lie, PRODUCTS)
    assert resolved.packages[0].cpm == 15.0  # re-stamped to the seller's real price


def test_resolve_drops_unknown_product_or_option():
    bogus = CampaignPlan(
        packages=[PackagePlan(product_id="nope", pricing_option_id="nope", cpm=1.0, budget=1.0)]
    )
    assert resolve_plan_pricing(bogus, PRODUCTS).is_empty


def test_enforce_spend_ceiling_rejects_over_cpm():
    plan = CampaignPlan(
        packages=[PackagePlan(product_id="p", pricing_option_id="o", cpm=25.0, budget=100.0)]
    )
    with pytest.raises(SpendCeilingExceeded):
        enforce_spend_ceiling(plan, _brief(max_cpm=12.0))


def test_enforce_spend_ceiling_rejects_over_budget():
    plan = CampaignPlan(
        packages=[PackagePlan(product_id="p", pricing_option_id="o", cpm=10.0, budget=20_000.0)]
    )
    with pytest.raises(SpendCeilingExceeded):
        enforce_spend_ceiling(plan, _brief(budget=10_000.0))


def test_enforce_spend_ceiling_allows_at_or_under():
    plan = CampaignPlan(
        packages=[PackagePlan(product_id="p", pricing_option_id="o", cpm=12.0, budget=10_000.0)]
    )
    enforce_spend_ceiling(plan, _brief(budget=10_000.0, max_cpm=12.0))  # exactly at both: allowed


def test_spend_ceiling_exception_is_not_a_valueerror():
    # So a broad `except (ValueError, RuntimeError)` on some path can't swallow a rejection.
    assert not issubclass(SpendCeilingExceeded, (ValueError, RuntimeError))


def test_fail_open_only_when_no_ceiling():
    # No max_cpm -> CPM gate does not fire (budget gate still does).
    plan = CampaignPlan(
        packages=[PackagePlan(product_id="p", pricing_option_id="o", cpm=999.0, budget=5.0)]
    )
    enforce_spend_ceiling(plan, _brief(budget=10_000.0, max_cpm=None))  # no raise


# ---- adversarial-audit regressions (currency, auction, min-spend, inf) ----

GBP_PRODUCT = [
    {
        "product_id": "prod_gbp",
        "name": "premium display advertising",
        "description": "x",
        "pricing_options": [
            {
                "pricing_option_id": "cpm_gbp_fixed",
                "fixed_price": 12.0,
                "currency": "GBP",
                "supported": True,
            }
        ],
    }
]

FLOOR_PRODUCT = [
    {
        "product_id": "prod_auction",
        "name": "premium display advertising",
        "description": "x",
        "pricing_options": [
            {
                "pricing_option_id": "cpm_usd_auction",
                "fixed_price": None,
                "floor_price": 5.0,
                "currency": "USD",
                "supported": True,
            }
        ],
    }
]

MIN_PRODUCT = [
    {
        "product_id": "prod_minspend",
        "name": "premium display advertising",
        "description": "x",
        "pricing_options": [
            {
                "pricing_option_id": "cpm_usd_fixed",
                "fixed_price": 10.0,
                "currency": "USD",
                "min_spend_per_package": 5000.0,
                "supported": True,
            }
        ],
    }
]


def test_foreign_currency_option_is_not_bought():
    # £12 (< the USD 15 ceiling numerically, but ~$15.2) must NOT slip past a USD brief.
    assert deterministic_plan(_brief(max_cpm=15.0, currency="USD"), GBP_PRODUCT).is_empty


def test_resolve_drops_foreign_currency_package():
    lie = CampaignPlan(
        packages=[
            PackagePlan(
                product_id="prod_gbp", pricing_option_id="cpm_gbp_fixed", cpm=12.0, budget=100.0
            )
        ]
    )
    assert resolve_plan_pricing(lie, GBP_PRODUCT, "USD").is_empty


def test_auction_floor_option_is_not_selected():
    # floor-only / auction has no fixed_price and needs a bid_price we can't send -> skip.
    assert deterministic_plan(_brief(max_cpm=30.0), FLOOR_PRODUCT).is_empty
    p = CampaignPlan(
        packages=[
            PackagePlan(
                product_id="prod_auction",
                pricing_option_id="cpm_usd_auction",
                cpm=5.0,
                budget=100.0,
            )
        ]
    )
    assert resolve_plan_pricing(p, FLOOR_PRODUCT, "USD").is_empty


def test_below_min_spend_is_dropped():
    # budget 3000 < the option's 5000 minimum -> can't buy.
    assert deterministic_plan(_brief(budget=3000.0, max_cpm=30.0), MIN_PRODUCT).is_empty


def test_min_spend_met_buys():
    plan = deterministic_plan(_brief(budget=6000.0, max_cpm=30.0), MIN_PRODUCT)
    assert len(plan.packages) == 1
    assert plan.packages[0].budget >= 5000.0


def test_inf_budget_rejected_at_validation():
    with pytest.raises(ValidationError):
        PackagePlan(product_id="p", pricing_option_id="o", cpm=1.0, budget=float("inf"))


# ---- LLM planner path, exercised with an injected TestModel (no key, no network) ----


def test_llm_plan_runs_with_injected_test_model():
    tm = TestModel(
        custom_output_args={
            "packages": [
                {
                    "product_id": "prod_display_premium",
                    "pricing_option_id": "cpm_usd_fixed",
                    "cpm": 15.0,
                    "budget": 5000.0,
                }
            ],
            "rationale": "test-model plan",
        }
    )
    plan = llm_plan(_brief(max_cpm=30.0), PRODUCTS, model=tm)
    assert plan is not None
    assert [p.product_id for p in plan.packages] == ["prod_display_premium"]


def test_llm_plan_returns_none_without_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert llm_plan(_brief(), PRODUCTS) is None  # no model injected, no key -> falls back


def test_plan_campaign_uses_injected_model_to_buy_nothing():
    tm = TestModel(custom_output_args={"packages": [], "rationale": "nothing fits"})
    assert plan_campaign(_brief(), PRODUCTS, use_llm=True, model=tm).is_empty


def test_lying_llm_cpm_is_restamped_then_rejected():
    # The LLM claims a $1 CPM on the $25 video to slip past a $15 ceiling — the guard must win.
    tm = TestModel(
        custom_output_args={
            "packages": [
                {
                    "product_id": "prod_video_premium",
                    "pricing_option_id": "cpm_usd_fixed",
                    "cpm": 1.0,  # fabricated
                    "budget": 100.0,
                }
            ],
            "rationale": "sneaky",
        }
    )
    plan = llm_plan(_brief(max_cpm=15.0), PRODUCTS, model=tm)
    resolved = resolve_plan_pricing(plan, PRODUCTS, "USD")
    assert resolved.packages[0].cpm == 25.0  # re-stamped to the real video price
    with pytest.raises(SpendCeilingExceeded):
        enforce_spend_ceiling(resolved, _brief(max_cpm=15.0))  # now correctly rejected
