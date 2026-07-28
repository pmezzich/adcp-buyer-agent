"""The campaign brief — the buyer's intent, the v2 form of v1's YAML personas.

A brief drives the whole pipeline: its text feeds get_products, its budget/max_cpm bound
the planner and the money guards, its flight timing goes on the wire. Swap the brief →
different buying behavior, no code change.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class CampaignBrief(BaseModel):
    brand_domain: str
    brief: str  # natural-language brief; feeds get_products and (optionally) the LLM planner
    budget: float = Field(gt=0, description="total campaign budget ceiling")
    max_cpm: float | None = Field(
        default=None, description="per-package CPM ceiling; None disables the CPM gate"
    )
    flight_start: str = "asap"  # StartTiming: 'asap' or an ISO aware datetime
    flight_end: str  # ISO aware datetime
    strategy: str = "balanced"  # hint for the ranker/LLM (e.g. reach, efficiency, balanced)
    max_packages: int = Field(default=3, ge=1)
    operator: str | None = None  # account operator; defaults to brand_domain
