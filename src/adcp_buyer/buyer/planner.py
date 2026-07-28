"""The planning tier: brief + discovered products -> a typed CampaignPlan.

Two implementations behind one entry point:
  * deterministic_plan — a rule-based ranker (relevance + CPM efficiency, ceiling filter).
    Zero tokens, no API key, always available. This is the mandatory fallback.
  * llm_plan — an optional Claude planner (pydantic-ai) for judgment on richer briefs.

Whatever the planner proposes, guard.resolve_plan_pricing re-stamps CPMs from the seller's
authoritative prices downstream, so the LLM can never self-authorize a price. Following IAB:
the LLM proposes a typed recommendation; deterministic code owns the money.
"""

from __future__ import annotations

import logging
import os
import re

from adcp_buyer.buyer.brief import CampaignBrief
from adcp_buyer.buyer.plan import CampaignPlan, PackagePlan, PricingSource

logger = logging.getLogger(__name__)


def _cheapest_option(product: dict, currency: str) -> tuple[str, float, float] | None:
    """Lowest-CPM buyable option in the brief's currency: (option_id, cpm, min_spend).

    Only fixed-price options in the matching currency are buyable today — floor/auction
    options need a bid_price we don't yet send, and a foreign-currency price can't be
    compared to the ceiling. Returns None if the product has no such option.
    """
    best: tuple[str, float, float] | None = None
    for po in product.get("pricing_options") or []:
        if po.get("supported") is False:
            continue
        if (po.get("currency") or "USD") != currency:
            continue
        price = po.get("fixed_price")
        if price is None:  # floor-only / auction — not buyable without a bid_price
            continue
        cpm = float(price)
        if best is None or cpm < best[1]:
            min_spend = po.get("min_spend_per_package")
            best = (
                po["pricing_option_id"],
                cpm,
                float(min_spend) if min_spend is not None else 0.0,
            )
    return best


def _allocate(
    packages: list[PackagePlan], mins: dict[str, float], budget: float
) -> list[PackagePlan]:
    """Equal-split the budget; drop packages whose share falls below their min_spend, retry.

    Absorbs rounding drift into the last package so the total never exceeds budget. Returns
    the surviving packages (possibly empty if nothing meets its minimum at any split size).
    """
    current = list(packages)
    for _ in range(len(packages)):
        if not current:
            break
        per = round(budget / len(current), 2)
        for pkg in current:
            pkg.budget = per
        drift = round(sum(p.budget for p in current) - budget, 2)
        if drift != 0:
            current[-1].budget = round(current[-1].budget - drift, 2)
        below = [p for p in current if p.budget < mins.get(p.product_id, 0.0)]
        if not below:
            return current
        # drop the least-affordable (highest min) and retry with a bigger per-package share
        current.remove(max(below, key=lambda p: mins.get(p.product_id, 0.0)))
    return current


def _score(product: dict, cpm: float, brief: CampaignBrief) -> float:
    """0..1 fit score: 60% brief relevance, 40% CPM efficiency under the ceiling."""
    text = f"{product.get('name', '')} {product.get('description', '')}".lower()
    words = {w for w in re.findall(r"[a-z]{4,}", brief.brief.lower())}
    relevance = (sum(1 for w in words if w in text) / len(words)) if words else 0.0
    ceiling = brief.max_cpm or (cpm if cpm > 0 else 1.0)
    efficiency = max(0.0, (ceiling - cpm) / ceiling) if ceiling else 0.0
    return round(0.6 * relevance + 0.4 * efficiency, 4)


def deterministic_plan(brief: CampaignBrief, products: list[dict]) -> CampaignPlan:
    """Rule-based ranker. Over-ceiling / un-buyable / foreign-currency products are dropped."""
    scored: list[tuple[float, PackagePlan]] = []
    mins: dict[str, float] = {}
    for product in products:
        opt = _cheapest_option(product, brief.currency)
        if opt is None:
            continue
        option_id, cpm, min_spend = opt
        if brief.max_cpm is not None and cpm > brief.max_cpm:
            continue  # disqualified: over the CPM ceiling
        pid = product.get("product_id")
        mins[pid] = min_spend
        scored.append(
            (
                _score(product, cpm, brief),
                PackagePlan(
                    product_id=pid,
                    pricing_option_id=option_id,
                    cpm=cpm,
                    budget=0.0,  # allocated below
                    pricing_source=PricingSource.SELLER_QUOTED,
                    rationale=f"cpm ${cpm:g} within ceiling; ranked deterministically",
                ),
            )
        )
    if not scored:
        return CampaignPlan(rationale="no product fits the brief within its CPM ceiling")

    scored.sort(key=lambda s: s[0], reverse=True)
    chosen = _allocate([pkg for _, pkg in scored[: brief.max_packages]], mins, brief.budget)
    if not chosen:
        return CampaignPlan(rationale="no product's minimum spend fits within the budget")
    return CampaignPlan(
        packages=chosen,
        rationale=f"selected {len(chosen)} of {len(products)} products by relevance + CPM efficiency",
    )


def llm_plan(brief: CampaignBrief, products: list[dict]) -> CampaignPlan | None:
    """Optional Claude planner. Returns None (caller falls back) if unavailable or on error."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    try:
        from pydantic_ai import Agent

        model = os.environ.get("BUYER_PLANNER_MODEL", "anthropic:claude-opus-4-8")
        agent = Agent(
            model,
            output_type=CampaignPlan,
            system_prompt=(
                "You are a media buyer. Given a campaign brief and a list of products with "
                "their pricing options, choose the packages to buy and split the budget. "
                "NEVER estimate, assume, or fabricate CPM pricing — only use the fixed_price / "
                "floor_price the product actually declares. Never exceed the brief's max_cpm or "
                "total budget. It is correct to buy nothing if nothing fits."
            ),
        )
        summary = [
            {
                "product_id": p.get("product_id"),
                "name": p.get("name"),
                "description": p.get("description"),
                "pricing_options": p.get("pricing_options"),
            }
            for p in products
        ]
        prompt = f"Brief: {brief.model_dump_json()}\n\nProducts: {summary}"
        return agent.run_sync(prompt).output
    except Exception as exc:  # noqa: BLE001 — any planner failure falls back deterministically
        logger.warning("llm_plan failed (%s); falling back to deterministic planner", exc)
        return None


def plan_campaign(
    brief: CampaignBrief, products: list[dict], *, use_llm: bool = True
) -> CampaignPlan:
    """Plan a campaign: LLM if available and enabled, else the deterministic ranker."""
    if use_llm:
        plan = llm_plan(brief, products)
        if plan is not None:
            return plan
    return deterministic_plan(brief, products)
