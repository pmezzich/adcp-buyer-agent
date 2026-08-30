"""The spend gate is the only thing between a planner and real money. Pin it.

Every case here is a seller-supplied price that must NOT be bookable. The dangerous ones are
not the obviously-bad prices but the ones that make the gate's own comparisons vacuous:
``nan > ceiling`` is False, so an unknown price sails through a ceiling of any size while
looking like a normal number on the way out.

``"NaN"`` as a JSON *string* is the realistic vector -- it is plain RFC-8259, any serializer
can emit it, and ``float("NaN")`` accepts it -- so these tests use the string form rather
than relying on a non-standard bare ``NaN`` literal.
"""

from __future__ import annotations

import math

import pytest

from adcp_buyer.buyer.brief import CampaignBrief
from adcp_buyer.buyer.guard import (
    SpendCeilingExceeded,
    authoritative_cpm,
    enforce_spend_ceiling,
    resolve_plan_pricing,
)
from adcp_buyer.buyer.plan import CampaignPlan, PackagePlan, PricingSource
from adcp_buyer.buyer.planner import deterministic_plan
from adcp_buyer.buyer.pricing import buyable_price

BRIEF = CampaignBrief(
    brand_domain="b.com",
    brief="display",
    budget=10_000.0,
    max_cpm=6.0,
    flight_end="2026-09-30T23:59:59Z",
)


def _product(options):
    return {"product_id": "p1", "name": "P1", "description": "d", "pricing_options": options}


def _opt(**kw):
    base = {
        "pricing_option_id": "po1",
        "pricing_model": "cpm",
        "currency": "USD",
        "supported": True,
    }
    base.update(kw)
    return base


def _plan(cpm=5.0, budget=9_000.0, source=PricingSource.SELLER_QUOTED):
    return CampaignPlan(
        packages=[
            PackagePlan(
                product_id="p1",
                pricing_option_id="po1",
                cpm=cpm,
                budget=budget,
                pricing_source=source,
            )
        ]
    )


# ---------------------------------------------------------------------------
# buyable_price -- the single predicate both the planner and the guard use
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "option, why",
    [
        (_opt(fixed_price="NaN"), "NaN as a JSON string"),
        (_opt(fixed_price=float("nan")), "NaN as a float"),
        (_opt(fixed_price=float("inf")), "positive infinity"),
        (_opt(fixed_price="-Infinity"), "negative infinity as a string"),
        (_opt(fixed_price=-1.0), "negative price"),
        (_opt(fixed_price="not-a-number"), "non-numeric string"),
        (_opt(fixed_price=None), "floor/auction option with no fixed price"),
        (_opt(fixed_price=5.0, currency="EUR"), "foreign currency"),
        (_opt(fixed_price=5.0, supported=False), "seller marked it unsupported"),
    ],
)
def test_unbookable_options_yield_no_price(option, why):
    assert buyable_price(option, "USD") is None, f"must not be bookable: {why}"


def test_a_normal_fixed_price_is_bookable():
    assert buyable_price(_opt(fixed_price=5.0), "USD") == 5.0


# ---------------------------------------------------------------------------
# authoritative_cpm
# ---------------------------------------------------------------------------


def test_authoritative_cpm_refuses_a_non_finite_seller_price():
    assert authoritative_cpm(_product([_opt(fixed_price="NaN")]), "po1") is None


def test_authoritative_cpm_ignores_an_unsupported_duplicate_of_the_same_option_id():
    """Sellers do not guarantee pricing_option_id is unique within a product.

    The planner skipped ``supported is False`` and the guard did not, so a product whose
    FIRST option under an id was unsupported-and-junk planned at the good price and then
    re-stamped from the junk one. Both now consult the same predicate.
    """
    product = _product(
        [_opt(fixed_price="NaN", supported=False), _opt(fixed_price=5.0, supported=True)]
    )
    assert authoritative_cpm(product, "po1") == 5.0


def test_authoritative_cpm_takes_the_highest_bookable_duplicate():
    """The ceiling must be checked against the worst the seller could charge."""
    product = _product([_opt(fixed_price=5.0), _opt(fixed_price=9.0)])
    assert authoritative_cpm(product, "po1") == 9.0


# ---------------------------------------------------------------------------
# resolve_plan_pricing -- re-stamping must go through validation
# ---------------------------------------------------------------------------


def test_a_non_finite_seller_price_drops_the_package_instead_of_stamping_it():
    resolved = resolve_plan_pricing(_plan(), [_product([_opt(fixed_price="NaN")])], "USD")
    assert resolved.packages == [], "a package with no bookable price must not survive"


