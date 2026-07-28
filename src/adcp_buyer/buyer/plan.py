"""The typed campaign plan — what the planner emits and the guards vet.

Mirrors IAB's discipline: the planning tier produces a typed, bounded recommendation; a
deterministic layer vets it and does the money commit. Every price carries a provenance
tag so a fabricated CPM can never masquerade as seller-quoted.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class PricingSource(str, Enum):
    SELLER_QUOTED = "seller_quoted"  # taken verbatim from the product's pricing_option
    NEGOTIATED = "negotiated"
    UNAVAILABLE = "unavailable"  # no authoritative price — must never be booked


class PackagePlan(BaseModel):
    product_id: str
    pricing_option_id: str
    cpm: float = Field(ge=0)  # the option's price; authoritative value is re-stamped by the guard
    budget: float = Field(ge=0)
    pricing_source: PricingSource = PricingSource.SELLER_QUOTED
    rationale: str = ""


class CampaignPlan(BaseModel):
    packages: list[PackagePlan] = Field(default_factory=list)
    rationale: str = ""

    @property
    def total_budget(self) -> float:
        return sum(p.budget for p in self.packages)

    @property
    def is_empty(self) -> bool:
        return not self.packages
