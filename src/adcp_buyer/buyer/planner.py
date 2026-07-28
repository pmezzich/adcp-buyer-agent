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


def _cheapest_option(product: dict) -> tuple[str, float] | None:
    """The lowest-CPM supported pricing option for a product, or None if unpriced."""
    best: tuple[str, float] | None = None
    for po in product.get("pricing_options") or []:
        if po.get("supported") is False:
            continue
        price = po.get("fixed_price")
        if price is None:
            price = po.get("floor_price")
        if price is None:
            continue
        cpm = float(price)
        if best is None or cpm < best[1]:
            best = (po["pricing_option_id"], cpm)
    return best


def _score(product: dict, cpm: float, brief: CampaignBrief) -> float:
    """0..1 fit score: 60% brief relevance, 40% CPM efficiency under the ceiling."""
    text = f"{product.get('name', '')} {product.get('description', '')}".lower()
    words = {w for w in re.findall(r"[a-z]{4,}", brief.brief.lower())}
    relevance = (sum(1 for w in words if w in text) / len(words)) if words else 0.0
    ceiling = brief.max_cpm or (cpm if cpm > 0 else 1.0)
    efficiency = max(0.0, (ceiling - cpm) / ceiling) if ceiling else 0.0
    return round(0.6 * relevance + 0.4 * efficiency, 4)


def deterministic_plan(brief: CampaignBrief, products: list[dict]) -> CampaignPlan:
    """Rule-based ranker. Over-ceiling products are disqualified; nothing forced."""
    scored: list[tuple[float, PackagePlan]] = []
    for product in products:
        opt = _cheapest_option(product)
        if opt is None:
            continue
        option_id, cpm = opt
        if brief.max_cpm is not None and cpm > brief.max_cpm:
            continue  # disqualified: over the CPM ceiling
        pid = product.get("product_id")
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
    chosen = [pkg for _, pkg in scored[: brief.max_packages]]

    # Equal budget split across chosen packages. Round to cents, then absorb any rounding
    # drift into the last package so the total is never a fraction over budget (which the
    # spend-ceiling guard would otherwise, correctly, reject).
    per = round(brief.budget / len(chosen), 2)
    for pkg in chosen:
        pkg.budget = per
    drift = round(sum(p.budget for p in chosen) - brief.budget, 2)
    if drift != 0:
        chosen[-1].budget = round(chosen[-1].budget - drift, 2)
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
