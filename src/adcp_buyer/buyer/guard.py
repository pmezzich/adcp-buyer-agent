"""Deterministic money guards — the seam between a plan and a spend.

Lifted in spirit from IAB's booking guards. Two moves close the "self-authorizing CPM"
loop: (1) re-stamp every package's CPM from the authoritative seller pricing option so a
planner can't claim a low price to slip past the ceiling, then (2) hard-reject over-ceiling
or over-budget plans. The rejection is NOT a ValueError/RuntimeError so a broad
`except (ValueError, RuntimeError)` on some call path can never silently swallow it.
"""

from __future__ import annotations

import logging
import math

from adcp_buyer.buyer.brief import CampaignBrief
from adcp_buyer.buyer.plan import CampaignPlan, PackagePlan, PricingSource
from adcp_buyer.buyer.pricing import buyable_price

logger = logging.getLogger(__name__)


class SpendCeilingExceeded(Exception):
    """A plan asked to spend beyond the brief's limits. Deliberately not a ValueError."""

    def __init__(self, message: str, *, detail: dict | None = None) -> None:
        super().__init__(message)
        self.detail = detail or {}


def authoritative_cpm(product: dict, pricing_option_id: str, currency: str = "USD") -> float | None:
    """The seller's real fixed CPM for an option, or None if it can't be booked as-is.

    Delegates the "is this bookable, and at what price" decision to
    :func:`~adcp_buyer.buyer.pricing.buyable_price`, which the planner uses too, so the
    price the ceiling is checked against is chosen by the same rule that chose the option.

    Scans EVERY option carrying the id rather than returning on the first match: sellers do
    not guarantee ``pricing_option_id`` is unique within a product, and returning on the
    first match let an unbookable duplicate mask a bookable one. When several are bookable,
    the highest price wins — the ceiling must be checked against the worst the seller could
    charge, not the best.
    """
    prices = [
        price
        for po in product.get("pricing_options") or []
        if po.get("pricing_option_id") == pricing_option_id
        and (price := buyable_price(po, currency)) is not None
    ]
    return max(prices) if prices else None


def resolve_plan_pricing(
    plan: CampaignPlan, products: list[dict], currency: str = "USD"
) -> CampaignPlan:
    """Re-stamp each package's CPM from the authoritative product pricing option.

    Defeats a self-authorizing planner: the ceiling check runs against the seller's real
    price, not the plan's claimed one. Packages whose product/option can't be resolved are
    dropped — an UNAVAILABLE price must never reach a booking.
    """
    index = {p.get("product_id"): p for p in products}
    resolved: list[PackagePlan] = []
    for pkg in plan.packages:
        product = index.get(pkg.product_id)
        cpm = authoritative_cpm(product, pkg.pricing_option_id, currency) if product else None
        if cpm is None:
            logger.warning(
                "dropping package %s/%s: no authoritative seller price",
                pkg.product_id,
                pkg.pricing_option_id,
            )
            continue
        if pkg.pricing_source is PricingSource.SELLER_QUOTED and abs(pkg.cpm - cpm) > 1e-9:
            logger.warning(
                "package %s claimed CPM %.4f but seller quotes %.4f; using the seller price",
                pkg.product_id,
                pkg.cpm,
                cpm,
            )
        # model_validate, NOT model_copy: model_copy skips validation, so PackagePlan's own
        # allow_inf_nan=False and ge=0 constraints were decoration on this path rather than
        # a floor. Re-validating makes the model the last line of defence it claims to be.
        resolved.append(
            PackagePlan.model_validate(
                {
                    **pkg.model_dump(),
                    "cpm": cpm,
                    "pricing_source": PricingSource.SELLER_QUOTED,
                }
            )
        )
    return plan.model_copy(update={"packages": resolved})


def enforce_spend_ceiling(plan: CampaignPlan, brief: CampaignBrief) -> None:
    """Hard gate. At-or-under is allowed; strictly over raises SpendCeilingExceeded.

    Fail-open (with a warning) only when the brief supplies no ceiling at all — a supplied
    limit is never silently disabled.
    """
    # Non-finite values first: every `>` against NaN is False, so an unchecked NaN would
    # pass BOTH gates below. Reject rather than compare.
    for pkg in plan.packages:
        if not math.isfinite(pkg.cpm) or not math.isfinite(pkg.budget):
            raise SpendCeilingExceeded(
                f"package {pkg.product_id} carries a non-finite cpm/budget "
                f"({pkg.cpm}/{pkg.budget}); refusing to compare it against any ceiling",
                detail={"product_id": pkg.product_id, "cpm": pkg.cpm, "budget": pkg.budget},
            )

    if brief.max_cpm is not None:
        for pkg in plan.packages:
            if pkg.cpm > brief.max_cpm:
                raise SpendCeilingExceeded(
                    f"package {pkg.product_id} CPM {pkg.cpm} exceeds max_cpm {brief.max_cpm}",
                    detail={"product_id": pkg.product_id, "cpm": pkg.cpm, "max_cpm": brief.max_cpm},
                )
    else:
        logger.warning("brief has no max_cpm; the per-package CPM ceiling is not enforced")

    total = plan.total_budget
    if not math.isfinite(total):
        raise SpendCeilingExceeded(
            f"plan total budget is non-finite ({total})", detail={"total": total}
        )
    if total > brief.budget:
        raise SpendCeilingExceeded(
            f"total plan budget {total} exceeds campaign budget {brief.budget}",
            detail={"total": total, "budget": brief.budget},
        )