def test_every_resolved_cpm_is_finite_and_non_negative():
    """The invariant the ceiling check depends on. model_copy skipped it; model_validate cannot."""
    for bad in ("NaN", float("inf"), -3.0, "not-a-number"):
        resolved = resolve_plan_pricing(_plan(), [_product([_opt(fixed_price=bad)])], "USD")
        for pkg in resolved.packages:
            assert math.isfinite(pkg.cpm) and pkg.cpm >= 0, f"leaked {bad!r} as cpm={pkg.cpm}"


def test_resolve_restamps_a_claimed_price_down_to_the_seller_price():
    resolved = resolve_plan_pricing(_plan(cpm=1.0), [_product([_opt(fixed_price=5.0)])], "USD")
    assert [p.cpm for p in resolved.packages] == [5.0]


# ---------------------------------------------------------------------------
# enforce_spend_ceiling -- the gate itself
# ---------------------------------------------------------------------------


def test_ceiling_refuses_a_nan_cpm_rather_than_comparing_it():
    """`nan > 6.0` is False, so an unchecked NaN passed a ceiling of any size."""
    plan = CampaignPlan(
        packages=[
            PackagePlan.model_construct(
                product_id="p1",
                pricing_option_id="po1",
                cpm=float("nan"),
                budget=9_000.0,
                pricing_source=PricingSource.SELLER_QUOTED,
                rationale="",
            )
        ]
    )
    with pytest.raises(SpendCeilingExceeded):
        enforce_spend_ceiling(plan, BRIEF)


def test_ceiling_refuses_a_nan_budget():
    plan = CampaignPlan(
        packages=[
            PackagePlan.model_construct(
                product_id="p1",
                pricing_option_id="po1",
                cpm=1.0,
                budget=float("nan"),
                pricing_source=PricingSource.SELLER_QUOTED,
                rationale="",
            )
        ]
    )
    with pytest.raises(SpendCeilingExceeded):
        enforce_spend_ceiling(plan, BRIEF)


def test_ceiling_still_refuses_an_ordinary_over_price():
    with pytest.raises(SpendCeilingExceeded):
        enforce_spend_ceiling(_plan(cpm=7.0), BRIEF)


def test_ceiling_allows_a_plan_exactly_at_the_limits():
    enforce_spend_ceiling(_plan(cpm=6.0, budget=10_000.0), BRIEF)


# ---------------------------------------------------------------------------
# End to end: the planner + guard pair, which is where the money actually moves
# ---------------------------------------------------------------------------


def test_planner_and_guard_together_never_book_a_non_finite_price():
    """The documented pipeline: plan -> resolve_plan_pricing -> enforce_spend_ceiling.

    Whatever the planner proposes, no non-finite price may reach an approved plan. Either
    the package is dropped or the gate raises; a silent pass is the failure.
    """
    products = [_product([_opt(fixed_price="NaN", supported=False), _opt(fixed_price=5.0)])]
    plan = resolve_plan_pricing(deterministic_plan(BRIEF, products), products, "USD")
    enforce_spend_ceiling(plan, BRIEF)  # must not raise: the bookable 5.0 option wins
    assert all(math.isfinite(p.cpm) and p.cpm <= BRIEF.max_cpm for p in plan.packages)


def test_a_product_priced_only_at_nan_yields_no_plan_at_all():
    products = [_product([_opt(fixed_price="NaN")])]
    plan = resolve_plan_pricing(deterministic_plan(BRIEF, products), products, "USD")
    assert plan.is_empty
    enforce_spend_ceiling(plan, BRIEF)  # vacuously fine -- there is nothing to buy


def test_resolve_never_emits_a_package_that_would_fail_its_own_model():
    """resolve_plan_pricing re-stamps CPM, but it copies every OTHER field through verbatim.

    Re-stamping with ``model_copy`` skipped validation entirely, so a package that had
    bypassed the model on the way in (a planner using model_construct, a future codec
    building from a wire dict) kept its invalid field on the way out and the ``ge=0`` /
    ``allow_inf_nan=False`` constraints on PackagePlan were decoration. Re-validating makes
    the model the floor. Reached here through ``budget``, which no upstream predicate filters.
    """
    smuggled = CampaignPlan(
        packages=[
            PackagePlan.model_construct(
                product_id="p1",
                pricing_option_id="po1",
                cpm=5.0,
                budget=float("nan"),
                pricing_source=PricingSource.SELLER_QUOTED,
                rationale="",
            )
        ]
    )
    products = [_product([_opt(fixed_price=5.0)])]
    with pytest.raises(Exception) as exc:
        resolve_plan_pricing(smuggled, products, "USD")
    assert "budget" in str(exc.value), f"expected a budget validation failure, got {exc.value}"
