"""Deterministic money guards — the seam between a plan and a spend.

Lifted in spirit from IAB's booking guards. Two moves close the "self-authorizing CPM"
loop: (1) re-stamp every package's CPM from the authoritative seller pricing option so a
planner can't claim a low price to slip past the ceiling, then (2) hard-reject over-ceiling
or over-budget plans. The rejection is NOT a ValueError/RuntimeError so a broad
`except (ValueError, RuntimeError)` on some call path can never silently swallow it.
"""

from __future__ import annotations

import logging

from adcp_buyer.buyer.brief import CampaignBrief
from adcp_buyer.buyer.plan import CampaignPlan, PackagePlan, PricingSource

logger = logging.getLogger(__name__)


class SpendCeilingExceeded(Exception):
    """A plan asked to spend beyond the brief's limits. Deliberately not a ValueError."""

    def __init__(self, message: str, *, detail: dict | None = None) -> None:
        super().__init__(message)
        self.detail = detail or {}


def authoritative_cpm(product: dict, pricing_option_id: str, currency: str = "USD") -> float | None:
    """The seller's real fixed CPM for an option, or None if it can't be booked as-is.

    Returns None when the option is in a different currency than the brief (comparing a
    foreign CPM against the ceiling is a real overspend) or when it has no fixed_price
    (floor/auction options need a bid_price the plan can't yet carry).
    """
    for po in product.get("pricing_options") or []:
        if po.get("pricing_option_id") != pricing_option_id:
            continue
        if (po.get("currency") or "USD") != currency:
            return None
        price = po.get("fixed_price")
        return float(price) if price is not None else None
    return None


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
        resolved.append(
            pkg.model_copy(update={"cpm": cpm, "pricing_source": PricingSource.SELLER_QUOTED})
        )
    return plan.model_copy(update={"packages": resolved})


def enforce_spend_ceiling(plan: CampaignPlan, brief: CampaignBrief) -> None:
    """Hard gate. At-or-under is allowed; strictly over raises SpendCeilingExceeded.

    Fail-open (with a warning) only when the brief supplies no ceiling at all — a supplied
    limit is never silently disabled.
    """
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
    if total > brief.budget:
        raise SpendCeilingExceeded(
            f"total plan budget {total} exceeds campaign budget {brief.budget}",
            detail={"total": total, "budget": brief.budget},
        )
