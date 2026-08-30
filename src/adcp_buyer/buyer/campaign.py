"""The buyer happy-path pipeline: brief -> discover -> plan -> guard -> durable buy.

This is the v2 form of v1's phase pipeline, with the money commit made durable. The LLM (or
the deterministic ranker) only proposes a typed plan; guard.resolve_plan_pricing re-stamps
seller prices and guard.enforce_spend_ceiling is the hard gate — only then does the durable
executor fire create_media_buy. Refusing to buy (over-ceiling, or nothing fits) is a normal
outcome, not an error.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from adcp_buyer.buyer import dbos_app
from adcp_buyer.buyer.brief import CampaignBrief
from adcp_buyer.buyer.executor import create_media_buy
from adcp_buyer.buyer.guard import SpendCeilingExceeded, enforce_spend_ceiling, resolve_plan_pricing
from adcp_buyer.buyer.plan import CampaignPlan
from adcp_buyer.buyer.planner import plan_campaign
from adcp_buyer.core.result import Exchange


class AccountRegistrationFailed(Exception):
    """sync_accounts did not return an account. Carries the Exchange so the reason survives."""

    def __init__(self, exchange: Exchange) -> None:
        super().__init__(exchange.describe())
        self.exchange = exchange


@dataclass
class CampaignResult:
    # "bought" | "no_buy" | "refused" | "error" | "indeterminate"
    status: str
    plan: CampaignPlan
    reason: str = ""
    media_buy_id: str | None = None
    exchange: Exchange | None = None
    products: list[dict[str, Any]] = field(default_factory=list)


def ensure_account(transport, brief: CampaignBrief) -> str:
    """Register the buyer account and return the seller-minted account_id.

    Raises AccountRegistrationFailed rather than returning None on any non-success. The
    previous version read ``(ex.wire_response or {}).get("accounts")`` without consulting
    ``ex.is_success``, so a seller rejection became an empty list and then a bare "could not
    register account" — the wire's own reason was discarded on the path that leads to a buy.

    No ``idempotency_key`` is sent. The pinned schema marks it required on
    sync-accounts-request, but salesagent rejects it over REST ("Extra inputs are not
    permitted") and MCP ("Unexpected keyword argument"), and ignores it on A2A — see
    prebid/salesagent#1983, #2119, #2120. Sending it makes every REST/MCP call a 400, so the
    buyer omits a field the spec requires and the seller cannot accept.
    """
    body = {
        "accounts": [
            {
                "account": {"account_id": brief.brand_domain},
                "brand": {"domain": brief.brand_domain},
                "operator": brief.operator or brief.brand_domain,
                "billing": "operator",
            }
        ]
    }
    ex = transport.call("sync_accounts", body)
    if not ex.is_success:
        raise AccountRegistrationFailed(ex)
    accounts = (ex.wire_response or {}).get("accounts") or []
    account_id = accounts[0].get("account_id") if accounts else None
    if not account_id:
        raise AccountRegistrationFailed(ex)
    return account_id


def _to_request(plan: CampaignPlan, brief: CampaignBrief, account_id: str) -> dict[str, Any]:
    return {
        "brand": {"domain": brief.brand_domain},
        "account": {"account_id": account_id},
        "start_time": brief.flight_start,
        "end_time": brief.flight_end,
        "packages": [
            {
                "product_id": p.product_id,
                "budget": p.budget,
                "pricing_option_id": p.pricing_option_id,
            }
            for p in plan.packages
        ],
    }


def run_campaign(
    brief: CampaignBrief, *, use_llm: bool = True, model: Any = None
) -> CampaignResult:
    """Run one campaign end-to-end. Requires DBOS initialized (dbos_app.init_dbos).

    ``model`` injects a pydantic-ai model into the planner (e.g. a TestModel to run the LLM
    path without a key); otherwise the real Claude model is used only when a key is set.
    """
    transport = dbos_app.get_transport()

    products = (transport.call("get_products", {"brief": brief.brief}).wire_response or {}).get(
        "products", []
    )

    plan = resolve_plan_pricing(
        plan_campaign(brief, products, use_llm=use_llm, model=model), products, brief.currency
    )
    if plan.is_empty:
        return CampaignResult(
            status="no_buy",
            plan=plan,
            reason=plan.rationale or "no product fit the brief",
            products=products,
        )

    try:
        enforce_spend_ceiling(plan, brief)
    except SpendCeilingExceeded as exc:
        return CampaignResult(status="refused", plan=plan, reason=str(exc), products=products)

    try:
        account_id = ensure_account(transport, brief)
    except AccountRegistrationFailed as exc:
        return CampaignResult(
            status="error",
            plan=plan,
            reason=f"could not register account: {exc}",
            exchange=exc.exchange,
            products=products,
        )

    ex = create_media_buy(_to_request(plan, brief, account_id))
    if ex.is_error:
        return CampaignResult(
            status="error", plan=plan, reason=ex.describe(), exchange=ex, products=products
        )
    if ex.is_indeterminate:
        # 2xx with nothing readable. The seller MAY have booked. Never report this as
        # bought, and never retry it blindly — the same idempotency key is the only safe
        # way to find out, which is what re-running the durable workflow does.
        return CampaignResult(
            status="indeterminate", plan=plan, reason=ex.describe(), exchange=ex, products=products
        )
    return CampaignResult(
        status="bought",
        plan=plan,
        media_buy_id=(ex.wire_response or {}).get("media_buy_id"),
        exchange=ex,
        products=products,
    )
