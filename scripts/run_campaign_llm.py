"""Run the LLM-planned path end-to-end WITHOUT an API key, using a stand-in model.

A pydantic-ai TestModel plays the role of Claude — it proposes a plan, which then flows
through the same deterministic guards and the durable executor as any other. In production
you delete the `model=` argument and set ANTHROPIC_API_KEY; the rest is identical.

    uv run python scripts/run_campaign_llm.py
"""

from __future__ import annotations

from pydantic_ai.models.test import TestModel

from adcp_buyer.buyer import dbos_app
from adcp_buyer.buyer.brief import CampaignBrief
from adcp_buyer.buyer.campaign import run_campaign

BASE, TOKEN, TENANT = "http://localhost:8092", "ci-test-token", "ci-test"

BRIEF = CampaignBrief(
    brand_domain="example-buyer.com",
    brief="premium display advertising for a consumer tech launch",
    budget=10_000.0,
    max_cpm=30.0,
    flight_end="2026-09-30T23:59:59Z",
)

# Stand-in for Claude: proposes buying the display product. In production, omit `model` and
# set ANTHROPIC_API_KEY — run_campaign(BRIEF) then uses the real model.
STANDIN = TestModel(
    custom_output_args={
        "packages": [
            {
                "product_id": "prod_display_premium",
                "pricing_option_id": "cpm_usd_fixed",
                "cpm": 15.0,
                "budget": 6000.0,
            }
        ],
        "rationale": "display fits the tech-launch brief within budget",
    }
)


def main() -> None:
    dbos_app.init_dbos(base_url=BASE, token=TOKEN, tenant=TENANT)
    result = run_campaign(BRIEF, use_llm=True, model=STANDIN)
    print(f"\nLLM-planned campaign: status={result.status} media_buy_id={result.media_buy_id}")
    for p in result.plan.packages:
        print(
            f"    buy {p.product_id} @ ${p.cpm:g} cpm  budget ${p.budget:g}  [{p.pricing_source.value}]"
        )
    if result.reason:
        print(f"    reason: {result.reason}")
    dbos_app.DBOS.destroy()


if __name__ == "__main__":
    main()
